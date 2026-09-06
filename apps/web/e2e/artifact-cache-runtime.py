"""Actual HTTP cache drill; only disposable evidence/session state is changed."""

import json
import signal
import sqlite3
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from artifact_runtime import artifact_runtime
from stream_runtime_checks import assert_stream_log_clean


def stop(_signal, _frame):
    raise SystemExit(0)


def main():
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path not in {"/own", "/foreign", "/signed-out"}:
                self.send_error(404)
                return
            index = 1 if self.path == "/foreign" else 0
            age = 0 if self.path == "/signed-out" else 300
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Set-Cookie", f"{runtime.cookie_name}={runtime.tokens[index]}; "
                             f"Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age={age}")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if self.path == "/restore":
                held.rename(path)
            elif self.path == "/remove":
                path.rename(held)
            elif self.path == "/revoke":
                # Only the owned synthetic organization, never a real identity provider.
                with sqlite3.connect(runtime.database) as connection:
                    connection.execute("DELETE FROM memberships WHERE organization_id=?",
                                       ("artifact-0",))
            else:
                self.send_error(404)
                return
            self.send_response(204)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()

        def log_message(self, *_args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        origin = f"http://localhost:{server.server_port}"
        with (TemporaryDirectory(prefix="kelpie-cache-browser-") as directory,
              artifact_runtime(Path(directory), web_origin=origin,
                               verify_log=assert_stream_log_clean) as runtime):
            work = runtime.works[0]
            url = f"{runtime.api_url}/api/work-items/{work}/artifacts/{runtime.artifacts[0]}"
            path = runtime.root / runtime.key(runtime.artifacts[0])
            held = Path(directory) / "held-evidence"
            path.rename(held)
            body = ("<!doctype html><meta charset=utf-8><title>Artifact cache drill</title>"
                    f'<h1>Artifact cache drill</h1><a href="{url}">Open evidence</a>').encode()
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                print(json.dumps({"bootstrapUrl": origin, "artifactUrl": url,
                                  "listUrl": f"{runtime.api_url}/api/work-items/{work}/artifacts"}),
                      flush=True)
                sys.stdin.readline()
            finally:
                server.shutdown()
                thread.join(timeout=2)


if __name__ == "__main__":
    main()
