# Authentication & Authorization

lean-ai-serve supports multiple authentication methods that can be used individually or combined. All authenticated requests include role-based access control (RBAC).

## Authentication Modes

Configure the mode in `config.yaml`:

```yaml
security:
  mode: "api_key"  # or: ldap, oidc, ldap+api_key, oidc+api_key
```

| Mode | Description | Token Type |
|------|-------------|------------|
| `api_key` | API key authentication only | `las-...` bearer token |
| `ldap` | LDAP/Active Directory login → JWT sessions | JWT bearer token |
| `oidc` | External OIDC provider (Keycloak, Azure AD, etc.) | OIDC JWT bearer token |
| `ldap+api_key` | Both LDAP sessions and API keys accepted | Either |
| `oidc+api_key` | Both OIDC tokens and API keys accepted | Either |
| `none` | No authentication (development only) | None required |

## API Key Authentication

API keys are the simplest auth method. Keys are bcrypt-hashed and stored in the database.

### Creating keys

**CLI:**

```bash
# Admin key with unlimited access
lean-ai-serve keys create --name "admin-key" --role admin

# User key restricted to specific models with rate limiting
lean-ai-serve keys create \
  --name "app-key" \
  --role user \
  --models "my-model,embed-model" \
  --rate-limit 60 \
  --expires 90
```

**API:**

```bash
curl -X POST http://localhost:8420/api/keys \
  -H "Authorization: Bearer las-<admin-key>" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "new-key",
    "role": "user",
    "models": ["my-model"],
    "rate_limit": 60,
    "expires_days": 90
  }'
```

### Key format

Keys use the prefix `las-` (lean-ai-serve) followed by a random token. The full key is shown only at creation time and cannot be retrieved later. Only the first 8 characters (prefix) are stored for identification.

### Using API keys

Include the key in the `Authorization` header:

```bash
curl http://localhost:8420/v1/chat/completions \
  -H "Authorization: Bearer las-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" \
  -H "Content-Type: application/json" \
  -d '{"model": "my-model", "messages": [{"role": "user", "content": "Hello"}]}'
```

### Managing keys

```bash
# List all keys
lean-ai-serve keys list

# Revoke by prefix or ID
lean-ai-serve keys revoke las-abc1
```

## LDAP Authentication

LDAP mode authenticates users against an LDAP/Active Directory server and issues JWT session tokens.

### Configuration

```yaml
security:
  mode: "ldap"
  jwt_secret: "ENV[JWT_SECRET]"  # Required for JWT signing
  jwt_expiry_hours: 8.0

  ldap:
    server_url: "ldaps://ad.corp.com:636"
    bind_dn: "CN=svc-lean-ai,OU=ServiceAccounts,DC=corp,DC=com"
    bind_password_env: "LEAN_AI_LDAP_BIND_PASSWORD"
    user_search_base: "OU=Users,DC=corp,DC=com"
    user_search_filter: "(sAMAccountName={username})"
    group_search_base: "OU=Groups,DC=corp,DC=com"
    group_role_mapping:
      "CN=AI-Admins,OU=Groups,DC=corp,DC=com": "admin"
      "CN=AI-ModelManagers,OU=Groups,DC=corp,DC=com": "model-manager"
      "CN=AI-Trainers,OU=Groups,DC=corp,DC=com": "trainer"
      "CN=AI-Users,OU=Groups,DC=corp,DC=com": "user"
      "CN=AI-Auditors,OU=Groups,DC=corp,DC=com": "auditor"
    default_role: "user"
    cache_ttl: 300
    connection_pool_size: 5
```

### Login flow

```bash
# Login with LDAP credentials
curl -X POST http://localhost:8420/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username": "jdoe", "password": "secret"}'
```

Response:

```json
{
  "token": "eyJhbGciOiJIUzI1NiIs...",
  "expires_at": "2026-04-02T04:00:00Z",
  "user": "jdoe",
  "roles": ["user"]
}
```

### Using JWT tokens

```bash
curl http://localhost:8420/v1/chat/completions \
  -H "Authorization: Bearer eyJhbGciOiJIUzI1NiIs..." \
  -H "Content-Type: application/json" \
  -d '{"model": "my-model", "messages": [{"role": "user", "content": "Hello"}]}'
```

### Token refresh and logout

```bash
# Refresh token (revokes old, issues new)
curl -X POST http://localhost:8420/api/auth/refresh \
  -H "Authorization: Bearer eyJ..."

# Logout (revokes token)
curl -X POST http://localhost:8420/api/auth/logout \
  -H "Authorization: Bearer eyJ..."
```

### Check current user

```bash
curl http://localhost:8420/api/auth/me \
  -H "Authorization: Bearer eyJ..."
```

```json
{
  "user_id": "jdoe",
  "display_name": "jdoe",
  "roles": ["user"],
  "allowed_models": ["*"],
  "auth_method": "ldap"
}
```

## OIDC Authentication

OIDC mode validates JWT tokens issued by an external identity provider (Keycloak, Azure AD, Okta, Auth0, etc.).

