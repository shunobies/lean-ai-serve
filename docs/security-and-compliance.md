# Security & Compliance

This guide covers lean-ai-serve's security features designed for enterprise and regulated environments, including HIPAA-grade audit logging, encryption at rest, HashiCorp Vault integration, and content filtering.

## HIPAA-Grade Audit Logging

The audit system provides a tamper-proof, append-only log of all significant operations.

### How it works

Every audit entry is chained using SHA-256 hashes:

```
Genesis Block (hash₀)
    ↓
Entry 1: hash₁ = SHA256(hash₀ + entry₁_data)
    ↓
Entry 2: hash₂ = SHA256(hash₁ + entry₂_data)
    ↓
Entry 3: hash₃ = SHA256(hash₂ + entry₃_data)
```

Each entry includes the hash of the previous entry (`parent_hash`), creating an immutable chain. Any modification to a past entry breaks the chain, which is detectable via verification.

### What is logged

| Field | Description |
|-------|-------------|
| `timestamp` | ISO 8601 timestamp |
| `request_id` | Unique request identifier |
| `user_id` | Authenticated user |
| `user_role` | User's role(s) |
| `source_ip` | Client IP address |
| `action` | Operation type (inference, model_load, key_create, etc.) |
| `model` | Model name (if applicable) |
| `prompt_hash` | SHA-256 hash of the prompt |
| `response_hash` | SHA-256 hash of the response |
| `token_count` | Total tokens used |
| `latency_ms` | Request latency |
| `status` | success or error |
| `chain_hash` | SHA-256 chain hash |

### Configuration

```yaml
audit:
  enabled: true
  log_prompts: true              # Log full prompt/response content
  log_prompts_hash_only: false   # Set true to store only hashes (for PII compliance)
  retention_days: 2190           # 6 years (HIPAA minimum)
  storage: "sqlite"
```

### Prompt logging modes

| Mode | `log_prompts` | `log_prompts_hash_only` | Stored |
|------|:---:|:---:|--------|
| Full content | true | false | Full prompt + response (optionally encrypted) |
| Hash only | true | true | SHA-256 hashes of prompt + response |
| Disabled | false | - | No prompt/response data |

### Chain verification

**CLI:**

```bash
# Verify last 10,000 entries
lean-ai-serve admin audit-verify --limit 10000
```

```
✓ Hash chain valid: verified 10000 entries
```

**API:**

```bash
curl http://localhost:8420/api/audit/verify?limit=10000 \
  -H "Authorization: Bearer las-..."
```

```json
{
  "valid": true,
  "message": "Hash chain valid: verified 10000 entries"
}
```

### Querying audit logs

```bash
# CLI
lean-ai-serve audit query --user jdoe --action inference --limit 50

# API
curl "http://localhost:8420/api/audit/logs?user_id=jdoe&action=inference&limit=50" \
  -H "Authorization: Bearer las-..."
```

### Exporting audit logs

```bash
# Export to CSV
lean-ai-serve admin audit-export \
  --format csv \
  --from 2026-01-01 \
  --to 2026-03-31 \
  --output audit-q1-2026.csv

# Export to JSON
lean-ai-serve admin audit-export \
  --format json \
  --limit 5000 \
  --output audit-export.json
```

## Encryption at Rest

Sensitive data (audit prompts/responses, config secrets) can be encrypted using AES-256-GCM.

### Configuration

```yaml
encryption:
  at_rest:
    enabled: true
    key_source: "file"               # file, env, or vault
    key_file: "/etc/lean-ai-serve/master.key"
```

### Master key management

#### Option 1: File-based key

```bash
# Generate a 256-bit master key
lean-ai-serve config generate-key /etc/lean-ai-serve/master.key
# File permissions automatically set to 600
```

```yaml
encryption:
  at_rest:
    enabled: true
    key_source: "file"
    key_file: "/etc/lean-ai-serve/master.key"
```

#### Option 2: Environment variable

```bash
export LEAN_AI_ENCRYPTION_KEY="hex-or-base64-encoded-32-bytes"
```

```yaml
encryption:
  at_rest:
    enabled: true
    key_source: "env"
    key_env_var: "LEAN_AI_ENCRYPTION_KEY"
```

