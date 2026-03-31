"""OpenAI-compatible API endpoints — proxied to vLLM backends."""

from __future__ import annotations

import asyncio
import json
import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Request

from lean_ai_serve.engine.proxy import proxy_request
from lean_ai_serve.engine.router import Router
from lean_ai_serve.models.schemas import AuthUser, ModelState
from lean_ai_serve.security.audit import AuditLogger
from lean_ai_serve.security.rate_limiter import check_rate_limit

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["openai-compatible"])


def _get_router(request: Request) -> Router:
    return request.app.state.router


def _get_audit(request: Request) -> AuditLogger:
    return request.app.state.audit


async def _resolve_or_wake(request: Request, model_name: str) -> int:
    """Resolve a model port, triggering auto-wake for sleeping models.

    Returns the port number.  Raises HTTPException if the model cannot serve.
    """
    route = _get_router(request)
    port = await route.resolve(model_name)
    if port is not None:
        return port

    # Check if model is sleeping and can auto-wake
    from lean_ai_serve.config import get_settings
    from lean_ai_serve.models.registry import ModelRegistry

    registry: ModelRegistry = request.app.state.registry
    model = await registry.get_model(model_name)
    if model and model.state == ModelState.SLEEPING:
        settings = get_settings()
        model_cfg = settings.models.get(model_name)
        if model_cfg and model_cfg.lifecycle.auto_wake_on_request:
            lifecycle = getattr(request.app.state, "lifecycle_manager", None)
            if lifecycle:
                asyncio.create_task(lifecycle.wake_model(model_name))
            raise HTTPException(
                status_code=503,
                detail=f"Model '{model_name}' is waking up, retry shortly",
                headers={"Retry-After": "30"},
            )

    raise HTTPException(status_code=404, detail=f"Model not loaded: {model_name}")


def _touch_request_tracker(request: Request, model_name: str) -> None:
    """Record that a request was made to this model (for idle detection)."""
    tracker = getattr(request.app.state, "request_tracker", None)
    if tracker:
        tracker.touch(model_name)


def _make_usage_callback(request: Request, user_id: str, model_name: str):
    """Create a usage callback and a mutable container for captured data."""
    usage_data: dict = {}

    def on_usage(usage: dict) -> None:
        usage_data.update(usage)

    return on_usage, usage_data


async def _record_usage(
    request: Request, user_id: str, model_name: str,
    usage_data: dict, latency_ms: int,
) -> None:
    """Record usage to the usage tracker if available."""
    usage_tracker = getattr(request.app.state, "usage_tracker", None)
    if usage_tracker and usage_data:
        await usage_tracker.record(
            user_id=user_id,
            model=model_name,
            prompt_tokens=usage_data.get("prompt_tokens", 0),
            completion_tokens=usage_data.get("completion_tokens", 0),
            latency_ms=latency_ms,
        )


# ---------------------------------------------------------------------------
# POST /v1/chat/completions
# ---------------------------------------------------------------------------


@router.post("/chat/completions")
async def chat_completions(
    request: Request,
    user: AuthUser = Depends(check_rate_limit),
):
    """Proxy chat completions to the appropriate vLLM backend."""
    body = await request.body()
    payload = json.loads(body)
    model_name = payload.get("model", "")

    if not user.can_access_model(model_name):
        raise HTTPException(status_code=403, detail=f"Access denied for model: {model_name}")

    port = await _resolve_or_wake(request, model_name)
    _touch_request_tracker(request, model_name)

    audit = _get_audit(request)
    start_time = time.monotonic()
    on_usage, usage_data = _make_usage_callback(request, user.user_id, model_name)

    response = await proxy_request(
        request, port, "/v1/chat/completions", on_usage=on_usage
    )

    latency_ms = int((time.monotonic() - start_time) * 1000)

    # Record usage
    await _record_usage(request, user.user_id, model_name, usage_data, latency_ms)

    # Audit log
    prompt_text = json.dumps(payload.get("messages", []))
    await audit.log(
        user_id=user.user_id,
        user_role=",".join(user.roles),
        source_ip=request.client.host if request.client else "",
        action="inference",
        model=model_name,
        prompt=prompt_text,
        token_count=usage_data.get("total_tokens", 0),
        latency_ms=latency_ms,
        status="success",
    )

    return response


# ---------------------------------------------------------------------------
# POST /v1/completions
# ---------------------------------------------------------------------------


@router.post("/completions")
async def completions(
    request: Request,
    user: AuthUser = Depends(check_rate_limit),
):
    """Proxy text completions (FIM) to vLLM."""
    body = await request.body()
    payload = json.loads(body)
    model_name = payload.get("model", "")

    if not user.can_access_model(model_name):
        raise HTTPException(status_code=403, detail=f"Access denied for model: {model_name}")

    port = await _resolve_or_wake(request, model_name)
    _touch_request_tracker(request, model_name)

    start_time = time.monotonic()
    on_usage, usage_data = _make_usage_callback(request, user.user_id, model_name)

    response = await proxy_request(
        request, port, "/v1/completions", on_usage=on_usage
    )

    latency_ms = int((time.monotonic() - start_time) * 1000)
    await _record_usage(request, user.user_id, model_name, usage_data, latency_ms)

    return response


# ---------------------------------------------------------------------------
# POST /v1/embeddings
# ---------------------------------------------------------------------------


@router.post("/embeddings")
async def embeddings(
    request: Request,
    user: AuthUser = Depends(check_rate_limit),
):
    """Proxy embedding requests to vLLM."""
    body = await request.body()
    payload = json.loads(body)
    model_name = payload.get("model", "")

    if not user.can_access_model(model_name):
        raise HTTPException(status_code=403, detail=f"Access denied for model: {model_name}")

    port = await _resolve_or_wake(request, model_name)
    _touch_request_tracker(request, model_name)

    start_time = time.monotonic()
    on_usage, usage_data = _make_usage_callback(request, user.user_id, model_name)

    response = await proxy_request(
        request, port, "/v1/embeddings", on_usage=on_usage
    )

    latency_ms = int((time.monotonic() - start_time) * 1000)
    await _record_usage(request, user.user_id, model_name, usage_data, latency_ms)

    return response


# ---------------------------------------------------------------------------
# GET /v1/models
# ---------------------------------------------------------------------------


@router.get("/models")
async def list_models(
    request: Request,
    user: AuthUser = Depends(check_rate_limit),
):
    """List models available to the authenticated user (OpenAI format)."""
    from lean_ai_serve.models.registry import ModelRegistry

    registry: ModelRegistry = request.app.state.registry
    models = await registry.list_models()

    data = []
    for m in models:
        if m.state == ModelState.LOADED and user.can_access_model(m.name):
            data.append(
                {
                    "id": m.name,
                    "object": "model",
                    "created": int(m.loaded_at.timestamp()) if m.loaded_at else 0,
                    "owned_by": "lean-ai-serve",
                }
            )

    return {"object": "list", "data": data}
