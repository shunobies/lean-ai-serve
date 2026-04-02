# lean-ai-serve HTTP API Reference

Base URL: `http://localhost:8420`

All request and response bodies use `application/json` unless otherwise noted.
Authenticated endpoints require an `Authorization: Bearer <token>` header.

---

## Client Libraries

lean-ai-serve exposes a fully **OpenAI-compatible API**, so any OpenAI SDK or client library works out of the box — just point the base URL to your lean-ai-serve instance.

### lean-ai (recommended)

[**lean-ai**](https://github.com/shunobies/lean-ai) is the companion agentic coding assistant that integrates natively with lean-ai-serve. Instead of writing custom HTTP handlers, lean-ai provides a full-featured AI development environment with a VS Code extension, multi-turn planning workflows, codebase indexing, and tool-augmented code generation — all backed by models served from lean-ai-serve.

**Setup:**

```bash
# Install lean-ai backend
cd lean-ai/backend
pip install -e ".[dev,openai]"
```

**Configure lean-ai-serve as the LLM provider** in lean-ai's `config.yaml`:

```yaml
# Use lean-ai-serve as the primary provider
llm_provider: serve
serve_url: "http://localhost:8420"
serve_api_key: "las-your-api-key"
serve_model: "qwen3-coder-30b"
serve_temperature: 0.7

# Or use it as the expert model alongside a local model
llm_provider: ollama
ollama_model: "qwen3-coder:8b"
expert_llm_provider: serve
serve_api_key: "las-your-api-key"
serve_expert_model: "qwen3-coder-30b"
```

See the [lean-ai README](https://github.com/shunobies/lean-ai) for full documentation.

### OpenAI SDK

Any OpenAI-compatible client works by setting the base URL:

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8420/v1",
    api_key="las-your-api-key",
)

response = client.chat.completions.create(
    model="qwen3-coder-30b",
    messages=[{"role": "user", "content": "Hello!"}],
)
```

### curl

All examples in this reference use curl. See individual endpoint sections below.

---

## Error Responses

All endpoints return errors in a consistent format:

```json
{
  "error": "Short error code",
  "detail": "Human-readable explanation"
}
```

| Status | Meaning |
|--------|---------|
| 400 | Bad request — malformed JSON, missing required fields, invalid parameter values |
| 401 | Unauthorized — missing or expired token |
| 403 | Forbidden — valid token but insufficient permissions, or model not in allowed list |
| 404 | Not found — model, dataset, job, adapter, or key does not exist |
| 409 | Conflict — model already loaded, job already running, or other state conflict |
| 429 | Rate limited — per-key or per-user rate limit exceeded. Check `Retry-After` header |
| 503 | Model waking up — auto-wake in progress. Check `Retry-After` header for estimated wait |

---

## Health and Status

### GET /health

Returns server health. **No authentication required.**

**Response:**

```json
{
  "status": "healthy",
  "version": "0.8.0",
  "models_loaded": 2,
  "ready": true,
  "checks": {
    "vllm": "ok",
    "disk": "ok",
    "gpu": "ok"
  }
}
```

**Example:**

```bash
curl http://localhost:8420/health
```

---

### GET /api/status

Returns detailed server status including GPU and model information.

**Permission:** `model:read`

**Response:**

```json
{
  "status": "running",
  "version": "0.8.0",
  "gpus": [
    {
      "index": 0,
      "name": "NVIDIA A100 80GB",
      "memory_total_mb": 81920,
      "memory_used_mb": 32456,
      "utilization_pct": 45
    }
  ],
  "models": [
    {
      "name": "mistral-7b",
      "state": "loaded",
      "port": 8001,
      "gpu_indices": [0]
    }
  ],
  "uptime_seconds": 86421
}
```

**Example:**

```bash
curl -H "Authorization: Bearer $TOKEN" \
  http://localhost:8420/api/status
```

---

### GET /api/gpu

Returns GPU device information.

**Permission:** `model:read`

**Response:**

```json
{
  "gpus": [
    {
      "index": 0,
      "name": "NVIDIA A100 80GB",
      "memory_total_mb": 81920,
      "memory_used_mb": 32456,
      "utilization_pct": 45
    }
  ]
}
```

**Example:**

```bash
curl -H "Authorization: Bearer $TOKEN" \
  http://localhost:8420/api/gpu
```

---

## OpenAI-Compatible Endpoints

These endpoints provide compatibility with the OpenAI API format. If the
requested model is sleeping with `auto_wake` enabled, the server returns
`503` with a `Retry-After: 30` header while the model starts up.

### POST /v1/chat/completions

Proxies chat completion requests to the underlying vLLM instance. Supports
streaming via `stream: true`.

**Permission:** `inference:call`

**Request body:**

```json
{
  "model": "mistral-7b",
  "messages": [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "Explain gradient descent in two sentences."}
  ],
  "max_tokens": 256,
  "temperature": 0.7,
  "stream": false
}
```

**Response (non-streaming):**

```json
{
  "id": "chatcmpl-abc123",
  "object": "chat.completion",
  "created": 1711929600,
  "model": "mistral-7b",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "Gradient descent is an optimization algorithm..."
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 28,
    "completion_tokens": 42,
    "total_tokens": 70
  }
}
```

When `stream: true`, the response is a series of `text/event-stream` chunks
following the OpenAI SSE format, each prefixed with `data: `.

**Example (non-streaming):**

```bash
curl -X POST http://localhost:8420/v1/chat/completions \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "mistral-7b",
    "messages": [{"role": "user", "content": "Hello"}],
    "max_tokens": 128
  }'
