"""Tests for OIDC token validation."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta

import httpx
import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from lean_ai_serve.config import OIDCConfig
from lean_ai_serve.security.oidc import OIDCValidator

ISSUER = "https://idp.example.com/realms/test"
AUDIENCE = "lean-ai-serve"
KID = "test-key-1"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def rsa_keypair():
    """Generate an RSA private/public key pair for JWT signing."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    return private_key, public_key


@pytest.fixture()
def jwks_response(rsa_keypair):
    """Build a JWKS JSON response from the public key."""
    _, public_key = rsa_keypair
    from jwt.algorithms import RSAAlgorithm

    jwk_dict = json.loads(RSAAlgorithm.to_jwk(public_key))
    jwk_dict["kid"] = KID
    jwk_dict["use"] = "sig"
    jwk_dict["alg"] = "RS256"
    return {"keys": [jwk_dict]}


@pytest.fixture()
def oidc_config():
    """OIDCConfig pointing to our test issuer."""
    return OIDCConfig(
        issuer_url=ISSUER,
        client_id="lean-ai-serve",
        audience=AUDIENCE,
        roles_claim="realm_access.roles",
        role_mapping={"ai-admin": "admin", "ai-user": "user"},
        default_role="user",
        jwks_cache_ttl=3600,
    )


