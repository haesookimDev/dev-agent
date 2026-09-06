"""Real EventSource/SQLite drill with disposable scoped sessions; no IdP, VM or SCM."""

import json
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from artifact_runtime import artifact_runtime
from stream_runtime_checks import assert_stream_log_clean, wait_state


def stop(_signal, _frame):
    raise SystemExit(0)


def main():
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    class Handler(BaseHTTPRequestHandler):
        def respond(self, body, content_type="application/json", cookie=False):
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            if cookie:
                self.send_header("Set-Cookie", f"{runtime.cookie_name}={runtime.tokens[0]}; "
                                 "Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=300")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/":
                self.respond(body, "text/html; charset=utf-8", cookie=True)
            elif self.path == "/state":
                response = runtime.clients[0].get("/__test/stream-state")
                response.raise_for_status()
                self.respond(response.content)
            else:
                self.send_error(404)

        def do_POST(self):
            routes = {"/arm": "/__test/arm-stream-pause",
                      "/release": "/__test/release-stream-pause"}
            if self.path not in routes:
                self.send_error(404)
                return
            response = runtime.clients[0].post(routes[self.path])
            response.raise_for_status()
            self.respond(response.content)

        def log_message(self, *_args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        origin = f"http://localhost:{server.server_port}"
        with (TemporaryDirectory(prefix="kelpie-stream-browser-") as directory,
              artifact_runtime(Path(directory), web_origin=origin,
                               app_target="stream_runtime_app:app",
                               verify_log=assert_stream_log_clean) as runtime):
            url = f"{runtime.api_url}/api/work-items/{runtime.works[0]}/events"
            body = ("<!doctype html><meta charset=utf-8><title>Stream cleanup drill</title>"
                    "<h1>Stream cleanup drill</h1><button id=connect>Connect</button>"
                    "<button id=disconnect disabled>Disconnect</button><pre id=events></pre>"
                    "<script>let source; const connect = document.querySelector('#connect');"
                    "const disconnect = document.querySelector('#disconnect');"
                    "connect.onclick = () => {"
                    f"source = new EventSource({json.dumps(url)}, {{withCredentials: true}});"
                    "source.onmessage = event => {"
                    "document.querySelector('#events').textContent += event.data + '\\n'; };"
                    "connect.disabled = true; disconnect.disabled = false; };"
                    "disconnect.onclick = () => { source.close(); connect.disabled = false;"
                    "disconnect.disabled = true; };"
                    "window.addEventListener('pagehide', () => source?.close());</script>").encode()
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                # Credentials remain in memory/HttpOnly cookies, never readiness output or URLs.
                print(json.dumps({"bootstrapUrl": origin}), flush=True)
                sys.stdin.readline()
                wait_state(runtime.clients[0], lambda state: state["active"] == 0
                           and state["checked_out"] == 0)
            finally:
                server.shutdown()
                thread.join(timeout=2)


if __name__ == "__main__":
    main()
