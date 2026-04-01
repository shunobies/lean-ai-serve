# Phase 6: Context Compression, Vault Integration & Production Polish

## Context

Phases 1-5 are complete. Phase 6 closes the final feature gaps, adds the last two unimplemented config options (context compression and Vault key storage), and hardens the system for production deployment. This phase focuses on polish: multi-worker safety, graceful shutdown improvements, CLI completeness, and documentation.

**What's already built and ready:**
- `ContextCompressionConfig` with `enabled`, `method`, `target_ratio`, `min_length` (`config.py:173-177`)
- `compression` optional dependency group: `llmlingua>=0.2.0` (`pyproject.toml:72`)
- `EncryptionAtRestConfig.key_source` supports "vault" but raises `NotImplementedError` (`encryption.py:62-66`)
- `KVCacheConfig.calculate_scales` and `turboquant_bits` defined but not wired (`config.py:99-100`)
- `load_settings()` note about env overrides — addressed in Phase 5
- All core features implemented: auth (API key, LDAP, OIDC), RBAC, audit, training, lifecycle, metrics, usage

---

## New Files

### 1. `middleware/compression.py` — Context compression middleware

- `ContextCompressor(config: ContextCompressionConfig)`:
  - `initialize()` — load LLMlingua2 model (lazy, on first use)
  - `compress(text: str) -> str`:
    - Skip if `len(text) < min_length`
    - Apply LLMlingua2 compression with `target_ratio`
    - Return compressed text
  - `compress_messages(messages: list[dict]) -> list[dict]`:
    - Compress long user/system messages in a chat completions payload
    - Preserve recent messages (last 2 turns) uncompressed
    - Only compress messages exceeding `min_length` characters

- `CompressionMiddleware(app, compressor)`:
  - Intercept POST `/v1/chat/completions` and `/v1/completions`
  - Parse JSON body, compress eligible message content
  - Forward compressed payload to vLLM
  - Add `X-Context-Compressed: true` response header when compression was applied
  - Add `X-Context-Original-Length` and `X-Context-Compressed-Length` headers

### 2. `security/vault.py` — HashiCorp Vault integration

- `VaultKeyProvider(config)`:
  - `fetch_key() -> bytes`:
    - Connect to Vault via `VAULT_ADDR` and `VAULT_TOKEN` env vars
    - Read secret from configurable path (default: `secret/data/lean-ai-serve/encryption-key`)
    - Supports token auth and AppRole auth
    - Retry with exponential backoff on transient errors
  - `rotate_key()` — fetch new key version, re-encrypt existing audit data
  - Key caching with TTL to avoid hitting Vault on every request

- Config additions to `EncryptionAtRestConfig`:
  ```python
  vault_path: str = "secret/data/lean-ai-serve/encryption-key"
  vault_key_field: str = "key"
  vault_auth_method: str = "token"  # token, approle
  vault_role_id_env: str = "VAULT_ROLE_ID"
  vault_secret_id_env: str = "VAULT_SECRET_ID"
  ```

### 3. `middleware/__init__.py` — empty package

### 4. `cli/config_commands.py` — Config management CLI

- `config show` — dump fully resolved config (YAML + env overrides) with secrets masked
- `config validate [path]` — validate a config file, report errors/warnings
- `config generate` — generate a starter config.yaml interactively
- `config diff <file1> <file2>` — compare two config files, highlight differences

### 5. `cli/admin_commands.py` — Administrative CLI commands

- `admin audit-verify` — verify audit log hash chain from CLI
- `admin audit-export --from --to --format [json|csv]` — export audit logs
- `admin token-cleanup` — manually trigger revoked token cleanup
- `admin db-stats` — show database table sizes and row counts
- `admin generate-key` — generate an encryption key file

---

## Existing File Modifications

### `security/encryption.py` — Vault integration
- Replace `NotImplementedError` for vault key_source with `VaultKeyProvider.fetch_key()`
- Add key rotation support: `re_encrypt_audit_log(old_key, new_key)`
- Add `key_source="vault"` path in `_load_key()`

### `engine/process.py` — Final command building gaps
- Wire `KVCacheConfig.calculate_scales`:
  ```python
  if config.kv_cache.calculate_scales:
      cmd.append("--calculate-kv-scales")
  ```
- Wire `KVCacheConfig.turboquant_bits` (when dtype is turboquant):
  ```python
  if config.kv_cache.dtype == "turboquant":
      cmd.extend(["--kv-cache-dtype", f"turboquant_{config.kv_cache.turboquant_bits}"])
  ```