#### Option 3: HashiCorp Vault

See [Vault integration](#hashicorp-vault-integration) below.

## HashiCorp Vault Integration

lean-ai-serve can fetch the master encryption key from HashiCorp Vault.

### Prerequisites

```bash
pip install lean-ai-serve[vault]
```

### Token authentication

```yaml
encryption:
  at_rest:
    enabled: true
    key_source: "vault"
    vault_path: "secret/data/lean-ai-serve/encryption-key"
    vault_key_field: "key"
    vault_auth_method: "token"
    vault_cache_ttl: 300
```

```bash
export VAULT_ADDR="https://vault.corp.com:8200"
export VAULT_TOKEN="hvs...."
```

### AppRole authentication

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

```bash
export VAULT_ADDR="https://vault.corp.com:8200"
export VAULT_ROLE_ID="..."
export VAULT_SECRET_ID="..."
```

### Vault key format

The Vault secret should contain the encryption key as a hex or base64-encoded string in the configured field:

```bash
vault kv put secret/lean-ai-serve/encryption-key \
  key="$(openssl rand -hex 32)"
```

### Caching and retry

- Keys are cached in memory for `vault_cache_ttl` seconds (default: 300)
- Failed Vault requests retry 3 times with exponential backoff (1s, 2s, 4s)

## Config Secret Management

Sensitive values in `config.yaml` support two patterns:

### ENV[] — Environment variables

```yaml
security:
  jwt_secret: "ENV[MY_JWT_SECRET]"

cache:
  huggingface_token: "ENV[HF_TOKEN]"
```

Values are resolved from environment variables at config load time.

### ENC[] — Encrypted values

```yaml
security:
  jwt_secret: "ENC[base64-encoded-ciphertext...]"
```

Values are decrypted at load time using the master key from `encryption.at_rest`.

### Encryption workflow

```bash
# 1. Generate master key
lean-ai-serve config generate-key /etc/lean-ai-serve/master.key

# 2. Encrypt a value
lean-ai-serve config encrypt-value "my-jwt-secret" -k /etc/lean-ai-serve/master.key
# Output: ENC[base64...]

# 3. Paste into config.yaml
# jwt_secret: "ENC[base64...]"

# 4. Configure encryption section
# encryption:
#   at_rest:
#     enabled: true
#     key_source: "file"
#     key_file: "/etc/lean-ai-serve/master.key"
```

## Content Filtering

Content filtering scans inference requests for sensitive patterns (PHI, PII) and takes configurable actions.

### Configuration

```yaml
security:
  content_filtering:
    enabled: true
    patterns:
      - name: "SSN"
        pattern: '\b\d{3}-\d{2}-\d{4}\b'
        action: "block"
      - name: "MRN"
        pattern: '\bMRN[:\s]?\d{6,}\b'
        action: "redact"
      - name: "Email"
        pattern: '\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'
        action: "warn"
```

### Actions

| Action | Behavior |
|--------|----------|
| `warn` | Log a warning, allow request to proceed |
| `redact` | Replace matched content with `[REDACTED]`, allow request |
| `block` | Reject the request with 400 error |

### Custom pattern files

```yaml
security:
  content_filtering:
    enabled: true
    custom_patterns_file: "/etc/lean-ai-serve/filter-patterns.yaml"
```

## TLS Configuration

Enable HTTPS for production deployments:

```yaml
server:
  tls:
    enabled: true
    cert_file: "/etc/ssl/certs/lean-ai-serve.pem"
    key_file: "/etc/ssl/private/lean-ai-serve.key"
```

## HIPAA Compliance Checklist

| Control | lean-ai-serve Feature |
|---------|----------------------|
| Access control | API key / LDAP / OIDC authentication with RBAC |
| Audit controls | SHA-256 hash-chain audit log with verification |
| Integrity controls | Tamper-proof audit chain, encryption at rest |
| Transmission security | TLS support, encrypted secrets |
| Person authentication | Multi-factor via OIDC, unique API keys |
| Audit log retention | Configurable retention (default: 6 years) |
| PHI safeguards | Content filtering (SSN, MRN detection), hash-only prompt logging |
| Encryption at rest | AES-256-GCM for audit data and config secrets |
