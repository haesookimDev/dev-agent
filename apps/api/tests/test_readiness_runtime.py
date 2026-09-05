"""A stalled PostgreSQL handshake must not hide API liveness or hang readiness."""

import os
import socket
import socketserver
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx


def test_running_api_remains_live_and_fails_readiness_with_an_unresponsive_database(tmp_path):
    stopping = threading.Event()

    class SilentPeer(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.settimeout(0.1)
            while not stopping.is_set():
                try:
                    if not self.request.recv(8192):
                        break
                except TimeoutError:
                    pass

    root = Path(__file__).resolve().parents[3]
    marker = "synthetic-private-readiness-password"
    with socketserver.ThreadingTCPServer(("127.0.0.1", 0), SilentPeer) as peer:
        thread = threading.Thread(target=peer.serve_forever, kwargs={"poll_interval": 0.05})
        thread.start()
        try:
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
            environment = {
                "PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(root / "apps/api"),
                "DATABASE_URL": (
                    f"postgresql+asyncpg://probe:{marker}@127.0.0.1:{peer.server_address[1]}/probe"
                ),
                "DATABASE_SCHEMA_MODE": "validate", "AUTH_MODE": "development",
                "WORKER_AUTH_MODE": "scoped", "DEVELOPMENT_ORGANIZATION": "readiness-test",
                "ARTIFACT_ROOT": str(tmp_path / "artifacts"),
                "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "",
            }
            log_path = tmp_path / "api.log"
            with log_path.open("wb") as log:
                process = subprocess.Popen(
                    [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
                     "--port", str(port), "--no-access-log", "--no-proxy-headers"],
                    cwd=tmp_path, env=environment, stdout=log, stderr=subprocess.STDOUT,
                )
                try:
                    with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=3) as client:
                        deadline = time.monotonic() + 10
                        while True:
                            assert process.poll() is None, "isolated API exited during startup"
                            try:
                                if client.get("/healthz", timeout=0.5).status_code == 200:
                                    break
                            except httpx.TransportError:
                                pass
                            assert time.monotonic() < deadline, "API startup was not bounded"
                            time.sleep(0.05)
                        with ThreadPoolExecutor(max_workers=1) as executor:
                            started = time.monotonic()
                            readiness = executor.submit(client.get, "/readyz")
                            assert client.get("/healthz", timeout=0.5).json() == {"status": "ok"}
                            assert not readiness.done()
                            response = readiness.result(timeout=4)
                            assert 1.5 <= time.monotonic() - started < 3.5
                            assert response.status_code == 503
                            assert response.json() == {
                                "status": "not_ready", "database_schema": "unreachable",
                            }
                        assert client.get("/healthz").status_code == 200
                finally:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
            retained_private_value = marker in log_path.read_text()
            assert not retained_private_value
        finally:
            stopping.set()
            peer.shutdown()
            thread.join(timeout=2)
            assert not thread.is_alive()
