from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import AsyncClient

from app.config import Settings, get_settings
from app.db import get_session
from app.iam import OrganizationPolicy, apply_policy
from app.main import app
from app.oidc import (
    OIDCAuthenticationError,
    OIDCIdentity,
    OIDCProvider,
    code_challenge,
    get_oidc_provider,
)


def oidc_settings() -> Settings:
    return Settings(
        _env_file=None,
        auth_mode="oidc",
        oidc_issuer_url="https://identity.example",
        oidc_client_id="kelpie-control",
        oidc_redirect_uri="https://control.example/auth/callback",
        oidc_cookie_secure=False,
        dashboard_url="https://dashboard.example",
        oidc_clock_skew_seconds=0,
    )


class FakeOIDCProvider:
    def __init__(self, *, expired: bool = False) -> None:
        self.state = ""
        self.nonce = ""
        self.challenge = ""
        self.code_verifier = ""
        self.expired = expired

    async def authorization_url(self, *, state: str, nonce: str, code_challenge: str) -> str:
        self.state = state
        self.nonce = nonce
        self.challenge = code_challenge
        return (
            "https://identity.example/authorize?"
            f"state={state}&nonce={nonce}&code_challenge={code_challenge}"
        )

    async def authenticate(
        self,
        *,
        code: str,
        code_verifier: str,
        expected_nonce: str,
    ) -> OIDCIdentity:
        assert code == "authorization-code"
        assert expected_nonce == self.nonce
        assert code_verifier
        self.code_verifier = code_verifier
        expiry = datetime.now(UTC) + (-timedelta(minutes=1) if self.expired else timedelta(hours=1))
        return OIDCIdentity(
            subject="user-123",
            issuer="https://identity.example",
            organization="acme",
            expires_at=expiry,
        )


@pytest.mark.asyncio
async def test_oidc_login_creates_session_and_consumes_state(client: AsyncClient) -> None:
    settings = oidc_settings()
    provider = FakeOIDCProvider()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_oidc_provider] = lambda: provider
    async for session in app.dependency_overrides[get_session]():
        await apply_policy(session, OrganizationPolicy.model_validate({
            "organization_id": "acme", "issuer": "https://identity.example", "claim": "acme",
            "members": [{"subject": "admin", "role": "administrator"},
                        {"subject": "user-123", "role": "viewer"}],
        }))
        await session.commit()

    login = await client.get("/auth/login", params={"return_to": "/ko/work-items"})

    assert login.status_code == 302
    assert login.headers["location"].startswith("https://identity.example/authorize?")
    parameters = parse_qs(urlsplit(login.headers["location"]).query)
    assert parameters["state"] == [provider.state]
    assert parameters["nonce"] == [provider.nonce]
    assert parameters["code_challenge"] == [provider.challenge]
    assert login.headers["cache-control"] == "no-store"

    callback = await client.get(
        "/auth/callback",
        params={"code": "authorization-code", "state": provider.state},
    )

    assert callback.status_code == 302
    assert callback.headers["location"] == "https://dashboard.example/ko/work-items"
    assert provider.code_verifier
    assert provider.challenge == code_challenge(provider.code_verifier)
    authenticated = await client.get(
        "/auth/session",
        headers={"X-Kelpie-User": "forged", "X-Kelpie-Role": "administrator"},
    )
    assert authenticated.status_code == 200
    assert authenticated.json() == {
        "subject": "user-123",
        "identity_provider": "https://identity.example",
        "organization": "acme",
        "role": "viewer",
    }

    replay = await client.get(
        "/auth/callback",
        params={"code": "authorization-code", "state": provider.state},
        headers={"Cookie": f"{settings.oidc_login_cookie_name}={provider.state}"},
    )
    assert replay.status_code == 401

    logout = await client.post("/auth/logout")
    assert logout.status_code == 204
    assert (await client.get("/auth/session")).status_code == 401


@pytest.mark.asyncio
async def test_oidc_mode_rejects_forged_identity_headers(client: AsyncClient) -> None:
    app.dependency_overrides[get_settings] = oidc_settings

    response = await client.get(
        "/api/work-items",
        headers={"X-Kelpie-User": "forged", "X-Kelpie-Role": "administrator"},
    )

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_oidc_callback_rejects_expired_identity(client: AsyncClient) -> None:
    settings = oidc_settings()
    provider = FakeOIDCProvider(expired=True)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_oidc_provider] = lambda: provider
    await client.get("/auth/login")

    response = await client.get(
        "/auth/callback",
        params={"code": "authorization-code", "state": provider.state},
    )

    assert response.status_code == 401
    assert (await client.get("/auth/session")).status_code == 401


def signed_id_token(
    private_key: rsa.RSAPrivateKey,
    *,
    issuer: str = "https://identity.example",
    audience: str | list[str] = "kelpie-control",
    nonce: str = "expected-nonce",
    expires_at: datetime | None = None,
    organization: str | None = "acme",
    authorized_party: str | None = None,
) -> str:
    now = datetime.now(UTC)
    claims: dict[str, object] = {
        "iss": issuer,
        "sub": "user-123",
        "aud": audience,
        "iat": int(now.timestamp()),
        "exp": int((expires_at or now + timedelta(minutes=5)).timestamp()),
        "nonce": nonce,
    }
    if organization is not None:
        claims["organization"] = organization
    if authorized_party is not None:
        claims["azp"] = authorized_party
    return jwt.encode(
        claims,
        private_key,
        algorithm="RS256",
        headers={"kid": "signing-key"},
    )


@pytest.fixture
def oidc_key_set() -> tuple[rsa.RSAPrivateKey, dict[str, object]]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    jwk.update({"kid": "signing-key", "use": "sig", "alg": "RS256"})
    return private_key, {"keys": [jwk]}


def test_id_token_validation_accepts_expected_claims(
    oidc_key_set: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    private_key, jwks = oidc_key_set
    provider = OIDCProvider(oidc_settings())

    identity = provider.validate_id_token(
        signed_id_token(private_key),
        expected_nonce="expected-nonce",
        jwks=jwks,
    )

    assert identity.subject == "user-123"
    assert identity.issuer == "https://identity.example"
    assert identity.organization == "acme"


@pytest.mark.parametrize(
    "overrides",
    [
        {"issuer": "https://attacker.example"},
        {"audience": "another-client"},
        {"nonce": "replayed-nonce"},
        {"expires_at": datetime.now(UTC) - timedelta(minutes=1)},
        {"organization": None},
        {"audience": ["kelpie-control", "another-client"]},
        {"authorized_party": "another-client"},
    ],
)
def test_id_token_validation_rejects_invalid_security_claims(
    oidc_key_set: tuple[rsa.RSAPrivateKey, dict[str, object]],
    overrides: dict[str, object],
) -> None:
    private_key, jwks = oidc_key_set
    provider = OIDCProvider(oidc_settings())
    token = signed_id_token(private_key, **overrides)

    with pytest.raises(OIDCAuthenticationError):
        provider.validate_id_token(token, expected_nonce="expected-nonce", jwks=jwks)


def test_id_token_validation_rejects_unknown_signature(
    oidc_key_set: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _, jwks = oidc_key_set
    attacker_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    provider = OIDCProvider(oidc_settings())

    with pytest.raises(OIDCAuthenticationError):
        provider.validate_id_token(
            signed_id_token(attacker_key),
            expected_nonce="expected-nonce",
            jwks=jwks,
        )