```

**Example (streaming):**

```bash
curl -X POST http://localhost:8420/v1/chat/completions \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -N \
  -d '{
    "model": "mistral-7b",
    "messages": [{"role": "user", "content": "Hello"}],
    "max_tokens": 128,
    "stream": true
  }'
```

---

### POST /v1/completions

Text and fill-in-the-middle (FIM) completions.

**Permission:** `inference:call`

**Request body:**

```json
{
  "model": "mistral-7b",
  "prompt": "The capital of France is",
  "max_tokens": 64,
  "temperature": 0.0
}
```

**Response:**

```json
{
  "id": "cmpl-xyz789",
  "object": "text_completion",
  "created": 1711929600,
  "model": "mistral-7b",
  "choices": [
    {
      "index": 0,
      "text": " Paris, which is also the largest city in France.",
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 7,
    "completion_tokens": 12,
    "total_tokens": 19
  }
}
```

**Example:**

```bash
curl -X POST http://localhost:8420/v1/completions \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "mistral-7b",
    "prompt": "The capital of France is",
    "max_tokens": 64
  }'
```

---

### POST /v1/embeddings

Generate embeddings for the given input text.

**Permission:** `inference:call`

**Request body:**

```json
{
  "model": "bge-large-en",
  "input": "The quick brown fox jumps over the lazy dog."
}
```

The `input` field accepts a single string or an array of strings.

**Response:**

```json
{
  "object": "list",
  "data": [
    {
      "object": "embedding",
      "index": 0,
      "embedding": [0.0023, -0.0091, 0.0152, "..."]
    }
  ],
  "model": "bge-large-en",
  "usage": {
    "prompt_tokens": 10,
    "total_tokens": 10
  }
}
```

**Example:**

```bash
curl -X POST http://localhost:8420/v1/embeddings \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "bge-large-en",
    "input": "Hello world"
  }'
```

---

### GET /v1/models

List all models available for inference.

**Permission:** `inference:call`

**Response:**

```json
{
  "object": "list",
  "data": [
    {
      "id": "mistral-7b",
      "object": "model",
      "created": 1711929600,
      "owned_by": "lean-ai-serve"
    },
    {
      "id": "bge-large-en",
      "object": "model",
      "created": 1711929600,
      "owned_by": "lean-ai-serve"
    }
  ]
}
```

**Example:**

```bash
curl -H "Authorization: Bearer $TOKEN" \
  http://localhost:8420/v1/models
```

---

## Model Management

### GET /api/models

List all registered models and their current state.

**Permission:** `model:read`

**Response:**

```json
{
  "models": [
    {
      "name": "mistral-7b",
      "source": "mistralai/Mistral-7B-Instruct-v0.3",
      "state": "loaded",
      "port": 8001,
      "gpu_indices": [0],
      "pid": 12345,
      "auto_wake": true,
      "loaded_at": "2026-03-30T14:22:00Z"
    }
  ]
}
```

**Example:**

```bash
curl -H "Authorization: Bearer $TOKEN" \
  http://localhost:8420/api/models
```

---

### GET /api/models/{name}

Get detailed information about a single model.

**Permission:** `model:read`

**Path parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| name | string | Model name |

**Response:**

```json
{
  "name": "mistral-7b",
  "source": "mistralai/Mistral-7B-Instruct-v0.3",
  "state": "loaded",
  "port": 8001,
  "gpu_indices": [0],
  "pid": 12345,
  "auto_wake": true,
  "loaded_at": "2026-03-30T14:22:00Z"
}
```

**Example:**

```bash
curl -H "Authorization: Bearer $TOKEN" \
  http://localhost:8420/api/models/mistral-7b
