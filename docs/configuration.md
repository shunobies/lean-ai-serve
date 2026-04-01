# Configuration Reference

Complete reference for the lean-ai-serve YAML configuration system.

---

## Overview

lean-ai-serve uses a single YAML file as its source of truth for all configuration. The file is parsed at startup, validated with Pydantic, and secret references are resolved before the settings object is constructed.

### Config Search Paths

When no explicit `--config` flag is given, lean-ai-serve searches the following locations in order and uses the first file found:

| Priority | Path |
|----------|------|
| 1 | `./config.yaml` |
| 2 | `./config.yml` |
| 3 | `/etc/lean-ai-serve/config.yaml` |

Pass an explicit path to skip the search:

```bash
lean-ai-serve serve --config /opt/lean-ai-serve/config.yaml
```

### Environment Variable Override

There is no implicit environment-variable-to-field mapping. All configuration lives in the YAML file. To inject environment values into specific fields, use the `ENV[VAR_NAME]` secret pattern described below.

### Validation

You can validate a config file without starting the server:

```bash
lean-ai-serve config validate --config /path/to/config.yaml
```

This runs Pydantic schema validation plus semantic checks (e.g. ensuring `issuer_url` is set when OIDC mode is enabled).

To view the fully resolved configuration (with defaults applied and secrets masked):

```bash
lean-ai-serve config show --config /path/to/config.yaml
```

---

## Secret Patterns

Any string value in the YAML file can use one of two patterns to avoid storing secrets in plain text.

### `ENV[VAR_NAME]` -- Environment Variable

Resolved at load time from the named environment variable. The variable must be set or the server will refuse to start.

```yaml
cache:
  huggingface_token: "ENV[HF_TOKEN]"
```

### `ENC[ciphertext]` -- Encrypted Value

Decrypted at load time using the AES-256-GCM master key configured in `encryption.at_rest`. The ciphertext is a base64-encoded blob containing a 96-bit nonce followed by the GCM ciphertext.

```yaml
security:
  jwt_secret: "ENC[base64-encoded-ciphertext]"
```

### Workflow: Setting Up Encrypted Secrets

1. **Generate a master key:**

   ```bash
   lean-ai-serve config generate-key /etc/lean-ai-serve/master.key
   ```

   This creates a 256-bit (32-byte) random key file with permissions set to `600`.

2. **Encrypt a value:**

   ```bash
   lean-ai-serve config encrypt-value "my-secret-value" \
       --key-file /etc/lean-ai-serve/master.key
   ```

   The command outputs an `ENC[...]` string.

3. **Paste into config.yaml:**

   ```yaml
   security:
     jwt_secret: "ENC[output-from-step-2]"
   ```

4. **Configure the key source:**

   ```yaml
   encryption:
     at_rest:
       enabled: true
       key_source: "file"
       key_file: "/etc/lean-ai-serve/master.key"
   ```

5. **Verify:**

   ```bash
   lean-ai-serve config decrypt-value "ENC[...]" \
       --key-file /etc/lean-ai-serve/master.key
   ```

> **Note:** The `encryption` section itself cannot contain `ENC[]` values -- it is the bootstrap for the master key and is skipped during secret resolution.

---

## Configuration Sections

All fields below show their YAML key, type, default value, and a description. Fields marked with `--` for the default are required when the parent feature is enabled.

---

### `server`

HTTP/HTTPS listener settings.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `host` | string | `0.0.0.0` | Bind address for the HTTP server. |
| `port` | int | `8420` | Bind port for the HTTP server. |
| `tls.enabled` | bool | `false` | Enable TLS termination at the server. |
| `tls.cert_file` | string | `""` | Path to the TLS certificate file (PEM). Required when TLS is enabled. |
| `tls.key_file` | string | `""` | Path to the TLS private key file (PEM). Required when TLS is enabled. |

```yaml
server:
  host: "0.0.0.0"
  port: 8420
  tls:
    enabled: true
    cert_file: "/etc/ssl/certs/lean-ai.pem"
    key_file: "/etc/ssl/private/lean-ai.key"
```

---

### `security`