def _make_mock_transport(jwks_response):
    """Create an httpx mock transport returning openid-config + JWKS."""
    openid_config = {
        "issuer": ISSUER,
        "jwks_uri": f"{ISSUER}/protocol/openid-connect/certs",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if ".well-known/openid-configuration" in str(request.url):
            return httpx.Response(200, json=openid_config)
        if "certs" in str(request.url):
            return httpx.Response(200, json=jwks_response)
        return httpx.Response(404)

    return httpx.MockTransport(handler)


@pytest.fixture()
async def validator(oidc_config, jwks_response):
    """Initialized OIDCValidator with mocked HTTP transport."""
    v = OIDCValidator(oidc_config)
    transport = _make_mock_transport(jwks_response)
    v._http = httpx.AsyncClient(transport=transport)

    # Discover JWKS URI and fetch keys
    discovery_url = f"{ISSUER}/.well-known/openid-configuration"
    resp = await v._http.get(discovery_url)
    v._jwks_uri = resp.json()["jwks_uri"]
    await v._refresh_jwks()
    yield v
    await v.close()


def _make_jwt(
    private_key,
    *,
    sub: str = "testuser",
    name: str = "Test User",
    roles: list[str] | None = None,
    kid: str = KID,
    issuer: str = ISSUER,
    audience: str = AUDIENCE,
    exp_delta: timedelta | None = None,
    extra_claims: dict | None = None,
):
    """Create a signed JWT token."""
    now = datetime.now(UTC)
    exp = now + (exp_delta if exp_delta is not None else timedelta(hours=1))
    payload = {
        "sub": sub,
        "name": name,
        "iss": issuer,
        "aud": audience,
        "iat": now,
        "exp": exp,
        "realm_access": {"roles": roles or ["user"]},
    }
    if extra_claims:
        payload.update(extra_claims)
    return pyjwt.encode(payload, private_key, algorithm="RS256", headers={"kid": kid})


# ---------------------------------------------------------------------------
# Token validation tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_valid_token(validator, rsa_keypair):
    """Valid JWT with correct issuer/audience/kid returns AuthUser."""
    private_key, _ = rsa_keypair
    token = _make_jwt(private_key, roles=["user"])
    user = await validator.validate_token(token)
    assert user is not None
    assert user.user_id == "testuser"
    assert user.display_name == "Test User"
    assert user.auth_method == "oidc"
    assert "user" in user.roles


@pytest.mark.asyncio
async def test_expired_token(validator, rsa_keypair):
    """Expired JWT returns None."""
    private_key, _ = rsa_keypair
    token = _make_jwt(private_key, exp_delta=timedelta(hours=-1))
    user = await validator.validate_token(token)
    assert user is None


@pytest.mark.asyncio
async def test_wrong_audience(validator, rsa_keypair):
    """JWT with wrong audience returns None."""
    private_key, _ = rsa_keypair
    token = _make_jwt(private_key, audience="wrong-audience")
    user = await validator.validate_token(token)
    assert user is None


@pytest.mark.asyncio
async def test_wrong_issuer(validator, rsa_keypair):
    """JWT with wrong issuer returns None."""
    private_key, _ = rsa_keypair
    token = _make_jwt(private_key, issuer="https://evil.example.com")
    user = await validator.validate_token(token)
    assert user is None


@pytest.mark.asyncio
async def test_bad_signature(validator):
    """JWT signed with a different key returns None."""
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = _make_jwt(other_key)
    user = await validator.validate_token(token)
    assert user is None


# ---------------------------------------------------------------------------
# Role mapping tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_role_mapping_direct(validator, rsa_keypair):
    """OIDC roles in role_mapping are translated."""
    private_key, _ = rsa_keypair
    token = _make_jwt(private_key, roles=["ai-admin"])
    user = await validator.validate_token(token)
    assert user is not None
    assert "admin" in user.roles


@pytest.mark.asyncio
async def test_role_mapping_passthrough(validator, rsa_keypair):
    """Unmapped OIDC roles are passed through."""
    private_key, _ = rsa_keypair
    token = _make_jwt(private_key, roles=["custom-role"])
    user = await validator.validate_token(token)
    assert user is not None
    assert "custom-role" in user.roles


@pytest.mark.asyncio
async def test_default_role_when_no_roles(validator, rsa_keypair):
    """No roles in token uses default_role."""
    private_key, _ = rsa_keypair
    token = _make_jwt(
        private_key,
        roles=None,
        extra_claims={"realm_access": {}},  # Empty roles
    )
    user = await validator.validate_token(token)
    assert user is not None
    assert "user" in user.roles  # default_role


# ---------------------------------------------------------------------------
# JWKS refresh tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_jwks_refresh_on_unknown_kid(oidc_config, jwks_response, rsa_keypair):
    """Unknown kid triggers JWKS refresh and retries."""
    private_key, _ = rsa_keypair

    # Start with empty keys, JWKS endpoint has the key
    v = OIDCValidator(oidc_config)
    transport = _make_mock_transport(jwks_response)
    v._http = httpx.AsyncClient(transport=transport)
    v._jwks_uri = f"{ISSUER}/protocol/openid-connect/certs"
    v._jwks_keys = {}  # Empty — will need to refresh

    token = _make_jwt(private_key, roles=["user"])
    user = await v.validate_token(token)

    assert user is not None
    assert user.user_id == "testuser"
    assert KID in v._jwks_keys  # Key was fetched
    await v.close()


@pytest.mark.asyncio
async def test_jwks_cache_ttl(validator, rsa_keypair):
    """Expired JWKS cache triggers refresh."""
    private_key, _ = rsa_keypair
    # Simulate expired cache
    validator._jwks_fetched_at = time.monotonic() - 7200  # 2 hours ago
    assert validator._is_jwks_expired()

    token = _make_jwt(private_key, roles=["user"])
    user = await validator.validate_token(token)
    assert user is not None
    # After validation, cache should be refreshed
    assert not validator._is_jwks_expired()


# ---------------------------------------------------------------------------
# Claim path traversal tests
# ---------------------------------------------------------------------------


def test_claim_traversal_nested():
    """Dot-notation traverses nested dict."""
    payload = {"realm_access": {"roles": ["admin", "user"]}}
    result = OIDCValidator._resolve_claim(payload, "realm_access.roles")
    assert result == ["admin", "user"]


def test_claim_traversal_flat():
    """Single-level claim reads top-level."""
    payload = {"roles": ["admin"]}
    result = OIDCValidator._resolve_claim(payload, "roles")
    assert result == ["admin"]


def test_claim_traversal_missing():
    """Missing claim path returns empty list."""
    payload = {"sub": "testuser"}
    result = OIDCValidator._resolve_claim(payload, "realm_access.roles")
    assert result == []


def test_claim_traversal_string_value():
    """String claim value wrapped in list."""
    payload = {"role": "admin"}
    result = OIDCValidator._resolve_claim(payload, "role")
    assert result == ["admin"]


# ---------------------------------------------------------------------------
# Initialize tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_initialize_fetches_openid_config(oidc_config, jwks_response):
    """initialize() discovers jwks_uri and fetches keys."""
    transport = _make_mock_transport(jwks_response)
    v = OIDCValidator(oidc_config)
    v._http = httpx.AsyncClient(transport=transport)

    # Manually call initialize logic (we already set _http)
    discovery_url = f"{ISSUER}/.well-known/openid-configuration"
    resp = await v._http.get(discovery_url)
    v._jwks_uri = resp.json()["jwks_uri"]
    await v._refresh_jwks()

    assert v._jwks_uri is not None
    assert len(v._jwks_keys) > 0
    assert KID in v._jwks_keys
    await v.close()
