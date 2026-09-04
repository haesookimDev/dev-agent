from functools import lru_cache
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite+aiosqlite:///./kelpie.db"
    auth_mode: str = "development"
    worker_shared_secret: str = "development-worker-secret-change-me"
    github_webhook_secret: str = "development-webhook-secret"
    agent_trigger_label: str = "agent-ready"
    slack_signing_secret: str = ""
    slack_bot_token: str = ""
    slack_channel_id: str = ""
    slack_approver_user_ids: Annotated[list[str], NoDecode] = []
    dashboard_url: str = "http://localhost:3000"
    preview_domain: str = "preview.localhost"
    preview_allowed_cidrs: Annotated[list[str], NoDecode] = ["10.0.0.0/8"]
    artifact_root: str = "/var/lib/kelpie/artifacts"
    github_app_id: int | None = None
    github_private_key_path: str = ""
    github_api_url: str = "https://api.github.com"
    git_bot_name: str = "kelpie[bot]"
    git_bot_email: str = "kelpie[bot]@users.noreply.github.com"
    cors_origins: Annotated[list[str], NoDecode] = ["http://localhost:3000"]
    worker_offline_seconds: int = 45
    lease_seconds: int = 120

    @field_validator("cors_origins", mode="before")
    @classmethod
    def split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    @field_validator("preview_allowed_cidrs", mode="before")
    @classmethod
    def split_preview_cidrs(cls, value: object) -> object:
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    @field_validator("slack_approver_user_ids", mode="before")
    @classmethod
    def split_user_ids(cls, value: object) -> object:
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    @field_validator("github_app_id", mode="before")
    @classmethod
    def empty_github_app_id(cls, value: object) -> object:
        return None if value == "" else value


@lru_cache
def get_settings() -> Settings:
    return Settings()
