"""Configuration system — YAML file with encrypted secret support."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, model_validator

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Sub-models for nested YAML sections
# ---------------------------------------------------------------------------


class TLSConfig(BaseModel):
    enabled: bool = False
    cert_file: str = ""
    key_file: str = ""


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8420
    tls: TLSConfig = Field(default_factory=TLSConfig)


class LDAPConfig(BaseModel):
    server_url: str = ""
    bind_dn: str = ""
    bind_password_env: str = "LEAN_AI_LDAP_BIND_PASSWORD"
    user_search_base: str = ""
    user_search_filter: str = "(sAMAccountName={username})"
    group_search_base: str = ""
    group_role_mapping: dict[str, str] = Field(default_factory=dict)
    default_role: str = "user"
    cache_ttl: int = 300
    connection_pool_size: int = 5


class OIDCConfig(BaseModel):
    issuer_url: str = ""
    client_id: str = ""
    audience: str = ""
    roles_claim: str = "realm_access.roles"
    role_mapping: dict[str, str] = Field(default_factory=dict)  # OIDC role -> app role
    default_role: str = "user"
    jwks_cache_ttl: int = 3600  # Seconds to cache JWKS keys


class ContentFilterPattern(BaseModel):
    name: str
    pattern: str
    action: str = "warn"  # warn, redact, block


class ContentFilterConfig(BaseModel):
    enabled: bool = False
    patterns: list[ContentFilterPattern] = Field(default_factory=list)
    custom_patterns_file: str = ""


class SecurityConfig(BaseModel):
    mode: str = "api_key"  # api_key, ldap, oidc, ldap+api_key, oidc+api_key
    api_keys: list[dict[str, Any]] = Field(default_factory=list)
    ldap: LDAPConfig = Field(default_factory=LDAPConfig)
    oidc: OIDCConfig = Field(default_factory=OIDCConfig)
    jwt_secret: str = ""  # Auto-generated at startup if empty
    jwt_expiry_hours: float = 8.0
    content_filtering: ContentFilterConfig = Field(default_factory=ContentFilterConfig)


class AuditConfig(BaseModel):
    enabled: bool = True
    log_prompts: bool = True
    log_prompts_hash_only: bool = False
    retention_days: int = 2190  # 6 years
    storage: str = "sqlite"  # sqlite or file


class EncryptionAtRestConfig(BaseModel):
    enabled: bool = False
    key_source: str = "file"  # file, env, vault
    key_file: str = ""
    key_env_var: str = "LEAN_AI_ENCRYPTION_KEY"
    # Vault-specific settings (when key_source="vault")
    vault_path: str = "secret/data/lean-ai-serve/encryption-key"
    vault_key_field: str = "key"
    vault_auth_method: str = "token"  # token, approle
    vault_role_id_env: str = "VAULT_ROLE_ID"
    vault_secret_id_env: str = "VAULT_SECRET_ID"
    vault_cache_ttl: int = 300  # Seconds to cache the fetched key


class EncryptionConfig(BaseModel):
    at_rest: EncryptionAtRestConfig = Field(default_factory=EncryptionAtRestConfig)


class CacheConfig(BaseModel):
    directory: str = "~/.cache/lean-ai-serve"
    huggingface_token: str = ""


class KVCacheConfig(BaseModel):
    dtype: str = "auto"  # auto, fp8, fp8_e4m3, fp8_e5m2, turboquant
    calculate_scales: bool = False
    turboquant_bits: float = 3.0


class ContextConfig(BaseModel):
    max_model_len: int | None = None
    cpu_offload_gb: float = 0.0
    swap_space: float = 4.0
    enable_prefix_caching: bool = True
    prefix_caching_hash: str = "sha256"
    max_num_batched_tokens: int | None = None
    rope_scaling: dict[str, Any] | None = None
    rope_theta: float | None = None


class SpeculativeConfig(BaseModel):
    enabled: bool = False
    strategy: str = "draft"  # draft, ngram, eagle
    draft_model: str | None = None
    num_tokens: int = 5
    draft_tensor_parallel_size: int = 1


class SamplingDefaults(BaseModel):
    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    repetition_penalty: float | None = None
    min_p: float | None = None


class LifecycleConfig(BaseModel):
    idle_sleep_timeout: int = 0  # Seconds, 0 = never
    sleep_level: int = 1  # 1=auto-wake capable, 2=full unload
    auto_wake_on_request: bool = True  # Level 1 only: auto-wake on inference request


class ModelConfig(BaseModel):
    """Configuration for a single model."""

    source: str  # HuggingFace repo ID or local path
    gpu: list[int] = Field(default_factory=lambda: [0])
    tensor_parallel_size: int = 1
    pipeline_parallel_size: int = 1
    max_model_len: int | None = None
    dtype: str = "auto"
    quantization: str | None = None
    tool_call_parser: str | None = None
    reasoning_parser: str | None = None
    guided_decoding_backend: str = "xgrammar"
    enable_lora: bool = False
    max_loras: int = 4
    max_lora_rank: int = 64
    gpu_memory_utilization: float | None = None  # None = use server default
    autoload: bool = False
    task: str = "chat"  # chat, embed, generate
    kv_cache: KVCacheConfig = Field(default_factory=KVCacheConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)
    speculative: SpeculativeConfig = Field(default_factory=SpeculativeConfig)
    sampling_defaults: SamplingDefaults = Field(default_factory=SamplingDefaults)
    lifecycle: LifecycleConfig = Field(default_factory=LifecycleConfig)


class TrainingConfig(BaseModel):
    """Training subsystem configuration."""

    enabled: bool = False
    backend: str = "llama-factory"  # llama-factory (extensible)
    output_directory: str = ""  # Default: {cache}/training_outputs
    max_concurrent_jobs: int = 1
    default_gpu: list[int] = Field(default_factory=lambda: [0])
    dataset_directory: str = ""  # Default: {cache}/datasets
    max_dataset_size_mb: int = 1024


class ContextCompressionConfig(BaseModel):
    enabled: bool = False
    method: str = "llmlingua2"
    target_ratio: float = 0.5
    min_length: int = 4096


class DefaultsConfig(BaseModel):
    gpu_memory_utilization: float = 0.90
    max_model_len: int | None = None
    dtype: str = "auto"


class MetricsConfig(BaseModel):
    enabled: bool = True
    gpu_poll_interval: int = 30  # Seconds between GPU metric polls


class LoggingConfig(BaseModel):
    json_output: bool = True  # True=JSON lines (production), False=console (dev)
    level: str = "INFO"


class AlertRuleConfig(BaseModel):
    name: str
    metric: str  # Metric name to check (e.g., "gpu_memory_used_pct")
    condition: str = "gt"  # gt, lt, gte, lte, eq
    threshold: float = 0.0
    severity: str = "warning"  # info, warning, critical
    message: str = ""


class AlertConfig(BaseModel):
    enabled: bool = True
    evaluation_interval: int = 60  # Seconds between alert evaluations
    rules: list[AlertRuleConfig] = Field(default_factory=list)


class TracingConfig(BaseModel):
    enabled: bool = False
    endpoint: str = ""  # OTLP exporter endpoint (e.g., "http://localhost:4317")
    protocol: str = "grpc"  # grpc or http
    service_name: str = "lean-ai-serve"


# ---------------------------------------------------------------------------
# Top-level settings
# ---------------------------------------------------------------------------


class Settings(BaseModel):
    """Top-level application settings loaded from YAML config file.

    YAML is the single source of truth.  Secret values support two patterns:

    - ``ENV[VAR_NAME]`` — resolved from the named environment variable.
    - ``ENC[ciphertext]`` — decrypted using the master key from
      ``encryption.at_rest``.
    """

    server: ServerConfig = Field(default_factory=ServerConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    audit: AuditConfig = Field(default_factory=AuditConfig)
    encryption: EncryptionConfig = Field(default_factory=EncryptionConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)
    defaults: DefaultsConfig = Field(default_factory=DefaultsConfig)
    models: dict[str, ModelConfig] = Field(default_factory=dict)
    training: TrainingConfig = Field(default_factory=TrainingConfig)
    context_compression: ContextCompressionConfig = Field(
        default_factory=ContextCompressionConfig
    )
    metrics: MetricsConfig = Field(default_factory=MetricsConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    alerts: AlertConfig = Field(default_factory=AlertConfig)
    tracing: TracingConfig = Field(default_factory=TracingConfig)

    @model_validator(mode="after")
    def _resolve_paths(self) -> Settings:
        """Expand ~ in paths and resolve training defaults."""
        self.cache.directory = str(Path(self.cache.directory).expanduser())
        if self.encryption.at_rest.key_file:
            self.encryption.at_rest.key_file = str(
                Path(self.encryption.at_rest.key_file).expanduser()
            )
        # Training path defaults
        cache = self.cache.directory
        if not self.training.output_directory:
            self.training.output_directory = str(Path(cache) / "training_outputs")
        if not self.training.dataset_directory:
            self.training.dataset_directory = str(Path(cache) / "datasets")
        return self

    @model_validator(mode="after")
    def _apply_model_defaults(self) -> Settings:
        """Apply server defaults to models that don't specify values."""
        for model in self.models.values():
            if model.gpu_memory_utilization is None:
                model.gpu_memory_utilization = self.defaults.gpu_memory_utilization
            if model.max_model_len is None and model.context.max_model_len is None:
                model.max_model_len = self.defaults.max_model_len
            if model.dtype == "auto":
                model.dtype = self.defaults.dtype
        return self


