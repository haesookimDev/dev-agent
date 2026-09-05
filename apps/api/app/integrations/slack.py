import asyncio
import hashlib
import hmac
import time
from pathlib import Path

import httpx
from fastapi import HTTPException, status

from ..config import Settings
from ..models import WorkItem


def verify_signature(
    body: bytes,
    timestamp: str | None,
    signature: str | None,
    signing_secret: str,
    *,
    now: int | None = None,
) -> None:
    if not signing_secret:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Slack is not configured")
    try:
        request_time = int(timestamp or "")
    except ValueError as error:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid Slack timestamp") from error
    current_time = now if now is not None else int(time.time())
    if abs(current_time - request_time) > 300:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "stale Slack request")
    base = b"v0:" + str(request_time).encode() + b":" + body
    expected = "v0=" + hmac.new(signing_secret.encode(), base, hashlib.sha256).hexdigest()
    if not signature or not hmac.compare_digest(signature, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid Slack signature")


class SlackNotifier:
    def __init__(self, settings: Settings):
        self.settings = settings

    @property
    def enabled(self) -> bool:
        return bool(self._token())

    def _token(self) -> str:
        if not self.settings.slack_channel_id:
            return ""
        return self.settings.read_secret("slack_bot_token")

    async def post_status(self, work: WorkItem) -> None:
        token = self._token()
        if not token:
            return
        url = f"{self.settings.dashboard_url.rstrip('/')}/work-items/{work.id}"
        text = f"Kelpie · {work.title}\nStatus: {work.status.value}\n{url}"
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                "https://slack.com/api/chat.postMessage",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "channel": self.settings.slack_channel_id,
                    "text": text,
                    "unfurl_links": False,
                    "metadata": {
                        "event_type": "kelpie_work_status",
                        "event_payload": {
                            "work_item_id": work.id,
                            "correlation_id": work.correlation_id,
                        },
                    },
                },
            )
            response.raise_for_status()
            result = response.json()
            if not result.get("ok"):
                raise RuntimeError(f"Slack rejected message: {result.get('error', 'unknown')}")

    async def upload_image(self, image: Path, title: str, thread_ts: str | None = None) -> None:
        token = self._token()
        if not token:
            return
        headers = {"Authorization": f"Bearer {token}"}
        size = await asyncio.to_thread(lambda: image.stat().st_size)
        contents = await asyncio.to_thread(image.read_bytes)
        async with httpx.AsyncClient(timeout=60) as client:
            start = await client.post(
                "https://slack.com/api/files.getUploadURLExternal",
                headers=headers,
                data={"filename": image.name, "length": size},
            )
            start.raise_for_status()
            upload = start.json()
            if not upload.get("ok"):
                raise RuntimeError(f"Slack upload initialization failed: {upload.get('error')}")
            sent = await client.post(upload["upload_url"], content=contents)
            sent.raise_for_status()
            completion: dict = {
                "files": [{"id": upload["file_id"], "title": title}],
                "channel_id": self.settings.slack_channel_id,
            }
            if thread_ts:
                completion["thread_ts"] = thread_ts
            finished = await client.post(
                "https://slack.com/api/files.completeUploadExternal",
                headers=headers,
                json=completion,
            )
            finished.raise_for_status()
            if not finished.json().get("ok"):
                raise RuntimeError("Slack file completion failed")
