# Phase 5: Observability, Metrics & OIDC Authentication

## Context

Phases 1-4 are complete. Phase 5 adds production observability (Prometheus metrics, structured request logging, background maintenance tasks), OIDC authentication (the last auth mode stub), and environment variable config overrides. The health endpoint is basic, the OIDC code path raises 501, audit log cleanup is manual, and there's no way to monitor system behavior at scale.

**What's already built and ready:**
- `OIDCConfig` with `issuer_url`, `client_id`, `audience`, `roles_claim` (`config.py:44-48`)
- OIDC placeholder in `authenticate()` that raises HTTP 501 (`auth.py:282-287`)
- Audit log with hash chain (`security/audit.py`)
- `cleanup_revoked_tokens(db)` function exists but isn't called periodically (`auth.py:54-64`)
- `RateLimiter.cleanup()` exists but isn't called periodically (`rate_limiter.py:63-68`)
- Health/status endpoints (`api/health.py`)
- structlog in dependencies (`pyproject.toml:57`)
- RBAC permissions already define `metrics:read` (`rbac.py:21,33,44`)
- `pydantic-settings` in dependencies but not used (`pyproject.toml:40`)
- `load_settings()` comment: "Environment variables can override YAML values (not yet implemented)" (`config.py:237-238`)

---

## New Files

### 1. `security/oidc.py` — OIDC token validation

- `OIDCValidator(config: OIDCConfig)`:
  - `initialize()` — fetch JWKS from `{issuer_url}/.well-known/openid-configuration`
  - `validate_token(token: str) -> AuthUser | None`:
    - Decode JWT using cached JWKS public keys (via `PyJWT` + `cryptography`)
    - Validate `iss`, `aud`, `exp` claims
    - Extract roles from configurable claim path (e.g., `realm_access.roles`)
    - Map OIDC roles to lean-ai-serve roles via optional `role_mapping` config
    - Return `AuthUser` with `auth_method="oidc"`
  - `_refresh_jwks()` — re-fetch JWKS if key ID not found (key rotation support)
  - `_resolve_claim(payload, claim_path) -> list[str]` — dot-notation claim traversal

### 2. `observability/__init__.py` — empty package

### 3. `observability/metrics.py` — Prometheus metrics

- `MetricsCollector`:
  - Counters: `requests_total{method, path, status}`, `inference_tokens_total{model, type}`, `auth_failures_total{method}`
  - Histograms: `request_duration_seconds{method, path}`, `inference_latency_seconds{model}`
  - Gauges: `models_loaded`, `gpu_memory_used_bytes{gpu}`, `gpu_utilization_pct{gpu}`, `training_jobs_active`, `rate_limit_remaining{key_id}`
  - `record_request(method, path, status, duration)`
  - `record_inference(model, prompt_tokens, completion_tokens, latency)`
  - `record_gpu_snapshot(gpu_info_list)`
  - `expose() -> str` — render Prometheus text format
- Note: Implement metrics with a lightweight dict-based approach (no prometheus_client dependency). Counters/histograms stored in-memory, rendered in Prometheus exposition format on demand.

### 4. `observability/middleware.py` — Request metrics middleware

- `MetricsMiddleware(app, metrics_collector)`:
  - Wraps all requests: captures start time, status code, path
  - Calls `metrics_collector.record_request()` on completion
  - Skips `/health` and `/metrics` to avoid self-instrumentation noise

### 5. `observability/tasks.py` — Background maintenance tasks

- `BackgroundScheduler(db, settings)`:
  - `start()` — launch asyncio background tasks
  - `stop()` — cancel all tasks
  - Tasks:
    - **Token cleanup** (every 1 hour): `cleanup_revoked_tokens(db)` — remove expired JWT revocations
    - **Rate limiter cleanup** (every 5 min): `rate_limiter.cleanup()` — prune empty sliding windows
    - **Audit retention** (daily): delete audit entries older than `audit.retention_days`
    - **Usage retention** (daily): delete usage records older than configured period
    - **GPU metrics snapshot** (every 30s): poll GPU info, update gauges
    - **Training job reaper** (every 5 min): detect zombie training processes, mark as failed

### 6. `api/metrics.py` — Metrics endpoint

| Method | Path | Permission |
|--------|------|-----------|
| GET | `/metrics` | public (no auth) |
| GET | `/api/metrics/summary` | metrics:read |

- `/metrics` — Prometheus scrape endpoint (text/plain)
- `/api/metrics/summary` — JSON summary of key metrics for dashboards

### 7. `observability/logging.py` — Structured logging setup

- `setup_logging(settings)`:
  - Configure structlog with JSON output for production, pretty console for dev
  - Add processors: timestamp, log level, request ID, caller info
  - Integrate with uvicorn access log
  - Add request ID middleware (X-Request-ID header, auto-generated if absent)

