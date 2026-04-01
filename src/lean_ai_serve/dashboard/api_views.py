"""HTMX partial-response endpoints — return HTML fragments for dynamic updates."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from lean_ai_serve.dashboard.dependencies import (
    SESSION_COOKIE,
    _LoginRedirectError,
    build_template_context,
    get_templates,
    require_dashboard_user,
    verify_csrf_token,
)
from lean_ai_serve.models.schemas import AuthUser, ModelState
from lean_ai_serve.security.auth import create_api_key, decode_jwt

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dashboard/api", tags=["dashboard-api"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _check_csrf(request: Request) -> bool:
    """Verify CSRF token from X-CSRF-Token header."""
    token_header = request.headers.get("X-CSRF-Token", "")
    cookie = request.cookies.get(SESSION_COOKIE, "")
    if not cookie:
        return False
    payload = decode_jwt(cookie)
    if not payload:
        return False
    jti = payload.get("jti", "")
    return verify_csrf_token(token_header, jti)


async def _require_user_and_csrf(request: Request) -> AuthUser:
    """Authenticate and verify CSRF for state-changing requests."""
    user = await require_dashboard_user(request)
    if request.method in ("POST", "PUT", "DELETE", "PATCH") and not _check_csrf(request):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")
    return user


# ---------------------------------------------------------------------------
# Model partials
# ---------------------------------------------------------------------------


@router.get("/partials/model-list", response_class=HTMLResponse)
async def partial_model_list(request: Request):
    """Return the model list HTML fragment for auto-refresh."""
    try:
        user = await require_dashboard_user(request)
    except _LoginRedirectError:
        return HTMLResponse("", status_code=401)

    templates = get_templates()
    registry = request.app.state.registry
    models = await registry.list_models()
    ctx = build_template_context(request, user, models=models)
    return templates.TemplateResponse(request, "models/_list.html", ctx)


@router.post("/models/{name}/load", response_class=HTMLResponse)
async def load_model(name: str, request: Request):
    """Load a model and return updated card."""
    try:
        user = await _require_user_and_csrf(request)
    except _LoginRedirectError:
        return HTMLResponse("", status_code=401)

    registry = request.app.state.registry
    pm = request.app.state.process_manager
    downloader = request.app.state.downloader
    settings = request.app.state.registry._db  # get settings from config
    from lean_ai_serve.config import get_settings
    settings = get_settings()

    model = await registry.get_model(name)
    if not model:
        return HTMLResponse("<div class='notice'>Model not found</div>", status_code=404)

    config = settings.models.get(name)
    if not config:
        return HTMLResponse("<div class='notice'>Model not configured</div>", status_code=404)

    try:
        await registry.set_state(name, ModelState.LOADING)
        model_path = downloader.get_local_path(config.source)
        if model_path:
            info = await pm.start(name, config, model_path)
            await registry.set_state(name, ModelState.LOADED, port=info.port, pid=info.pid)
    except Exception as e:
        logger.exception("Failed to load model '%s'", name)
        await registry.set_state(name, ModelState.ERROR, error_message=str(e))

    model = await registry.get_model(name)
    templates = get_templates()
    ctx = build_template_context(request, user, model=model, model_name=name)
    return templates.TemplateResponse(request, "models/_card.html", ctx)


@router.post("/models/{name}/unload", response_class=HTMLResponse)
async def unload_model(name: str, request: Request):
    """Unload a model and return updated card."""
    try:
        user = await _require_user_and_csrf(request)
    except _LoginRedirectError:
        return HTMLResponse("", status_code=401)

    registry = request.app.state.registry
    pm = request.app.state.process_manager

    try:
        await pm.stop(name)
        await registry.set_state(name, ModelState.DOWNLOADED)
    except Exception as e:
        logger.exception("Failed to unload model '%s'", name)
        await registry.set_state(name, ModelState.ERROR, error_message=str(e))

    model = await registry.get_model(name)
    templates = get_templates()
    ctx = build_template_context(request, user, model=model, model_name=name)
    return templates.TemplateResponse(request, "models/_card.html", ctx)


@router.post("/models/{name}/sleep", response_class=HTMLResponse)
async def sleep_model(name: str, request: Request):
    """Sleep a model and return updated card."""
    try:
        user = await _require_user_and_csrf(request)
    except _LoginRedirectError:
        return HTMLResponse("", status_code=401)

    registry = request.app.state.registry
    pm = request.app.state.process_manager

    try:
        await pm.stop(name)
        await registry.set_state(name, ModelState.SLEEPING)
    except Exception:
        logger.exception("Failed to sleep model '%s'", name)

    model = await registry.get_model(name)
    templates = get_templates()
    ctx = build_template_context(request, user, model=model, model_name=name)
    return templates.TemplateResponse(request, "models/_card.html", ctx)


@router.post("/models/{name}/wake", response_class=HTMLResponse)
async def wake_model(name: str, request: Request):
    """Wake a sleeping model and return updated card."""
    try:
        user = await _require_user_and_csrf(request)
    except _LoginRedirectError:
        return HTMLResponse("", status_code=401)

    registry = request.app.state.registry
    pm = request.app.state.process_manager
    downloader = request.app.state.downloader
    from lean_ai_serve.config import get_settings
    settings = get_settings()

    config = settings.models.get(name)
    if not config:
        return HTMLResponse("<div class='notice'>Model not configured</div>", status_code=404)

    try:
        await registry.set_state(name, ModelState.LOADING)
        model_path = downloader.get_local_path(config.source)
        if model_path:
            info = await pm.start(name, config, model_path)
            await registry.set_state(name, ModelState.LOADED, port=info.port, pid=info.pid)
    except Exception as e:
        logger.exception("Failed to wake model '%s'", name)
        await registry.set_state(name, ModelState.ERROR, error_message=str(e))

    model = await registry.get_model(name)
    templates = get_templates()
    ctx = build_template_context(request, user, model=model, model_name=name)
    return templates.TemplateResponse(request, "models/_card.html", ctx)


# ---------------------------------------------------------------------------
# Metrics partials
# ---------------------------------------------------------------------------


@router.get("/partials/metrics", response_class=HTMLResponse)
async def partial_metrics(request: Request):
    """Return updated metrics charts data."""
    try:
        user = await require_dashboard_user(request)
    except _LoginRedirectError:
        return HTMLResponse("", status_code=401)

    metrics = getattr(request.app.state, "metrics", None)
    metrics_summary = metrics.summary() if metrics else {}

    templates = get_templates()
    ctx = build_template_context(request, user, metrics_summary=metrics_summary)
    return templates.TemplateResponse(request, "monitoring/_charts.html", ctx)


@router.get("/partials/alerts", response_class=HTMLResponse)
async def partial_alerts(request: Request):
    """Return updated alerts list."""
    try:
        user = await require_dashboard_user(request)
    except _LoginRedirectError:
        return HTMLResponse("", status_code=401)

    alert_evaluator = getattr(request.app.state, "alert_evaluator", None)
    active_alerts = alert_evaluator.active_alerts() if alert_evaluator else []

    templates = get_templates()
    ctx = build_template_context(request, user, active_alerts=active_alerts)
    return templates.TemplateResponse(request, "monitoring/_alerts.html", ctx)


# ---------------------------------------------------------------------------
# API Key partials
# ---------------------------------------------------------------------------


@router.post("/keys/create", response_class=HTMLResponse)
async def create_key(request: Request):
    """Create a new API key and return the key display + updated table."""
    try:
        user = await _require_user_and_csrf(request)
    except _LoginRedirectError:
        return HTMLResponse("", status_code=401)

    form = await request.form()
    db = request.app.state.db

    name = form.get("name", "").strip()
    role = form.get("role", "user")
    models_str = form.get("models", "*").strip()
    rate_limit = int(form.get("rate_limit", 0))
    expires_days = form.get("expires_days", "")
    expires_days = int(expires_days) if expires_days else None

    models_list = [m.strip() for m in models_str.split(",") if m.strip()]

    key_id, raw_key = await create_api_key(
        db,
        name=name,
        role=role,
        models=models_list,
        rate_limit=rate_limit,
        expires_days=expires_days,
    )

    # Return both the new key display and the updated keys table
    keys = await db.fetchall(
        "SELECT id, name, key_prefix, role, models, rate_limit, "
        "created_at, last_used_at, expires_at FROM api_keys ORDER BY created_at DESC"
    )

    templates = get_templates()
    ctx = build_template_context(
        request, user, api_keys=keys, new_key=raw_key, new_key_name=name
    )
    return templates.TemplateResponse(request, "security/_keys_table.html", ctx)


@router.delete("/keys/{key_id}", response_class=HTMLResponse)
async def revoke_key(key_id: str, request: Request):
    """Revoke an API key."""
    try:
        user = await _require_user_and_csrf(request)
    except _LoginRedirectError:
        return HTMLResponse("", status_code=401)

    db = request.app.state.db
    await db.execute("DELETE FROM api_keys WHERE id = ?", (key_id,))
    await db.commit()

    # Return updated keys table
    keys = await db.fetchall(
        "SELECT id, name, key_prefix, role, models, rate_limit, "
        "created_at, last_used_at, expires_at FROM api_keys ORDER BY created_at DESC"
    )

    templates = get_templates()
    ctx = build_template_context(request, user, api_keys=keys)
    return templates.TemplateResponse(request, "security/_keys_table.html", ctx)


# ---------------------------------------------------------------------------
# Audit partials
# ---------------------------------------------------------------------------


@router.get("/partials/audit", response_class=HTMLResponse)
async def partial_audit(request: Request):
    """Return filtered audit log entries."""
    try:
        user = await require_dashboard_user(request)
    except _LoginRedirectError:
        return HTMLResponse("", status_code=401)

    db = request.app.state.db
    params = request.query_params

    # Build query with optional filters
    conditions = []
    values: list = []

    if params.get("user_id"):
        conditions.append("user_id = ?")
        values.append(params["user_id"])
    if params.get("action"):
        conditions.append("action = ?")
        values.append(params["action"])
    if params.get("model"):
        conditions.append("model = ?")
        values.append(params["model"])

    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
    offset = int(params.get("offset", 0))
    limit = int(params.get("limit", 50))

    query = f"SELECT * FROM audit_log{where} ORDER BY timestamp DESC LIMIT ? OFFSET ?"
    values.extend([limit, offset])

    audit_entries = await db.fetchall(query, tuple(values))

    templates = get_templates()
    ctx = build_template_context(
        request, user, audit_entries=audit_entries, offset=offset, limit=limit
    )
    return templates.TemplateResponse(request, "security/_audit_table.html", ctx)


# ---------------------------------------------------------------------------
# Training partials
# ---------------------------------------------------------------------------


@router.post("/training/jobs", response_class=HTMLResponse)
async def submit_training_job(request: Request):
    """Submit a new training job."""
    try:
        user = await _require_user_and_csrf(request)
    except _LoginRedirectError:
        return HTMLResponse("", status_code=401)

    form = await request.form()
    orchestrator = getattr(request.app.state, "training_orchestrator", None)
    if not orchestrator:
        return HTMLResponse("<div class='notice'>Training not enabled</div>", status_code=503)

    try:
        await orchestrator.submit(
            name=form.get("name", "").strip(),
            base_model=form.get("base_model", "").strip(),
            dataset=form.get("dataset", "").strip(),
            user_id=user.user_id,
            epochs=int(form.get("epochs", 3)),
            learning_rate=float(form.get("learning_rate", 2e-4)),
            batch_size=int(form.get("batch_size", 4)),
            lora_rank=int(form.get("lora_rank", 16)),
        )
    except Exception as e:
        return HTMLResponse(
            f"<div class='notice' role='alert'>Error: {e}</div>", status_code=400
        )

    # Return updated job list
    jobs = await orchestrator.list_jobs()
    templates = get_templates()
    ctx = build_template_context(request, user, jobs=jobs)
    return templates.TemplateResponse(request, "training/_job_card.html", ctx)


@router.post("/training/jobs/{job_id}/cancel", response_class=HTMLResponse)
async def cancel_training_job(job_id: str, request: Request):
    """Cancel a training job."""
    try:
        user = await _require_user_and_csrf(request)
    except _LoginRedirectError:
        return HTMLResponse("", status_code=401)

    orchestrator = getattr(request.app.state, "training_orchestrator", None)
    if orchestrator:
        await orchestrator.cancel(job_id)

    jobs = await orchestrator.list_jobs() if orchestrator else []
    templates = get_templates()
    ctx = build_template_context(request, user, jobs=jobs)
    return templates.TemplateResponse(request, "training/_job_card.html", ctx)