```

---

### POST /api/models/pull

Pull a model from a remote source. Returns a server-sent event (SSE) stream
of download progress updates.

**Permission:** `model:write`

**Request body:**

```json
{
  "source": "mistralai/Mistral-7B-Instruct-v0.3",
  "name": "mistral-7b",
  "revision": "main"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| source | string | yes | HuggingFace repo ID or other model source |
| name | string | no | Local alias for the model (defaults to source basename) |
| revision | string | no | Branch, tag, or commit hash (defaults to `main`) |

**Response (SSE stream):**

```
data: {"file": "model-00001-of-00002.safetensors", "downloaded_mb": 512, "total_mb": 4096, "pct": 12.5}

data: {"file": "model-00001-of-00002.safetensors", "downloaded_mb": 4096, "total_mb": 4096, "pct": 100.0}

data: {"status": "complete", "name": "mistral-7b", "size_gb": 14.2}
```

**Example:**

```bash
curl -X POST http://localhost:8420/api/models/pull \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -N \
  -d '{
    "source": "mistralai/Mistral-7B-Instruct-v0.3",
    "name": "mistral-7b"
  }'
```

---

### POST /api/models/{name}/load

Load a model onto GPU(s) and start the vLLM process.

**Permission:** `model:deploy`

**Path parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| name | string | Model name |

**Response:**

```json
{
  "status": "loaded",
  "port": 8001,
  "pid": 12345
}
```

Returns `409` if the model is already loaded.

**Example:**

```bash
curl -X POST http://localhost:8420/api/models/mistral-7b/load \
  -H "Authorization: Bearer $TOKEN"
```

---

### POST /api/models/{name}/unload

Stop the vLLM process and release GPU resources.

**Permission:** `model:deploy`

**Path parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| name | string | Model name |

**Response:**

```json
{
  "status": "unloaded"
}
```

**Example:**

```bash
curl -X POST http://localhost:8420/api/models/mistral-7b/unload \
  -H "Authorization: Bearer $TOKEN"
```

---

### POST /api/models/{name}/sleep

Put a loaded model to sleep, releasing GPU memory but keeping configuration
ready for fast restart.

**Permission:** `model:deploy`

**Path parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| name | string | Model name |

**Response:**

```json
{
  "status": "sleeping"
}
```

**Example:**

```bash
curl -X POST http://localhost:8420/api/models/mistral-7b/sleep \
  -H "Authorization: Bearer $TOKEN"
```

---

### POST /api/models/{name}/wake

Wake a sleeping model and reload it onto GPU(s).

**Permission:** `model:deploy`

**Path parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| name | string | Model name |

**Response:**

```json
{
  "status": "loaded",
  "port": 8001
}
```

**Example:**

```bash
curl -X POST http://localhost:8420/api/models/mistral-7b/wake \
  -H "Authorization: Bearer $TOKEN"
```

---

### DELETE /api/models/{name}

Delete a model from the local registry and remove its files from disk.
The model must be unloaded first.

**Permission:** `model:write`

**Path parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| name | string | Model name |

**Response:**

```json
{
  "status": "deleted"
}
```

Returns `409` if the model is still loaded.

**Example:**

```bash
curl -X DELETE http://localhost:8420/api/models/mistral-7b \
  -H "Authorization: Bearer $TOKEN"
```

---

## Authentication

### POST /api/auth/login

Authenticate with username and password to obtain a JWT.

**Permission:** none

**Request body:**

```json
{
  "username": "admin",
  "password": "s3cret"
}
```

**Response:**

```json
{
  "token": "eyJhbGciOiJIUzI1NiIs...",
  "expires_at": "2026-04-02T10:00:00Z",
  "user": "admin",
  "roles": ["admin"]
}
```

Returns `401` if credentials are invalid.

**Example:**

```bash
curl -X POST http://localhost:8420/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username": "admin", "password": "s3cret"}'
```

---

### POST /api/auth/refresh

Refresh an existing valid JWT to extend its expiration.

**Permission:** valid JWT required

**Response:**

```json
{
  "token": "eyJhbGciOiJIUzI1NiIs...",
  "expires_at": "2026-04-02T10:00:00Z",
  "user": "admin",
  "roles": ["admin"]
}
```

**Example:**

```bash
curl -X POST http://localhost:8420/api/auth/refresh \
  -H "Authorization: Bearer $TOKEN"
```

---

### POST /api/auth/logout

Revoke the current JWT, adding it to the server-side deny list.

**Permission:** valid JWT required

**Response:**

```json
{
  "status": "logged_out"
}
```

**Example:**

```bash
curl -X POST http://localhost:8420/api/auth/logout \
  -H "Authorization: Bearer $TOKEN"
```

---

### GET /api/auth/me

Return profile information for the currently authenticated user.

**Permission:** valid JWT required

**Response:**

```json
{
  "user_id": "admin",
  "display_name": "Admin User",
  "roles": ["admin"],
  "allowed_models": ["*"],
  "auth_method": "local"
}
```

**Example:**

```bash
curl -H "Authorization: Bearer $TOKEN" \
  http://localhost:8420/api/auth/me
```

---

## API Keys

### POST /api/keys

Create a new API key.

**Permission:** `model:write`

**Request body:**

```json
{
  "name": "ci-pipeline",
  "role": "inference",
  "models": ["mistral-7b"],
  "rate_limit": 100,
  "expires_days": 90
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| name | string | yes | Human-readable label for the key |
| role | string | yes | Role to assign (e.g. `inference`, `admin`) |
| models | string[] | no | Restrict key to specific models. Omit for all models |
| rate_limit | integer | no | Max requests per minute. Omit for default limit |
| expires_days | integer | no | Days until expiration. Omit for no expiration |

**Response (201):**

```json
{
  "id": "key_8f3a1b2c",
  "key": "las_k_abc123def456...",
  "name": "ci-pipeline",
  "role": "inference"
}
```

The `key` field is returned only at creation time. Store it securely.

**Example:**

```bash
curl -X POST http://localhost:8420/api/keys \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "ci-pipeline",
    "role": "inference",
    "models": ["mistral-7b"],
    "expires_days": 90
  }'
