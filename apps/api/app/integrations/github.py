import asyncio
import time
from pathlib import Path
from urllib.parse import quote

import httpx
import jwt

from ..config import Settings


class GitHubAppClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    @property
    def configured(self) -> bool:
        return bool(self.settings.github_app_id and self.settings.github_private_key_path)

    async def app_jwt(self) -> str:
        if not self.configured:
            raise RuntimeError("GitHub App is not configured")
        private_key = await asyncio.to_thread(
            Path(self.settings.github_private_key_path).read_text
        )
        now = int(time.time())
        return jwt.encode(
            {"iat": now - 30, "exp": now + 9 * 60, "iss": self.settings.github_app_id},
            private_key,
            algorithm="RS256",
        )

    async def installation_for_repository(self, repository: str) -> int | None:
        if not self.configured:
            return None
        token = await self.app_jwt()
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(
                f"{self.settings.github_api_url}/repos/{repository}/installation",
                headers=self._headers(token),
            )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return int(response.json()["id"])

    async def installation_token(self, installation_id: int) -> str:
        app_token = await self.app_jwt()
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                f"{self.settings.github_api_url}/app/installations/{installation_id}/access_tokens",
                headers=self._headers(app_token),
                json={
                    "permissions": {
                        "contents": "write",
                        "pull_requests": "write",
                        "issues": "write",
                    }
                },
            )
        response.raise_for_status()
        return response.json()["token"]

    async def repository(self, repository: str, token: str) -> dict:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(
                f"{self.settings.github_api_url}/repos/{repository}",
                headers=self._headers(token),
            )
        response.raise_for_status()
        return response.json()

    async def create_pull_request(
        self,
        repository: str,
        token: str,
        *,
        title: str,
        head: str,
        base: str,
        body: str,
    ) -> str:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                f"{self.settings.github_api_url}/repos/{repository}/pulls",
                headers=self._headers(token),
                json={"title": title, "head": head, "base": base, "body": body},
            )
        response.raise_for_status()
        return response.json()["html_url"]

    async def find_pull_request(
        self, repository: str, token: str, *, owner: str, head: str
    ) -> str | None:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(
                f"{self.settings.github_api_url}/repos/{repository}/pulls",
                headers=self._headers(token),
                params={"state": "all", "head": f"{owner}:{head}", "per_page": 1},
            )
        response.raise_for_status()
        matches = response.json()
        return matches[0]["html_url"] if matches else None

    async def branch_exists(self, repository: str, token: str, branch: str) -> bool:
        encoded_branch = quote(branch, safe="")
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(
                f"{self.settings.github_api_url}/repos/{repository}/git/ref/heads/{encoded_branch}",
                headers=self._headers(token),
            )
        if response.status_code == 404:
            return False
        response.raise_for_status()
        return True

    @staticmethod
    def _headers(token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
