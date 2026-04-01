# Architecture

This document describes the internal architecture of lean-ai-serve, including system diagrams, component responsibilities, and data flow.

## System Architecture

```mermaid
graph TB
    Client["Client / SDK"] -->|"HTTP/HTTPS"| Server["FastAPI Server :8420"]

    subgraph lean-ai-serve
        Server --> MW["Middleware Stack"]

        subgraph Middleware
            MW --> RID["RequestID MW"]
            RID --> MET["Metrics MW"]
            MET --> CF["Content Filter MW"]
            CF --> CMP["Compression MW"]
        end

        CMP --> Routes["Route Handlers"]

        Routes --> OAI["/v1/* — OpenAI-Compatible"]
        Routes --> MAPI["/api/models/* — Model Mgmt"]
        Routes --> TAPI["/api/training/* — Fine-Tuning"]
        Routes --> AUTH["/api/auth/* — Authentication"]
        Routes --> AAPI["/api/audit/* — Audit Logs"]
        Routes --> KAPI["/api/keys/* — API Keys"]
        Routes --> UAPI["/api/usage/* — Usage"]
        Routes --> HAPI["/health, /metrics"]

        OAI --> Router["Router"]
        Router --> Proxy["Reverse Proxy"]

        MAPI --> Registry["Model Registry"]
        MAPI --> PM["Process Manager"]
        MAPI --> LC["Lifecycle Manager"]

        TAPI --> Orch["Training Orchestrator"]

        AUTH --> AuthMod["Auth Module"]
        AuthMod --> AK["API Key Auth"]
        AuthMod --> LDAP["LDAP Service"]
        AuthMod --> OIDC["OIDC Validator"]
        AuthMod --> RBAC["RBAC Engine"]

        PM -->|"spawns"| vLLM1["vLLM Process :port1"]
        PM -->|"spawns"| vLLM2["vLLM Process :port2"]

        Proxy --> vLLM1
        Proxy --> vLLM2
    end

    subgraph Data Stores
        SQLite[("SQLite DB")]
        HF["HuggingFace Hub"]
        Vault["HashiCorp Vault"]
    end

    subgraph Observability
        Prom["Prometheus"]
        OTel["OTLP Collector"]
    end

    Registry --> SQLite
    Orch --> SQLite
    AuthMod --> SQLite
    HAPI --> Prom
    Server -.-> OTel
```

## Request Flow

Sequence diagram showing an inference request from client to vLLM and back:

```mermaid
sequenceDiagram
    participant C as Client
    participant RID as RequestID MW
    participant Met as Metrics MW
    participant CF as Content Filter
    participant Comp as Compression MW
    participant Auth as Auth Module
    participant RL as Rate Limiter
    participant Rt as Router
    participant P as Proxy
    participant V as vLLM
    participant A as Audit Logger
    participant U as Usage Tracker

    C->>RID: POST /v1/chat/completions
    RID->>Met: Add X-Request-ID
    Met->>CF: Start timer
    CF->>Comp: Scan for PHI/PII
    Comp->>Auth: Compress context (if enabled)
    Auth->>RL: Validate Bearer token
    RL->>Rt: Check rate limit

    alt Model is sleeping
        Rt-->>C: 503 Retry-After: 30
        Note right of Rt: Triggers auto-wake in background
    end

    Rt->>P: Resolve model → port
    P->>V: Forward to vLLM
    V-->>P: Completion response
    P-->>Rt: Response + token usage
    Rt->>U: Record token usage
    Rt->>A: Log audit entry (with chain hash)
    Note right of Met: Record latency + status code
    Rt-->>C: Response + X-RateLimit-* headers
```

## Model Lifecycle State Machine

Models transition through the following states:

```mermaid
stateDiagram-v2
    [*] --> not_downloaded: Model defined in config
    not_downloaded --> downloading: pull (CLI or API)
    downloading --> downloaded: Download complete
    downloading --> error: Download failed

    downloaded --> loading: load / autoload
    loading --> loaded: vLLM healthy
    loading --> error: vLLM failed to start

    loaded --> unloading: unload
    unloading --> downloaded: Process stopped

    loaded --> sleeping: Idle timeout (level 1)
    loaded --> downloaded: Idle timeout (level 2)

    sleeping --> loading: wake / auto-wake on request

    error --> loading: Retry load
    error --> not_downloaded: Delete model

    loaded --> not_downloaded: Delete model
    downloaded --> not_downloaded: Delete model
```

**State descriptions:**