```

---

### GET /api/keys

List all API keys. Secret values are not included.

**Permission:** `model:write`

**Response:**

```json
[
  {
    "id": "key_8f3a1b2c",
    "name": "ci-pipeline",
    "role": "inference",
    "models": ["mistral-7b"],
    "rate_limit": 100,
    "created_at": "2026-03-30T12:00:00Z",
    "expires_at": "2026-06-28T12:00:00Z",
    "last_used_at": "2026-03-31T18:42:00Z"
  }
]
```

**Example:**

```bash
curl -H "Authorization: Bearer $TOKEN" \
  http://localhost:8420/api/keys
```

---

### DELETE /api/keys/{key_id}

Revoke an API key. The key becomes immediately unusable.

**Permission:** `model:write`

**Path parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| key_id | string | The key ID returned at creation |

**Response:**

```json
{
  "status": "revoked"
}
```

**Example:**

```bash
curl -X DELETE http://localhost:8420/api/keys/key_8f3a1b2c \
  -H "Authorization: Bearer $TOKEN"
```

---

## Audit

### GET /api/audit/logs

Query the tamper-evident audit log.

**Permission:** `audit:read`

**Query parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| user_id | string | | Filter by user |
| action | string | | Filter by action type (e.g. `model.load`, `inference.call`) |
| model | string | | Filter by model name |
| from_time | string | | ISO 8601 start time |
| to_time | string | | ISO 8601 end time |
| limit | integer | 100 | Max entries to return |
| offset | integer | 0 | Pagination offset |

**Response:**

```json
{
  "entries": [
    {
      "id": "aud_001",
      "timestamp": "2026-03-31T18:42:00Z",
      "user_id": "admin",
      "action": "model.load",
      "model": "mistral-7b",
      "detail": "Loaded on GPU 0",
      "ip": "192.168.1.10"
    }
  ],
  "total": 247
}
```

**Example:**

```bash
curl -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8420/api/audit/logs?action=model.load&limit=10"
```

---

### GET /api/audit/verify

Verify the integrity of the audit log hash chain.

**Permission:** `audit:read`

**Query parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| limit | integer | 1000 | Number of recent entries to verify |

**Response:**

```json
{
  "valid": true,
  "message": "All 1000 entries verified"
}
```

If tampering is detected:

```json
{
  "valid": false,
  "message": "Hash chain broken at entry aud_482"
}
```

**Example:**

```bash
curl -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8420/api/audit/verify?limit=5000"
```

---

## Usage

### GET /api/usage

Query per-hour usage records. Useful for billing and capacity planning.

**Permission:** `usage:read`

**Query parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| user_id | string | | Filter by user |
| model | string | | Filter by model name |
| from_hour | string | | ISO 8601 start hour |
| to_hour | string | | ISO 8601 end hour |
| limit | integer | 168 | Max records to return (168 = one week of hours) |

**Response:**

```json
{
  "records": [
    {
      "hour": "2026-03-31T18:00:00Z",
      "user_id": "admin",
      "model": "mistral-7b",
      "request_count": 142,
      "prompt_tokens": 28400,
      "completion_tokens": 15600,
      "total_tokens": 44000
    }
  ],
  "count": 24
}
```

**Example:**

```bash
curl -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8420/api/usage?model=mistral-7b&limit=24"
```

---

### GET /api/usage/me

Get a usage summary for the currently authenticated user.

**Permission:** `usage:read_own`

**Query parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| period_hours | integer | 24 | Look-back window in hours |

**Response:**

```json
{
  "user_id": "admin",
  "period_hours": 24,
  "total_requests": 312,
  "total_prompt_tokens": 62400,
  "total_completion_tokens": 34100,
  "total_tokens": 96500,
  "by_model": {
    "mistral-7b": {
      "request_count": 312,
      "total_tokens": 96500
    }
  }
}
```

**Example:**

```bash
curl -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8420/api/usage/me?period_hours=48"
```

---

### GET /api/usage/models/{name}

Get a usage summary for a specific model.

**Permission:** `usage:read`

**Path parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| name | string | Model name |

**Query parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| period_hours | integer | 24 | Look-back window in hours |

**Response:**

```json
{
  "model": "mistral-7b",
  "period_hours": 24,
  "total_requests": 1580,
  "total_prompt_tokens": 316000,
  "total_completion_tokens": 172000,
  "total_tokens": 488000,
  "unique_users": 5
}
```

**Example:**

```bash
curl -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8420/api/usage/models/mistral-7b?period_hours=24"
```

---

## Metrics

### GET /metrics

Prometheus-compatible metrics endpoint. **No authentication required.**

Returns metrics in the Prometheus text exposition format.

**Example:**

```bash
curl http://localhost:8420/metrics
```

**Response (text/plain):**

```
# HELP lean_ai_requests_total Total inference requests
# TYPE lean_ai_requests_total counter
lean_ai_requests_total{model="mistral-7b"} 1580
# HELP lean_ai_gpu_memory_used_bytes GPU memory usage in bytes
# TYPE lean_ai_gpu_memory_used_bytes gauge
lean_ai_gpu_memory_used_bytes{gpu="0"} 34028994560
```

---

### GET /api/metrics/summary

Returns a JSON summary of key server metrics.

**Permission:** `metrics:read`

**Response:**

```json
{
  "requests_total": 15800,
  "requests_last_hour": 420,
  "avg_latency_ms": 185.3,
  "p99_latency_ms": 892.1,
  "tokens_generated_total": 4880000,
  "active_models": 2,
  "gpu_utilization_pct": [45.2, 38.7]
}
```

**Example:**

```bash
curl -H "Authorization: Bearer $TOKEN" \
  http://localhost:8420/api/metrics/summary
