import os
import traceback

import pytest

from app.config import Settings
from app.secrets import MAX_SECRET_BYTES, FileSecretProvider, SecretUnavailableError


def test_environment_compatibility_and_no_secret_in_settings_serialization() -> None:
    secret = "synthetic-environment-secret"
    settings = Settings(_env_file=None, slack_bot_token=secret)
    assert settings.read_secret("slack_bot_token") == secret
    assert secret not in repr(settings)
    assert secret not in settings.model_dump_json()
    assert secret not in repr(settings.secret_provider("slack_bot_token"))
    assert settings.read_secret("oidc_client_secret") == ""
    with pytest.raises(SecretUnavailableError):
        settings.read_secret("oidc_client_secret", required=True)
    with pytest.raises(ValueError, match="unknown secret name"):
        settings.secret_provider("database_url")


@pytest.mark.parametrize("name", [
    "oidc_client_secret", "worker_shared_secret", "github_webhook_secret",
    "slack_signing_secret", "slack_bot_token", "github_private_key",
])
def test_file_precedence_and_live_atomic_rotation(tmp_path, name) -> None:
    source = tmp_path / "secret"
    source.write_text("synthetic-first\n", encoding="utf-8")
    path_setting = "github_private_key_path" if name == "github_private_key" else f"{name}_file"
    settings = Settings(_env_file=None, **{path_setting: str(source)})
    provider = settings.secret_provider(name)
    assert settings.read_secret(name) == "synthetic-first"
    replacement = tmp_path / "replacement"
    replacement.write_text("synthetic-second\r\n", encoding="utf-8")
    replacement.replace(source)
    assert provider.read() == "synthetic-second"
    assert settings.read_secret(name) == "synthetic-second"
    source.unlink()
    with pytest.raises(SecretUnavailableError):
        settings.read_secret(name)


def test_projected_volume_symlink_rotation(tmp_path) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "value").write_text("synthetic-first")
    (second / "value").write_text("synthetic-second")
    current = tmp_path / "..data"
    current.symlink_to(first, target_is_directory=True)
    source = tmp_path / "value"
    source.symlink_to("..data/value")
    provider = FileSecretProvider(str(source))
    assert provider.read() == "synthetic-first"
    replacement = tmp_path / "..next"
    replacement.symlink_to(second, target_is_directory=True)
    replacement.replace(current)
    assert provider.read() == "synthetic-second"


@pytest.mark.parametrize("contents", [
    b"", b" \r\n", b"\xff", b"value\x00", b"x" * (MAX_SECRET_BYTES + 1),
])
def test_invalid_files_fail_closed_without_leaking_contents(tmp_path, contents) -> None:
    source = tmp_path / "secret"
    source.write_bytes(contents)
    settings = Settings(_env_file=None, worker_shared_secret_file=str(source))
    with pytest.raises(SecretUnavailableError, match="configured secret is unavailable"):
        settings.read_secret("worker_shared_secret")


def test_missing_file_never_falls_back_or_exposes_path(tmp_path) -> None:
    path = str(tmp_path / "confidential-filename")
    provider = FileSecretProvider(path)
    assert path not in repr(provider)
    try:
        provider.read()
    except SecretUnavailableError as error:
        assert path not in "".join(traceback.format_exception(error))
    else:
        pytest.fail("missing file was accepted")


@pytest.mark.parametrize("kind", ["directory", "fifo"])
def test_non_regular_files_are_rejected_without_blocking(tmp_path, kind) -> None:
    source = tmp_path / "secret"
    if kind == "directory":
        source.mkdir()
    else:
        os.mkfifo(source)
    with pytest.raises(SecretUnavailableError):
        FileSecretProvider(str(source)).read()


def test_pem_format_is_preserved(tmp_path) -> None:
    contents = "-----BEGIN SYNTHETIC KEY-----\nline-one\nline-two\n-----END SYNTHETIC KEY-----"
    source = tmp_path / "key.pem"
    source.write_text(contents + "\n")
    assert FileSecretProvider(str(source)).read() == contents