Authentication mode, JWT settings, and sub-sections for LDAP, OIDC, and content filtering.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `mode` | string | `api_key` | Authentication mode. One of: `api_key`, `ldap`, `oidc`, `ldap+api_key`, `oidc+api_key`. |
| `jwt_secret` | string | `""` | Secret key for signing JWT tokens. Auto-generated at startup if empty (sessions will not survive restarts). Supports `ENV[]` and `ENC[]` patterns. |
| `jwt_expiry_hours` | float | `8.0` | JWT token lifetime in hours. |

---

### `security.ldap`

LDAP/Active Directory authentication. Used when `security.mode` includes `ldap`.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `server_url` | string | `""` | LDAP server URL (e.g. `ldaps://ad.corp.com:636`). |
| `bind_dn` | string | `""` | Distinguished name for the bind account. |
| `bind_password_env` | string | `LEAN_AI_LDAP_BIND_PASSWORD` | Environment variable holding the bind password. |
| `user_search_base` | string | `""` | Base DN for user searches. |
| `user_search_filter` | string | `(sAMAccountName={username})` | LDAP filter for user lookup. `{username}` is replaced at runtime. |
| `group_search_base` | string | `""` | Base DN for group searches. |
| `group_role_mapping` | dict | `{}` | Maps LDAP group DNs to application roles. |
| `default_role` | string | `user` | Role assigned when no group mapping matches. |
| `cache_ttl` | int | `300` | Seconds to cache LDAP authentication results. |
| `connection_pool_size` | int | `5` | Number of persistent LDAP connections in the pool. |

```yaml
security:
  mode: "ldap"
  ldap:
    server_url: "ldaps://ad.corp.com:636"
    bind_dn: "CN=svc-lean-ai,OU=ServiceAccounts,DC=corp,DC=com"
    bind_password_env: "LEAN_AI_LDAP_BIND_PASSWORD"
    user_search_base: "OU=Users,DC=corp,DC=com"
    user_search_filter: "(sAMAccountName={username})"
    group_search_base: "OU=Groups,DC=corp,DC=com"
    group_role_mapping:
      "CN=AI-Admins,OU=Groups,DC=corp,DC=com": "admin"
      "CN=AI-Users,OU=Groups,DC=corp,DC=com": "user"
    default_role: "user"
    cache_ttl: 300
    connection_pool_size: 5
```

---

### `security.oidc`

OpenID Connect authentication. Used when `security.mode` includes `oidc`.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `issuer_url` | string | `""` | OIDC issuer URL (e.g. `https://keycloak.corp.com/realms/ai`). |
| `client_id` | string | `""` | OAuth2 client ID. |
| `audience` | string | `""` | Expected JWT audience claim. |
| `roles_claim` | string | `realm_access.roles` | Dot-notation path to the roles array in the JWT payload. |
| `role_mapping` | dict | `{}` | Maps IdP role names to lean-ai-serve roles. |
| `default_role` | string | `user` | Fallback role when no mapping matches. |
| `jwks_cache_ttl` | int | `3600` | Seconds to cache the JWKS key set from the IdP. |

```yaml
security:
  mode: "oidc"
  oidc:
    issuer_url: "https://keycloak.corp.com/realms/ai"
    client_id: "lean-ai-serve"
    audience: "lean-ai-serve"
    roles_claim: "realm_access.roles"
    role_mapping:
      "keycloak-admin": "admin"
      "keycloak-user": "user"
    default_role: "user"
    jwks_cache_ttl: 3600
```

---

### `security.content_filtering`

Regex-based content filtering for detecting sensitive data (e.g. PHI, PII) in prompts and responses.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | `false` | Enable content filtering. |
| `patterns` | list | `[]` | List of filter pattern objects (see below). |
| `custom_patterns_file` | string | `""` | Path to an external file containing additional patterns. |

Each entry in `patterns` has:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `name` | string | -- | Human-readable name for the pattern. |
| `pattern` | string | -- | Regular expression to match. |
| `action` | string | `warn` | Action on match: `warn`, `redact`, or `block`. |

```yaml
security:
  content_filtering:
    enabled: true
    patterns:
      - name: "SSN"
        pattern: '\b\d{3}-\d{2}-\d{4}\b'
        action: "warn"
      - name: "MRN"
        pattern: '\bMRN[:\s]?\d{6,}\b'
        action: "block"
```

---

### `audit`

