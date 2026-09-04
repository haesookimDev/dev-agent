from app.config import Settings


def test_comma_separated_list_environment_values(monkeypatch) -> None:
    monkeypatch.setenv("CORS_ORIGINS", "https://one.example, https://two.example")
    monkeypatch.setenv("SLACK_APPROVER_USER_IDS", "U123,U456")
    monkeypatch.setenv("PREVIEW_ALLOWED_CIDRS", "10.20.0.0/16,10.30.0.0/16")
    monkeypatch.setenv("GITHUB_APP_ID", "")

    settings = Settings(_env_file=None)

    assert settings.cors_origins == ["https://one.example", "https://two.example"]
    assert settings.slack_approver_user_ids == ["U123", "U456"]
    assert settings.preview_allowed_cidrs == ["10.20.0.0/16", "10.30.0.0/16"]
    assert settings.github_app_id is None
