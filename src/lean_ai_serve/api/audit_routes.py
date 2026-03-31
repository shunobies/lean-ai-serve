"""Audit log query endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from lean_ai_serve.models.schemas import (
    AuditQueryParams,
    AuditResponse,
    AuthUser,
)
from lean_ai_serve.security.audit import AuditLogger
from lean_ai_serve.security.auth import require_permission

router = APIRouter(prefix="/api/audit", tags=["audit"])


def _get_audit(request: Request) -> AuditLogger:
    return request.app.state.audit


# ---------------------------------------------------------------------------
# GET /api/audit/logs
# ---------------------------------------------------------------------------


@router.get("/logs", response_model=AuditResponse)
async def query_logs(
    request: Request,
    params: AuditQueryParams = Depends(),
    user: AuthUser = Depends(require_permission("audit:read")),
):
    """Query audit log entries with filters."""
    audit = _get_audit(request)
    entries, total = await audit.query(
        user_id=params.user_id,
        action=params.action,
        model=params.model,
        status=params.status,
        from_time=params.from_time,
        to_time=params.to_time,
        limit=params.limit,
        offset=params.offset,
    )
    return AuditResponse(entries=entries, total=total)


# ---------------------------------------------------------------------------
# GET /api/audit/verify — verify hash chain
# ---------------------------------------------------------------------------


@router.get("/verify")
async def verify_chain(
    request: Request,
    limit: int = 1000,
    user: AuthUser = Depends(require_permission("audit:read")),
):
    """Verify audit log hash chain integrity."""
    audit = _get_audit(request)
    is_valid, message = await audit.verify_chain(limit=limit)
    return {"valid": is_valid, "message": message}
