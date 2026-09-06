"""Harmless browser probe: changes only its own DOM, never reads credentials or sends data."""

import sqlite3
import struct
import zlib

MARKUP = (b'<!doctype html><meta charset="utf-8"><h1 id="probe">Synthetic probe not run</h1>'
          b"<script>document.documentElement.dataset.artifactProbe='executed';"
          b"document.getElementById('probe').textContent='Synthetic script executed';</script>")
FILENAME_PROBES = ("검증 결과 ✅.txt", "100%20 complete; v2.txt")


def png_evidence():
    def chunk(name, content):
        return (struct.pack("!I", len(content)) + name + content
                + struct.pack("!I", zlib.crc32(name + content)))
    pixels = b"\x00" + b"\x32\x65\xc8" * 32
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack("!IIBBBBB", 32, 32, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(pixels * 32)) + chunk(b"IEND", b""))


def seed_content(runtime):
    work = runtime.works[0]
    client = runtime.clients[0]
    artifacts = {}
    for name, content_type, content in [
        ("plain-probe.txt", "text/plain", MARKUP),
        ("evidence.png", "image/png", png_evidence()),
        ("result.json", "application/json", b'{"result":"synthetic evidence"}'),
    ]:
        response = client.post(f"/api/runs/{work}/artifacts/upload", headers=runtime.leases[work],
            params={"name": name, "content_type": content_type}, content=content)
        assert response.status_code == 201
        artifacts[name] = response.json()["id"]
    key = runtime.key(artifacts["plain-probe.txt"])
    for name in FILENAME_PROBES:
        response = client.post(f"/api/runs/{work}/artifacts", headers=runtime.leases[work], json={
            "kind": "evidence", "name": name, "content_type": "text/plain",
            "object_key": key, "size_bytes": len(MARKUP),
        })
        assert response.status_code == 201
        artifacts[name] = response.json()["id"]
    response = client.post(f"/api/runs/{work}/artifacts", headers=runtime.leases[work], json={
        "kind": "evidence", "name": "unsupported-report.html", "content_type": "text/html",
        "object_key": key, "size_bytes": len(MARKUP),
    })
    if response.status_code == 201:
        artifacts["unsupported-report.html"] = response.json()["id"]
    else:
        assert response.status_code == 415
        identity = runtime.retain_alias(key, "unsupported-report.html")
        # Simulate historical metadata; new API registration must never permit this type.
        with sqlite3.connect(runtime.database) as connection:
            connection.execute("UPDATE artifacts SET content_type=? WHERE id=?",
                               ("text/html", identity))
        artifacts["unsupported-report.html"] = identity
    return {"work": work, "artifacts": artifacts, "registration": response.status_code}