---

## Existing File Modifications

### `config.py` — Environment variable overrides + OIDC enhancements
- Switch `Settings` from `BaseModel` to `pydantic_settings.BaseSettings`:
  - `env_prefix = "LEAN_AI_SERVE_"`
  - `env_nested_delimiter = "__"` (e.g., `LEAN_AI_SERVE_SERVER__PORT=9000`)
  - Maintain YAML loading as primary, env vars as override layer
- Add to `OIDCConfig`:
  ```python
  role_mapping: dict[str, str] = {}  # OIDC role -> lean-ai-serve role
  default_role: str = "user"
  jwks_cache_ttl: int = 3600
  ```
- Add `MetricsConfig`:
  ```python
  class MetricsConfig(BaseModel):
      enabled: bool = True
      gpu_poll_interval: int = 30   # seconds
  ```

### `security/auth.py` — Wire OIDC validation
- Replace 501 placeholder with actual `OIDCValidator.validate_token()` call
- Initialize `OIDCValidator` in lifespan when mode includes "oidc"
- Support `oidc+api_key` combined mode (try API key first, then OIDC)

### `main.py` — Lifespan additions
- Initialize `OIDCValidator` if OIDC mode configured
- Initialize `MetricsCollector` and `MetricsMiddleware`
- Initialize `BackgroundScheduler`, start on startup, stop on shutdown
- Call `setup_logging(settings)` at startup
- Add request ID middleware

### `cli/main.py` — Additions
- `lean-ai-serve config show` — dump resolved config (with env overrides applied, secrets masked)
- `lean-ai-serve config validate` — validate config file without starting server

### `api/health.py` — Enhanced health check
- Include metrics summary in `/api/status` response
- Add `ready` field (all autoload models loaded)
- Add `checks` dict: `{db: ok, gpu: ok, oidc: ok/not_configured}`

### `pyproject.toml` — Dependencies
```toml
# Add to main dependencies
"httpx>=0.27.0,<1.0",  # already present — also used for OIDC JWKS fetch

# Add to optional
oidc = ["pyjwt[crypto]>=2.9.0,<3.0"]  # already in main deps, just documenting
```
No new dependencies needed — PyJWT and httpx are already in the dependency list.

### `config.example.yaml` — Document OIDC, metrics, env override examples

---

## Implementation Order

| Step | Module | Depends On |
|------|--------|------------|
| 1 | OIDC validator + tests | None |
| 2 | Wire OIDC into auth.py + lifespan | Step 1 |
| 3 | Metrics collector (no-dependency Prometheus format) + tests | None |
| 4 | Metrics middleware + endpoint | Step 3 |
| 5 | Background scheduler + tests | None |
| 6 | Structured logging setup | None |
| 7 | Environment variable overrides (pydantic-settings) | None |
| 8 | Wire everything into main.py + cli | Steps 1-7 |
| 9 | Enhanced health checks | Steps 3-5 |
| 10 | Verify all existing + new tests pass, lint clean | Steps 1-9 |

---

## Tests

- `test_oidc.py`: valid token decode, expired token, wrong audience, bad signature, role mapping (direct, mapped, default), JWKS refresh on unknown kid, claim path traversal (nested, flat, missing)
- `test_metrics.py`: counter increment, histogram recording, gauge set, Prometheus text format rendering, GPU snapshot recording
- `test_metrics_middleware.py`: request counting, latency recording, path exclusions, status code tracking
- `test_background_tasks.py`: token cleanup runs, rate limiter cleanup runs, audit retention deletes old entries, scheduler start/stop lifecycle
- `test_config_env.py`: env var overrides YAML, nested env vars (`SERVER__PORT`), env precedence over file, default values preserved
- `test_oidc_api.py`: end-to-end with mocked JWKS endpoint, combined oidc+api_key mode

All JWKS fetches mocked via httpx mock transport — no real IdP needed.

---

## Verification

1. **OIDC auth**: Keycloak/Auth0 JWT → validated against mocked JWKS → AuthUser with correct roles
2. **OIDC+API key**: API key works alongside OIDC in combined mode
3. **Prometheus metrics**: GET /metrics → valid Prometheus text format with request/inference counters
4. **Background cleanup**: Expired tokens cleaned up automatically after 1 hour cycle
5. **Env overrides**: `LEAN_AI_SERVE_SERVER__PORT=9000` overrides YAML port
6. **Structured logging**: JSON log output in production, readable console in dev
7. **Health check**: `/api/status` includes readiness and component health
8. **All existing + new tests pass**
9. **Lint clean**: `ruff check src/ tests/`
