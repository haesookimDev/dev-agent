"""HTTP storage policy for authenticated artifact lists, files and handled errors."""

import re

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

ARTIFACT_PATH = re.compile(r"/api/work-items/[^/]+/artifacts(?:/[^/]+)?/?")


class ArtifactCacheMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        root = scope.get("root_path", "")
        if root and path.startswith(root + "/"):
            path = path[len(root):]
        if scope["type"] != "http" or ARTIFACT_PATH.fullmatch(path) is None:
            await self.app(scope, receive, send)
            return

        async def protect_response(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["Cache-Control"] = "no-store"
                # Also vary navigations without Origin; do not grant additional CORS access.
                vary = ", ".join(headers.getlist("vary"))
                if "origin" not in {field.strip().lower() for field in vary.split(",")}:
                    headers["Vary"] = f"{vary}, Origin" if vary else "Origin"
            await send(message)

        # Do not buffer bodies, catch application errors or alter stream cancellation.
        await self.app(scope, receive, protect_response)
