"""Real loopback TLS: custom API trust must preserve certificate/name checks."""

import asyncio
import ssl
import subprocess

import httpx
import pytest

from kelpie_runner.main import ControlClient


@pytest.fixture
def control_ca(tmp_path, monkeypatch):
    cert, key = tmp_path / "ca.pem", tmp_path / "key.pem"
    subprocess.run([
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
        "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost",
        "-addext", "basicConstraints=critical,CA:TRUE", "-addext",
        "keyUsage=critical,digitalSignature,keyCertSign,cRLSign",
        "-keyout", str(key), "-out", str(cert),
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
    for name in ("SSL_CERT_FILE", "SSL_CERT_DIR", "KELPIE_CONTROL_CA_FILE",
                 "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                 "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1")
    return cert, key


def test_control_ca_is_scoped_and_tls_checks_remain_enabled(control_ca, monkeypatch):
    cert, key = control_ca

    async def scenario():
        seen = []

        async def respond(reader, writer):
            try:
                headers = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 3)
                seen.append(headers)
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n"
                             b"Connection: close\r\n\r\n{}")
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        server = await asyncio.start_server(respond, "127.0.0.1", 0, ssl=context)
        port = server.sockets[0].getsockname()[1]
        async with server:
            for host, trusted, succeeds in [("localhost", False, False),
                                            ("localhost", True, True),
                                            ("127.0.0.1", True, False),
                                            ("localhost", False, False)]:
                if trusted:
                    monkeypatch.setenv("KELPIE_CONTROL_CA_FILE", str(cert))
                else:
                    monkeypatch.delenv("KELPIE_CONTROL_CA_FILE", raising=False)
                control = ControlClient(f"https://{host}:{port}", "work", "test-lease", "trace")
                try:
                    if succeeds:
                        await control.event("runner.connected", "TLS verified")
                    else:
                        with pytest.raises(httpx.ConnectError):
                            await control.event("must.not.arrive", "Rejected before credentials")
                finally:
                    await control.close()
            # Only the explicitly trusted, name-matching request sent headers.
            assert len(seen) == 1
            assert seen[0].startswith(b"POST /api/runs/work/events HTTP/1.1\r\n")
            assert b"X-Kelpie-Lease: test-lease\r\n" in seen[0]
            assert b"X-Kelpie-Correlation-ID: trace\r\n" in seen[0]
            with pytest.raises(ssl.SSLCertVerificationError):
                reader, writer = await asyncio.open_connection(
                    "127.0.0.1", port, ssl=ssl.create_default_context(),
                    server_hostname="localhost",
                )
                writer.close()
                await writer.wait_closed()

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["missing", "malformed"])
def test_invalid_explicit_ca_fails_closed(tmp_path, monkeypatch, kind):
    cert = tmp_path / "invalid-ca.pem"
    if kind == "malformed":
        cert.write_text("not a certificate\n")
    monkeypatch.setenv("KELPIE_CONTROL_CA_FILE", str(cert))
    with pytest.raises((OSError, ssl.SSLError)):
        ControlClient("https://control.example.test", "work", "test-lease", "trace")
