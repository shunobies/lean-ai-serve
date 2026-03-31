"""Pydantic models for API request/response schemas."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Model state machine
# ---------------------------------------------------------------------------


class ModelState(StrEnum):
    NOT_DOWNLOADED = "not_downloaded"
    DOWNLOADING = "downloading"
    DOWNLOADED = "downloaded"
    LOADING = "loading"
    LOADED = "loaded"
    SLEEPING = "sleeping"
    UNLOADING = "unloading"
    ERROR = "error"


# ---------------------------------------------------------------------------
# Model responses
# ---------------------------------------------------------------------------


class ModelInfo(BaseModel):
    """Public model information returned by API."""

    name: str
    source: str
    state: ModelState
    gpu: list[int] = Field(default_factory=list)
    tensor_parallel_size: int = 1
    max_model_len: int | None = None
    task: str = "chat"
    port: int | None = None
    enable_lora: bool = False
    active_adapters: list[str] = Field(default_factory=list)
    autoload: bool = False
    downloaded_at: datetime | None = None
    loaded_at: datetime | None = None
    error_message: str | None = None


class ModelsResponse(BaseModel):
    models: list[ModelInfo]


# ---------------------------------------------------------------------------
# Model pull
# ---------------------------------------------------------------------------


class PullRequest(BaseModel):
    source: str  # HuggingFace repo ID
    name: str | None = None  # Friendly name (defaults to repo ID)
    revision: str = "main"


class PullProgress(BaseModel):
    """SSE event for download progress."""

    status: str  # downloading, verifying, complete, error
    filename: str | None = None
    downloaded_bytes: int = 0
    total_bytes: int = 0
    progress_pct: float = 0.0
    message: str = ""


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    token: str
    expires_at: datetime
    user: str
    roles: list[str]


class AuthUser(BaseModel):
    """Authenticated user context injected into requests."""

    user_id: str
    display_name: str
    roles: list[str]
    allowed_models: list[str] = Field(default_factory=lambda: ["*"])
    auth_method: str = "api_key"  # api_key, ldap, oidc, none
    key_id: str = ""  # API key ID (for rate limiting)
    rate_limit: int = 0  # Requests per minute (0 = unlimited)

    def can_access_model(self, model_name: str) -> bool:
        return "*" in self.allowed_models or model_name in self.allowed_models


class UserInfoResponse(BaseModel):
    """Response for GET /api/auth/me."""

    user_id: str
    display_name: str
    roles: list[str]
    allowed_models: list[str]
    auth_method: str


# ---------------------------------------------------------------------------
# API Keys
# ---------------------------------------------------------------------------


class APIKeyCreate(BaseModel):
    name: str
    role: str = "user"
    models: list[str] = Field(default_factory=lambda: ["*"])
    rate_limit: int = 0  # 0 = unlimited
    expires_days: int | None = None


class APIKeyInfo(BaseModel):
    id: str
    name: str
    role: str
    models: list[str]
    rate_limit: int
    created_at: datetime
    expires_at: datetime | None = None
    last_used_at: datetime | None = None
    prefix: str  # First 8 chars of the key for identification


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


class AuditEntry(BaseModel):
    id: int
    timestamp: datetime
    request_id: str
    user_id: str
    user_role: str
    source_ip: str
    action: str
    model: str | None = None
    prompt_hash: str | None = None
    response_hash: str | None = None
    token_count: int = 0
    latency_ms: int = 0
    status: str = "success"
    error_detail: str | None = None
    chain_hash: str | None = None


class AuditQueryParams(BaseModel):
    user_id: str | None = None
    action: str | None = None
    model: str | None = None
    status: str | None = None
    from_time: datetime | None = None
    to_time: datetime | None = None
    limit: int = 100
    offset: int = 0


class AuditResponse(BaseModel):
    entries: list[AuditEntry]
    total: int


# ---------------------------------------------------------------------------
# Health / Status
# ---------------------------------------------------------------------------


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str = ""
    models_loaded: int = 0


class GPUInfo(BaseModel):
    index: int
    name: str = ""
    memory_total_mb: int = 0
    memory_used_mb: int = 0
    memory_free_mb: int = 0
    utilization_pct: float = 0.0
    temperature_c: int | None = None
    model_loaded: str | None = None


class StatusResponse(BaseModel):
    status: str = "ok"
    version: str = ""
    gpus: list[GPUInfo] = Field(default_factory=list)
    models: list[ModelInfo] = Field(default_factory=list)
    uptime_seconds: float = 0.0


# ---------------------------------------------------------------------------
# VRAM Estimation
# ---------------------------------------------------------------------------


class EstimateRequest(BaseModel):
    model_name: str
    context_length: int = 131072
    batch_size: int = 1
    kv_dtype: str = "bf16"  # bf16, fp8


class EstimateResponse(BaseModel):
    model_weights_gb: float
    kv_cache_gb: float
    overhead_gb: float = 2.0
    total_gb: float
    recommendation: str