| State | Description |
|-------|-------------|
| `not_downloaded` | Model is registered in config but files are not on disk |
| `downloading` | Model is being pulled from HuggingFace Hub |
| `downloaded` | Model files are cached locally, not loaded into GPU |
| `loading` | vLLM subprocess is starting and running health checks |
| `loaded` | Model is serving inference requests |
| `sleeping` | Process stopped, auto-wake enabled (level 1 only) |
| `unloading` | Process is being stopped |
| `error` | An operation failed (download, load, etc.) |

## Authentication Flow

```mermaid
flowchart TD
    R["Incoming Request"] --> H{"Has Authorization header?"}
    H -->|No| M{"Security mode = none?"}
    M -->|Yes| ANON["Anonymous admin user"]
    M -->|No| E401["401 Unauthorized"]

    H -->|Yes| T{"Token starts with las-?"}

    T -->|"Yes (API Key)"| AK{"api_key in modes?"}
    AK -->|Yes| VK["Verify bcrypt hash in DB"]
    VK -->|Valid| CHK_EXP{"Key expired?"}
    CHK_EXP -->|No| CHK_RL{"Rate limit check"}
    CHK_RL -->|Pass| CHK_MODEL{"Model access check"}
    CHK_MODEL -->|Pass| OK["Authorized — proceed"]
    CHK_RL -->|Fail| E429["429 Too Many Requests"]
    CHK_EXP -->|Yes| E401
    VK -->|Invalid| E401
    AK -->|No| E401

    T -->|"No (JWT Token)"| J{"ldap in modes?"}
    J -->|Yes| DJ["Decode JWT (HS256)"]
    DJ -->|Valid| REV{"Token revoked?"}
    REV -->|No| OK
    REV -->|Yes| E401
    DJ -->|Invalid| O

    J -->|No| O{"oidc in modes?"}
    O -->|Yes| VO["Validate against JWKS endpoint"]
    VO -->|Valid| MAP["Map OIDC roles → app roles"]
    MAP --> OK
    VO -->|Invalid| E401
    O -->|No| E401
```

## Training Workflow

```mermaid
flowchart TD
    subgraph "1. Data Preparation"
        UP["Upload Dataset<br/>POST /api/training/datasets"] --> VAL["Validate Format"]
        VAL --> STORE["Store in dataset_directory"]
    end

    subgraph "2. Job Submission"
        SUB["Submit Job<br/>POST /api/training/jobs"] --> CHK1{"Model downloaded?"}
        CHK1 -->|Yes| CHK2{"Dataset exists?"}
        CHK2 -->|Yes| CHK3{"Concurrent limit OK?"}
        CHK3 -->|Yes| CHK4{"GPU available?"}
        CHK4 -->|Yes| Q["Job queued in DB"]
        CHK1 -->|No| ERR["400 Error"]
        CHK2 -->|No| ERR
        CHK3 -->|No| ERR
        CHK4 -->|No| ERR
    end

    subgraph "3. Execution"
        Q --> START["Start Job<br/>POST /jobs/{id}/start"]
        START --> CFG["Generate LLaMA-Factory YAML"]
        CFG --> PROC["Launch llamafactory-cli train"]
        PROC --> SSE["Stream progress via SSE"]
    end

    subgraph "4. Completion"
        PROC -->|"Exit 0"| DONE["Job completed"]
        PROC -->|"Exit ≠ 0"| FAIL["Job failed"]
        PROC -->|"SIGTERM"| CANC["Job cancelled"]
        DONE --> REG["Auto-register LoRA adapter"]
    end

    subgraph "5. Deployment"
        REG --> DEP["Deploy Adapter<br/>POST /adapters/{name}/deploy"]
        DEP --> VLLM["vLLM /v1/load_lora_adapter"]
        VLLM --> INF["Inference with adapter via model name"]
    end
```

## Startup Sequence

