"""Training orchestrator — job submission, GPU scheduling, lifecycle management."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from lean_ai_serve.config import Settings
from lean_ai_serve.db import Database
from lean_ai_serve.training.adapters import AdapterRegistry
from lean_ai_serve.training.backend import TrainingBackend
from lean_ai_serve.training.datasets import DatasetManager
from lean_ai_serve.training.schemas import (
    TrainingJobInfo,
    TrainingJobState,
    TrainingProgress,
    TrainingSubmitRequest,
)

logger = logging.getLogger(__name__)


class TrainingOrchestrator:
    """Coordinates training jobs — validates inputs, schedules GPUs,
    delegates to backend, registers adapters on completion."""

    def __init__(
        self,
        db: Database,
        settings: Settings,
        backend: TrainingBackend,
        datasets: DatasetManager,
        adapters: AdapterRegistry,
    ) -> None:
        self._db = db
        self._settings = settings
        self._backend = backend
        self._datasets = datasets
        self._adapters = adapters
        self._output_base = Path(settings.training.output_directory)
        self._output_base.mkdir(parents=True, exist_ok=True)
        self._max_concurrent = settings.training.max_concurrent_jobs
        self._default_gpu = settings.training.default_gpu
        # Track running jobs by ID for cancellation
        self._running: dict[str, asyncio.Task] = {}
        # Track GPU usage: gpu_id -> job_id
        self._gpu_locks: dict[int, str] = {}

    async def submit(
        self,
        request: TrainingSubmitRequest,
        submitted_by: str,
    ) -> TrainingJobInfo:
        """Submit a training job.

        Validates inputs (model exists + downloaded, dataset exists),
        assigns GPUs, persists to DB, returns job info.
        Does NOT start training — call stream_progress() to begin.

        Raises ValueError on validation failure.
        """
        # Validate dataset exists
        dataset = await self._datasets.get(request.dataset)
        if dataset is None:
            raise ValueError(f"Dataset '{request.dataset}' not found")

        # Validate base model exists and is downloaded
        model_row = await self._db.fetchone(
            "SELECT state, source FROM models WHERE name = ?",
            (request.base_model,),
        )
        if model_row is None:
            raise ValueError(f"Model '{request.base_model}' not found")
        if model_row["state"] not in ("downloaded", "loaded", "sleeping"):
            raise ValueError(
                f"Model '{request.base_model}' must be downloaded first "
                f"(current state: {model_row['state']})"
            )

        # Check concurrent job limit
        running_count = await self._count_running_jobs()
        if running_count >= self._max_concurrent:
            raise ValueError(
                f"Max concurrent training jobs reached ({self._max_concurrent})"
            )

        # Assign GPUs
        gpu_ids = request.gpu or self._default_gpu
        conflict = self._check_gpu_conflicts(gpu_ids)
        if conflict:
            raise ValueError(
                f"GPU(s) {conflict} already in use by another training job"
            )

        # Generate job ID and adapter name
        job_id = str(uuid4())
        adapter_name = request.adapter_name or f"{request.base_model}-{request.name}"
        output_dir = str(self._output_base / job_id)

        # Build backend config
        dataset_path = await self._datasets.get_path(request.dataset)
        config = await self._backend.build_config(
            request=request,
            dataset_path=dataset_path,
            model_source=model_row["source"],
            output_dir=output_dir,
        )

        now = datetime.now(UTC)
        info = TrainingJobInfo(
            id=job_id,
            name=request.name,
            base_model=request.base_model,
            dataset=request.dataset,
            state=TrainingJobState.QUEUED,
            gpu=gpu_ids,
            adapter_name=adapter_name,
            output_path=output_dir,
            submitted_by=submitted_by,
            submitted_at=now,
            config=config,
        )

        # Persist to DB
        await self._db.execute(
            """
            INSERT INTO training_jobs
                (id, name, base_model, dataset, config_json, state, gpu,
                 output_path, adapter_name, submitted_by, submitted_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                request.name,
                request.base_model,
                request.dataset,
                json.dumps(config),
                TrainingJobState.QUEUED.value,
                json.dumps(gpu_ids),
                output_dir,
                adapter_name,
                submitted_by,
                now.isoformat(),
            ),
        )
        await self._db.commit()

        logger.info(
            "Training job submitted: %s (model=%s, dataset=%s, gpu=%s)",
            job_id, request.base_model, request.dataset, gpu_ids,
        )
        return info

    async def stream_progress(self, job_id: str) -> AsyncIterator[TrainingProgress]:
        """Start training and stream progress events.

        Transitions job from QUEUED → RUNNING → COMPLETED/FAILED.
        On completion, registers the output adapter automatically.
        """
        job = await self.get_job(job_id)
        if job is None:
            yield TrainingProgress(status="error", message=f"Job {job_id} not found")
            return

        if job.state != TrainingJobState.QUEUED:
            yield TrainingProgress(
                status="error",
                message=f"Job {job_id} is {job.state}, expected queued",
            )
            return

        # Lock GPUs
        for gpu_id in job.gpu:
            self._gpu_locks[gpu_id] = job_id

        # Mark as running
        await self._set_job_state(job_id, TrainingJobState.RUNNING)

        final_status = "error"
        final_message = ""

        try:
            async for event in self._backend.launch(
                config=job.config,
                output_dir=job.output_path,
                gpu_ids=job.gpu,
            ):
                yield event

                if event.status == "complete":
                    final_status = "complete"
                elif event.status == "error":
                    final_status = "error"
                    final_message = event.message
                elif event.status == "cancelled":
                    final_status = "cancelled"

            # Handle completion
            if final_status == "complete":
                await self._set_job_state(job_id, TrainingJobState.COMPLETED)
                # Auto-register adapter
                await self._register_adapter_from_job(job)
            elif final_status == "cancelled":
                await self._set_job_state(job_id, TrainingJobState.CANCELLED)
            else:
                await self._set_job_state(
                    job_id, TrainingJobState.FAILED, error=final_message
                )

        except Exception as e:
            logger.exception("Training job %s failed", job_id)
            await self._set_job_state(
                job_id, TrainingJobState.FAILED, error=str(e)
            )
            yield TrainingProgress(status="error", message=str(e))
        finally:
            # Release GPU locks
            for gpu_id in job.gpu:
                self._gpu_locks.pop(gpu_id, None)

    async def cancel_job(self, job_id: str) -> bool:
        """Cancel a running training job."""
        job = await self.get_job(job_id)
        if job is None:
            return False

        if job.state not in (TrainingJobState.QUEUED, TrainingJobState.RUNNING):
            return False

        if job.state == TrainingJobState.RUNNING and job.output_path:
            cancelled = await self._backend.cancel(job.output_path)
            if not cancelled:
                return False

        await self._set_job_state(job_id, TrainingJobState.CANCELLED)

        # Release GPU locks
        for gpu_id in job.gpu:
            self._gpu_locks.pop(gpu_id, None)

        logger.info("Training job cancelled: %s", job_id)
        return True

    async def get_job(self, job_id: str) -> TrainingJobInfo | None:
        """Get a training job by ID."""
        row = await self._db.fetchone(
            "SELECT * FROM training_jobs WHERE id = ?", (job_id,)
        )
        if row is None:
            return None
        return self._row_to_info(row)

    async def list_jobs(
        self,
        state: TrainingJobState | None = None,
        submitted_by: str | None = None,
    ) -> list[TrainingJobInfo]:
        """List training jobs with optional filters."""
        conditions = []
        params: list = []

        if state:
            conditions.append("state = ?")
            params.append(state.value)
        if submitted_by:
            conditions.append("submitted_by = ?")
            params.append(submitted_by)

        where = ""
        if conditions:
            where = "WHERE " + " AND ".join(conditions)

        rows = await self._db.fetchall(
            f"SELECT * FROM training_jobs {where} ORDER BY submitted_at DESC",
            tuple(params) if params else None,
        )
        return [self._row_to_info(row) for row in rows]

    def get_gpu_usage(self) -> dict[int, str | None]:
        """Return current GPU assignment: gpu_id → job_id or None."""
        return dict(self._gpu_locks)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _count_running_jobs(self) -> int:
        """Count jobs in QUEUED or RUNNING state."""
        row = await self._db.fetchone(
            "SELECT COUNT(*) as cnt FROM training_jobs WHERE state IN (?, ?)",
            (TrainingJobState.QUEUED.value, TrainingJobState.RUNNING.value),
        )
        return row["cnt"] if row else 0

    def _check_gpu_conflicts(self, gpu_ids: list[int]) -> list[int]:
        """Check if any requested GPUs are in use. Returns conflicting GPU IDs."""
        conflicts = [g for g in gpu_ids if g in self._gpu_locks]
        return conflicts

    async def _set_job_state(
        self,
        job_id: str,
        state: TrainingJobState,
        error: str | None = None,
    ) -> None:
        """Update job state in DB."""
        updates = ["state = ?"]
        params: list = [state.value]

        now = datetime.now(UTC).isoformat()
        if state == TrainingJobState.RUNNING:
            updates.append("started_at = ?")
            params.append(now)
        elif state in (
            TrainingJobState.COMPLETED,
            TrainingJobState.FAILED,
            TrainingJobState.CANCELLED,
        ):
            updates.append("completed_at = ?")
            params.append(now)

        if error:
            updates.append("error_message = ?")
            params.append(error)

        params.append(job_id)
        set_clause = ", ".join(updates)
        await self._db.execute(
            f"UPDATE training_jobs SET {set_clause} WHERE id = ?",
            tuple(params),
        )
        await self._db.commit()

    async def _register_adapter_from_job(self, job: TrainingJobInfo) -> None:
        """Auto-register adapter after successful training."""
        if not job.adapter_name or not job.output_path:
            return

        try:
            await self._adapters.register(
                name=job.adapter_name,
                base_model=job.base_model,
                source_path=job.output_path,
                training_job_id=job.id,
                metadata={
                    "training_job": job.id,
                    "dataset": job.dataset,
                },
            )
            logger.info(
                "Auto-registered adapter '%s' from job %s",
                job.adapter_name, job.id,
            )
        except (ValueError, Exception) as e:
            logger.warning(
                "Failed to auto-register adapter for job %s: %s",
                job.id, e,
            )

    @staticmethod
    def _row_to_info(row) -> TrainingJobInfo:
        """Convert DB row to TrainingJobInfo."""
        config = json.loads(row["config_json"]) if row["config_json"] else {}
        gpu = json.loads(row["gpu"]) if row["gpu"] else []
        metrics = json.loads(row["metrics_json"]) if row["metrics_json"] else None

        return TrainingJobInfo(
            id=row["id"],
            name=row["name"],
            base_model=row["base_model"],
            dataset=row["dataset"],
            state=TrainingJobState(row["state"]),
            gpu=gpu,
            adapter_name=row["adapter_name"],
            output_path=row["output_path"],
            submitted_by=row["submitted_by"],
            submitted_at=datetime.fromisoformat(row["submitted_at"]),
            started_at=(
                datetime.fromisoformat(row["started_at"])
                if row["started_at"]
                else None
            ),
            completed_at=(
                datetime.fromisoformat(row["completed_at"])
                if row["completed_at"]
                else None
            ),
            error_message=row["error_message"],
            metrics=metrics,
            config=config,
        )
