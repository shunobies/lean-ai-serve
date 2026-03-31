"""Authentication endpoints — login, refresh, logout, user info."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request

from lean_ai_serve.config import get_settings
from lean_ai_serve.models.schemas import AuthUser, LoginRequest, LoginResponse, UserInfoResponse
from lean_ai_serve.security.auth import (
    authenticate,
    decode_jwt,
    get_db_from_request,
    issue_jwt,
    revoke_token,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login", response_model=LoginResponse)
async def login(body: LoginRequest, request: Request):
    """Authenticate via LDAP and receive a JWT session token."""
    settings = get_settings()

    if "ldap" not in settings.security.mode.lower():
        raise HTTPException(
            status_code=400, detail="LDAP authentication is not enabled"
        )

    ldap_service = getattr(request.app.state, "ldap_service", None)
    if ldap_service is None:
        raise HTTPException(
            status_code=503, detail="LDAP service not initialized"
        )

    user = await ldap_service.authenticate(body.username, body.password)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token, jti, expires_at = issue_jwt(
        user_id=user.user_id,
        display_name=user.display_name,
        roles=user.roles,
        models=user.allowed_models,
    )

    logger.info("Login successful: %s", user.user_id)
    return LoginResponse(
        token=token,
        expires_at=expires_at,
        user=user.user_id,
        roles=user.roles,
    )


@router.post("/refresh", response_model=LoginResponse)
async def refresh(
    request: Request,
    user: AuthUser = Depends(authenticate),
):
    """Refresh a JWT — revokes the old token and issues a new one."""
    if user.auth_method != "ldap":
        raise HTTPException(
            status_code=400,
            detail="Token refresh is only available for LDAP sessions",
        )

    # Extract the old token's jti to revoke it
    from lean_ai_serve.security.auth import _bearer_scheme

    credentials = await _bearer_scheme(request)
    if credentials is None:
        raise HTTPException(status_code=401, detail="No token provided")

    old_payload = decode_jwt(credentials.credentials)
    if old_payload and "jti" in old_payload:
        db = get_db_from_request(request)
        exp = datetime.fromtimestamp(old_payload["exp"], tz=UTC)
        await revoke_token(db, old_payload["jti"], user.user_id, exp.isoformat())

    # Issue new token
    token, jti, expires_at = issue_jwt(
        user_id=user.user_id,
        display_name=user.display_name,
        roles=user.roles,
        models=user.allowed_models,
    )

    logger.info("Token refreshed for: %s", user.user_id)
    return LoginResponse(
        token=token,
        expires_at=expires_at,
        user=user.user_id,
        roles=user.roles,
    )


@router.post("/logout")
async def logout(
    request: Request,
    user: AuthUser = Depends(authenticate),
):
    """Revoke the current JWT session token."""
    if user.auth_method != "ldap":
        raise HTTPException(
            status_code=400,
            detail="Logout is only available for LDAP sessions",
        )

    from lean_ai_serve.security.auth import _bearer_scheme

    credentials = await _bearer_scheme(request)
    if credentials is None:
        raise HTTPException(status_code=401, detail="No token provided")

    payload = decode_jwt(credentials.credentials)
    if payload and "jti" in payload:
        db = get_db_from_request(request)
        exp = datetime.fromtimestamp(payload["exp"], tz=UTC)
        await revoke_token(db, payload["jti"], user.user_id, exp.isoformat())
        logger.info("Logout: revoked token for %s", user.user_id)

    return {"detail": "Logged out successfully"}


@router.get("/me", response_model=UserInfoResponse)
async def me(user: AuthUser = Depends(authenticate)):
    """Return information about the currently authenticated user."""
    return UserInfoResponse(
        user_id=user.user_id,
        display_name=user.display_name,
        roles=user.roles,
        allowed_models=user.allowed_models,
        auth_method=user.auth_method,
    )