```mermaid
sequenceDiagram
    participant CLI as lean-ai-serve start
    participant UV as Uvicorn
    participant APP as create_app()
    participant LS as lifespan()

    CLI->>UV: Start server
    UV->>APP: Create FastAPI app
    APP->>APP: Register routers
    APP->>APP: Add middleware stack
    UV->>LS: Enter lifespan

    LS->>LS: Setup structured logging
    LS->>LS: Connect SQLite database
    LS->>LS: Sync model registry from config

    opt encryption.at_rest.enabled
        LS->>LS: Initialize EncryptionService
    end

    LS->>LS: Initialize AuditLogger
    LS->>LS: Load revoked JWT tokens

    opt security.mode includes ldap
        LS->>LS: Initialize LDAPService
    end

    opt security.mode includes oidc
        LS->>LS: Initialize OIDCValidator + fetch JWKS
    end

    opt metrics.enabled
        LS->>LS: Create MetricsCollector
    end

    opt alerts.enabled
        LS->>LS: Create AlertEvaluator
    end

    opt tracing.enabled
        LS->>LS: Setup OpenTelemetry
    end

    LS->>LS: Create Downloader, ProcessManager, Router
    LS->>LS: Create RequestTracker, UsageTracker
    LS->>LS: Start LifecycleManager (poll every 60s)
    LS->>LS: Start BackgroundScheduler (7 tasks)

    opt training.enabled
        LS->>LS: Initialize DatasetManager, AdapterRegistry, TrainingBackend, Orchestrator
    end

    loop For each model with autoload: true
        LS->>LS: Spawn vLLM subprocess + wait for health
    end

    LS-->>UV: Server ready
```

## Component Map

| Directory | Module | Responsibility |
|-----------|--------|----------------|
| `api/` | `openai_compat.py` | OpenAI-compatible inference endpoints (`/v1/*`) |
| | `models.py` | Model lifecycle API (pull, load, unload, sleep, wake, delete) |
| | `health.py` | Health check and status endpoints |
| | `keys.py` | API key CRUD |
| | `audit_routes.py` | Audit log query and chain verification |
| | `auth_routes.py` | Login, logout, refresh, user info |
| | `usage.py` | Usage tracking queries |
| | `metrics.py` | Prometheus metrics and alert endpoints |
| | `training.py` | Training jobs, datasets, adapters API |
| `cli/` | `main.py` | Typer CLI entry point and top-level commands |
| | `keys.py` | API key management subcommands |
| | `audit.py` | Audit query and verify subcommands |
| | `config_cmd.py` | Config show, validate, encrypt/decrypt subcommands |
| | `admin.py` | Admin tasks (audit-export, db-stats, token-cleanup) |
| | `training.py` | Training CLI subcommands |
| `engine/` | `process.py` | vLLM subprocess lifecycle (start, stop, health check) |
| | `proxy.py` | HTTP reverse proxy to vLLM (streaming + non-streaming) |
| | `router.py` | Model name → port resolution |
| | `lifecycle.py` | Idle sleep/wake daemon + request tracking |
| | `validators.py` | Configuration validation (GPU, speculative decoding) |
| `models/` | `registry.py` | Model state persistence (SQLite) |
| | `downloader.py` | HuggingFace Hub download with progress streaming |
| | `schemas.py` | Pydantic models for API request/response types |
| `security/` | `auth.py` | Authentication dispatch (API key, JWT, LDAP, OIDC) |
| | `ldap_auth.py` | LDAP/Active Directory integration |
| | `oidc.py` | OIDC token validation with JWKS caching |
| | `rbac.py` | Role-based access control (6 roles, permissions) |
| | `audit.py` | HIPAA-grade audit logging with SHA-256 hash chain |
| | `encryption.py` | AES-256 encryption at rest |
| | `vault.py` | HashiCorp Vault key provider |
| | `rate_limiter.py` | Per-API-key sliding window rate limiting |
| | `content_filter.py` | PHI/PII pattern detection (warn/redact/block) |
| | `secrets.py` | ENV[] and ENC[] secret resolution |
| | `usage.py` | Token usage tracking and aggregation |
| `observability/` | `metrics.py` | Dict-based Prometheus metrics (no external dependency) |
| | `middleware.py` | HTTP request metrics middleware |
| | `logging.py` | Structured logging (structlog) + RequestID middleware |
| | `alerts.py` | Rule-based alert evaluation |
| | `tasks.py` | Background scheduler (7 periodic tasks) |
| | `tracing.py` | OpenTelemetry integration |
| `middleware/` | `compression.py` | LLMlingua2 context compression |
| `training/` | `orchestrator.py` | Training job lifecycle and GPU scheduling |
| | `backend.py` | Training backend abstraction (LLaMA-Factory) |
| | `datasets.py` | Dataset upload, validation, and storage |
| | `adapters.py` | LoRA adapter registry and deployment |
| | `schemas.py` | Training data models |
| `utils/` | `gpu.py` | NVIDIA GPU introspection via nvidia-ml-py |
| Root | `main.py` | FastAPI app factory and lifespan management |
| | `config.py` | YAML configuration system (Pydantic) |
| | `db.py` | Async SQLite database layer |

## Source Tree

