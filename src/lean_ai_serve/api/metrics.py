"""Metrics API endpoints — Prometheus scrape + JSON summary + alerts."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse

from lean_ai_serve.models.schemas import AuthUser
from lean_ai_serve.security.auth import require_permission

router = APIRouter(tags=["metrics"])


def _get_metrics(request: Request):
    return getattr(request.app.state, "metrics", None)


def _get_alert_evaluator(request: Request):
    return getattr(request.app.state, "alert_evaluator", None)


# ---------------------------------------------------------------------------
# GET /metrics — Prometheus scrape endpoint (public, no auth)
# ---------------------------------------------------------------------------


@router.get("/metrics", response_class=PlainTextResponse)
async def prometheus_metrics(request: Request):
    """Expose all metrics in Prometheus text exposition format."""
    metrics = _get_metrics(request)
    if metrics is None:
        return PlainTextResponse("# Metrics not enabled\n", status_code=200)

    text = metrics.expose()

    # Append alert metrics if available
    evaluator = _get_alert_evaluator(request)
    if evaluator is not None:
        text += "\n" + evaluator.expose_alerts() + "\n"

    return PlainTextResponse(text, media_type="text/plain; version=0.0.4; charset=utf-8")


# ---------------------------------------------------------------------------
# GET /api/metrics/summary — JSON summary (requires metrics:read)
# ---------------------------------------------------------------------------


@router.get("/api/metrics/summary")
async def metrics_summary(
    request: Request,
    user: AuthUser = Depends(require_permission("metrics:read")),
):
    """Return a JSON-friendly summary of key metrics."""
    metrics = _get_metrics(request)
    if metrics is None:
        return {"error": "Metrics not enabled"}
    return metrics.summary()


# ---------------------------------------------------------------------------
# GET /api/metrics/alerts — Active alerts (requires metrics:read)
# ---------------------------------------------------------------------------


@router.get("/api/metrics/alerts")
async def active_alerts(
    request: Request,
    user: AuthUser = Depends(require_permission("metrics:read")),
):
    """Return currently firing alerts."""
    evaluator = _get_alert_evaluator(request)
    if evaluator is None:
        return {"alerts": [], "message": "Alert evaluator not enabled"}
    alerts = evaluator.get_active_alerts()
    return {
        "alerts": [a.model_dump() for a in alerts],
        "count": len(alerts),
    }