### Configuration

```yaml
security:
  mode: "oidc"

  oidc:
    issuer_url: "https://keycloak.corp.com/realms/ai"
    client_id: "lean-ai-serve"
    audience: "lean-ai-serve"
    roles_claim: "realm_access.roles"  # Dot-notation path in JWT payload
    role_mapping:
      "ai-admin": "admin"
      "ai-model-manager": "model-manager"
      "ai-trainer": "trainer"
      "ai-user": "user"
      "ai-auditor": "auditor"
    default_role: "user"
    jwks_cache_ttl: 3600
```

### How it works

1. User authenticates with the OIDC provider and obtains a JWT access token
2. User sends the JWT in the `Authorization: Bearer <token>` header
3. lean-ai-serve fetches the provider's JWKS (JSON Web Key Set) and validates the token signature
4. Roles are extracted from the configured claim path and mapped to lean-ai-serve roles
5. JWKS keys are cached for `jwks_cache_ttl` seconds

### roles_claim path

The `roles_claim` field supports dot-notation to reach nested JWT claims:

- `realm_access.roles` → `token["realm_access"]["roles"]` (Keycloak)
- `roles` → `token["roles"]` (Azure AD)
- `resource_access.lean-ai-serve.roles` → nested resource access

## RBAC — Role-Based Access Control

Every authenticated user has one or more roles. Each role grants a set of permissions that control access to API endpoints.

### Roles and permissions

| Role | Permissions |
|------|-------------|
| **admin** | `*` (all permissions) |
| **model-manager** | `inference:call`, `model:read`, `model:write`, `model:deploy`, `adapter:read`, `adapter:deploy`, `metrics:read`, `audit:read_own` |
| **trainer** | `inference:call`, `model:read`, `training:submit`, `training:read`, `dataset:upload`, `dataset:read`, `adapter:read`, `metrics:read`, `audit:read_own` |
| **user** | `inference:call`, `usage:read_own`, `audit:read_own` |
| **auditor** | `audit:read`, `metrics:read`, `usage:read`, `model:read` |
| **service-account** | `inference:call` |

### Permission reference

| Permission | Required for |
|------------|-------------|
| `inference:call` | `/v1/chat/completions`, `/v1/completions`, `/v1/embeddings`, `/v1/models` |
| `model:read` | `GET /api/models`, `GET /api/status`, `GET /api/gpu` |
| `model:write` | `POST /api/models/pull`, `DELETE /api/models/{name}`, API key management |
| `model:deploy` | Load, unload, sleep, wake models |
| `training:submit` | Submit and start training jobs |
| `training:read` | List training jobs and GPU status |
| `dataset:upload` | Upload and delete datasets |
| `dataset:read` | List and preview datasets |
| `adapter:read` | List adapters |
| `adapter:deploy` | Import, deploy, undeploy, delete adapters |
| `audit:read` | Query all audit logs, verify chain |
| `audit:read_own` | Query own audit entries only |
| `metrics:read` | View metrics summary and alerts |
| `usage:read` | Query all usage records |
| `usage:read_own` | Query own usage only |

### Model restrictions

API keys can be restricted to specific models:

```bash
lean-ai-serve keys create --name "embed-only" --role user --models "embed-model"
```

When `allowed_models` is set (not `*`), the user can only call inference on the listed models. Model access is checked independently of RBAC permissions.

## Rate Limiting

Rate limiting is per-API-key using a sliding window algorithm (1-minute window).

### Configuration

Set rate limits when creating API keys:

```bash
# 60 requests per minute
lean-ai-serve keys create --name "rate-limited" --role user --rate-limit 60
```

### Response headers

Rate-limited endpoints return these headers:

| Header | Description |
|--------|-------------|
| `X-RateLimit-Limit` | Max requests per window |
| `X-RateLimit-Remaining` | Remaining requests in current window |
| `X-RateLimit-Reset` | Window reset time (Unix timestamp) |

### Rate limit exceeded

When the limit is exceeded, the server returns:

```
HTTP/1.1 429 Too Many Requests
Retry-After: 42
X-RateLimit-Limit: 60
X-RateLimit-Remaining: 0

{"detail": "Rate limit exceeded. Retry after 42 seconds."}
```

## JWT Token Management

For LDAP mode, lean-ai-serve manages JWT lifecycle:

- **Algorithm:** HS256 (HMAC-SHA256)
- **Secret:** Configurable via `security.jwt_secret` (auto-generated if empty, but sessions won't survive restarts)
- **Expiry:** Configurable via `security.jwt_expiry_hours` (default: 8 hours)
- **Revocation:** Revoked tokens stored in the `revoked_tokens` table and in-memory set
- **Cleanup:** Background scheduler removes expired revoked tokens hourly

### Best practices

- Set `jwt_secret` explicitly for production (use `ENV[]` or `ENC[]` pattern)
- Use `ldap+api_key` mode so service accounts can use API keys while users use LDAP
- Set appropriate `jwt_expiry_hours` for your security policy
