"""Disposable OIDC provider and HTTP/WebSocket target; never a deployment service."""

import asyncio
import base64
import hashlib
import json
import os
import secrets
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlencode

import httpx
import jwt
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse

ISSUER = "https://localhost:19443"
CALLBACK = "https://localhost:18443/auth/callback"
DIRECTORY = Path(os.environ["KELPIE_PREVIEW_TEST_DIRECTORY"])


def initialize():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Kelpie disposable test")])
    now = datetime.now(UTC)
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                   .public_key(key.public_key()).serial_number(x509.random_serial_number())
                   .not_valid_before(now - timedelta(minutes=1))
                   .not_valid_after(now + timedelta(days=1))
                   .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
                   .add_extension(x509.SubjectAlternativeName([
                       x509.DNSName("localhost"), x509.DNSName("*.preview.localhost"),
                   ]), critical=False).sign(key, hashes.SHA256()))
    private_file = DIRECTORY / "tls.key"
    private_file.touch(mode=0o600)
    private_file.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                              serialization.PrivateFormat.PKCS8,
                                              serialization.NoEncryption()))
    (DIRECTORY / "tls.crt").write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    (DIRECTORY / "policy.json").write_text(json.dumps({
        "organization_id": "preview-test", "issuer": ISSUER, "claim": "preview-test",
        "members": [{"subject": "preview-admin", "role": "administrator"}],
        "repositories": [{"name": "demo/preview-test"}],
    }))


async def worker():
    credential = (DIRECTORY / "worker-token").read_text().strip()
    async with httpx.AsyncClient(base_url="http://127.0.0.1:18530", timeout=10) as client:
        headers = {"Authorization": f"Bearer {credential}"}
        response = await client.post("/api/workers/register", headers=headers, json={
            "name": "preview-browser-worker", "cpu_total": 16, "memory_mb_total": 32768,
            "disk_gb_available": 300, "labels": {"virtualization": "http-test-only"},
        })
        response.raise_for_status()
        worker_id = response.json()["id"]
        leases = {}
        while True:
            response = await client.post(f"/api/workers/{worker_id}/claim", headers=headers,
                                         json={"cpu": 2, "memory_mb": 4096, "disk_gb": 30})
            response.raise_for_status()
            claim = response.json()
            if claim:
                item = claim["work_item"]
                lease_headers = {"X-Kelpie-Lease": claim["lease_token"]}
                leases[item["id"]] = lease_headers
                for state in ("analyzing", "implementing"):
                    response = await client.post(f"/api/runs/{item['id']}/transition",
                                                 headers=lease_headers, json={
                                                     "status": state,
                                                     "expected_version": item["version"],
                                                     "message": "Isolated HTTP Preview fixture",
                                                 })
                    response.raise_for_status()
                    item = response.json()
                response = await client.post(f"/api/runs/{item['id']}/preview",
                                             headers=lease_headers, json={
                                                 "target_url": "http://127.0.0.1:16330",
                                                 "ttl_seconds": 3600,
                                             })
                response.raise_for_status()
            for work_id, lease_headers in list(leases.items()):
                response = await client.get(f"/api/runs/{work_id}", headers=lease_headers)
                if response.status_code in {401, 404, 409, 410}:
                    del leases[work_id]
                else:
                    response.raise_for_status()
            await asyncio.sleep(2)


identity = FastAPI()
target = FastAPI()
signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
codes = {}


@identity.get("/.well-known/openid-configuration")
async def discovery():
    return {"issuer": ISSUER, "authorization_endpoint": f"{ISSUER}/authorize",
            "token_endpoint": f"{ISSUER}/token", "jwks_uri": f"{ISSUER}/jwks",
            "id_token_signing_alg_values_supported": ["RS256"],
            "token_endpoint_auth_methods_supported": ["none"]}


@identity.get("/jwks")
async def jwks():
    key = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(signing_key.public_key()))
    return {"keys": [{**key, "kid": "disposable", "use": "sig", "alg": "RS256"}]}


@identity.get("/authorize")
async def authorize(request: Request):
    parameters = dict(request.query_params)
    if (parameters.get("client_id") != "preview-test"
            or parameters.get("redirect_uri") != CALLBACK
            or parameters.get("code_challenge_method") != "S256"
            or not all(parameters.get(key) for key in ("nonce", "state", "code_challenge"))):
        raise HTTPException(400, "Invalid test authorization")
    code = secrets.token_urlsafe(32)
    codes[code] = {**parameters, "expires": time.time() + 30}
    return RedirectResponse(f"{CALLBACK}?{urlencode({'code': code, 'state': parameters['state']})}")


@identity.post("/token")
async def token(request: Request):
    parameters = parse_qs((await request.body()).decode())
    code = parameters.get("code", [""])[0]
    saved = codes.pop(code, None)
    verifier = parameters.get("code_verifier", [""])[0]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    if (not saved or saved["expires"] < time.time() or saved["code_challenge"] != challenge
            or parameters.get("redirect_uri") != [CALLBACK]
            or parameters.get("client_id") != ["preview-test"]
            or parameters.get("grant_type") != ["authorization_code"]):
        raise HTTPException(400, "Invalid test code")
    now = int(time.time())
    signed = jwt.encode({"iss": ISSUER, "sub": "preview-admin", "aud": "preview-test",
                         "organization": "preview-test", "nonce": saved["nonce"],
                         "iat": now, "exp": now + 3600}, signing_key, algorithm="RS256",
                        headers={"kid": "disposable"})
    return {"id_token": signed, "token_type": "Bearer", "expires_in": 3600}


@target.get("/", response_class=HTMLResponse)
async def application():
    return """<!doctype html><html lang="en"><meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Isolated Preview</title><style>
    body{font:18px system-ui;background:#f1f5f9;color:#132238;padding:32px;line-height:1.6}
    main{max-width:640px;margin:auto;background:white;padding:32px;border-radius:20px}
    button{font:inherit;padding:12px;border-radius:8px;border:1px solid #64748b}
    </style><main><h1>Isolated Preview</h1><p>HTTP test application · VM isolation is not simulated.</p>
    <button id="counter">Clicked 0 times</button><p id="socket" role="status">Connecting…</p>
    <script>let count=0;document.querySelector('#counter').onclick=e=>{
    e.target.textContent='Clicked '+(++count)+' times'};
    const ws=new WebSocket('wss://'+location.host+'/echo');
    ws.onopen=()=>ws.send('Preview connected');
    ws.onmessage=e=>document.querySelector('#socket').textContent=e.data;
    ws.onclose=()=>document.querySelector('#socket').textContent='Preview disconnected';
    </script></main></html>"""


@target.get("/headers")
async def headers(request: Request):
    # Booleans only: credentials must never be echoed, including failure evidence.
    return {"has_authorization": "authorization" in request.headers,
            "has_platform_cookie": "kelpie" in request.headers.get("cookie", ""),
            "has_work_scope": bool(request.headers.get("x-kelpie-work-item"))}


@target.websocket("/echo")
async def echo(socket: WebSocket):
    await socket.accept()
    try:
        while True:
            await socket.send_text(await socket.receive_text())
    except WebSocketDisconnect:
        pass


async def serve():
    options = {"host": "127.0.0.1", "access_log": False, "log_level": "warning"}
    await asyncio.gather(worker(),
                         uvicorn.Server(uvicorn.Config(identity, port=19330, **options)).serve(),
                         uvicorn.Server(uvicorn.Config(target, port=16330, **options)).serve())


if __name__ == "__main__":
    if sys.argv[1:] == ["initialize"]:
        initialize()
    else:
        asyncio.run(serve())
