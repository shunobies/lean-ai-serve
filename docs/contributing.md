# Contributing

This guide covers development setup, project structure, testing, and code style for contributing to lean-ai-serve.

## Development Setup

```bash
# Clone the repository
git clone https://github.com/your-org/lean-ai-serve.git
cd lean-ai-serve

# Create virtual environment
python3.11 -m venv .venv
source .venv/bin/activate

# Install in development mode with all extras
pip install -e ".[dev,gpu,ldap,vault,compression,training,tracing]"
```

## Project Structure

```
lean-ai-serve/
├── src/lean_ai_serve/          # Main source code
│   ├── api/                    # FastAPI route handlers (9 modules)
│   ├── cli/                    # Typer CLI commands (6 modules)
│   ├── engine/                 # vLLM process management (5 modules)
│   ├── models/                 # Model registry & schemas (3 modules)
│   ├── security/               # Auth, audit, encryption (11 modules)
│   ├── observability/          # Metrics, logging, tracing (6 modules)
│   ├── middleware/              # HTTP middleware (1 module)
│   ├── training/               # Fine-tuning subsystem (5 modules)
│   ├── utils/                  # GPU utilities (1 module)
│   ├── main.py                 # FastAPI app factory + lifespan
│   ├── config.py               # YAML configuration (Pydantic)
│   └── db.py                   # Async SQLite wrapper
├── tests/                      # Test suite (32+ test modules)
├── docs/                       # Documentation
├── dashboards/                 # Grafana dashboard JSON
├── config.example.yaml         # Annotated config template
└── pyproject.toml              # Project metadata & dependencies
```

See [architecture.md](architecture.md) for a complete component map.

## Running Tests

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=lean_ai_serve --cov-report=term-missing

# Run a specific test file
pytest tests/test_auth.py

# Run a specific test
pytest tests/test_auth.py::test_api_key_validation

# Verbose output
pytest -v
```

### Test configuration

Tests use `asyncio_mode = "auto"` (from `pyproject.toml`). Key test fixtures in `tests/conftest.py`:

- `settings` — Creates a temporary config with `security.mode = "none"` and a temp cache directory
- `db` — Fresh in-memory SQLite database per test
- `event_loop` — Session-scoped event loop for all async tests

### Test organization

| Test File | What It Tests |
|-----------|--------------|
| `test_auth.py` | API key and JWT authentication |
| `test_oidc.py`, `test_oidc_api.py` | OIDC token validation and API integration |
| `test_ldap.py` | LDAP authentication |
| `test_rbac.py` | Role-based access control |
| `test_rate_limiter.py` | Sliding window rate limiting |
| `test_audit.py` | Audit logging and hash chain |
| `test_encryption.py` | AES-256 encryption |
| `test_vault.py` | HashiCorp Vault integration |
| `test_secrets.py` | ENV[] and ENC[] resolution |
| `test_config.py`, `test_config_env.py` | Configuration loading |
| `test_registry.py` | Model state management |
| `test_metrics.py` | Prometheus metrics collector |
| `test_metrics_middleware.py` | Request metrics middleware |
| `test_alerts.py` | Alert rule evaluation |
| `test_compression.py` | Context compression |
| `test_content_filter.py` | PHI/PII content filtering |
| `test_lifecycle.py` | Idle sleep/wake |
| `test_validators.py` | Configuration validation |
| `test_training_*.py` | Training subsystem (backend, orchestrator, API) |
| `test_datasets.py` | Dataset management |
| `test_adapters.py` | LoRA adapter registry |
| `test_admin_cli.py` | CLI commands |

### Mocking

All external services are mocked in tests:
- vLLM subprocess → mocked ProcessManager
- LDAP server → mocked ldap3 connection
- OIDC JWKS → mocked HTTP responses
- Vault → mocked hvac client
- LLMlingua → mocked compressor
- HuggingFace → mocked download functions

## Code Style

lean-ai-serve uses [ruff](https://github.com/astral-sh/ruff) for linting and formatting.

### Configuration (from `pyproject.toml`):

```toml
[tool.ruff]
target-version = "py311"
line-length = 100
src = ["src", "tests"]

[tool.ruff.lint]
select = ["E", "F", "W", "I", "N", "UP", "B", "A", "SIM"]
ignore = ["B008"]  # FastAPI Depends() pattern
```

### Run linter

```bash
# Check
ruff check src/ tests/

# Auto-fix
ruff check --fix src/ tests/

# Format
ruff format src/ tests/
```

## Conventions

### Adding a new API endpoint

1. Create or extend a router file in `src/lean_ai_serve/api/`
2. Use `Depends(require_permission("permission:name"))` for auth
3. Access shared state via `request.app.state.<component>`
4. Add Pydantic models to `src/lean_ai_serve/models/schemas.py`
5. Register the router in `create_app()` in `main.py`
6. Write tests in `tests/`

### Adding a new CLI command

1. For top-level commands: add to `src/lean_ai_serve/cli/main.py`
2. For subcommands: create a new file in `cli/` and register with `app.add_typer()`
3. Use `_init_settings(config)` to load config
4. Use `_run(async_func())` for async operations
5. Use Rich console for output formatting

### Async patterns

- All database operations are async (aiosqlite)
- Use `asyncio.create_task()` for fire-and-forget background work
- Shared state is stored on `app.state` (not global singletons)
- FastAPI `Depends()` for request-scoped dependency injection