```

---

### GET /api/metrics/alerts

Returns currently active alerts based on configured thresholds.

**Permission:** `metrics:read`

**Response:**

```json
{
  "alerts": [
    {
      "name": "gpu_memory_high",
      "severity": "warning",
      "message": "GPU 0 memory usage at 92%",
      "triggered_at": "2026-03-31T19:15:00Z"
    }
  ]
}
```

**Example:**

```bash
curl -H "Authorization: Bearer $TOKEN" \
  http://localhost:8420/api/metrics/alerts
```

---

## Training

All training endpoints are available only when `training.enabled` is set to
`true` in the server configuration.

### Datasets

#### POST /api/training/datasets

Upload a training dataset. Uses `multipart/form-data` encoding.

**Permission:** `dataset:upload`

**Form fields:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| file | file | yes | The dataset file (JSONL, CSV, or Parquet) |
| name | string | yes | Unique name for the dataset |
| format | string | yes | File format: `jsonl`, `csv`, or `parquet` |
| description | string | no | Human-readable description |

**Response (201):**

```json
{
  "name": "customer-support-v2",
  "format": "jsonl",
  "description": "Customer support conversations",
  "size_bytes": 15728640,
  "row_count": 12500,
  "uploaded_at": "2026-03-31T20:00:00Z",
  "uploaded_by": "admin"
}
```

**Example:**

```bash
curl -X POST http://localhost:8420/api/training/datasets \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@customer_support.jsonl" \
  -F "name=customer-support-v2" \
  -F "format=jsonl" \
  -F "description=Customer support conversations"
```

---

#### GET /api/training/datasets

List all uploaded datasets.

**Permission:** `dataset:read`

**Response:**

```json
[
  {
    "name": "customer-support-v2",
    "format": "jsonl",
    "description": "Customer support conversations",
    "size_bytes": 15728640,
    "row_count": 12500,
    "uploaded_at": "2026-03-31T20:00:00Z",
    "uploaded_by": "admin"
  }
]
```

**Example:**

```bash
curl -H "Authorization: Bearer $TOKEN" \
  http://localhost:8420/api/training/datasets
```

---

#### GET /api/training/datasets/{name}

Get metadata for a specific dataset.

**Permission:** `dataset:read`

**Path parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| name | string | Dataset name |

**Response:**

```json
{
  "name": "customer-support-v2",
  "format": "jsonl",
  "description": "Customer support conversations",
  "size_bytes": 15728640,
  "row_count": 12500,
  "uploaded_at": "2026-03-31T20:00:00Z",
  "uploaded_by": "admin"
}
```

**Example:**

```bash
curl -H "Authorization: Bearer $TOKEN" \
  http://localhost:8420/api/training/datasets/customer-support-v2
