"""Model management API — pull, load, unload, sleep, delete."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from lean_ai_serve.config import ModelConfig
from lean_ai_serve.engine.process import ProcessManager
from lean_ai_serve.models.downloader import ModelDownloader
from lean_ai_serve.models.registry import ModelRegistry
from lean_ai_serve.models.schemas import (
    AuthUser,
    ModelInfo,
    ModelsResponse,
    ModelState,
    PullRequest,
)
from lean_ai_serve.security.auth import require_permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/models", tags=["models"])


def _get_registry(request: Request) -> ModelRegistry:
    return request.app.state.registry


def _get_downloader(request: Request) -> ModelDownloader:
    return request.app.state.downloader


def _get_pm(request: Request) -> ProcessManager:
    return request.app.state.process_manager


# ---------------------------------------------------------------------------
# GET /api/models — list all models
# ---------------------------------------------------------------------------


@router.get("", response_model=ModelsResponse)
async def list_models(
    request: Request,
    user: AuthUser = Depends(require_permission("model:read")),
):
    models = await _get_registry(request).list_models()
    return ModelsResponse(models=models)


# ---------------------------------------------------------------------------
# GET /api/models/{name} — get model details
# ---------------------------------------------------------------------------


@router.get("/{name}", response_model=ModelInfo)
async def get_model(
    name: str,
    request: Request,
    user: AuthUser = Depends(require_permission("model:read")),
):
    model = await _get_registry(request).get_model(name)
    if model is None:
        raise HTTPException(status_code=404, detail=f"Model not found: {name}")
    return model


# ---------------------------------------------------------------------------
# POST /api/models/pull — download from HuggingFace
# ---------------------------------------------------------------------------


@router.post("/pull")
async def pull_model(
    body: PullRequest,
    request: Request,
    user: AuthUser = Depends(require_permission("model:write")),
):
    """Download a model from HuggingFace Hub. Returns SSE progress stream."""
    registry = _get_registry(request)
    downloader = _get_downloader(request)
    name = body.name or body.source.split("/")[-1]

    # Check if already exists
    existing = await registry.get_model(name)
    if existing and existing.state not in (ModelState.NOT_DOWNLOADED, ModelState.ERROR):
        raise HTTPException(
            status_code=409,
            detail=f"Model '{name}' already exists (state={existing.state.value})",
        )

    # Verify repo exists
    if not await downloader.check_exists(body.source):
        raise HTTPException(status_code=404, detail=f"HuggingFace repo not found: {body.source}")

    # Register or update in DB
    config = ModelConfig(source=body.source)
    await registry.register_model(name, body.source, config)
    await registry.set_state(name, ModelState.DOWNLOADING)

    async def event_stream():
        try:
            async for progress in downloader.download(body.source, body.revision):
                yield f"data: {progress.model_dump_json()}\n\n"

                if progress.status == "complete":
                    await registry.set_state(name, ModelState.DOWNLOADED)
                elif progress.status == "error":
                    await registry.set_state(
                        name, ModelState.ERROR, error_message=progress.message
                    )
        except Exception as e:
            logger.exception("Pull failed for %s", name)
            await registry.set_state(name, ModelState.ERROR, error_message=str(e))
            yield f'data: {{"status": "error", "message": "{e}"}}\n\n'

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# POST /api/models/{name}/load — start vLLM
# ---------------------------------------------------------------------------


@router.post("/{name}/load")
async def load_model(
    name: str,
    request: Request,
    user: AuthUser = Depends(require_permission("model:deploy")),
):
    """Load a downloaded model into vLLM."""
    registry = _get_registry(request)
    pm = _get_pm(request)

    model = await registry.get_model(name)
    if model is None:
        raise HTTPException(status_code=404, detail=f"Model not found: {name}")

    if model.state == ModelState.LOADED:
        return {"status": "already_loaded", "port": model.port}

    if model.state not in (ModelState.DOWNLOADED, ModelState.ERROR, ModelState.SLEEPING):
        raise HTTPException(
            status_code=409,
            detail=f"Cannot load model in state: {model.state.value}",
        )

    config = await registry.get_config(name)
    if config is None:
        raise HTTPException(status_code=500, detail="Model config not found in registry")

    # Find local model path
    downloader = _get_downloader(request)
    model_path = downloader.get_local_path(config.source)
    if model_path is None:
        raise HTTPException(
            status_code=409,
            detail="Model files not found locally — pull first",
        )

    await registry.set_state(name, ModelState.LOADING)

    try:
        info = await pm.start(name, config, model_path)
        await registry.set_state(
            name, ModelState.LOADED, port=info.port, pid=info.pid
        )
        return {"status": "loading", "port": info.port, "pid": info.pid}
    except Exception as e:
        logger.exception("Failed to load model '%s'", name)
        await registry.set_state(name, ModelState.ERROR, error_message=str(e))
        raise HTTPException(status_code=500, detail=str(e)) from e


# ---------------------------------------------------------------------------
# POST /api/models/{name}/unload — stop vLLM
# ---------------------------------------------------------------------------


@router.post("/{name}/unload")
async def unload_model(
    name: str,
    request: Request,
    user: AuthUser = Depends(require_permission("model:deploy")),
):
    """Unload a model (stop vLLM process)."""
    registry = _get_registry(request)
    pm = _get_pm(request)

    model = await registry.get_model(name)
    if model is None:
        raise HTTPException(status_code=404, detail=f"Model not found: {name}")

    if model.state not in (ModelState.LOADED, ModelState.LOADING, ModelState.ERROR):
        raise HTTPException(
            status_code=409,
            detail=f"Cannot unload model in state: {model.state.value}",
        )

    await registry.set_state(name, ModelState.UNLOADING)
    stopped = await pm.stop(name)

    if stopped:
        await registry.set_state(name, ModelState.DOWNLOADED)
        return {"status": "unloaded"}
    else:
        await registry.set_state(name, ModelState.DOWNLOADED)
        return {"status": "not_running"}


# ---------------------------------------------------------------------------
# DELETE /api/models/{name} — remove model
# ---------------------------------------------------------------------------


@router.delete("/{name}")
async def delete_model(
    name: str,
    request: Request,
    user: AuthUser = Depends(require_permission("model:write")),
):
    """Delete a model from registry and cache."""
    registry = _get_registry(request)
    pm = _get_pm(request)
    downloader = _get_downloader(request)

    model = await registry.get_model(name)
    if model is None:
        raise HTTPException(status_code=404, detail=f"Model not found: {name}")

    # Stop if running
    if model.state in (ModelState.LOADED, ModelState.LOADING):
        await pm.stop(name)

    # Delete cached files
    await downloader.delete_cached(model.source)

    # Remove from DB
    await registry.delete_model(name)

    return {"status": "deleted"}
