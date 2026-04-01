"""Dashboard page routes — full HTML page handlers."""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from lean_ai_serve.config import get_settings
from lean_ai_serve.dashboard.dependencies import (
    SESSION_COOKIE,
    _LoginRedirectError,
    build_template_context,
    get_templates,
    require_dashboard_user,
)
from lean_ai_serve.security.auth import authenticate_api_key, issue_jwt

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


# ---------------------------------------------------------------------------
# Login / Logout
# ---------------------------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """Render the login page."""
    settings = get_settings()
    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "auth_mode": settings.security.mode,
            "error": request.query_params.get("error", ""),
        },
    )


@router.post("/login")
async def login_submit(request: Request):
    """Handle login form submission."""
    settings = get_settings()
    form = await request.form()
    modes = settings.security.mode.lower().split("+")
    db = request.app.state.db

    # API key auth
    if "api_key" in modes:
        api_key = form.get("api_key", "").strip()
        if api_key:
            user = await authenticate_api_key(db, api_key)
            if user:
                token, _jti, _exp = issue_jwt(
                    user.user_id, user.display_name, user.roles, user.allowed_models
                )
                response = RedirectResponse("/dashboard/", status_code=303)
                response.set_cookie(
                    SESSION_COOKIE,
                    token,
                    httponly=True,
                    samesite="strict",
                    max_age=int(settings.security.jwt_expiry_hours * 3600),
                )
                return response
            return RedirectResponse("/dashboard/login?error=invalid_key", status_code=303)

    # LDAP auth
    if "ldap" in modes:
        username = form.get("username", "").strip()
        password = form.get("password", "")
        if username and password:
            ldap_service = getattr(request.app.state, "ldap_service", None)
            if ldap_service:
                user = await ldap_service.authenticate(username, password)
                if user:
                    token, _jti, _exp = issue_jwt(
                        user.user_id,
                        user.display_name,
                        user.roles,
                        user.allowed_models,
                    )
                    response = RedirectResponse("/dashboard/", status_code=303)
                    response.set_cookie(
                        SESSION_COOKIE,
                        token,
                        httponly=True,
                        samesite="strict",
                        max_age=int(settings.security.jwt_expiry_hours * 3600),
                    )
                    return response
            return RedirectResponse("/dashboard/login?error=invalid_credentials", status_code=303)

    return RedirectResponse("/dashboard/login?error=unsupported_auth", status_code=303)


@router.post("/logout")
async def logout(request: Request):
    """Clear session cookie and redirect to login."""
    response = RedirectResponse("/dashboard/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


# ---------------------------------------------------------------------------
# Dashboard pages
# ---------------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
async def home_page(request: Request):
    """Dashboard home — overview with KPIs, model status, GPU info."""
    try:
        user = await require_dashboard_user(request)
    except _LoginRedirectError:
        return RedirectResponse("/dashboard/login", status_code=303)

    templates = get_templates()

    # Gather data from app state
    registry = request.app.state.registry
    models = await registry.list_models()

    # Metrics summary
    metrics = getattr(request.app.state, "metrics", None)
    metrics_summary = metrics.summary() if metrics else {}

    # Alerts
    alert_evaluator = getattr(request.app.state, "alert_evaluator", None)
    active_alerts = alert_evaluator.active_alerts() if alert_evaluator else []

    # Uptime
    start_time = getattr(request.app.state, "start_time", time.monotonic())
    uptime_seconds = int(time.monotonic() - start_time)

    ctx = build_template_context(
        request,
        user,
        models=models,
        metrics_summary=metrics_summary,
        active_alerts=active_alerts,
        uptime_seconds=uptime_seconds,
    )
    return templates.TemplateResponse(request, "home.html", ctx)


@router.get("/models", response_class=HTMLResponse)
async def models_page(request: Request):
    """Model management page."""
    try:
        user = await require_dashboard_user(request)
    except _LoginRedirectError:
        return RedirectResponse("/dashboard/login", status_code=303)

    templates = get_templates()
    registry = request.app.state.registry
    models = await registry.list_models()

    ctx = build_template_context(request, user, models=models)
    return templates.TemplateResponse(request, "models.html", ctx)


@router.get("/monitoring", response_class=HTMLResponse)
async def monitoring_page(request: Request):
    """Monitoring page — metrics charts and alerts."""
    try:
        user = await require_dashboard_user(request)
    except _LoginRedirectError:
        return RedirectResponse("/dashboard/login", status_code=303)

    templates = get_templates()
    metrics = getattr(request.app.state, "metrics", None)
    metrics_summary = metrics.summary() if metrics else {}

    alert_evaluator = getattr(request.app.state, "alert_evaluator", None)
    active_alerts = alert_evaluator.active_alerts() if alert_evaluator else []

    ctx = build_template_context(
        request,
        user,
        metrics_summary=metrics_summary,
        active_alerts=active_alerts,
    )
    return templates.TemplateResponse(request, "monitoring.html", ctx)


@router.get("/security", response_class=HTMLResponse)
async def security_page(request: Request):
    """Security page — API keys and audit logs."""
    try:
        user = await require_dashboard_user(request)
    except _LoginRedirectError:
        return RedirectResponse("/dashboard/login", status_code=303)

    templates = get_templates()
    db = request.app.state.db

    # Load API keys
    keys = await db.fetchall(
        "SELECT id, name, key_prefix, role, models, rate_limit, "
        "created_at, last_used_at, expires_at FROM api_keys ORDER BY created_at DESC"
    )

    # Load recent audit entries
    audit_entries = await db.fetchall(
        "SELECT * FROM audit_log ORDER BY timestamp DESC LIMIT 50"
    )

    ctx = build_template_context(
        request,
        user,
        api_keys=keys,
        audit_entries=audit_entries,
    )
    return templates.TemplateResponse(request, "security.html", ctx)


@router.get("/training", response_class=HTMLResponse)
async def training_page(request: Request):
    """Training page — jobs, datasets, adapters."""
    try:
        user = await require_dashboard_user(request)
    except _LoginRedirectError:
        return RedirectResponse("/dashboard/login", status_code=303)

    settings = get_settings()
    if not settings.training.enabled:
        return RedirectResponse("/dashboard/", status_code=303)

    templates = get_templates()

    # Training jobs
    orchestrator = getattr(request.app.state, "training_orchestrator", None)
    jobs = []
    if orchestrator:
        jobs = await orchestrator.list_jobs()

    # Datasets
    dataset_manager = getattr(request.app.state, "dataset_manager", None)
    datasets = []
    if dataset_manager:
        datasets = await dataset_manager.list_datasets()

    # Adapters
    adapter_registry = getattr(request.app.state, "adapter_registry", None)
    adapters = []
    if adapter_registry:
        adapters = await adapter_registry.list_adapters()

    # Models for the submit form dropdown
    registry = request.app.state.registry
    models = await registry.list_models()

    ctx = build_template_context(
        request,
        user,
        jobs=jobs,
        datasets=datasets,
        adapters=adapters,
        models=models,
    )
    return templates.TemplateResponse(request, "training.html", ctx)


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    """Settings page — read-only config view."""
    try:
        user = await require_dashboard_user(request)
    except _LoginRedirectError:
        return RedirectResponse("/dashboard/login", status_code=303)

    templates = get_templates()

    start_time = getattr(request.app.state, "start_time", time.monotonic())
    uptime_seconds = int(time.monotonic() - start_time)

    ctx = build_template_context(
        request,
        user,
        uptime_seconds=uptime_seconds,
    )
    return templates.TemplateResponse(request, "settings.html", ctx)