```

---

#### GET /api/training/datasets/{name}/preview

Preview the first rows of a dataset.

**Permission:** `dataset:read`

**Path parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| name | string | Dataset name |

**Query parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| limit | integer | 5 | Number of rows to return |

**Response:**

```json
{
  "rows": [
    {
      "messages": [
        {"role": "user", "content": "How do I reset my password?"},
        {"role": "assistant", "content": "Go to Settings > Security..."}
      ]
    }
  ],
  "count": 5
}
```

**Example:**

```bash
curl -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8420/api/training/datasets/customer-support-v2/preview?limit=3"
```

---

#### DELETE /api/training/datasets/{name}

Delete a dataset from the server.

**Permission:** `dataset:upload`

**Path parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| name | string | Dataset name |

**Response:**

```json
{
  "status": "deleted"
}
```

Returns `409` if the dataset is currently in use by a running training job.

**Example:**

```bash
curl -X DELETE http://localhost:8420/api/training/datasets/customer-support-v2 \
  -H "Authorization: Bearer $TOKEN"
```

---

### Jobs

#### POST /api/training/jobs

Create a new training job. The job is created in a `pending` state and must
be started separately.

**Permission:** `training:submit`

**Request body (TrainingSubmitRequest):**

```json
{
  "base_model": "mistral-7b",
  "dataset": "customer-support-v2",
  "adapter_name": "cs-mistral-v1",
  "method": "lora",
  "hyperparameters": {
    "epochs": 3,
    "learning_rate": 2e-4,
    "batch_size": 4,
    "lora_r": 16,
    "lora_alpha": 32,
    "lora_dropout": 0.05
  },
  "gpu_indices": [1]
}
```

**Response (201):**

```json
{
  "job_id": "train_a1b2c3",
  "state": "pending",
  "base_model": "mistral-7b",
  "dataset": "customer-support-v2",
  "adapter_name": "cs-mistral-v1",
  "method": "lora",
  "hyperparameters": {
    "epochs": 3,
    "learning_rate": 2e-4,
    "batch_size": 4,
    "lora_r": 16,
    "lora_alpha": 32,
    "lora_dropout": 0.05
  },
  "gpu_indices": [1],
  "created_at": "2026-03-31T20:30:00Z",
  "created_by": "admin"
}
```

**Example:**

```bash
curl -X POST http://localhost:8420/api/training/jobs \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "base_model": "mistral-7b",
    "dataset": "customer-support-v2",
    "adapter_name": "cs-mistral-v1",
    "method": "lora",
    "hyperparameters": {
      "epochs": 3,
      "learning_rate": 2e-4,
      "batch_size": 4,
      "lora_r": 16,
      "lora_alpha": 32
    },
    "gpu_indices": [1]
  }'
```

---

#### GET /api/training/jobs

List training jobs, optionally filtered by state.

**Permission:** `training:read`

**Query parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| state | string | Filter by state: `pending`, `running`, `completed`, `failed`, `cancelled` |

**Response:**

```json
[
  {
    "job_id": "train_a1b2c3",
    "state": "running",
    "base_model": "mistral-7b",
    "dataset": "customer-support-v2",
    "adapter_name": "cs-mistral-v1",
    "method": "lora",
    "progress_pct": 42.0,
    "current_epoch": 2,
    "current_loss": 0.834,
    "created_at": "2026-03-31T20:30:00Z",
    "started_at": "2026-03-31T20:31:00Z"
  }
]
```

**Example:**

```bash
curl -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8420/api/training/jobs?state=running"
```

---

#### GET /api/training/jobs/{job_id}

Get detailed information about a specific training job.

**Permission:** `training:read`

**Path parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| job_id | string | Training job ID |

**Response:**

```json
{
  "job_id": "train_a1b2c3",
  "state": "running",
  "base_model": "mistral-7b",
  "dataset": "customer-support-v2",
  "adapter_name": "cs-mistral-v1",
  "method": "lora",
  "hyperparameters": {
    "epochs": 3,
    "learning_rate": 2e-4,
    "batch_size": 4,
    "lora_r": 16,
    "lora_alpha": 32,
    "lora_dropout": 0.05
  },
  "gpu_indices": [1],
  "progress_pct": 42.0,
  "current_epoch": 2,
  "current_loss": 0.834,
  "created_at": "2026-03-31T20:30:00Z",
  "started_at": "2026-03-31T20:31:00Z",
  "created_by": "admin"
}
```

**Example:**

```bash
curl -H "Authorization: Bearer $TOKEN" \
  http://localhost:8420/api/training/jobs/train_a1b2c3