- Add `CUDA_VISIBLE_DEVICES` scoping if not done in Phase 4

### `main.py` — Wire compression middleware
- If `settings.context_compression.enabled`:
  ```python
  from lean_ai_serve.middleware.compression import CompressionMiddleware, ContextCompressor
  compressor = ContextCompressor(settings.context_compression)
  app.add_middleware(CompressionMiddleware, compressor=compressor)
  ```
- Improve shutdown: add timeout guards, log component shutdown times, handle partial init failures gracefully

### `cli/main.py` — Register new command groups
- Add `config` subcommand group from `cli/config_commands.py`
- Add `admin` subcommand group from `cli/admin_commands.py`
- Add `--version` flag to root command
- Add `lean-ai-serve check` — comprehensive startup pre-check (config + GPU + deps + permissions)

### `config.py` — Vault config additions
- Extend `EncryptionAtRestConfig` with vault-specific fields
- Add validation: when `key_source="vault"`, ensure `VAULT_ADDR` env exists

### `config.example.yaml` — Complete documentation
- Document all config sections with comments
- Add vault encryption example
- Add context compression example
- Add multi-GPU examples with tensor/pipeline parallelism

### `pyproject.toml` — Optional dependencies
```toml
vault = ["hvac>=2.1.0,<3.0"]  # HashiCorp Vault client
```

---

## Implementation Order

| Step | Module | Depends On |
|------|--------|------------|
| 1 | Context compression middleware + tests | None |
| 2 | Vault key provider + tests | None |
| 3 | Wire vault into encryption.py | Step 2 |
| 4 | Final process.py command gaps (KV cache) | None |
| 5 | CLI config commands + tests | None |
| 6 | CLI admin commands + tests | None |
| 7 | Wire compression into main.py | Step 1 |
| 8 | Shutdown hardening + graceful degradation | None |
| 9 | config.example.yaml — complete documentation | Steps 1-8 |
| 10 | Verify all existing + new tests pass, lint clean | Steps 1-9 |

---

## Tests

- `test_compression.py`: compress long text (verify ratio), skip short text, preserve recent messages, message list compression, middleware intercept (mocked LLMlingua2), header injection, disabled passthrough
- `test_vault.py`: token auth fetch, approle auth fetch, key caching, connection error retry, missing env var error, key rotation
- `test_encryption_vault.py`: end-to-end encrypt/decrypt with vault-sourced key (mocked Vault)
- `test_process_kvcache.py`: calculate_scales flag, turboquant dtype formatting, combined options
- `test_config_cli.py`: show (secrets masked), validate (valid + invalid), generate output format
- `test_admin_cli.py`: audit-verify, db-stats, token-cleanup, generate-key

All Vault calls mocked via httpx/responses — no real Vault server needed.

---

## Verification

1. **Context compression**: Long prompt → compressed by LLMlingua2 → shorter payload to vLLM → correct response
2. **Compression skip**: Short prompt (`< min_length`) → passes through uncompressed
3. **Vault encryption**: `key_source=vault` → key fetched from mocked Vault → encrypt/decrypt works
4. **Vault AppRole**: `vault_auth_method=approle` → authenticates with role_id/secret_id
5. **KV cache args**: `kv_cache.calculate_scales=true` → `--calculate-kv-scales` in vLLM command
6. **Config CLI**: `lean-ai-serve config show` → resolved config with `jwt_secret: "***"` masked
7. **Config validate**: Invalid YAML → clear error message with line number
8. **Admin verify**: `lean-ai-serve admin audit-verify` → "Chain verified: N entries OK"
9. **Graceful shutdown**: SIGTERM → all components shut down in order with timeout guards
10. **All existing + new tests pass**
11. **Lint clean**: `ruff check src/ tests/`

---

## Post-Phase 6: Future Considerations

These items are intentionally deferred beyond Phase 6:

- **Web dashboard UI** — React/Vue frontend for model management, monitoring, and training
- **Multi-instance / HA** — Distributed rate limiting (Redis), shared state, leader election
- **Model A/B testing** — Traffic splitting between model versions
- **Prompt templates** — Stored prompt template library with versioning
- **Webhook notifications** — Training completion, model health alerts, quota warnings
- **Plugin system** — Custom middleware/backend extensibility
- **Kubernetes operator** — CRD-based model deployment on K8s
- **Cost tracking** — Per-user/per-model cost estimation and billing integration
