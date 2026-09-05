import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.config import Settings
from app.integrations.github import GitHubAppClient


async def test_github_app_jwt_has_string_issuer(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    source = tmp_path / "private-key.pem"
    source.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
    ))
    github = GitHubAppClient(Settings(_env_file=None, github_app_id=123,
                                     github_private_key_path=str(source)))
    token = await github.app_jwt()
    payload = jwt.decode(token, key.public_key(), algorithms=["RS256"], issuer="123")
    assert payload["iss"] == "123"
    assert payload["exp"] - payload["iat"] == 570
