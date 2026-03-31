"""Training API — datasets, training jobs, adapters."""

from __future__ import annotations

import logging

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import StreamingResponse

from lean_ai_serve.models.schemas import AuthUser
from lean_ai_serve.security.auth import require_permission
from lean_ai_serve.training.adapters import AdapterError, AdapterRegistry
from lean_ai_serve.training.datasets import DatasetManager, DatasetValidationError
from lean_ai_serve.training.orchestrator import TrainingOrchestrator
from lean_ai_serve.training.schemas import (
    AdapterDeployRequest,
    AdapterImportRequest,
    AdapterInfo,
    AdapterState,
    DatasetFormat,
    DatasetInfo,
    TrainingJobInfo,
    TrainingJobState,
    TrainingSubmitRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/training", tags=["training"])


# ---------------------------------------------------------------------------
# Dependency helpers
# ---------------------------------------------------------------------------


def _get_datasets(request: Request) -> DatasetManager:
    return request.app.state.dataset_manager


def _get_orchestrator(request: Request) -> TrainingOrchestrator:
    return request.app.state.training_orchestrator


def _get_adapters(request: Request) -> AdapterRegistry:
    return request.app.state.adapter_registry


# ===========================================================================
# DATASETS
# ===========================================================================


@router.post("/datasets", response_model=DatasetInfo, status_code=201)
async def upload_dataset(
    request: Request,
    file: UploadFile = File(...),
    name: str = Form(...),
    dataset_format: str = Form(..., alias="format"),
    description: str = Form(""),
    user: AuthUser = Depends(require_permission("dataset:upload")),
):
    """Upload a new training dataset."""
    dm = _get_datasets(request)

    try:
        fmt = DatasetFormat(dataset_format)
    except ValueError as e:
        valid = ", ".join(f.value for f in DatasetFormat)
        raise HTTPException(
            status_code=400,
            detail=f"Invalid format: {dataset_format}. Must be one of: {valid}",
        ) from e

    content = await file.read()

    try:
        info = await dm.upload(
            name=name,
            fmt=fmt,
            content=content,
            uploaded_by=user.user_id,
            description=description,
        )
        return info
    except DatasetValidationError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e


@router.get("/datasets", response_model=list[DatasetInfo])
async def list_datasets(
    request: Request,
    user: AuthUser = Depends(require_permission("dataset:read")),
):
    """List all datasets."""
    return await _get_datasets(request).list_datasets()


@router.get("/datasets/{name}", response_model=DatasetInfo)
async def get_dataset(
    name: str,
    request: Request,
    user: AuthUser = Depends(require_permission("dataset:read")),
):
    """Get dataset details."""
    ds = await _get_datasets(request).get(name)
    if ds is None:
        raise HTTPException(status_code=404, detail=f"Dataset not found: {name}")
    return ds


@router.get("/datasets/{name}/preview")
async def preview_dataset(
    name: str,
    request: Request,
    limit: int = 5,
    user: AuthUser = Depends(require_permission("dataset:read")),
):
    """Preview first N rows of a dataset."""
    rows = await _get_datasets(request).preview(name, limit)
    if not rows:
        raise HTTPException(
            status_code=404, detail=f"Dataset not found or empty: {name}"
        )
    return {"rows": rows, "count": len(rows)}


@router.delete("/datasets/{name}")
async def delete_dataset(
    name: str,
    request: Request,
    user: AuthUser = Depends(require_permission("dataset:upload")),
):
    """Delete a dataset."""
    deleted = await _get_datasets(request).delete(name)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Dataset not found: {name}")
    return {"status": "deleted"}


# ===========================================================================
# TRAINING JOBS
# ===========================================================================


@router.post("/jobs", response_model=TrainingJobInfo, status_code=201)
async def submit_training_job(
    body: TrainingSubmitRequest,
    request: Request,
    user: AuthUser = Depends(require_permission("training:submit")),
):
    """Submit a new training job."""
    orch = _get_orchestrator(request)
    try:
        info = await orch.submit(body, submitted_by=user.user_id)
        return info
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.get("/jobs", response_model=list[TrainingJobInfo])
async def list_training_jobs(
    request: Request,
    state: str | None = None,
    user: AuthUser = Depends(require_permission("training:read")),
):
    """List training jobs, optionally filtered by state."""
    job_state = None
    if state:
        try:
            job_state = TrainingJobState(state)
        except ValueError as e:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid state: {state}",
            ) from e
    return await _get_orchestrator(request).list_jobs(state=job_state)