Audit logging for prompts and API activity.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | `true` | Enable audit logging. |
| `log_prompts` | bool | `true` | Log full prompt and response content. |
| `log_prompts_hash_only` | bool | `false` | Store only a SHA-256 hash of prompts instead of full content. Takes precedence over `log_prompts` when both are `true`. |
| `retention_days` | int | `2190` | Days to retain audit records. Default is 6 years (HIPAA minimum). |
| `storage` | string | `sqlite` | Audit storage backend: `sqlite` or `file`. |

```yaml
audit:
  enabled: true
  log_prompts: true
  log_prompts_hash_only: false
  retention_days: 2190
  storage: "sqlite"
```

---

### `encryption.at_rest`

Master key configuration for decrypting `ENC[]` config values and encrypting audit data at rest.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | `false` | Enable encryption at rest. |
| `key_source` | string | `file` | Where to load the master key from: `file`, `env`, or `vault`. |
| `key_file` | string | `""` | Path to the 256-bit master key file. Used when `key_source` is `file`. |
| `key_env_var` | string | `LEAN_AI_ENCRYPTION_KEY` | Environment variable holding the key (hex or base64). Used when `key_source` is `env`. |
| `vault_path` | string | `secret/data/lean-ai-serve/encryption-key` | Vault secret path. Used when `key_source` is `vault`. |
| `vault_key_field` | string | `key` | Field name within the Vault secret that holds the key. |
| `vault_auth_method` | string | `token` | Vault authentication method: `token` or `approle`. |
| `vault_role_id_env` | string | `VAULT_ROLE_ID` | Environment variable for the AppRole role ID. |
| `vault_secret_id_env` | string | `VAULT_SECRET_ID` | Environment variable for the AppRole secret ID. |
| `vault_cache_ttl` | int | `300` | Seconds to cache the key fetched from Vault. |

```yaml
encryption:
  at_rest:
    enabled: true
    key_source: "vault"
    vault_path: "secret/data/lean-ai-serve/encryption-key"
    vault_key_field: "key"
    vault_auth_method: "approle"
    vault_role_id_env: "VAULT_ROLE_ID"
    vault_secret_id_env: "VAULT_SECRET_ID"
    vault_cache_ttl: 300
```

---

### `cache`

Local cache and model download settings.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `directory` | string | `~/.cache/lean-ai-serve` | Base directory for cached models, datasets, and training outputs. `~` is expanded at load time. |
| `huggingface_token` | string | `""` | HuggingFace API token for downloading gated models. Supports `ENV[]` and `ENC[]` patterns. |

```yaml
cache:
  directory: "~/.cache/lean-ai-serve"
  huggingface_token: "ENV[HF_TOKEN]"
```

---

### `defaults`

Global defaults applied to all models that do not specify their own values.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `gpu_memory_utilization` | float | `0.90` | Fraction of GPU memory to allocate (0.0--1.0). |
| `max_model_len` | int or null | `null` | Maximum sequence length. `null` uses the model's built-in maximum. |
| `dtype` | string | `auto` | Data type for model weights: `auto`, `float16`, `bfloat16`, `float32`. |

```yaml
defaults:
  gpu_memory_utilization: 0.85
  max_model_len: 32768
  dtype: "auto"
```

---

### `models`

A dictionary of named model configurations. Each key is a model alias used in API requests.

#### Top-level model fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `source` | string | -- | HuggingFace repo ID or local path to the model. **Required.** |
| `gpu` | list[int] | `[0]` | GPU device indices to use. |
| `tensor_parallel_size` | int | `1` | Number of GPUs for tensor parallelism. |
| `pipeline_parallel_size` | int | `1` | Number of GPUs for pipeline parallelism. |
| `max_model_len` | int or null | `null` | Override maximum sequence length. `null` uses the model default or `defaults.max_model_len`. |
| `dtype` | string | `auto` | Data type override. `auto` inherits from `defaults.dtype`. |
| `quantization` | string or null | `null` | Quantization method (e.g. `awq`, `gptq`, `squeezellm`). |
| `tool_call_parser` | string or null | `null` | Tool/function call parser (e.g. `hermes`, `mistral`). |
| `reasoning_parser` | string or null | `null` | Reasoning/chain-of-thought parser (e.g. `qwen3`). |
| `guided_decoding_backend` | string | `xgrammar` | Backend for structured/guided decoding. |
| `enable_lora` | bool | `false` | Enable LoRA adapter support for this model. |
| `max_loras` | int | `4` | Maximum number of concurrent LoRA adapters. |
| `max_lora_rank` | int | `64` | Maximum LoRA rank supported. |
| `gpu_memory_utilization` | float or null | `null` | Per-model GPU memory fraction. `null` inherits from `defaults.gpu_memory_utilization`. |
| `autoload` | bool | `false` | Automatically load this model at server startup. |
| `task` | string | `chat` | Model task type: `chat`, `embed`, or `generate`. |