```

---

#### POST /api/training/jobs/{job_id}/start

Start a pending training job. Returns an SSE stream of training progress events.

**Permission:** `training:submit`

**Path parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| job_id | string | Training job ID |

**Response (SSE stream):**

```
data: {"event": "epoch_start", "epoch": 1, "total_epochs": 3}

data: {"event": "step", "epoch": 1, "step": 50, "total_steps": 200, "loss": 1.234, "lr": 2e-4}

data: {"event": "epoch_end", "epoch": 1, "avg_loss": 0.987}

data: {"event": "complete", "final_loss": 0.612, "adapter_path": "/models/adapters/cs-mistral-v1"}
```

Returns `409` if the job is not in `pending` state.

**Example:**

```bash
curl -X POST http://localhost:8420/api/training/jobs/train_a1b2c3/start \
  -H "Authorization: Bearer $TOKEN" \
  -N
```

---

#### POST /api/training/jobs/{job_id}/cancel

Cancel a running or pending training job.

**Permission:** `training:submit`

**Path parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| job_id | string | Training job ID |

**Response:**

```json
{
  "status": "cancelled"
}
```

Returns `409` if the job is already completed, failed, or cancelled.

**Example:**

```bash
curl -X POST http://localhost:8420/api/training/jobs/train_a1b2c3/cancel \
  -H "Authorization: Bearer $TOKEN"
```

---

#### GET /api/training/gpu-status

Show current GPU assignments for training jobs.

**Permission:** `training:read`

**Response:**

```json
{
  "gpu_assignments": {
    "0": {
      "status": "inference",
      "model": "mistral-7b"
    },
    "1": {
      "status": "training",
      "job_id": "train_a1b2c3",
      "adapter_name": "cs-mistral-v1"
    }
  }
}
```

**Example:**

```bash
curl -H "Authorization: Bearer $TOKEN" \
  http://localhost:8420/api/training/gpu-status
```

---

### Adapters

#### GET /api/training/adapters

List available LoRA adapters, optionally filtered by base model.

**Permission:** `adapter:read`

**Query parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| base_model | string | Filter by base model name |

**Response:**

```json
[
  {
    "name": "cs-mistral-v1",
    "base_model": "mistral-7b",
    "method": "lora",
    "training_job_id": "train_a1b2c3",
    "created_at": "2026-03-31T22:00:00Z",
    "deployed": true,
    "deployed_to": "mistral-7b",
    "metadata": {
      "final_loss": 0.612,
      "epochs": 3
    }
  }
]
```

**Example:**

```bash
curl -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8420/api/training/adapters?base_model=mistral-7b"
```

---

#### GET /api/training/adapters/{name}

Get detailed information about a specific adapter.

**Permission:** `adapter:read`

**Path parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| name | string | Adapter name |

**Response:**

```json
{
  "name": "cs-mistral-v1",
  "base_model": "mistral-7b",
  "method": "lora",
  "training_job_id": "train_a1b2c3",
  "created_at": "2026-03-31T22:00:00Z",
  "deployed": true,
  "deployed_to": "mistral-7b",
  "metadata": {
    "final_loss": 0.612,
    "epochs": 3
  }
}
```

**Example:**

```bash
curl -H "Authorization: Bearer $TOKEN" \
  http://localhost:8420/api/training/adapters/cs-mistral-v1