```
src/lean_ai_serve/
├── __init__.py                 # Package version
├── main.py                     # FastAPI app factory + lifespan
├── config.py                   # YAML config → Pydantic models
├── db.py                       # Async SQLite wrapper
├── api/                        # HTTP route handlers
│   ├── openai_compat.py        # /v1/chat/completions, /v1/completions, etc.
│   ├── models.py               # /api/models/* (CRUD + lifecycle)
│   ├── health.py               # /health, /api/status, /api/gpu
│   ├── keys.py                 # /api/keys/* (API key management)
│   ├── audit_routes.py         # /api/audit/* (query + verify)
│   ├── auth_routes.py          # /api/auth/* (login, logout, refresh)
│   ├── usage.py                # /api/usage/* (token tracking)
│   ├── metrics.py              # /metrics, /api/metrics/*
│   └── training.py             # /api/training/* (datasets, jobs, adapters)
├── cli/                        # Typer CLI commands
│   ├── main.py                 # Entry point + top-level commands
│   ├── keys.py                 # keys create/list/revoke
│   ├── audit.py                # audit query/verify
│   ├── config_cmd.py           # config show/validate/generate-key/encrypt-value
│   ├── admin.py                # admin audit-verify/audit-export/db-stats/token-cleanup
│   └── training.py             # training datasets/jobs/adapters
├── engine/                     # vLLM process management
│   ├── process.py              # Subprocess lifecycle
│   ├── proxy.py                # HTTP reverse proxy
│   ├── router.py               # Model → port resolver
│   ├── lifecycle.py            # Idle sleep/wake daemon
│   └── validators.py           # Pre-flight config validation
├── models/                     # Model management
│   ├── registry.py             # State persistence (SQLite)
│   ├── downloader.py           # HuggingFace download
│   └── schemas.py              # Pydantic schemas
├── security/                   # Authentication & compliance
│   ├── auth.py                 # Auth dispatcher
│   ├── ldap_auth.py            # LDAP/AD client
│   ├── oidc.py                 # OIDC/JWKS validator
│   ├── rbac.py                 # Role permissions
│   ├── audit.py                # Hash-chain audit logger
│   ├── encryption.py           # AES-256 encryption
│   ├── vault.py                # Vault key provider
│   ├── rate_limiter.py         # Sliding window rate limiter
│   ├── content_filter.py       # PHI/PII detection
│   ├── secrets.py              # ENV[]/ENC[] resolver
│   └── usage.py                # Usage tracker
├── observability/              # Monitoring & logging
│   ├── metrics.py              # Prometheus metrics collector
│   ├── middleware.py            # Request metrics middleware
│   ├── logging.py              # Structured logging setup
│   ├── alerts.py               # Alert rule evaluator
│   ├── tasks.py                # Background scheduler
│   └── tracing.py              # OpenTelemetry setup
├── middleware/                  # HTTP middleware
│   └── compression.py          # LLMlingua2 context compression
├── training/                   # Fine-tuning subsystem
│   ├── orchestrator.py         # Job lifecycle
│   ├── backend.py              # LLaMA-Factory backend
│   ├── datasets.py             # Dataset manager
│   ├── adapters.py             # LoRA adapter registry
│   └── schemas.py              # Training schemas
└── utils/                      # Utilities
    └── gpu.py                  # NVIDIA GPU info
```

## Database Schema

lean-ai-serve uses SQLite for all persistent state. Tables:

| Table | Purpose | Key Columns |
|-------|---------|-------------|
| `models` | Model registry | name, source, state, port, pid, gpu_assignment, config_json |
| `api_keys` | API key store | id, name, key_hash, key_prefix, role, models, rate_limit, expires_at |
| `audit_log` | Tamper-proof audit trail | id, timestamp, user_id, action, model, prompt_hash, chain_hash |
| `usage` | Hourly token usage | hour, user_id, model, prompt_tokens, completion_tokens |
| `revoked_tokens` | JWT revocation list | jti, revoked_at, expires_at |
| `training_jobs` | Fine-tuning jobs | job_id, model, state, dataset, adapter_id |
| `adapters` | LoRA adapter metadata | name, base_model, source_path, state |
| `datasets` | Training datasets | name, format, size_bytes, row_count, uploaded_by |

## Shutdown Sequence

Shutdown proceeds in reverse order with 15-second per-component timeout guards:

1. **Background scheduler** — cancel all periodic tasks
2. **Auth connectors** — close LDAP pool, OIDC, adapter registry (in parallel)
3. **Lifecycle manager** — stop idle/wake polling
4. **Process manager** — SIGTERM all vLLM subprocesses (30s grace → SIGKILL)
5. **Proxy client** — close httpx connection pool
6. **Database** — final writes, close connection
