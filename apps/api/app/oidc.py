import asyncio
import base64
import hashlib
import hmac
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from typing import Annotated, Any
from urllib.parse import urlencode

import httpx
import jwt
from fastapi import Depends

from .config import Settings, get_settings


class OIDCConfigurationError(RuntimeError):
    pass


class OIDCAuthenticationError(RuntimeError):
    pass


@dataclass(frozen=True)
class OIDCIdentity:
    subject: str
    issuer: str
    organization: str
    expires_at: datetime


class OIDCProvider:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._metadata: dict[str, Any] | None = None
        self._jwks: dict[str, Any] | None = None
        self._metadata_expires_at = 0.0
        self._jwks_expires_at = 0.0
        self._cache_lock = asyncio.Lock()

    async def authorization_url(
        self,
        *,
        state: str,
        nonce: str,
        code_challenge: str,
    ) -> str:
        metadata = await self._get_metadata()
        parameters = {
            "client_id": self.settings.oidc_client_id,
            "redirect_uri": self.settings.oidc_redirect_uri,
            "response_type": "code",
            "scope": " ".join(self.settings.oidc_scopes),
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        separator = "&" if "?" in metadata["authorization_endpoint"] else "?"
        return f"{metadata['authorization_endpoint']}{separator}{urlencode(parameters)}"

    async def authenticate(
        self,
        *,
        code: str,
        code_verifier: str,
        expected_nonce: str,
    ) -> OIDCIdentity:
        metadata = await self._get_metadata()
        token_response = await self._exchange_code(metadata, code, code_verifier)
        id_token = token_response.get("id_token")
        if not isinstance(id_token, str) or not id_token:
            raise OIDCAuthenticationError("OIDC token response did not include an ID token")
        jwks = await self._get_jwks(metadata)
        return self.validate_id_token(id_token, expected_nonce=expected_nonce, jwks=jwks)

    def validate_id_token(
        self,
        token: str,
        *,
        expected_nonce: str,
        jwks: dict[str, Any],
    ) -> OIDCIdentity:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.InvalidTokenError as error:
            raise OIDCAuthenticationError("invalid OIDC ID token header") from error

        algorithm = header.get("alg")
        if algorithm not in self.settings.oidc_allowed_algorithms:
            raise OIDCAuthenticationError("OIDC ID token uses a disallowed algorithm")
        key_id = header.get("kid")
        if not isinstance(key_id, str) or not key_id:
            raise OIDCAuthenticationError("OIDC ID token is missing a key identifier")

        try:
            keys = jwt.PyJWKSet.from_dict(jwks).keys
            signing_key = next(key for key in keys if key.key_id == key_id)
        except (KeyError, StopIteration, jwt.PyJWKError) as error:
            raise OIDCAuthenticationError("OIDC ID token signing key was not found") from error

        try:
            claims = jwt.decode(
                token,
                key=signing_key.key,
                algorithms=self.settings.oidc_allowed_algorithms,
                audience=self.settings.oidc_client_id,
                issuer=self.settings.oidc_issuer_url,
                leeway=self.settings.oidc_clock_skew_seconds,
                options={"require": ["iss", "sub", "aud", "exp", "iat", "nonce"]},
            )
        except jwt.InvalidTokenError as error:
            raise OIDCAuthenticationError("OIDC ID token validation failed") from error

        nonce = claims.get("nonce")
        if not isinstance(nonce, str) or not hmac.compare_digest(nonce, expected_nonce):
            raise OIDCAuthenticationError("OIDC ID token nonce does not match")

        audience = claims.get("aud")
        authorized_party = claims.get("azp")
        if isinstance(audience, list) and len(audience) > 1 and authorized_party is None:
            raise OIDCAuthenticationError("OIDC ID token is missing the authorized party")
        if authorized_party is not None and authorized_party != self.settings.oidc_client_id:
            raise OIDCAuthenticationError("OIDC ID token authorized party does not match")

        organization = claims.get(self.settings.oidc_organization_claim)
        if not isinstance(organization, str) or not organization.strip():
            raise OIDCAuthenticationError("OIDC ID token is missing the organization claim")
        subject = claims.get("sub")
        issuer = claims.get("iss")
        normalized_organization = organization.strip()
        if not isinstance(subject, str) or len(subject) > 255:
            raise OIDCAuthenticationError("OIDC subject is invalid")
        if not isinstance(issuer, str) or len(issuer) > 1024:
            raise OIDCAuthenticationError("OIDC issuer is invalid")
        if len(normalized_organization) > 255:
            raise OIDCAuthenticationError("OIDC organization claim is invalid")

        expires_at = datetime.fromtimestamp(claims["exp"], tz=UTC)
        return OIDCIdentity(
            subject=subject,
            issuer=issuer,
            organization=normalized_organization,
            expires_at=expires_at,
        )

    async def _get_metadata(self) -> dict[str, Any]:
        now = time.monotonic()
        if self._metadata is not None and now < self._metadata_expires_at:
            return self._metadata
        async with self._cache_lock:
            now = time.monotonic()
            if self._metadata is not None and now < self._metadata_expires_at:
                return self._metadata
            discovery_url = (
                f"{self.settings.oidc_issuer_url.rstrip('/')}"
                "/.well-known/openid-configuration"
            )
            metadata = await self._get_json(discovery_url, "OIDC discovery")
            if metadata.get("issuer") != self.settings.oidc_issuer_url:
                raise OIDCConfigurationError("OIDC discovery issuer does not match configuration")
            for field in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
                value = metadata.get(field)
                if not isinstance(value, str) or not value.startswith("https://"):
                    raise OIDCConfigurationError(f"OIDC discovery {field} must use https")
            supported = metadata.get("id_token_signing_alg_values_supported", [])
            if supported and not set(self.settings.oidc_allowed_algorithms).intersection(supported):
                raise OIDCConfigurationError("OIDC provider does not support an allowed algorithm")
            self._metadata = metadata
            self._metadata_expires_at = now + 300
            return metadata

    async def _get_jwks(self, metadata: dict[str, Any]) -> dict[str, Any]:
        now = time.monotonic()
        if self._jwks is not None and now < self._jwks_expires_at:
            return self._jwks
        async with self._cache_lock:
            now = time.monotonic()
            if self._jwks is not None and now < self._jwks_expires_at:
                return self._jwks
            jwks = await self._get_json(metadata["jwks_uri"], "OIDC JWKS")
            if not isinstance(jwks.get("keys"), list):
                raise OIDCConfigurationError("OIDC JWKS response is missing keys")
            self._jwks = jwks
            self._jwks_expires_at = now + 300
            return jwks

    async def _exchange_code(
        self,
        metadata: dict[str, Any],
        code: str,
        code_verifier: str,
    ) -> dict[str, Any]:
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.settings.oidc_redirect_uri,
            "client_id": self.settings.oidc_client_id,
            "code_verifier": code_verifier,
        }
        auth: httpx.BasicAuth | None = None
        methods = metadata.get("token_endpoint_auth_methods_supported", ["client_secret_basic"])
        if self.settings.oidc_client_secret:
            if "client_secret_basic" in methods:
                auth = httpx.BasicAuth(
                    self.settings.oidc_client_id,
                    self.settings.oidc_client_secret,
                )
            elif "client_secret_post" in methods:
                data["client_secret"] = self.settings.oidc_client_secret
            else:
                raise OIDCConfigurationError(
                    "OIDC provider does not support the configured client authentication"
                )
        elif "none" not in methods:
            raise OIDCConfigurationError("OIDC provider requires a client secret")

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.post(metadata["token_endpoint"], data=data, auth=auth)
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise OIDCAuthenticationError("OIDC authorization code exchange failed") from error
        if not isinstance(payload, dict):
            raise OIDCAuthenticationError("OIDC token response is invalid")
        return payload

    async def _get_json(self, url: str, operation: str) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(url)
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise OIDCConfigurationError(f"{operation} request failed") from error
        if not isinstance(payload, dict):
            raise OIDCConfigurationError(f"{operation} response is invalid")
        return payload


