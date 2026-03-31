"""OpenAI-compatible API endpoints — proxied to vLLM backends."""

from __future__ import annotations

import json
import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Request

from lean_ai_serve.engine.proxy import proxy_request
from lean_ai_serve.engine.router import Router
from lean_ai_serve.models.schemas import AuthUser, ModelState
from lean_ai_serve.security.audit import AuditLogger
from lean_ai_serve.security.auth import require_permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["openai-compatible"])


def _get_router(request: Request) -> Router:
    return request.app.state.router


def _get_audit(request: Request) -> AuditLogger:
    return request.app.state.audit


# ---------------------------------------------------------------------------
# POST /v1/chat/completions
# ---------------------------------------------------------------------------


@router.post("/chat/completions")
async def chat_completions(
    request: Request,
    user: AuthUser = Depends(require_permission("inference:call")),
):
    """Proxy chat completions to the appropriate vLLM backend."""
    body = await request.body()
    payload = json.loads(body)
    model_name = payload.get("model", "")

    if not user.can_access_model(model_name):
        raise HTTPException(status_code=403, detail=f"Access denied for model: {model_name}")

    route = _get_router(request)
    port = await route.resolve(model_name)
    if port is None:
        raise HTTPException(status_code=404, detail=f"Model not loaded: {model_name}")

    audit = _get_audit(request)
    start_time = time.monotonic()

    response = await proxy_request(request, port, "/v1/chat/completions")

    # Audit log (non-blocking)
    latency_ms = int((time.monotonic() - start_time) * 1000)
    prompt_text = json.dumps(payload.get("messages", []))
    await audit.log(
        user_id=user.user_id,
        user_role=",".join(user.roles),
        source_ip=request.client.host if request.client else "",
        action="inference",
        model=model_name,
        prompt=prompt_text,
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
    user: AuthUser = Depends(require_permission("inference:call")),
):
    """Proxy text completions (FIM) to vLLM."""
    body = await request.body()
    payload = json.loads(body)
    model_name = payload.get("model", "")

    if not user.can_access_model(model_name):
        raise HTTPException(status_code=403, detail=f"Access denied for model: {model_name}")

    route = _get_router(request)
    port = await route.resolve(model_name)
    if port is None:
        raise HTTPException(status_code=404, detail=f"Model not loaded: {model_name}")

    return await proxy_request(request, port, "/v1/completions")


# ---------------------------------------------------------------------------
# POST /v1/embeddings
# ---------------------------------------------------------------------------


@router.post("/embeddings")
async def embeddings(
    request: Request,
    user: AuthUser = Depends(require_permission("inference:call")),
):
    """Proxy embedding requests to vLLM."""
    body = await request.body()
    payload = json.loads(body)
    model_name = payload.get("model", "")

    if not user.can_access_model(model_name):
        raise HTTPException(status_code=403, detail=f"Access denied for model: {model_name}")

    route = _get_router(request)
    port = await route.resolve(model_name)
    if port is None:
        raise HTTPException(status_code=404, detail=f"Model not loaded: {model_name}")

    return await proxy_request(request, port, "/v1/embeddings")


# ---------------------------------------------------------------------------
# GET /v1/models
# ---------------------------------------------------------------------------


@router.get("/models")
async def list_models(
    request: Request,
    user: AuthUser = Depends(require_permission("inference:call")),
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
