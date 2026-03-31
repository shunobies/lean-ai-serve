"""Pydantic models for training API request/response schemas."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class TrainingJobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AdapterState(StrEnum):
    AVAILABLE = "available"
    DEPLOYED = "deployed"
    ERROR = "error"


class DatasetFormat(StrEnum):
    SHAREGPT = "sharegpt"
    ALPACA = "alpaca"
    JSONL = "jsonl"
    CSV = "csv"


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class TrainingSubmitRequest(BaseModel):
    """Submit a new training job."""

    name: str
    base_model: str  # Must reference a downloaded model
    dataset: str  # Must reference an uploaded dataset
    adapter_name: str | None = None  # Auto-generated if empty
    gpu: list[int] | None = None  # Auto-assigned if empty
    # Training hyperparameters
    num_epochs: float = 3.0
    learning_rate: float = 2e-4
    batch_size: int = 4
    gradient_accumulation_steps: int = 4
    lora_rank: int = 16
    lora_alpha: int = 32
    lora_target: str | None = None  # Comma-separated modules, None = auto
    max_seq_length: int = 2048
    warmup_ratio: float = 0.1
    weight_decay: float = 0.01
    save_steps: int = 100
    logging_steps: int = 10
    extra_args: dict[str, Any] = Field(default_factory=dict)


class AdapterDeployRequest(BaseModel):
    """Deploy an adapter to a loaded model."""

    model_name: str  # Target vLLM model (must have enable_lora=True)


class AdapterImportRequest(BaseModel):
    """Import an externally-trained adapter."""

    name: str
    base_model: str
    path: str  # Filesystem path to adapter weights
    metadata: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class DatasetInfo(BaseModel):
    name: str
    path: str
    format: DatasetFormat
    row_count: int | None = None
    size_bytes: int | None = None
    uploaded_by: str
    created_at: datetime
    description: str = ""


class TrainingJobInfo(BaseModel):
    id: str
    name: str
    base_model: str
    dataset: str
    state: TrainingJobState
    gpu: list[int] = Field(default_factory=list)
    adapter_name: str | None = None
    output_path: str | None = None
    submitted_by: str
    submitted_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error_message: str | None = None
    metrics: dict[str, Any] | None = None
    config: dict[str, Any] = Field(default_factory=dict)


class AdapterInfo(BaseModel):
    name: str
    base_model: str
    source_path: str
    state: AdapterState
    training_job_id: str | None = None
    created_at: datetime
    deployed_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class TrainingProgress(BaseModel):
    """SSE event for training progress streaming."""

    status: str  # running, step, eval, complete, error, cancelled
    step: int = 0
    total_steps: int = 0
    epoch: float = 0.0
    loss: float | None = None
    learning_rate: float | None = None
    eval_loss: float | None = None
    progress_pct: float = 0.0
    message: str = ""
    metrics: dict[str, Any] = Field(default_factory=dict)
