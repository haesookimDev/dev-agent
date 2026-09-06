"""Disposable scoped-auth HTTP services for Chromium artifact acceptance, not a real IdP/VM."""

import html
import json
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from artifact_content_runtime import seed_content
from artifact_runtime import artifact_runtime


def stop(_signal, _frame):
    raise SystemExit(0)


def main():
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    with (TemporaryDirectory(prefix="kelpie-content-browser-") as directory,
          artifact_runtime(Path(directory)) as runtime):
        evidence = seed_content(runtime)
        links = "".join(
            f'<li><a href="{runtime.api_url}/api/work-items/{evidence["work"]}'
            f'/artifacts/{identity}">{html.escape(name)}</a></li>'
            for name, identity in evidence["artifacts"].items()
        )
        body = ("<!doctype html><meta charset=utf-8><title>Synthetic evidence</title>"
                f"<h1>Synthetic evidence</h1><ul>{links}</ul>").encode()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Set-Cookie", f"{runtime.cookie_name}={runtime.tokens[0]}; "
                                 "Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=300")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            # No session/lease/worker credentials in stdout or URLs.
            print(json.dumps({**evidence, "apiUrl": runtime.api_url,
                              "bootstrapUrl": f"http://localhost:{server.server_port}"}),
                  flush=True)
            sys.stdin.readline()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    main()
