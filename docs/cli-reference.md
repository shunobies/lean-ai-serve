# CLI Reference

`lean-ai-serve` is the command-line interface for the lean-ai-serve inference server. It is built on [Typer](https://typer.tiangolo.com/) with [Rich](https://rich.readthedocs.io/) console output.

**Entry point:** `lean-ai-serve` (installed via `pip install lean-ai-serve`)

```
lean-ai-serve [OPTIONS] COMMAND [ARGS]
```

---

## Global Options

| Option | Short | Description |
|--------|-------|-------------|
| `--version` | `-V` | Show version and exit. |
| `--help` | | Show help message and exit. |

```bash
lean-ai-serve --version
# lean-ai-serve 0.1.0
```

---

## Top-Level Commands

### `start`

Start the lean-ai-serve server.

```
lean-ai-serve start [OPTIONS]
```

Starts a uvicorn process with the FastAPI application. When TLS is enabled in the
configuration file, the server passes `ssl_certfile` and `ssl_keyfile` through to
uvicorn automatically.

**Options:**

| Option | Short | Type | Default | Description |
|--------|-------|------|---------|-------------|
| `--config` | `-c` | TEXT | `None` | Path to config.yaml |
| `--host` | | TEXT | `None` | Override bind host (falls back to `server.host` in config) |
| `--port` | `-p` | INTEGER | `None` | Override bind port (falls back to `server.port` in config) |

**Example:**

```bash
lean-ai-serve start -c config.yaml --host 0.0.0.0 --port 8000
```

```
TLS enabled
INFO:     Started server process [12345]
INFO:     Uvicorn running on https://0.0.0.0:8000
```

---

### `pull`

Download a model from HuggingFace Hub.

```
lean-ai-serve pull SOURCE [OPTIONS]
```

Checks that the HuggingFace repository exists, registers the model in the local
database, and streams the download with progress output.

**Arguments:**

| Argument | Required | Description |
|----------|----------|-------------|
| `SOURCE` | Yes | HuggingFace model ID (e.g. `Qwen/Qwen2.5-7B-Instruct`) |

**Options:**

| Option | Short | Type | Default | Description |
|--------|-------|------|---------|-------------|
| `--name` | `-n` | TEXT | `None` | Friendly name for the model. Defaults to the repo name portion of the source. |
| `--config` | `-c` | TEXT | `None` | Path to config.yaml |

**Example:**

```bash
lean-ai-serve pull Qwen/Qwen2.5-7B-Instruct -n qwen-7b -c config.yaml
```

```
Pulling Qwen/Qwen2.5-7B-Instruct as 'qwen-7b'
  Downloading model-00001-of-00004.safetensors...
  Downloading model-00002-of-00004.safetensors...
  Downloading model-00003-of-00004.safetensors...
  Downloading model-00004-of-00004.safetensors...
  Verifying...
Done! Model saved to /home/user/.cache/lean_ai_serve/models/Qwen--Qwen2.5-7B-Instruct
```

---

### `models`

List all registered models and their status.

```
lean-ai-serve models [OPTIONS]
```

Displays a Rich table with the current state of every model tracked by the registry.

**Options:**

| Option | Short | Type | Default | Description |
|--------|-------|------|---------|-------------|
| `--config` | `-c` | TEXT | `None` | Path to config.yaml |

**Example:**

```bash
lean-ai-serve models -c config.yaml
```

```
                           Models
┌────────────┬─────────────────────────┬────────────┬─────┬──────┬──────┬──────┐
│ Name       │ Source                  │ State      │ GPU │ Port │ Task │ LoRA │
├────────────┼─────────────────────────┼────────────┼─────┼──────┼──────┼──────┤
│ qwen-7b    │ Qwen/Qwen2.5-7B-Inst…  │ loaded     │ 0   │ 8001 │ gen  │ yes  │
│ mistral-7b │ mistralai/Mistral-7B-…  │ downloaded │     │ -    │ gen  │ no   │
└────────────┴─────────────────────────┴────────────┴─────┴──────┴──────┴──────┘
```

**State values:** `not_downloaded`, `downloading`, `downloaded`, `loading`, `loaded`, `sleeping`, `error`

---

### `load`

Load a downloaded model into vLLM for serving.

```
lean-ai-serve load NAME [OPTIONS]
```

Starts a vLLM subprocess for the given model. The model must be in `downloaded`,
`error`, or `sleeping` state.

**Arguments:**

| Argument | Required | Description |
|----------|----------|-------------|
| `NAME` | Yes | Model name to load |

**Options:**

| Option | Short | Type | Default | Description |
|--------|-------|------|---------|-------------|
| `--config` | `-c` | TEXT | `None` | Path to config.yaml |

**Example:**

```bash
lean-ai-serve load qwen-7b -c config.yaml
```

```
Loading qwen-7b...
Model loaded! port=8001 pid=54321
```

---

### `unload`

Unload a model (stop its vLLM process).

```
lean-ai-serve unload NAME [OPTIONS]
```

Stops the vLLM subprocess associated with the model and sets the model state back
to `downloaded`.

**Arguments:**

| Argument | Required | Description |
|----------|----------|-------------|
| `NAME` | Yes | Model name to unload |

**Options:**

| Option | Short | Type | Default | Description |
|--------|-------|------|---------|-------------|
| `--config` | `-c` | TEXT | `None` | Path to config.yaml |

**Example:**

```bash
lean-ai-serve unload qwen-7b -c config.yaml
```

```
Model 'qwen-7b' unloaded
```

---

### `status`

Show server status including GPUs and loaded models.

```
lean-ai-serve status [OPTIONS]
```

Queries NVIDIA GPUs via `nvidia-ml-py` and displays a table with memory usage,
utilization, temperature, and which model (if any) is loaded on each device.

**Options:**

| Option | Short | Type | Default | Description |
|--------|-------|------|---------|-------------|
| `--config` | `-c` | TEXT | `None` | Path to config.yaml |

**Example:**

```bash
lean-ai-serve status -c config.yaml
```

```
                              GPUs
┌───────┬────────────────────┬───────────────────┬─────────────┬──────┬──────────┐
│ Index │ Name               │ Memory (Used/Tot) │ Utilization │ Temp │ Model    │
├───────┼────────────────────┼───────────────────┼─────────────┼──────┼──────────┤
│     0 │ NVIDIA RTX 4090    │ 12045 / 24564 MB  │ 78%         │ 72C  │ qwen-7b  │
│     1 │ NVIDIA RTX 4090    │ 245 / 24564 MB    │ 0%          │ 38C  │ -        │
└───────┴────────────────────┴───────────────────┴─────────────┴──────┴──────────┘
```

---

### `check`

Pre-flight check: validate config, GPUs, and optional dependencies.

```
lean-ai-serve check [OPTIONS]
```

Runs a series of validation checks to ensure the system is properly configured
before starting the server. Checks include:

- Configuration file loads successfully
- Security settings (OIDC issuer, LDAP server) are consistent
- Tracing endpoint is set when tracing is enabled
- NVIDIA GPUs are detected
- Python is available in PATH
- Optional dependencies (`llmlingua` for context compression, `hvac` for Vault integration)

**Options:**

| Option | Short | Type | Default | Description |
|--------|-------|------|---------|-------------|
| `--config` | `-c` | TEXT | `None` | Path to config.yaml |

**Example:**

```bash
lean-ai-serve check -c config.yaml
```

```
  ✓ Config loaded successfully
  ✓ GPUs detected: 2
  ✓ Python available in PATH
  ✓ llmlingua available (context compression)
  ⚠ Vault key source configured but hvac not installed

1 warning(s)
```

---

## `keys` Subcommands

Manage API keys for authentication.

```
lean-ai-serve keys COMMAND [OPTIONS]
```

### `keys create`

Create a new API key.

```
lean-ai-serve keys create [OPTIONS]
```

Generates a new API key, stores its hash in the database, and prints the raw key
to the console. The raw key cannot be retrieved after creation.

**Options:**

| Option | Short | Type | Default | Description |
|--------|-------|------|---------|-------------|
| `--name` | | TEXT | **required** | Name for the API key |
| `--role` | | TEXT | `user` | Role: `admin`, `model-manager`, `trainer`, `user` |
| `--models` | | TEXT | `*` | Comma-separated model names or `*` for all models |
| `--rate-limit` | | INTEGER | `0` | Requests per minute (`0` = unlimited) |
| `--expires` | | INTEGER | `None` | Expire after N days |
| `--config` | `-c` | TEXT | `None` | Path to config.yaml |

**Example:**

```bash
lean-ai-serve keys create --name "dev-team" --role trainer --models "qwen-7b,mistral-7b" --rate-limit 60 --expires 90 -c config.yaml
```

```
API Key Created
  Name:  dev-team
  Role:  trainer
  ID:    a1b2c3d4-e5f6-7890-abcd-ef1234567890
  Key:   lais_k8Xm2pQ9vR4tY7wZ1bN3cF6gH0jL5sA8dE

Save this key — it cannot be retrieved later.
```

---

### `keys list`

List all API keys.

```
lean-ai-serve keys list [OPTIONS]
```

Displays a table of all registered API keys with their metadata. The full key
value is never shown -- only the prefix is displayed.

**Options:**

| Option | Short | Type | Default | Description |
|--------|-------|------|---------|-------------|
| `--config` | `-c` | TEXT | `None` | Path to config.yaml |

**Example:**

```bash
lean-ai-serve keys list -c config.yaml
```

```
                                     API Keys
┌──────────┬────────┬─────────┬────────┬────────────┬────────────┬─────────┬───────────┐
│ Name     │ Prefix │ Role    │ Models │ Rate Limit │ Created    │ Expires │ Last Used │
├──────────┼────────┼─────────┼────────┼────────────┼────────────┼─────────┼───────────┤
│ dev-team │ lais_  │ trainer │ qwen-… │ 60         │ 2025-01-15 │ 2025-04 │ 2025-01-… │
│ admin    │ lais_  │ admin   │ *      │ unlimited  │ 2025-01-01 │ never   │ 2025-01-… │
└──────────┴────────┴─────────┴────────┴────────────┴────────────┴─────────┴───────────┘
```

---

### `keys revoke`

Revoke an API key by prefix or ID.

```
lean-ai-serve keys revoke KEY_PREFIX [OPTIONS]
```

Permanently removes one or more API keys matching the given prefix or ID from
the database.

**Arguments:**

| Argument | Required | Description |
|----------|----------|-------------|
| `KEY_PREFIX` | Yes | Key prefix or ID to revoke |

**Options:**

| Option | Short | Type | Default | Description |
|--------|-------|------|---------|-------------|
| `--config` | `-c` | TEXT | `None` | Path to config.yaml |

**Example:**

```bash
lean-ai-serve keys revoke lais_k8Xm -c config.yaml
```

```
Revoked 1 key(s)
```

---

## `audit` Subcommands

Query and manage audit logs.

```
lean-ai-serve audit COMMAND [OPTIONS]
```

### `audit query`

Query recent audit log entries.

```
lean-ai-serve audit query [OPTIONS]
```

Displays a table of audit log entries with optional filters for user, action,
and model.

**Options:**

| Option | Short | Type | Default | Description |
|--------|-------|------|---------|-------------|
| `--user` | | TEXT | `None` | Filter by user ID |
| `--action` | | TEXT | `None` | Filter by action |
| `--model` | | TEXT | `None` | Filter by model |
| `--limit` | | INTEGER | `20` | Max entries to show |
| `--config` | `-c` | TEXT | `None` | Path to config.yaml |

**Example:**

```bash
lean-ai-serve audit query --user admin --action inference --limit 10 -c config.yaml
```

```
                        Audit Log (247 total)
┌─────────────────────┬───────┬───────────┬─────────┬─────────┬─────────┐
│ Time                │ User  │ Action    │ Model   │ Status  │ Latency │
├─────────────────────┼───────┼───────────┼─────────┼─────────┼─────────┤
│ 2025-01-15T14:23:01 │ admin │ inference │ qwen-7b │ success │ 342ms   │
│ 2025-01-15T14:22:58 │ admin │ inference │ qwen-7b │ success │ 289ms   │
└─────────────────────┴───────┴───────────┴─────────┴─────────┴─────────┘
```

---

### `audit verify`

Verify audit log hash chain integrity.

```
lean-ai-serve audit verify [OPTIONS]
```

Verifies the hash chain of the audit log to detect any tampering. Each log entry
contains a hash of the previous entry, forming an immutable chain.

**Options:**

| Option | Short | Type | Default | Description |
|--------|-------|------|---------|-------------|
| `--limit` | | INTEGER | `1000` | Number of entries to verify |
| `--config` | `-c` | TEXT | `None` | Path to config.yaml |

**Example:**

```bash
lean-ai-serve audit verify --limit 5000 -c config.yaml
```

```
Hash chain verified: 5000 entries OK
```

---

## `config` Subcommands

Configuration management.

```
lean-ai-serve config COMMAND [OPTIONS]
```

### `config show`

Show the resolved configuration (YAML + defaults).

```
lean-ai-serve config show [OPTIONS]
```

Displays the fully resolved configuration as JSON with syntax highlighting.
Sensitive fields (JWT secrets, bind passwords, tokens, client secrets) are masked
by default.

**Options:**

| Option | Short | Type | Default | Description |
|--------|-------|------|---------|-------------|
| `--config` | `-c` | TEXT | `None` | Path to config.yaml |
| `--raw` | | BOOL | `False` | Show without masking secrets |

**Example:**

```bash
lean-ai-serve config show -c config.yaml
```

```json
{
  "server": {
    "host": "0.0.0.0",
    "port": 8000
  },
  "security": {
    "mode": "oidc",
    "jwt_secret": "***REDACTED***",
    "oidc": {
      "issuer_url": "https://auth.example.com",
      "client_secret": "***REDACTED***"
    }
  },
  ...
}
```

---

### `config validate`

Validate configuration without starting the server.

```
lean-ai-serve config validate [OPTIONS]
```

Loads the configuration file, runs semantic checks, and prints a summary. Semantic
checks include:

- OIDC issuer URL must be set when security mode is `oidc`
- LDAP server URL must be set when security mode is `ldap`
- Tracing endpoint must be set when tracing is enabled
- Metrics must be enabled when alerts are enabled

**Options:**

| Option | Short | Type | Default | Description |
|--------|-------|------|---------|-------------|
| `--config` | `-c` | TEXT | `None` | Path to config.yaml |

**Example:**

```bash
lean-ai-serve config validate -c config.yaml
```

```
Configuration is valid

  Security mode: oidc
  Metrics: enabled
  Alerts: enabled
  Tracing: enabled
  Logging: INFO (JSON)
  Models: 3
  Training: enabled
```

---

### `config generate-key`

Generate a 256-bit master key for encrypting config secrets.

```
lean-ai-serve config generate-key OUTPUT
```

Writes a cryptographically random 256-bit key to the specified file and sets
file permissions to `600` (owner read/write only).

**Arguments:**

| Argument | Required | Description |
|----------|----------|-------------|
| `OUTPUT` | Yes | Path to write the key file |

**Example:**

```bash
lean-ai-serve config generate-key /etc/lean-ai-serve/master.key
```

```
Master key generated: /etc/lean-ai-serve/master.key
File permissions set to 600.  Keep this file safe.
```

---

### `config encrypt-value`

Encrypt a value for use in config.yaml as `ENC[...]`.

```
lean-ai-serve config encrypt-value VALUE [OPTIONS]
```

Encrypts a plaintext string using the master key so it can be stored safely in the
configuration file using the `ENC[...]` pattern. Provide either `--config` (to read
the key from the `encryption.at_rest` section) or `--key-file` (direct path).

**Arguments:**

| Argument | Required | Description |
|----------|----------|-------------|
| `VALUE` | Yes | Plain text value to encrypt |

**Options:**

| Option | Short | Type | Default | Description |
|--------|-------|------|---------|-------------|
| `--config` | `-c` | TEXT | `None` | Config file (reads key from `encryption.at_rest` section) |
| `--key-file` | `-k` | TEXT | `None` | Direct path to key file |

**Example:**

```bash
lean-ai-serve config encrypt-value "my-secret-password" -k /etc/lean-ai-serve/master.key
```

```
Encrypted value:
ENC[AES256:base64encodedciphertext==]

Paste this into your config.yaml
```

---

### `config decrypt-value`

Decrypt an `ENC[...]` value from config.yaml.

```
lean-ai-serve config decrypt-value VALUE [OPTIONS]
```

Decrypts an `ENC[...]` string back to plaintext using the master key.

**Arguments:**

| Argument | Required | Description |
|----------|----------|-------------|
| `VALUE` | Yes | `ENC[...]` string to decrypt |

**Options:**

| Option | Short | Type | Default | Description |
|--------|-------|------|---------|-------------|
| `--config` | `-c` | TEXT | `None` | Config file (reads key from `encryption.at_rest` section) |
| `--key-file` | `-k` | TEXT | `None` | Direct path to key file |

**Example:**

```bash
lean-ai-serve config decrypt-value "ENC[AES256:base64encodedciphertext==]" -k /etc/lean-ai-serve/master.key
```

```
Decrypted value: my-secret-password
```

---

## `admin` Subcommands

Administrative commands.

```
lean-ai-serve admin COMMAND [OPTIONS]
```

### `admin audit-verify`

Verify audit log hash chain integrity (admin version).

```
lean-ai-serve admin audit-verify [OPTIONS]
```

Same verification as `audit verify` but with a higher default limit suitable for
administrative auditing.

**Options:**

| Option | Short | Type | Default | Description |
|--------|-------|------|---------|-------------|
| `--config` | `-c` | TEXT | `None` | Path to config.yaml |
| `--limit` | `-n` | INTEGER | `10000` | Max entries to verify |

**Example:**

```bash
lean-ai-serve admin audit-verify -c config.yaml
```

```
✓ Hash chain verified: 10000 entries OK
```

---

### `admin audit-export`

Export audit log entries to JSON or CSV.

```
lean-ai-serve admin audit-export [OPTIONS]
```

Exports audit log entries with optional time range filtering. Output can be
directed to a file or printed to stdout.

**Options:**

| Option | Short | Type | Default | Description |
|--------|-------|------|---------|-------------|
| `--config` | `-c` | TEXT | `None` | Path to config.yaml |
| `--format` | `-f` | TEXT | `json` | Output format: `json` or `csv` |
| `--from` | | TEXT | `None` | Start time (ISO 8601, e.g. `2025-01-01T00:00:00`) |
| `--to` | | TEXT | `None` | End time (ISO 8601) |
| `--limit` | `-n` | INTEGER | `1000` | Max entries to export |
| `--output` | `-o` | TEXT | `None` | Output file path (default: stdout) |

**Example:**

```bash
lean-ai-serve admin audit-export -f csv --from 2025-01-01T00:00:00 --to 2025-01-31T23:59:59 -n 500 -o audit-jan.csv -c config.yaml
```

```
Exported 500 entries to audit-jan.csv
(500 of 1247 total entries)
```

---

### `admin token-cleanup`

Manually clean up expired revoked JWT tokens.

```
lean-ai-serve admin token-cleanup [OPTIONS]
```

Removes expired entries from the revoked tokens table. This is useful for keeping
the database lean; expired revoked tokens no longer need to be tracked since the
JWTs themselves have expired.

**Options:**

| Option | Short | Type | Default | Description |
|--------|-------|------|---------|-------------|
| `--config` | `-c` | TEXT | `None` | Path to config.yaml |

**Example:**

```bash
lean-ai-serve admin token-cleanup -c config.yaml
```

```
Cleaned up 23 expired revoked tokens
```

---

### `admin db-stats`

Show database table sizes and row counts.

```
lean-ai-serve admin db-stats [OPTIONS]
```

Lists all tracked database tables with their row counts and shows the total
database file size.

**Options:**

| Option | Short | Type | Default | Description |
|--------|-------|------|---------|-------------|
| `--config` | `-c` | TEXT | `None` | Path to config.yaml |

**Example:**

```bash
lean-ai-serve admin db-stats -c config.yaml
```

```
          Database Statistics
┌────────────────┬──────┐
│ Table          │ Rows │
├────────────────┼──────┤
│ models         │    3 │
│ api_keys       │    5 │
│ audit_log      │ 1247 │
│ usage          │  892 │
│ adapters       │    2 │
│ training_jobs  │    4 │
│ datasets       │    6 │
│ revoked_tokens │   12 │
└────────────────┴──────┘

  Total rows: 2171
  DB file size: 1.4 MB
```

---

## `db` Subcommands

Database setup and diagnostics.

```
lean-ai-serve db COMMAND [OPTIONS]
```

### `db init`

Initialize the database — create all tables and indexes.

```
lean-ai-serve db init [OPTIONS]
```

Connects to the configured database backend, creates all tables and indexes using
SQLAlchemy's `metadata.create_all()`, and reports success. For SQLite this happens
automatically on startup, but for PostgreSQL, Oracle, or MySQL you should run this
once after configuring your `database.url`.

**Options:**

| Option | Short | Type | Default | Description |
|--------|-------|------|---------|-------------|
| `--config` | `-c` | TEXT | `None` | Path to config.yaml |

**Example:**

```bash
lean-ai-serve db init -c config.yaml
```

```
Connected to postgresql database
  Created/verified 8 tables
Database ready.
```

---

### `db check`

Verify that all expected tables exist in the database.

```
lean-ai-serve db check [OPTIONS]
```

Inspects the database schema and reports any missing tables. Useful after upgrades
or when troubleshooting database issues.

**Options:**

| Option | Short | Type | Default | Description |
|--------|-------|------|---------|-------------|
| `--config` | `-c` | TEXT | `None` | Path to config.yaml |

**Example:**

```bash
lean-ai-serve db check -c config.yaml
```

```
All 8 tables present
```

If tables are missing:

```
Found 6/8 tables
  Missing: training_jobs
  Missing: datasets

Run 'lean-ai-serve db init' to create missing tables.
```

---

### `db info`

Show database connection info and table row counts.

```
lean-ai-serve db info [OPTIONS]
```

Displays the database backend type, file path (for SQLite), file size, and row
counts for all tracked tables.

**Options:**

| Option | Short | Type | Default | Description |
|--------|-------|------|---------|-------------|
| `--config` | `-c` | TEXT | `None` | Path to config.yaml |

**Example:**

```bash
lean-ai-serve db info -c config.yaml
```

```
  Backend:  sqlite
  Path:     /home/user/.cache/lean-ai-serve/lean_ai_serve.db
  Size:     1.4 MB

          Tables
┌────────────────┬──────┐
│ Table          │ Rows │
├────────────────┼──────┤
│ models         │    3 │
│ api_keys       │    5 │
│ audit_log      │ 1247 │
│ usage          │  892 │
│ adapters       │    2 │
│ training_jobs  │    4 │
│ datasets       │    6 │
│ revoked_tokens │   12 │
└────────────────┴──────┘

  Total rows: 2171
```

---

## `training` Subcommands

Manage training jobs, datasets, and adapters.

```
lean-ai-serve training COMMAND [OPTIONS]
```

### `training datasets`

List uploaded training datasets.

```
lean-ai-serve training datasets [OPTIONS]
```

Displays a table of all datasets registered in the system.

**Options:**

| Option | Short | Type | Default | Description |
|--------|-------|------|---------|-------------|
| `--config` | `-c` | TEXT | `None` | Path to config.yaml |

**Example:**

```bash
lean-ai-serve training datasets -c config.yaml
```

```
                           Datasets
┌──────────────┬────────┬──────┬──────────┬─────────────┬────────────┐
│ Name         │ Format │ Rows │ Size     │ Uploaded By │ Created    │
├──────────────┼────────┼──────┼──────────┼─────────────┼────────────┤
│ chat-v1      │ jsonl  │ 5000 │ 12.3 KB  │ admin       │ 2025-01-10 │
│ instruct-v2  │ jsonl  │ 8200 │ 24.1 KB  │ trainer1    │ 2025-01-12 │
└──────────────┴────────┴──────┴──────────┴─────────────┴────────────┘
```

---

### `training jobs`

List training jobs.

```
lean-ai-serve training jobs [OPTIONS]
```

Displays a table of all training jobs with optional state filtering. Valid states:
`queued`, `running`, `completed`, `failed`, `cancelled`.

**Options:**

| Option | Short | Type | Default | Description |
|--------|-------|------|---------|-------------|
| `--state` | | TEXT | `None` | Filter by state |
| `--config` | `-c` | TEXT | `None` | Path to config.yaml |

**Example:**

```bash
lean-ai-serve training jobs --state running -c config.yaml
```

```
                              Training Jobs
┌──────────┬────────────┬────────────┬──────────┬─────────┬──────────────┬──────────────┐
│ ID       │ Name       │ Base Model │ Dataset  │ State   │ Submitted By │ Submitted At │
├──────────┼────────────┼────────────┼──────────┼─────────┼──────────────┼──────────────┤
│ a1b2c3d4 │ lora-qwen  │ qwen-7b    │ chat-v1  │ running │ trainer1     │ 2025-01-15   │
└──────────┴────────────┴────────────┴──────────┴─────────┴──────────────┴──────────────┘
```

---

### `training adapters`

List registered LoRA adapters.

```
lean-ai-serve training adapters [OPTIONS]
```

Displays a table of all LoRA adapters with optional base model filtering.

**Options:**

| Option | Short | Type | Default | Description |
|--------|-------|------|---------|-------------|
| `--model` | | TEXT | `None` | Filter by base model |
| `--config` | `-c` | TEXT | `None` | Path to config.yaml |

**Example:**

```bash
lean-ai-serve training adapters --model qwen-7b -c config.yaml
```

```
                           Adapters
┌────────────┬────────────┬───────────┬──────────┬────────────┬──────────┐
│ Name       │ Base Model │ State     │ Job ID   │ Created    │ Deployed │
├────────────┼────────────┼───────────┼──────────┼────────────┼──────────┤
│ chat-lora  │ qwen-7b    │ deployed  │ a1b2c3d4 │ 2025-01-15 │ 2025-01… │
│ instruct-v │ qwen-7b    │ available │ e5f6g7h8 │ 2025-01-14 │ -        │
└────────────┴────────────┴───────────┴──────────┴────────────┴──────────┘
```