@router.get("/jobs/{job_id}", response_model=TrainingJobInfo)
async def get_training_job(
    job_id: str,
    request: Request,
    user: AuthUser = Depends(require_permission("training:read")),
):
    """Get training job details."""
    job = await _get_orchestrator(request).get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")
    return job


@router.post("/jobs/{job_id}/start")
async def start_training_job(
    job_id: str,
    request: Request,
    user: AuthUser = Depends(require_permission("training:submit")),
):
    """Start a queued training job. Returns SSE progress stream."""
    orch = _get_orchestrator(request)

    job = await orch.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")
    if job.state != TrainingJobState.QUEUED:
        raise HTTPException(
            status_code=409,
            detail=f"Job is {job.state.value}, expected queued",
        )

    async def event_stream():
        async for progress in orch.stream_progress(job_id):
            yield f"data: {progress.model_dump_json()}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.post("/jobs/{job_id}/cancel")
async def cancel_training_job(
    job_id: str,
    request: Request,
    user: AuthUser = Depends(require_permission("training:submit")),
):
    """Cancel a running or queued training job."""
    cancelled = await _get_orchestrator(request).cancel_job(job_id)
    if not cancelled:
        raise HTTPException(
            status_code=409,
            detail="Job cannot be cancelled (not running or already finished)",
        )
    return {"status": "cancelled"}


@router.get("/gpu-status")
async def gpu_status(
    request: Request,
    user: AuthUser = Depends(require_permission("training:read")),
):
    """Show current GPU usage by training jobs."""
    return {"gpu_assignments": _get_orchestrator(request).get_gpu_usage()}


# ===========================================================================
# ADAPTERS
# ===========================================================================


@router.get("/adapters", response_model=list[AdapterInfo])
async def list_adapters(
    request: Request,
    base_model: str | None = None,
    user: AuthUser = Depends(require_permission("adapter:read")),
):
    """List adapters, optionally filtered by base model."""
    return await _get_adapters(request).list_adapters(base_model)


@router.get("/adapters/{name}", response_model=AdapterInfo)
async def get_adapter(
    name: str,
    request: Request,
    user: AuthUser = Depends(require_permission("adapter:read")),
):
    """Get adapter details."""
    adapter = await _get_adapters(request).get(name)
    if adapter is None:
        raise HTTPException(status_code=404, detail=f"Adapter not found: {name}")
    return adapter


@router.post("/adapters/import", response_model=AdapterInfo, status_code=201)
async def import_adapter(
    body: AdapterImportRequest,
    request: Request,
    user: AuthUser = Depends(require_permission("adapter:deploy")),
):
    """Import an externally-trained adapter."""
    try:
        info = await _get_adapters(request).register(
            name=body.name,
            base_model=body.base_model,
            source_path=body.path,
            metadata=body.metadata,
        )
        return info
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except AdapterError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/adapters/{name}/deploy")
async def deploy_adapter(
    name: str,
    body: AdapterDeployRequest,
    request: Request,
    user: AuthUser = Depends(require_permission("adapter:deploy")),
):
    """Deploy an adapter to a loaded vLLM model."""
    adapters = _get_adapters(request)

    # Look up the model port from process manager
    pm = request.app.state.process_manager
    port = pm.get_port(body.model_name)
    if port is None:
        raise HTTPException(
            status_code=400,
            detail=f"Model '{body.model_name}' is not loaded or not healthy",
        )

    try:
        await adapters.deploy(name, port)
        return {"status": "deployed", "model": body.model_name}
    except AdapterError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/adapters/{name}/undeploy")
async def undeploy_adapter(
    name: str,
    request: Request,
    user: AuthUser = Depends(require_permission("adapter:deploy")),
):
    """Undeploy an adapter from its vLLM model."""
    adapters = _get_adapters(request)

    adapter = await adapters.get(name)
    if adapter is None:
        raise HTTPException(status_code=404, detail=f"Adapter not found: {name}")

    # Find the model port
    pm = request.app.state.process_manager
    port = pm.get_port(adapter.base_model)
    if port is None:
        # Model not running — just mark as available
        await adapters.set_state(name, AdapterState.AVAILABLE)
        return {
            "status": "undeployed",
            "note": "Model not running, adapter state reset",
        }

    try:
        await adapters.undeploy(name, port)
        return {"status": "undeployed"}
    except AdapterError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.delete("/adapters/{name}")
async def delete_adapter(
    name: str,
    request: Request,
    user: AuthUser = Depends(require_permission("adapter:deploy")),
):
    """Delete an adapter."""
    try:
        deleted = await _get_adapters(request).delete(name)
        if not deleted:
            raise HTTPException(
                status_code=404, detail=f"Adapter not found: {name}"
            )
        return {"status": "deleted"}
    except AdapterError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