#### `models.<name>.kv_cache`

KV cache configuration for the model.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `dtype` | string | `auto` | KV cache data type: `auto`, `fp8`, `fp8_e4m3`, `fp8_e5m2`, `turboquant`. |
| `calculate_scales` | bool | `false` | Calculate KV cache quantization scales (useful for FP8). |
| `turboquant_bits` | float | `3.0` | Bit width for TurboQuant KV cache compression. |

#### `models.<name>.context`

Context window and memory management settings.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `max_model_len` | int or null | `null` | Context-level max sequence length override. |
| `cpu_offload_gb` | float | `0.0` | Gigabytes of KV cache to offload to CPU memory. |
| `swap_space` | float | `4.0` | GiB of CPU swap space for KV cache. |
| `enable_prefix_caching` | bool | `true` | Enable automatic prefix caching for repeated prompt prefixes. |
| `prefix_caching_hash` | string | `sha256` | Hash algorithm for prefix cache keys. |
| `max_num_batched_tokens` | int or null | `null` | Maximum tokens per batch iteration. `null` uses the engine default. |
| `rope_scaling` | dict or null | `null` | RoPE scaling configuration (e.g. `{"type": "yarn", "factor": 4.0}`). |
| `rope_theta` | float or null | `null` | Override for the RoPE theta base frequency. |

#### `models.<name>.speculative`

Speculative decoding configuration.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | `false` | Enable speculative decoding. |
| `strategy` | string | `draft` | Speculative strategy: `draft`, `ngram`, or `eagle`. |
| `draft_model` | string or null | `null` | HuggingFace repo ID or path to the draft model. Required for `draft` and `eagle` strategies. |
| `num_tokens` | int | `5` | Number of tokens to speculate ahead. |
| `draft_tensor_parallel_size` | int | `1` | Tensor parallel size for the draft model. |

#### `models.<name>.sampling_defaults`

Default sampling parameters applied to requests that do not specify their own.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `temperature` | float or null | `null` | Sampling temperature. `null` uses the engine default. |
| `top_p` | float or null | `null` | Top-p (nucleus) sampling threshold. |
| `top_k` | int or null | `null` | Top-k sampling cutoff. |
| `repetition_penalty` | float or null | `null` | Repetition penalty factor. |
| `min_p` | float or null | `null` | Min-p sampling threshold. |

#### `models.<name>.lifecycle`

Idle sleep and auto-wake behavior.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `idle_sleep_timeout` | int | `0` | Seconds of idle time before sleeping the model. `0` disables idle sleep. |
| `sleep_level` | int | `1` | Sleep depth: `1` = auto-wake capable (weights stay in memory), `2` = full unload. |
| `auto_wake_on_request` | bool | `true` | Automatically wake the model when an inference request arrives. Only applies at `sleep_level` 1. |

#### Full model example

```yaml
models:
  qwen3-coder-30b:
    source: "Qwen/Qwen3-Coder-30B-A3B"
    gpu: [0, 1]
    tensor_parallel_size: 2
    max_model_len: 131072
    tool_call_parser: "hermes"
    reasoning_parser: "qwen3"
    enable_lora: true
    max_loras: 4
    autoload: true
    task: "chat"
    kv_cache:
      dtype: "fp8_e4m3"
      calculate_scales: true
    context:
      enable_prefix_caching: true
      cpu_offload_gb: 0
      swap_space: 4.0
    speculative:
      enabled: false
    sampling_defaults:
      temperature: 0.7
      top_p: 0.9
    lifecycle:
      idle_sleep_timeout: 3600
      sleep_level: 1
      auto_wake_on_request: true

  bge-embed:
    source: "BAAI/bge-large-en-v1.5"
    gpu: [0]
    autoload: true
    task: "embed"
```

