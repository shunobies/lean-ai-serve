# Phase 4: Model Lifecycle & Operational Hardening

## Context

Phases 1-3 of lean-ai-serve are complete (147 tests, lint clean). Phase 4 closes the remaining gaps in model lifecycle management and hardens operations for production use. The infrastructure is already partially wired — `LifecycleConfig` exists, `ModelState.SLEEPING` is defined, the `usage` table is created but empty, and TLS config fields exist but aren't consumed.

**What's already built and ready:**
- `LifecycleConfig` with `idle_sleep_timeout` and `sleep_level` (`config.py:130-132`)
- `ModelState.SLEEPING` state in enum (`models/schemas.py:21`)
- Load endpoint accepts transition from SLEEPING state (`api/models.py:145`)
- `ProcessManager` with start/stop/health_check (`engine/process.py`)
- `usage` DB table with `(hour, user_id, model)` unique index (`db.py:66-77`)
- `TLSConfig` with `enabled`, `cert_file`, `key_file` (`config.py:19-22`)
- `SpeculativeConfig` with strategy/draft_model/num_tokens (`config.py:114-119`)
- vLLM command building for speculative decoding (`engine/process.py:272-290`)
- `KVCacheConfig` with `calculate_scales` and `turboquant_bits` (`config.py:97-100`)
- Rate limiter singleton (`security/rate_limiter.py`)

---

## New Files

### 1. `engine/lifecycle.py` — Model sleep/wake daemon

- `LifecycleManager(registry, process_manager, settings)`:
  - `start()` — launch background `asyncio.Task` polling loop
  - `stop()` — cancel the background task
  - `_poll_loop()` — runs every 60s, checks each LOADED model's last request time
  - `_sleep_model(name, level)`:
    - Level 1 (CPU offload): Not directly supported by vLLM — stop the process but keep the model downloaded (fast reload)
    - Level 2 (discard): Stop the process entirely (same as unload)
    - Transitions model to `ModelState.SLEEPING` in registry
  - `_wake_model(name)` — restart vLLM process from downloaded state → LOADING → LOADED
  - `get_idle_times() -> dict[str, float]` — per-model seconds since last request

- `RequestTracker`:
  - `touch(model_name)` — called on every proxied request
  - `last_seen(model_name) -> float | None` — returns monotonic timestamp

### 2. `engine/validators.py` — Pre-start validation

- `validate_gpu_availability(config: ModelConfig) -> list[str]`:
  - Check `tensor_parallel_size <= len(config.gpu)`
  - Check `pipeline_parallel_size` compatibility
  - Verify requested GPUs exist (via nvidia-ml-py or psutil fallback)
  - Return list of warnings/errors
- `validate_speculative_config(config: ModelConfig) -> list[str]`:
  - Verify draft model path exists when strategy=draft
  - Warn if eagle strategy selected (not yet supported in vLLM args)
  - Validate num_tokens range
- `validate_model_config(config: ModelConfig) -> list[str]`:
  - Aggregate all validation checks
  - Called before `ProcessManager.start()`

### 3. `security/usage.py` — Usage aggregation

- `UsageTracker(db)`:
  - `record(user_id, model, prompt_tokens, completion_tokens, latency_ms)` — upserts into hourly buckets
  - `query_usage(user_id=None, model=None, from_time=None, to_time=None) -> list[UsageSummary]`
  - `get_user_summary(user_id, period_hours=24) -> UserUsageSummary`
  - `get_model_summary(model, period_hours=24) -> ModelUsageSummary`
  - `cleanup_old(days=90)` — purge old usage records

### 4. `api/usage.py` — Usage API endpoints

| Method | Path | Permission |
|--------|------|-----------|
| GET | `/api/usage` | usage:read |
| GET | `/api/usage/me` | usage:read_own |
| GET | `/api/usage/models/{name}` | usage:read |
| GET | `/api/usage/export` | usage:read |

---

## Existing File Modifications

### `engine/process.py` — Enhanced command building
- Wire missing KV cache options: `calculate_scales`, `turboquant_bits`
- Add Eagle speculative strategy support (when vLLM adds it)
- Add pre-start validation call to `validate_model_config()`
- Pass `CUDA_VISIBLE_DEVICES` environment variable scoped to `config.gpu`

