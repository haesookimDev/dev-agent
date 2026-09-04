import pytest
from pydantic import ValidationError

from app.config import Settings


def test_comma_separated_list_environment_values(monkeypatch) -> None:
    monkeypatch.setenv("CORS_ORIGINS", "https://one.example, https://two.example")
    monkeypatch.setenv("SLACK_APPROVER_USER_IDS", "U123,U456")
    monkeypatch.setenv("PREVIEW_ALLOWED_CIDRS", "10.20.0.0/16,10.30.0.0/16")
    monkeypatch.setenv("OIDC_ALLOWED_ALGORITHMS", "RS256,ES256")
    monkeypatch.setenv("OIDC_SCOPES", "openid,profile,groups")
    monkeypatch.setenv("GITHUB_APP_ID", "")

    settings = Settings(_env_file=None)

    assert settings.cors_origins == ["https://one.example", "https://two.example"]
    assert settings.slack_approver_user_ids == ["U123", "U456"]
    assert settings.preview_allowed_cidrs == ["10.20.0.0/16", "10.30.0.0/16"]
    assert settings.oidc_allowed_algorithms == ["RS256", "ES256"]
    assert settings.oidc_scopes == ["openid", "profile", "groups"]
    assert settings.github_app_id is None


def test_database_schema_mode_defaults_to_validation() -> None:
    assert Settings(_env_file=None).database_schema_mode == "validate"


def test_oidc_mode_requires_https_endpoints_and_asymmetric_algorithms() -> None:
    with pytest.raises(ValidationError, match="OIDC_CLIENT_ID"):
        Settings(_env_file=None, auth_mode="oidc")

    with pytest.raises(ValidationError, match="must use https"):
        Settings(
            _env_file=None,
            auth_mode="oidc",
            oidc_issuer_url="http://identity.example",
            oidc_client_id="kelpie",
            oidc_redirect_uri="https://control.example/auth/callback",
        )

    with pytest.raises(ValidationError, match="asymmetric signing"):
        Settings(
            _env_file=None,
            auth_mode="oidc",
            oidc_issuer_url="https://identity.example",
            oidc_client_id="kelpie",
            oidc_redirect_uri="https://control.example/auth/callback",
            oidc_allowed_algorithms=["HS256"],
            dashboard_url="https://dashboard.example",
        )
