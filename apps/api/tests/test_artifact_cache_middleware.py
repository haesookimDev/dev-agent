import pytest

from app.artifact_cache import ArtifactCacheMiddleware


@pytest.mark.parametrize("path,root", [
    ("/api/work-items/work/artifacts", ""),
    ("/api/work-items/work/artifacts/evidence", ""),
    ("/api/work-items/work/artifacts/evidence/", ""),
    ("/prefix/api/work-items/work/artifacts/evidence", "/prefix"),
    ("/api/work-items/work/artifacts/evidence", "/prefix"),
])
@pytest.mark.parametrize("status", [200, 401, 404, 410, 503])
async def test_artifact_headers_preserve_status_body_and_existing_vary(path, root, status):
    received = []
    chunks = [{"type": "http.response.body", "body": b"first", "more_body": True},
              {"type": "http.response.body", "body": b"last", "more_body": False}]

    async def downstream(scope, receive, send):
        await send({"type": "http.response.start", "status": status, "headers": [
            (b"cache-control", b"public, max-age=3600"), (b"vary", b"Accept-Encoding"),
            (b"vary", b"Cookie"), (b"content-security-policy", b"sandbox"),
        ]})
        for chunk in chunks:
            await send(chunk)
            assert received[-1] is chunk, "Body chunks must be forwarded without buffering"

    async def collect(message):
        received.append(message)

    await ArtifactCacheMiddleware(downstream)(
        {"type": "http", "path": path, "root_path": root}, None, collect)
    start = received[0]
    assert start["status"] == status
    headers = dict(start["headers"])
    assert headers[b"cache-control"] == b"no-store"
    assert headers[b"vary"] == b"Accept-Encoding, Cookie, Origin"
    assert headers[b"content-security-policy"] == b"sandbox"
    assert received[1:] == chunks


@pytest.mark.parametrize("scope", [
    {"type": "http", "path": "/api/work-items/work/events"},
    {"type": "http", "path": "/api/runs/work/artifacts/upload"},
    {"type": "http", "path": "/api/work-items/work/artifacts-extra"},
    {"type": "http", "path": "/other/api/work-items/work/artifacts"},
    {"type": "http", "path": "/api/work-items/work/artifacts/evidence/extra"},
    {"type": "http", "path": "/api/work-items/work/event-log"},
    {"type": "http", "path": "/readyz"},
    {"type": "lifespan"},
    {"type": "websocket", "path": "/api/work-items/work/artifacts/evidence"},
])
async def test_unrelated_requests_bypass_cache_policy_entirely(scope):
    receive = object()
    send = object()
    calls = []

    async def downstream(actual_scope, actual_receive, actual_send):
        assert actual_scope is scope and actual_receive is receive and actual_send is send
        calls.append(True)

    await ArtifactCacheMiddleware(downstream)(scope, receive, send)
    assert calls == [True]


async def test_existing_origin_vary_is_not_duplicated():
    received = []

    async def downstream(scope, receive, send):
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"vary", b"Accept-Encoding, origin")]})

    async def collect(message):
        received.append(message)

    await ArtifactCacheMiddleware(downstream)(
        {"type": "http", "path": "/api/work-items/work/artifacts"}, None, collect)
    assert dict(received[0]["headers"])[b"vary"] == b"Accept-Encoding, origin"