### `engine/proxy.py` — Request tracking
- After successful proxy: call `request_tracker.touch(model_name)`
- Parse streaming/non-streaming response to extract token counts
- Feed token counts into `UsageTracker.record()`

### `api/openai_compat.py` — Token count extraction
- Extract `usage.prompt_tokens` and `usage.completion_tokens` from vLLM response
- Pass to `UsageTracker` for aggregation
- For streaming responses, accumulate from the final `[DONE]` chunk

### `main.py` — Lifespan additions
- Initialize `RequestTracker` and `LifecycleManager` on app.state
- Start lifecycle polling in startup, stop in shutdown
- Initialize `UsageTracker` on app.state
- Pass `ssl_certfile`/`ssl_keyfile` to uvicorn when TLS enabled (in `cli/main.py`)

### `cli/main.py` — TLS support
- When `settings.server.tls.enabled`: pass `--ssl-certfile` and `--ssl-keyfile` to uvicorn
- Add `usage` subcommand group: `usage summary`, `usage export`

### `api/models.py` — Sleep/wake endpoints

| Method | Path | Permission |
|--------|------|-----------|
| POST | `/api/models/{name}/sleep` | model:deploy |
| POST | `/api/models/{name}/wake` | model:deploy |
| GET | `/api/models/idle-times` | model:read |

### `security/rate_limiter.py` — Cleanup integration
- Wire `cleanup()` into a periodic background task (every 5 min)
- Add model-level rate limiting (optional `rate_limit` per model in config)

### `config.py` — Enhancements
- Add `LifecycleConfig.auto_wake_on_request: bool = True` — auto-wake sleeping models on incoming request
- Add `ServerConfig.workers: int = 1` — uvicorn workers count

### `config.example.yaml` — Document lifecycle and TLS sections

---

## Implementation Order

| Step | Module | Depends On |
|------|--------|------------|
| 1 | `engine/validators.py` + tests | None |
| 2 | `security/usage.py` + tests | None |
| 3 | `engine/lifecycle.py` + tests | None |
| 4 | Wire usage tracking into proxy + openai_compat | Step 2 |
| 5 | `api/usage.py` + tests | Step 2 |
| 6 | Wire lifecycle into main.py + model endpoints | Step 3 |
| 7 | Enhanced process.py (KV cache, CUDA_VISIBLE_DEVICES, validation) | Step 1 |
| 8 | TLS support in cli/main.py | None |
| 9 | Verify all existing + new tests pass, lint clean | Steps 1-8 |

---

## Tests

- `test_validators.py`: GPU count vs tensor_parallel, nonexistent GPU, speculative config validation, eagle warning, combined validation
- `test_usage.py`: record + query, hourly bucketing, user/model summary, cleanup, concurrent upserts
- `test_lifecycle.py`: idle detection, sleep transition (level 1 and 2), wake transition, auto-wake on request, disabled timeout (0 = never), multiple models
- `test_usage_api.py`: endpoint permissions, query filters, export format, /me endpoint scoping
- `test_process_enhanced.py`: CUDA_VISIBLE_DEVICES env, KV cache turboquant args, validation-before-start

All vLLM processes and GPU queries mocked — no hardware needed in tests.

---

## Verification

1. **Idle sleep**: Model loaded → no requests for `idle_sleep_timeout` → state transitions to SLEEPING
2. **Wake**: SLEEPING model → POST /wake → LOADING → LOADED
3. **Auto-wake**: Request to sleeping model with `auto_wake_on_request=true` → automatic wake + proxy
4. **Usage tracking**: Inference requests → usage table populated with correct hourly buckets
5. **Usage API**: GET /api/usage returns aggregated data; /me scoped to current user
6. **GPU validation**: `tensor_parallel_size=4` with `gpu=[0]` → rejected with clear error
7. **TLS**: `tls.enabled=true` + cert/key → uvicorn starts with SSL
8. **All 147 existing + new tests pass**
9. **Lint clean**: `ruff check src/ tests/`
