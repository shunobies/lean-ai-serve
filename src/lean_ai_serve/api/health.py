"""Health and status endpoints."""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Request

from lean_ai_serve.models.registry import ModelRegistry
from lean_ai_serve.models.schemas import (
    AuthUser,
    HealthResponse,
    ModelState,
    StatusResponse,
)
from lean_ai_serve.security.auth import require_permission
from lean_ai_serve.utils.gpu import get_gpu_info

router = APIRouter(tags=["health"])


def _get_registry(request: Request) -> ModelRegistry:
    return request.app.state.registry


# ---------------------------------------------------------------------------
# GET /health — public, no auth required
# ---------------------------------------------------------------------------


@router.get("/health", response_model=HealthResponse)
async def health(request: Request):
    from lean_ai_serve import __version__

    registry = _get_registry(request)
    models = await registry.list_models()
    loaded = sum(1 for m in models if m.state == ModelState.LOADED)
    return HealthResponse(
        status="ok",
        version=__version__,
        models_loaded=loaded,
    )


# ---------------------------------------------------------------------------
# GET /api/status — detailed status (admin only)
# ---------------------------------------------------------------------------


@router.get("/api/status", response_model=StatusResponse)
async def status(
    request: Request,
    user: AuthUser = Depends(require_permission("model:read")),
):
    from lean_ai_serve import __version__

    registry = _get_registry(request)
    models = await registry.list_models()
    gpus = get_gpu_info()

    # Map loaded models to GPUs
    for model in models:
        if model.state == ModelState.LOADED:
            for gpu in gpus:
                if gpu.index in model.gpu:
                    gpu.model_loaded = model.name

    uptime = time.monotonic() - request.app.state.start_time

    return StatusResponse(
        status="ok",
        version=__version__,
        gpus=gpus,
        models=models,
        uptime_seconds=uptime,
    )


# ---------------------------------------------------------------------------
# GET /api/gpu — GPU info
# ---------------------------------------------------------------------------


@router.get("/api/gpu")
async def gpu_info(
    user: AuthUser = Depends(require_permission("model:read")),
):
    return {"gpus": [g.model_dump() for g in get_gpu_info()]}