```

---

#### POST /api/training/adapters/import

Import an externally trained adapter.

**Permission:** `adapter:deploy`

**Request body:**

```json
{
  "name": "external-lora-v1",
  "base_model": "mistral-7b",
  "path": "/data/adapters/external-lora-v1",
  "metadata": {
    "source": "external",
    "notes": "Trained on internal dataset"
  }
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| name | string | yes | Unique name for the adapter |
| base_model | string | yes | Name of the compatible base model |
| path | string | yes | Filesystem path to the adapter weights |
| metadata | object | no | Arbitrary metadata to store with the adapter |

**Response (201):**

```json
{
  "name": "external-lora-v1",
  "base_model": "mistral-7b",
  "method": "lora",
  "created_at": "2026-04-01T08:00:00Z",
  "deployed": false,
  "metadata": {
    "source": "external",
    "notes": "Trained on internal dataset"
  }
}
```

**Example:**

```bash
curl -X POST http://localhost:8420/api/training/adapters/import \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "external-lora-v1",
    "base_model": "mistral-7b",
    "path": "/data/adapters/external-lora-v1"
  }'
```

---

#### POST /api/training/adapters/{name}/deploy

Deploy an adapter to a running model, making it available for inference.

**Permission:** `adapter:deploy`

**Path parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| name | string | Adapter name |

**Request body:**

```json
{
  "model_name": "mistral-7b"
}
```

**Response:**

```json
{
  "status": "deployed"
}
```

Returns `409` if the target model is not loaded or the adapter is already deployed.

**Example:**

```bash
curl -X POST http://localhost:8420/api/training/adapters/cs-mistral-v1/deploy \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"model_name": "mistral-7b"}'
```

---

#### POST /api/training/adapters/{name}/undeploy

Remove an adapter from a running model.

**Permission:** `adapter:deploy`

**Path parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| name | string | Adapter name |

**Response:**

```json
{
  "status": "undeployed"
}
```

**Example:**

```bash
curl -X POST http://localhost:8420/api/training/adapters/cs-mistral-v1/undeploy \
  -H "Authorization: Bearer $TOKEN"
```

---

#### DELETE /api/training/adapters/{name}

Delete an adapter from disk. The adapter must be undeployed first.

**Permission:** `adapter:deploy`

**Path parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| name | string | Adapter name |

**Response:**

```json
{
  "status": "deleted"
}
```

Returns `409` if the adapter is currently deployed.

**Example:**

```bash
curl -X DELETE http://localhost:8420/api/training/adapters/cs-mistral-v1 \
  -H "Authorization: Bearer $TOKEN"
```

---

## Web Dashboard

The built-in web dashboard is a server-rendered UI using HTMX, Jinja2, and Pico CSS. It is enabled by default and available at `/dashboard/`. All dashboard routes use cookie-based session authentication (JWT stored in an HTTP-only cookie) rather than Bearer tokens.

### Dashboard Pages

| Route | Description | Auth Required |
|-------|-------------|---------------|
| `GET /dashboard/login` | Login page (API key, LDAP, or OIDC) | No |
| `POST /dashboard/login` | Submit login credentials | No |
| `POST /dashboard/logout` | Clear session cookie and redirect to login | Yes |
| `GET /dashboard/` | Home — health, KPIs, model overview, alerts | Yes |
| `GET /dashboard/models` | Model management — load, unload, sleep, wake | Yes |
| `GET /dashboard/monitoring` | Metrics charts and active alerts | Yes |
| `GET /dashboard/security` | API key management and audit logs | Yes |
| `GET /dashboard/training` | Training jobs, datasets, adapters (if enabled) | Yes |
| `GET /dashboard/settings` | Read-only server configuration (secrets masked) | Yes |

### Dashboard HTMX API

These endpoints return HTML fragments for HTMX partial page updates. They are used internally by the dashboard and are not intended for external API consumption.

| Route | Method | CSRF | Description |
|-------|--------|------|-------------|
| `/dashboard/api/partials/model-list` | GET | No | Model list HTML fragment |
| `/dashboard/api/models/{name}/load` | POST | Yes | Load model, return updated card |
| `/dashboard/api/models/{name}/unload` | POST | Yes | Unload model, return updated card |
| `/dashboard/api/models/{name}/sleep` | POST | Yes | Sleep model, return updated card |
| `/dashboard/api/models/{name}/wake` | POST | Yes | Wake model, return updated card |
| `/dashboard/api/partials/metrics` | GET | No | Metrics summary HTML fragment |
| `/dashboard/api/partials/alerts` | GET | No | Active alerts HTML fragment |
| `/dashboard/api/partials/audit` | GET | No | Audit log table HTML fragment |
| `/dashboard/api/keys/create` | POST | Yes | Create API key, return keys table |
| `/dashboard/api/keys/{key_id}` | DELETE | Yes | Revoke API key, return keys table |
| `/dashboard/api/training/jobs/submit` | POST | Yes | Submit training job |
| `/dashboard/api/training/jobs/{id}/cancel` | POST | Yes | Cancel training job |

**CSRF protection:** State-changing requests (POST, DELETE) require an `X-CSRF-Token` header. The token is derived from the session JWT's `jti` claim via HMAC and is automatically included by HTMX through `hx-headers` on the page body.

### Static Assets

Static files are served from `/static/` and include vendored JavaScript/CSS libraries (no CDN dependency, works air-gapped):

| Path | Description |
|------|-------------|
| `/static/css/pico.min.css` | Pico CSS 2.x — semantic classless CSS framework |
| `/static/css/dashboard.css` | Custom dashboard styles, dark mode, responsive layout |
| `/static/js/htmx.min.js` | HTMX 2.x — server-driven interactivity |
| `/static/js/alpine.min.js` | Alpine.js 3.x — client-side state (modals, tabs, toggles) |
| `/static/js/chart.min.js` | Chart.js 4.x — metrics visualization |
| `/static/js/dashboard.js` | Dashboard initialization, chart setup, toast notifications |
