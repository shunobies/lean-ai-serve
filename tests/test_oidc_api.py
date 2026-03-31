"""End-to-end tests for OIDC authentication via FastAPI."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock

import jwt as pyjwt
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from lean_ai_serve.config import OIDCConfig, Settings, set_settings
from lean_ai_serve.db import Database
from lean_ai_serve.models.schemas import AuthUser
from lean_ai_serve.security.oidc import OIDCValidator

# ---------------------------------------------------------------------------
# RSA keypair helpers
# ---------------------------------------------------------------------------


def _generate_keypair():
    """Generate an RSA private key and PEM-encoded public key."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key


def _make_token(private_key, kid: str, payload: dict) -> str:
    """Encode a JWT with RS256 using the given private key."""
    headers = {"kid": kid, "alg": "RS256"}
    return pyjwt.encode(payload, private_key, algorithm="RS256", headers=headers)


def _base_payload(
    sub: str = "oidc-user",
    name: str = "OIDC User",
    iss: str = "https://idp.example.com",
    aud: str = "lean-ai-serve",
    roles: list[str] | None = None,
) -> dict:
    """Create a base JWT payload with sensible defaults."""
    now = int(time.time())
    return {
        "sub": sub,
        "name": name,
        "iss": iss,
        "aud": aud,
        "iat": now,
        "exp": now + 3600,
        "realm_access": {"roles": roles or ["user"]},
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def rsa_key():
    return _generate_keypair()


@pytest_asyncio.fixture
async def db(tmp_path) -> Database:
    db = Database(tmp_path / "test.db")
    await db.connect()
    yield db
    await db.close()


@pytest.fixture
def oidc_config() -> OIDCConfig:
    return OIDCConfig(
        issuer_url="https://idp.example.com",
        audience="lean-ai-serve",
        roles_claim="realm_access.roles",
        role_mapping={"idp-admin": "admin"},
        default_role="user",
    )


@pytest_asyncio.fixture
async def oidc_validator(oidc_config, rsa_key) -> OIDCValidator:
    """Create an OIDCValidator with pre-loaded JWKS (no HTTP needed)."""
    validator = OIDCValidator(oidc_config)
    # Manually set up internal state instead of calling initialize()
    validator._http = AsyncMock()
    validator._jwks_uri = "https://idp.example.com/.well-known/jwks.json"
    validator._jwks_fetched_at = time.monotonic()

    # Build JWK from RSA public key
    from jwt.algorithms import RSAAlgorithm

    pub_key = rsa_key.public_key()
    jwk_dict = RSAAlgorithm.to_jwk(pub_key, as_dict=True)
    jwk_dict["kid"] = "test-kid-1"
    jwk_dict["use"] = "sig"
    jwk_dict["alg"] = "RS256"

    validator._jwks_keys = {"test-kid-1": jwk_dict}

    yield validator
    await validator.close()


@pytest_asyncio.fixture
async def app(db, oidc_validator, tmp_path) -> FastAPI:
    """Create a test FastAPI app with OIDC auth mode and a protected endpoint."""
    settings = Settings(
        cache={"directory": str(tmp_path / "cache")},
        security={"mode": "oidc"},
    )
    set_settings(settings)

    test_app = FastAPI()

    # Add a simple protected endpoint
    from lean_ai_serve.security.auth import authenticate as real_authenticate

    @test_app.get("/api/whoami")
    async def whoami(user: AuthUser = pytest.importorskip("fastapi").Depends(real_authenticate)):
        return {"user_id": user.user_id, "roles": user.roles, "auth_method": user.auth_method}

    # Inject app state
    test_app.state.db = db
    test_app.state.oidc_validator = oidc_validator

    return test_app


@pytest_asyncio.fixture
async def client(app) -> AsyncClient:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_oidc_valid_token(client, rsa_key):
    """A valid OIDC token should authenticate successfully."""
    payload = _base_payload(roles=["user"])
    token = _make_token(rsa_key, "test-kid-1", payload)

    resp = await client.get(
        "/api/whoami",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["user_id"] == "oidc-user"
    assert "user" in body["roles"]
    assert body["auth_method"] == "oidc"


@pytest.mark.asyncio
async def test_oidc_expired_token(client, rsa_key):
    """An expired OIDC token should return 401."""
    payload = _base_payload()
    payload["exp"] = int(time.time()) - 3600  # Expired 1 hour ago

    token = _make_token(rsa_key, "test-kid-1", payload)

    resp = await client.get(
        "/api/whoami",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_oidc_wrong_audience(client, rsa_key):
    """A token with the wrong audience should return 401."""
    payload = _base_payload(aud="wrong-audience")
    token = _make_token(rsa_key, "test-kid-1", payload)

    resp = await client.get(
        "/api/whoami",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_oidc_no_token(client):
    """Request without token should return 401."""
    resp = await client.get("/api/whoami")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_oidc_role_mapping(client, rsa_key):
    """OIDC roles should be mapped via role_mapping config."""
    payload = _base_payload(roles=["idp-admin"])
    token = _make_token(rsa_key, "test-kid-1", payload)

    resp = await client.get(
        "/api/whoami",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "admin" in body["roles"]  # idp-admin -> admin


@pytest.mark.asyncio
async def test_oidc_unknown_kid(client, rsa_key):
    """Token with unknown kid should return 401."""
    payload = _base_payload()
    token = _make_token(rsa_key, "unknown-kid", payload)

    resp = await client.get(
        "/api/whoami",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_oidc_bad_signature(client):
    """Token signed with a different key should return 401."""
    other_key = _generate_keypair()
    payload = _base_payload()
    token = _make_token(other_key, "test-kid-1", payload)

    resp = await client.get(
        "/api/whoami",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_combined_oidc_and_api_key(db, oidc_validator, rsa_key, tmp_path):
    """In oidc+api_key mode, both auth methods should work."""
    from lean_ai_serve.security.auth import create_api_key

    settings = Settings(
        cache={"directory": str(tmp_path / "cache")},
        security={"mode": "oidc+api_key"},
    )
    set_settings(settings)

    test_app = FastAPI()

    from lean_ai_serve.security.auth import authenticate as real_authenticate

    @test_app.get("/api/whoami")
    async def whoami(user: AuthUser = pytest.importorskip("fastapi").Depends(real_authenticate)):
        return {"user_id": user.user_id, "auth_method": user.auth_method}

    test_app.state.db = db
    test_app.state.oidc_validator = oidc_validator

    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Test OIDC auth
        payload = _base_payload()
        token = _make_token(rsa_key, "test-kid-1", payload)
        resp = await client.get(
            "/api/whoami",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert resp.json()["auth_method"] == "oidc"

        # Test API key auth
        key_id, raw_key = await create_api_key(db, name="test-key", role="user")
        resp = await client.get(
            "/api/whoami",
            headers={"Authorization": f"Bearer {raw_key}"},
        )
        assert resp.status_code == 200
        assert resp.json()["auth_method"] == "api_key"


@pytest.fixture(autouse=True)
def _clear_settings():
    yield
    set_settings(None)