def code_challenge(code_verifier: str) -> str:
    digest = hashlib.sha256(code_verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


@lru_cache(maxsize=16)
def _cached_provider(
    issuer_url: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    organization_claim: str,
    scopes: tuple[str, ...],
    allowed_algorithms: tuple[str, ...],
    clock_skew_seconds: int,
) -> OIDCProvider:
    settings = Settings(
        _env_file=None,
        auth_mode="oidc",
        oidc_issuer_url=issuer_url,
        oidc_client_id=client_id,
        oidc_client_secret=client_secret,
        oidc_redirect_uri=redirect_uri,
        oidc_organization_claim=organization_claim,
        oidc_scopes=list(scopes),
        oidc_allowed_algorithms=list(allowed_algorithms),
        oidc_clock_skew_seconds=clock_skew_seconds,
        dashboard_url="https://unused.invalid",
    )
    return OIDCProvider(settings)


def get_oidc_provider(
    settings: Annotated[Settings, Depends(get_settings)],
) -> OIDCProvider:
    return _cached_provider(
        settings.oidc_issuer_url,
        settings.oidc_client_id,
        settings.oidc_client_secret,
        settings.oidc_redirect_uri,
        settings.oidc_organization_claim,
        tuple(settings.oidc_scopes),
        tuple(settings.oidc_allowed_algorithms),
        settings.oidc_clock_skew_seconds,
    )
