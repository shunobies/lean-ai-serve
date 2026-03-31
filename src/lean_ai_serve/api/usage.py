"""Usage tracking API endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from lean_ai_serve.models.schemas import AuthUser
from lean_ai_serve.security.auth import require_permission
from lean_ai_serve.security.usage import UsageTracker

router = APIRouter(prefix="/api/usage", tags=["usage"])


def _get_usage(request: Request) -> UsageTracker:
    return request.app.state.usage_tracker


# ---------------------------------------------------------------------------
# GET /api/usage — query usage records (admin/auditor)
# ---------------------------------------------------------------------------


@router.get("")
async def query_usage(
    request: Request,
    user_id: str | None = None,
    model: str | None = None,
    from_hour: str | None = None,
    to_hour: str | None = None,
    limit: int = 168,
    user: AuthUser = Depends(require_permission("usage:read")),
):
    """Query usage records with optional filters."""
    tracker = _get_usage(request)
    records = await tracker.query(
        user_id=user_id, model=model,
        from_hour=from_hour, to_hour=to_hour, limit=limit,
    )
    return {"records": records, "count": len(records)}


# ---------------------------------------------------------------------------
# GET /api/usage/me — current user's usage summary
# ---------------------------------------------------------------------------


@router.get("/me")
async def my_usage(
    request: Request,
    period_hours: int = 24,
    user: AuthUser = Depends(require_permission("usage:read_own")),
):
    """Get current user's usage summary."""
    tracker = _get_usage(request)
    return await tracker.get_user_summary(user.user_id, period_hours)


# ---------------------------------------------------------------------------
# GET /api/usage/models/{name} — per-model usage summary
# ---------------------------------------------------------------------------


@router.get("/models/{name}")
async def model_usage(
    name: str,
    request: Request,
    period_hours: int = 24,
    user: AuthUser = Depends(require_permission("usage:read")),
):
    """Get usage summary for a specific model."""
    tracker = _get_usage(request)
    return await tracker.get_model_summary(name, period_hours)