---

### `training`

Fine-tuning subsystem configuration.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | `false` | Enable the training subsystem. |
| `backend` | string | `llama-factory` | Training backend. Currently only `llama-factory` is supported. |
| `output_directory` | string | `""` | Directory for training outputs. Empty string defaults to `{cache.directory}/training_outputs`. |
| `max_concurrent_jobs` | int | `1` | Maximum number of concurrent training jobs. |
| `default_gpu` | list[int] | `[0]` | GPU indices assigned to training jobs that do not specify their own. |
| `dataset_directory` | string | `""` | Directory for uploaded datasets. Empty string defaults to `{cache.directory}/datasets`. |
| `max_dataset_size_mb` | int | `1024` | Maximum upload size per dataset in megabytes. |

```yaml
training:
  enabled: true
  backend: "llama-factory"
  max_concurrent_jobs: 2
  default_gpu: [2, 3]
  max_dataset_size_mb: 2048
```

---

### `metrics`

Prometheus-compatible metrics endpoint.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | `true` | Enable the `/metrics` endpoint. |
| `gpu_poll_interval` | int | `30` | Seconds between GPU metric collection snapshots. |

```yaml
metrics:
  enabled: true
  gpu_poll_interval: 15
```

---

### `logging`

Structured logging configuration.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `json_output` | bool | `true` | `true` for JSON-lines output (production), `false` for human-readable console output (development). |
| `level` | string | `INFO` | Log level: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`. |

```yaml
logging:
  json_output: false
  level: "DEBUG"
```

---

### `alerts`

Threshold-based alerting that evaluates collected metrics on a periodic interval.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | `true` | Enable the alerting subsystem. Requires `metrics.enabled` to be `true`. |
| `evaluation_interval` | int | `60` | Seconds between alert rule evaluations. |
| `rules` | list | `[]` | List of alert rule objects (see below). |

Each entry in `rules` has:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `name` | string | -- | Human-readable name for the alert rule. |
| `metric` | string | -- | Metric name to evaluate (e.g. `gpu_memory_used_pct`). |
| `condition` | string | `gt` | Comparison operator: `gt`, `lt`, `gte`, `lte`, `eq`. |
| `threshold` | float | `0.0` | Threshold value to compare against. |
| `severity` | string | `warning` | Alert severity: `info`, `warning`, `critical`. |
| `message` | string | `""` | Custom alert message template. |

```yaml
alerts:
  enabled: true
  evaluation_interval: 60
  rules:
    - name: "high_gpu_memory"
      metric: "gpu_memory_used_pct"
      condition: "gt"
      threshold: 90.0
      severity: "warning"
      message: "GPU memory usage exceeds 90%"
    - name: "low_gpu_utilization"
      metric: "gpu_utilization_pct"
      condition: "lt"
      threshold: 10.0
      severity: "info"
      message: "GPU utilization below 10% -- consider sleeping idle models"
```

---

### `tracing`

OpenTelemetry distributed tracing.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | `false` | Enable OpenTelemetry tracing. Requires the `tracing` extra: `pip install lean-ai-serve[tracing]`. |
| `endpoint` | string | `""` | OTLP exporter endpoint (e.g. `http://otel-collector:4317`). |
| `protocol` | string | `grpc` | OTLP transport protocol: `grpc` or `http`. |
| `service_name` | string | `lean-ai-serve` | Service name reported in traces. |

```yaml
tracing:
  enabled: true
  endpoint: "http://otel-collector:4317"
  protocol: "grpc"
  service_name: "lean-ai-serve"
```

---

### `context_compression`

Prompt context compression to reduce token usage for long inputs.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | `false` | Enable context compression. Requires: `pip install llmlingua`. |
| `method` | string | `llmlingua2` | Compression algorithm. |
| `target_ratio` | float | `0.5` | Target compression ratio (0.0--1.0). Lower values produce more aggressive compression. |
| `min_length` | int | `4096` | Skip compression for prompts shorter than this many characters. |

