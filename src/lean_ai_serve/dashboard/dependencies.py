"""Dashboard FastAPI dependencies — session auth, CSRF, template helpers."""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets

from fastapi import Request
from fastapi.templating import Jinja2Templates

from lean_ai_serve.config import get_settings
from lean_ai_serve.dashboard import get_templates_dir
from lean_ai_serve.models.schemas import AuthUser
from lean_ai_serve.security.auth import _revoked_tokens, decode_jwt
from lean_ai_serve.security.rbac import get_permissions

logger = logging.getLogger(__name__)

SESSION_COOKIE = "las_session"

# Lazily initialised templates singleton
_templates: Jinja2Templates | None = None

# Session secret for CSRF (auto-generated if not configured)
_csrf_secret: str | None = None


def get_templates() -> Jinja2Templates:
    """Return the shared Jinja2Templates instance."""
    global _templates
    if _templates is None:
        _templates = Jinja2Templates(directory=str(get_templates_dir()))
    return _templates


def _get_csrf_secret() -> str:
    """Return the CSRF HMAC secret, generating one if needed."""
    global _csrf_secret
    if _csrf_secret is None:
        settings = get_settings()
        secret = settings.dashboard.session_secret
        if not secret:
            secret = secrets.token_urlsafe(48)
            logger.debug("Generated ephemeral dashboard session secret")
        _csrf_secret = secret
    return _csrf_secret


def generate_csrf_token(jti: str) -> str:
    """Derive a CSRF token from the JWT's jti claim."""
    return hmac.new(
        _get_csrf_secret().encode(),
        jti.encode(),
        hashlib.sha256,
    ).hexdigest()[:48]


def verify_csrf_token(token: str, jti: str) -> bool:
    """Verify a CSRF token against the expected value."""
    expected = generate_csrf_token(jti)
    return hmac.compare_digest(token, expected)


async def get_dashboard_user(request: Request) -> AuthUser | None:
    """Read session cookie and return AuthUser, or None if unauthenticated."""
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    payload = decode_jwt(token)
    if not payload:
        return None
    jti = payload.get("jti")
    if jti and jti in _revoked_tokens:
        return None
    return AuthUser(
        user_id=payload["sub"],
        display_name=payload.get("name", payload["sub"]),
        roles=payload.get("roles", ["user"]),
        allowed_models=payload.get("models", ["*"]),
        auth_method=payload.get("auth_method", "session"),
    )


async def require_dashboard_user(request: Request) -> AuthUser:
    """Return authenticated user or redirect to login."""
    user = await get_dashboard_user(request)
    if user is None:
        raise _LoginRedirectError()
    return user


class _LoginRedirectError(Exception):
    """Raised to trigger a redirect to the login page."""


def build_template_context(
    request: Request,
    user: AuthUser,
    **extra: object,
) -> dict:
    """Build the standard Jinja2 template context dict."""
    settings = get_settings()
    permissions = get_permissions(user.roles)
    jti = ""
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        payload = decode_jwt(token)
        if payload:
            jti = payload.get("jti", "")
    return {
        "user": user,
        "user_permissions": permissions,
        "csrf_token": generate_csrf_token(jti) if jti else "",
        "settings": settings,
        "training_enabled": settings.training.enabled,
        **extra,
    }
