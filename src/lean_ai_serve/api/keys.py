"""API key management endpoints."""

from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request

from lean_ai_serve.db import Database
from lean_ai_serve.models.schemas import APIKeyCreate, APIKeyInfo, AuthUser
from lean_ai_serve.security.auth import create_api_key, require_permission

router = APIRouter(prefix="/api/keys", tags=["keys"])


def _get_db(request: Request) -> Database:
    return request.app.state.db


# ---------------------------------------------------------------------------
# GET /api/keys — list all API keys
# ---------------------------------------------------------------------------


@router.get("", response_model=list[APIKeyInfo])
async def list_keys(
    request: Request,
    user: AuthUser = Depends(require_permission("model:write")),
):
    """List all API keys (admin/model-manager)."""
    db = _get_db(request)
    rows = await db.fetchall("SELECT * FROM api_keys ORDER BY created_at DESC")
    keys = []
    for row in rows:
        keys.append(
            APIKeyInfo(
                id=row["id"],
                name=row["name"],
                role=row["role"],
                models=json.loads(row["models"]),
                rate_limit=row["rate_limit"],
                created_at=datetime.fromisoformat(row["created_at"]),
                expires_at=(
                    datetime.fromisoformat(row["expires_at"])
                    if row["expires_at"]
                    else None
                ),
                last_used_at=(
                    datetime.fromisoformat(row["last_used_at"])
                    if row["last_used_at"]
                    else None
                ),
                prefix=row["key_prefix"],
            )
        )
    return keys


# ---------------------------------------------------------------------------
# POST /api/keys — create new key
# ---------------------------------------------------------------------------


@router.post("")
async def create_key(
    body: APIKeyCreate,
    request: Request,
    user: AuthUser = Depends(require_permission("model:write")),
):
    """Create a new API key. Returns the raw key (shown only once)."""
    db = _get_db(request)
    key_id, raw_key = await create_api_key(
        db,
        name=body.name,
        role=body.role,
        models=body.models,
        rate_limit=body.rate_limit,
        expires_days=body.expires_days,
    )
    return {"id": key_id, "key": raw_key, "name": body.name, "role": body.role}


# ---------------------------------------------------------------------------
# DELETE /api/keys/{key_id} — revoke key
# ---------------------------------------------------------------------------


@router.delete("/{key_id}")
async def revoke_key(
    key_id: str,
    request: Request,
    user: AuthUser = Depends(require_permission("model:write")),
):
    """Revoke (delete) an API key."""
    db = _get_db(request)
    result = await db.execute("DELETE FROM api_keys WHERE id = ?", (key_id,))
    await db.commit()
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail=f"API key not found: {key_id}")
    return {"status": "revoked", "id": key_id}