```yaml
context_compression:
  enabled: true
  method: "llmlingua2"
  target_ratio: 0.5
  min_length: 4096
```

---

## Common Configuration Examples

### Minimal: Single GPU, API Key Auth

```yaml
server:
  port: 8420

security:
  mode: "api_key"

models:
  llama3:
    source: "meta-llama/Meta-Llama-3-8B-Instruct"
    gpu: [0]
    autoload: true
```

### Production: OIDC + TLS + Encrypted Secrets + Vault

```yaml
server:
  host: "0.0.0.0"
  port: 443
  tls:
    enabled: true
    cert_file: "/etc/ssl/certs/lean-ai.pem"
    key_file: "/etc/ssl/private/lean-ai.key"

security:
  mode: "oidc"
  jwt_secret: "ENC[base64-ciphertext-here]"
  jwt_expiry_hours: 4.0
  oidc:
    issuer_url: "https://keycloak.corp.com/realms/ai"
    client_id: "lean-ai-serve"
    audience: "lean-ai-serve"
    role_mapping:
      "keycloak-admin": "admin"
      "keycloak-model-manager": "model-manager"
      "keycloak-user": "user"
  content_filtering:
    enabled: true
    patterns:
      - name: "SSN"
        pattern: '\b\d{3}-\d{2}-\d{4}\b'
        action: "block"

audit:
  enabled: true
  log_prompts: true
  log_prompts_hash_only: false
  retention_days: 2190

encryption:
  at_rest:
    enabled: true
    key_source: "vault"
    vault_path: "secret/data/lean-ai-serve/encryption-key"
    vault_key_field: "key"
    vault_auth_method: "approle"
    vault_role_id_env: "VAULT_ROLE_ID"
    vault_secret_id_env: "VAULT_SECRET_ID"

cache:
  directory: "/data/lean-ai-serve/cache"
  huggingface_token: "ENV[HF_TOKEN]"

defaults:
  gpu_memory_utilization: 0.90
  dtype: "auto"

models:
  qwen3-coder:
    source: "Qwen/Qwen3-Coder-30B-A3B"
    gpu: [0, 1]
    tensor_parallel_size: 2
    max_model_len: 131072
    tool_call_parser: "hermes"
    reasoning_parser: "qwen3"
    enable_lora: true
    autoload: true
    kv_cache:
      dtype: "fp8_e4m3"
      calculate_scales: true
    context:
      enable_prefix_caching: true
    lifecycle:
      idle_sleep_timeout: 3600
      sleep_level: 1
      auto_wake_on_request: true

metrics:
  enabled: true
  gpu_poll_interval: 15

logging:
  json_output: true
  level: "INFO"

alerts:
  enabled: true
  evaluation_interval: 60
  rules:
    - name: "high_gpu_memory"
      metric: "gpu_memory_used_pct"
      condition: "gt"
      threshold: 90.0
      severity: "warning"

tracing:
  enabled: true
  endpoint: "http://otel-collector:4317"
  protocol: "grpc"
  service_name: "lean-ai-serve"
```

### Development: Console Logging, No Auth

```yaml
server:
  port: 8420

security:
  mode: "api_key"

logging:
  json_output: false
  level: "DEBUG"

metrics:
  enabled: true
  gpu_poll_interval: 10

defaults:
  gpu_memory_utilization: 0.70

models:
  dev-model:
    source: "microsoft/Phi-3-mini-4k-instruct"
    gpu: [0]
    autoload: true
    task: "chat"
```

### Multi-Model with Training

```yaml
defaults:
  gpu_memory_utilization: 0.85

models:
  chat-model:
    source: "meta-llama/Meta-Llama-3-8B-Instruct"
    gpu: [0]
    autoload: true
    task: "chat"
    enable_lora: true
    lifecycle:
      idle_sleep_timeout: 1800
      sleep_level: 1

  embed-model:
    source: "BAAI/bge-large-en-v1.5"
    gpu: [1]
    autoload: true
    task: "embed"

training:
  enabled: true
  backend: "llama-factory"
  max_concurrent_jobs: 1
  default_gpu: [2]
  max_dataset_size_mb: 2048
```

---

## Canonical Reference

See [`config.example.yaml`](../config.example.yaml) in the repository root for a fully commented configuration file showing all available options with their defaults.
