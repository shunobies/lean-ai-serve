"""OIDC token validation via JWKS endpoint discovery."""

from __future__ import annotations

import logging
import time

import httpx
import jwt

from lean_ai_serve.config import OIDCConfig
from lean_ai_serve.models.schemas import AuthUser

logger = logging.getLogger(__name__)

# Algorithms accepted for OIDC JWT validation
_OIDC_ALGORITHMS = ["RS256", "RS384", "RS512"]


class OIDCValidator:
    """Validates OIDC JWT tokens against an IdP's JWKS endpoint.

    Fetches public keys from the OpenID Connect discovery endpoint,
    caches them with configurable TTL, and auto-refreshes on unknown kid.
    """

    def __init__(self, config: OIDCConfig) -> None:
        self._config = config
        self._jwks_uri: str | None = None
        self._jwks_keys: dict[str, dict] = {}  # kid -> JWK dict
        self._jwks_fetched_at: float = 0.0
        self._http: httpx.AsyncClient | None = None

    async def initialize(self) -> None:
        """Discover JWKS URI from OpenID configuration and fetch initial keys."""
        self._http = httpx.AsyncClient(timeout=10.0)

        discovery_url = f"{self._config.issuer_url.rstrip('/')}/.well-known/openid-configuration"
        logger.info("Fetching OIDC discovery from %s", discovery_url)

        resp = await self._http.get(discovery_url)
        resp.raise_for_status()
        openid_config = resp.json()

        self._jwks_uri = openid_config["jwks_uri"]
        await self._refresh_jwks()
        logger.info(
            "OIDC validator initialized (issuer=%s, %d keys cached)",
            self._config.issuer_url,
            len(self._jwks_keys),
        )

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._http:
            await self._http.aclose()
            self._http = None

    async def validate_token(self, token: str) -> AuthUser | None:
        """Decode and validate an OIDC JWT token.

        Returns AuthUser on success, None on any validation failure.
        """
        try:
            # Decode header to get kid (without verification)
            unverified = jwt.get_unverified_header(token)
            kid = unverified.get("kid")
            if not kid:
                logger.debug("OIDC token missing kid header")
                return None

            # Look up signing key
            key_data = await self._get_signing_key(kid)
            if not key_data:
                logger.debug("No matching JWKS key for kid=%s", kid)
                return None

            # Build the public key from JWK
            from jwt import PyJWK

            jwk = PyJWK(key_data)

            # Decode and validate
            payload = jwt.decode(
                token,
                jwk.key,
                algorithms=_OIDC_ALGORITHMS,
                issuer=self._config.issuer_url,
                audience=self._config.audience,
            )

            # Extract roles from configurable claim path
            oidc_roles = self._resolve_claim(payload, self._config.roles_claim)
            roles = self._map_roles(oidc_roles)

            return AuthUser(
                user_id=payload.get("sub", "unknown"),
                display_name=payload.get("name", payload.get("sub", "unknown")),
                roles=roles,
                allowed_models=["*"],
                auth_method="oidc",
            )

        except jwt.ExpiredSignatureError:
            logger.debug("OIDC token expired")
            return None
        except jwt.InvalidAudienceError:
            logger.debug("OIDC token has wrong audience")
            return None
        except jwt.InvalidIssuerError:
            logger.debug("OIDC token has wrong issuer")
            return None
        except jwt.InvalidTokenError as e:
            logger.debug("Invalid OIDC token: %s", e)
            return None
        except Exception:
            logger.exception("Unexpected error validating OIDC token")
            return None

    async def _get_signing_key(self, kid: str) -> dict | None:
        """Get a signing key by kid, refreshing JWKS if needed."""
        # Check cache first
        if kid in self._jwks_keys and not self._is_jwks_expired():
            return self._jwks_keys[kid]

        # Key not found or cache expired — refresh
        await self._refresh_jwks()
        return self._jwks_keys.get(kid)

    async def _refresh_jwks(self) -> None:
        """Fetch JWKS from the IdP endpoint."""
        if not self._jwks_uri or not self._http:
            return

        resp = await self._http.get(self._jwks_uri)
        resp.raise_for_status()
        jwks = resp.json()

        self._jwks_keys.clear()
        for key in jwks.get("keys", []):
            kid = key.get("kid")
            if kid:
                self._jwks_keys[kid] = key
        self._jwks_fetched_at = time.monotonic()
        logger.debug("Refreshed JWKS: %d keys cached", len(self._jwks_keys))

    @staticmethod
    def _resolve_claim(payload: dict, claim_path: str) -> list[str]:
        """Traverse a dot-notation claim path to extract roles.

        E.g., "realm_access.roles" traverses payload["realm_access"]["roles"].
        Returns empty list if path doesn't resolve or result isn't a list.
        """
        parts = claim_path.split(".")
        current = payload
        for part in parts:
            if isinstance(current, dict):
                current = current.get(part)
            else:
                return []
            if current is None:
                return []

        if isinstance(current, list):
            return [str(r) for r in current]
        if isinstance(current, str):
            return [current]
        return []

    def _map_roles(self, oidc_roles: list[str]) -> list[str]:
        """Map OIDC roles to lean-ai-serve roles.

        Roles present in role_mapping are translated. Roles not in the mapping
        are passed through unchanged. If no roles resolve, returns [default_role].
        """
        if not oidc_roles:
            return [self._config.default_role]

        mapped = []
        for role in oidc_roles:
            if role in self._config.role_mapping:
                mapped.append(self._config.role_mapping[role])
            else:
                mapped.append(role)

        return mapped if mapped else [self._config.default_role]

    def _is_jwks_expired(self) -> bool:
        """Check if cached JWKS has exceeded TTL."""
        return time.monotonic() - self._jwks_fetched_at > self._config.jwks_cache_ttl
