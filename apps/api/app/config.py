from functools import lru_cache
from typing import Annotated, Literal, Self

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from .secrets import (
    EnvironmentSecretProvider,
    FileSecretProvider,
    SecretProvider,
    SecretUnavailableError,
)

SecretName = Literal[
    "oidc_client_secret", "worker_shared_secret", "github_webhook_secret",
    "slack_signing_secret", "slack_bot_token", "github_private_key", "gateway_secret",
]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

    database_url: str = "sqlite+aiosqlite:///./kelpie.db"
    database_schema_mode: Literal["validate", "bootstrap"] = "validate"
    auth_mode: Literal["development", "oidc"] = "development"
    development_subject: str = "local-admin"
    development_organization: str = "local"
    oidc_issuer_url: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: str = Field(default="", repr=False, exclude=True)
    oidc_client_secret_file: str = ""
    oidc_redirect_uri: str = ""
    oidc_organization_claim: str = "organization"
    oidc_scopes: Annotated[list[str], NoDecode] = ["openid", "profile"]
    oidc_allowed_algorithms: Annotated[list[str], NoDecode] = ["RS256"]
    oidc_session_cookie_name: str = "kelpie_session"
    oidc_login_cookie_name: str = "kelpie_oidc_state"
    oidc_cookie_secure: bool = True
    oidc_login_ttl_seconds: int = 300
    oidc_session_ttl_seconds: int = 28800
    oidc_clock_skew_seconds: int = 30
    worker_shared_secret: str = Field(
        default="development-worker-secret-change-me", repr=False, exclude=True,
    )
    worker_shared_secret_file: str = ""
    worker_auth_mode: Literal["scoped", "development"] = "scoped"
    gateway_secret: str = Field(default="", repr=False, exclude=True)
    gateway_secret_file: str = ""
    github_webhook_secret: str = Field(
        default="development-webhook-secret", repr=False, exclude=True,
    )
    github_webhook_secret_file: str = ""
    agent_trigger_label: str = "agent-ready"
    slack_signing_secret: str = Field(default="", repr=False, exclude=True)
    slack_signing_secret_file: str = ""
    slack_bot_token: str = Field(default="", repr=False, exclude=True)
    slack_bot_token_file: str = ""
    slack_channel_id: str = ""
    slack_approver_user_ids: Annotated[list[str], NoDecode] = []
    dashboard_url: str = "http://localhost:3000"
    preview_domain: str = "preview.localhost"
    preview_access_enabled: bool = False
    preview_https_port: int = Field(default=443, ge=1, le=65535)
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
    log_format: Literal["json", "text"] = "json"
    otel_service_name: str = "kelpie-api"
    otel_exporter_otlp_traces_endpoint: str = ""

    def secret_provider(self, name: SecretName) -> SecretProvider:
        if name == "github_private_key":
            path = self.github_private_key_path
            return FileSecretProvider(path) if path else EnvironmentSecretProvider("")
        if name not in {
            "oidc_client_secret", "worker_shared_secret", "github_webhook_secret",
            "slack_signing_secret", "slack_bot_token", "gateway_secret",
        }:
            raise ValueError("unknown secret name")
        path = getattr(self, f"{name}_file")
        if path:
            return FileSecretProvider(path)
        return EnvironmentSecretProvider(getattr(self, name))

    def read_secret(self, name: SecretName, *, required: bool = False) -> str:
        value = self.secret_provider(name).read()
        if required and not value.strip():
            raise SecretUnavailableError()
        return value

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

    @field_validator("oidc_allowed_algorithms", "oidc_scopes", mode="before")
    @classmethod
    def split_oidc_lists(cls, value: object) -> object:
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    @field_validator("github_app_id", mode="before")
    @classmethod
    def empty_github_app_id(cls, value: object) -> object:
        return None if value == "" else value

    @model_validator(mode="after")
    def validate_oidc_configuration(self) -> Self:
        if self.worker_auth_mode == "development" and self.auth_mode != "development":
            raise ValueError("shared Worker authentication is restricted to development mode")
        if self.auth_mode != "oidc":
            return self
        required = {
            "OIDC_ISSUER_URL": self.oidc_issuer_url,
            "OIDC_CLIENT_ID": self.oidc_client_id,
            "OIDC_REDIRECT_URI": self.oidc_redirect_uri,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ValueError(f"OIDC mode requires {', '.join(missing)}")
        if not self.oidc_issuer_url.startswith("https://"):
            raise ValueError("OIDC_ISSUER_URL must use https")
        if not self.oidc_redirect_uri.startswith("https://"):
            raise ValueError("OIDC_REDIRECT_URI must use https")
        if not self.dashboard_url.startswith("https://"):
            raise ValueError("DASHBOARD_URL must use https in OIDC mode")
        if not self.oidc_allowed_algorithms:
            raise ValueError("OIDC_ALLOWED_ALGORITHMS must not be empty")
        if "openid" not in self.oidc_scopes:
            raise ValueError("OIDC_SCOPES must include openid")
        if any(
            algorithm.startswith("HS") or algorithm == "none"
            for algorithm in self.oidc_allowed_algorithms
        ):
            raise ValueError("OIDC_ALLOWED_ALGORITHMS must use asymmetric signing")
        if self.oidc_login_ttl_seconds < 60 or self.oidc_login_ttl_seconds > 900:
            raise ValueError("OIDC_LOGIN_TTL_SECONDS must be between 60 and 900")
        if self.oidc_session_ttl_seconds < 300 or self.oidc_session_ttl_seconds > 86400:
            raise ValueError("OIDC_SESSION_TTL_SECONDS must be between 300 and 86400")
        if self.oidc_clock_skew_seconds < 0 or self.oidc_clock_skew_seconds > 300:
            raise ValueError("OIDC_CLOCK_SKEW_SECONDS must be between 0 and 300")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