def load_settings(config_path: str | Path | None = None) -> Settings:
    """Load settings from YAML config file.

    YAML is the single source of truth for all configuration.  Secret values
    can use ``ENV[VAR_NAME]`` or ``ENC[ciphertext]`` patterns — see
    :mod:`lean_ai_serve.security.secrets` for details.
    """
    from lean_ai_serve.security.secrets import resolve_config_secrets

    yaml_data: dict[str, Any] = {}

    if config_path is None:
        # Search common locations
        candidates = [
            Path("config.yaml"),
            Path("config.yml"),
            Path("/etc/lean-ai-serve/config.yaml"),
        ]
        for candidate in candidates:
            if candidate.exists():
                config_path = candidate
                break

    if config_path is not None:
        config_path = Path(config_path)
        if config_path.exists():
            logger.info("Loading config from %s", config_path)
            with open(config_path) as f:
                yaml_data = yaml.safe_load(f) or {}
        else:
            logger.warning("Config file not found: %s — using defaults", config_path)

    # Resolve ENV[] and ENC[] secret references
    encryption_config = yaml_data.get("encryption")
    yaml_data = resolve_config_secrets(yaml_data, encryption_config)

    return Settings(**yaml_data)


# Module-level singleton — initialized lazily or by main.py
_settings: Settings | None = None


def get_settings() -> Settings:
    """Return the global settings instance."""
    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings


def set_settings(settings: Settings) -> None:
    """Replace the global settings instance (used at startup)."""
    global _settings
    _settings = settings
