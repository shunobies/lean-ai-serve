# Deploying lean-ai-serve on Microsoft Azure

This guide covers deploying lean-ai-serve securely on Azure using GPU virtual machines or Azure Kubernetes Service (AKS), Azure Database for PostgreSQL, Azure Key Vault, and Azure Active Directory (Entra ID) integration.

> See also: [Deployment Guide](deployment.md) for Docker, systemd, nginx, and generic production setup.
> [Security & Compliance](security-and-compliance.md) for HIPAA audit logging and encryption details.
> [Authentication](authentication.md) for OIDC, LDAP, and RBAC configuration.

---

## Prerequisites & Resource Planning

### Required Accounts and Permissions

- Azure subscription with GPU VM quota approved (request via Azure Portal > Subscriptions > Usage + quotas)
- Azure CLI (`az`) installed and authenticated
- A resource group for the deployment

```bash
az group create --name rg-lean-ai --location eastus
```

Required IAM roles on the resource group:

| Role | Purpose |
|------|---------|
| Contributor | Create VMs, VNets, Application Gateway |
| Key Vault Secrets Officer | Manage secrets in Key Vault |
| Storage Blob Data Contributor | Write audit exports to Blob Storage |
| AcrPush (optional) | Push container images to Azure Container Registry |

### GPU Instance Selection

| SKU | GPUs | GPU Type | vCPUs | RAM | Use Case |
|-----|------|----------|-------|----|----------|
| Standard_NC4as_T4_v3 | 1 | T4 16 GB | 4 | 28 GB | Small models (7B parameters) |
| Standard_NC64as_T4_v3 | 4 | T4 16 GB | 64 | 440 GB | Multi-model or tensor parallel |
| Standard_NC24ads_A100_v4 | 1 | A100 80 GB | 24 | 220 GB | Large models (30B+) |
| Standard_NC48ads_A100_v4 | 2 | A100 80 GB | 48 | 440 GB | Very large models (70B+), tensor parallel |

### Estimated Resource Requirements

| Component | Recommended SKU | Purpose |
|-----------|----------------|---------|
| GPU VM | Standard_NC24ads_A100_v4 | Inference server |
| PostgreSQL | Standard_B2ms (2 vCores, 8 GB) | Metadata, audit logs, API keys |
| Application Gateway | Standard_v2 | TLS termination, WAF, routing |
| Key Vault | Standard | Secrets and encryption key management |
| Storage Account | Standard_LRS | Audit exports, model staging |

---

## Network Architecture

All inference traffic must remain within the Azure Virtual Network. GPU VMs must never have public IP addresses.

> **SECURITY:** Never allow inference traffic to traverse the public internet. All communication between clients, the load balancer, GPU VMs, and the database must stay within the VNet or traverse encrypted private links.

### VNet and Subnet Design

```bash
# Create the virtual network
az network vnet create \
  --resource-group rg-lean-ai \
  --name vnet-lean-ai \
  --address-prefix 10.0.0.0/16

# GPU subnet (no public IPs, inference workloads)
az network vnet subnet create \
  --resource-group rg-lean-ai \
  --vnet-name vnet-lean-ai \
  --name snet-gpu \
  --address-prefix 10.0.1.0/24

# Application Gateway subnet
az network vnet subnet create \
  --resource-group rg-lean-ai \
  --vnet-name vnet-lean-ai \
  --name snet-appgw \
  --address-prefix 10.0.2.0/24

# Database subnet (delegated to PostgreSQL Flexible Server)
az network vnet subnet create \
  --resource-group rg-lean-ai \
  --vnet-name vnet-lean-ai \
  --name snet-db \
  --address-prefix 10.0.3.0/24 \
  --delegation Microsoft.DBforPostgreSQL/flexibleServers
```

### Network Security Groups

```bash
# NSG for GPU subnet
az network nsg create --resource-group rg-lean-ai --name nsg-gpu

# Allow inbound from Application Gateway subnet only
az network nsg rule create \
  --resource-group rg-lean-ai \
  --nsg-name nsg-gpu \
  --name AllowAppGw \
  --priority 100 \
  --direction Inbound \
  --source-address-prefixes 10.0.2.0/24 \
  --destination-port-ranges 8420 \
  --access Allow --protocol Tcp

# Deny all other inbound from internet
az network nsg rule create \
  --resource-group rg-lean-ai \
  --nsg-name nsg-gpu \
  --name DenyInternetInbound \
  --priority 4000 \
  --direction Inbound \
  --source-address-prefixes Internet \
  --destination-port-ranges '*' \
  --access Deny --protocol '*'

# Restrict outbound internet (allow only Azure services and DNS)
az network nsg rule create \
  --resource-group rg-lean-ai \
  --nsg-name nsg-gpu \
  --name DenyInternetOutbound \
  --priority 4000 \
  --direction Outbound \
  --destination-address-prefixes Internet \
  --destination-port-ranges '*' \
  --access Deny --protocol '*'

# Associate NSG with GPU subnet
az network vnet subnet update \
  --resource-group rg-lean-ai \
  --vnet-name vnet-lean-ai \
  --name snet-gpu \
  --network-security-group nsg-gpu
```

> **SECURITY:** GPU VMs must reside in private subnets with no public IP addresses. Use NSG outbound rules or Azure Firewall to restrict egress to only required destinations.

> **SECURITY:** Never expose `/metrics` or `/health` endpoints to the public internet. These endpoints should only be accessible within the VNet or via a restricted management path.

### Private Endpoints

Create Azure Private Link endpoints to keep traffic to managed services off the public internet:

```bash
# Private endpoint for Key Vault
az network private-endpoint create \
  --resource-group rg-lean-ai \
  --name pe-keyvault \
  --vnet-name vnet-lean-ai \
  --subnet snet-gpu \
  --private-connection-resource-id <KEY_VAULT_RESOURCE_ID> \
  --group-id vault \
  --connection-name keyvault-connection

# Private endpoint for Storage Account
az network private-endpoint create \
  --resource-group rg-lean-ai \
  --name pe-storage \
  --vnet-name vnet-lean-ai \
  --subnet snet-gpu \
  --private-connection-resource-id <STORAGE_ACCOUNT_RESOURCE_ID> \
  --group-id blob \
  --connection-name storage-connection
```

PostgreSQL Flexible Server uses VNet integration (subnet delegation) rather than Private Link.

---

## Compute Setup

### Option A: GPU Virtual Machine

```bash
# Create GPU VM with no public IP
az vm create \
  --resource-group rg-lean-ai \
  --name vm-lean-ai-gpu \
  --image Canonical:ubuntu-24_04-lts:server:latest \
  --size Standard_NC24ads_A100_v4 \
  --vnet-name vnet-lean-ai \
  --subnet snet-gpu \
  --public-ip-address "" \
  --admin-username azureuser \
  --generate-ssh-keys

# Install NVIDIA GPU drivers
az vm extension set \
  --resource-group rg-lean-ai \
  --vm-name vm-lean-ai-gpu \
  --name NvidiaGpuDriverLinux \
  --publisher Microsoft.HpcCompute \
  --version 1.9
```

SSH to the VM via Azure Bastion or a jump box (never assign a public IP), then install:

```bash
# Install Python 3.11 and create a virtual environment
sudo apt update && sudo apt install -y python3.11 python3.11-venv
python3.11 -m venv /opt/lean-ai-serve/venv

# Install lean-ai-serve with GPU, LDAP, Vault, and PostgreSQL support
/opt/lean-ai-serve/venv/bin/pip install \
  "lean-ai-serve[gpu,ldap,vault,postgres]" vllm

# Deploy config and start with systemd (see deployment.md for service file)
sudo mkdir -p /etc/lean-ai-serve
sudo cp config.yaml /etc/lean-ai-serve/config.yaml
sudo chmod 640 /etc/lean-ai-serve/config.yaml
```

See [Deployment Guide](deployment.md) for the systemd service file template.

### Option B: Azure Kubernetes Service (AKS)

```bash
# Create a private AKS cluster
az aks create \
  --resource-group rg-lean-ai \
  --name aks-lean-ai \
  --network-plugin azure \
  --vnet-subnet-id <SNET_GPU_RESOURCE_ID> \
  --enable-private-cluster \
  --node-count 1 \
  --node-vm-size Standard_DS3_v2 \
  --generate-ssh-keys

# Add a GPU node pool
az aks nodepool add \
  --resource-group rg-lean-ai \
  --cluster-name aks-lean-ai \
  --name gpupool \
  --node-count 1 \
  --node-vm-size Standard_NC24ads_A100_v4 \
  --node-taints "nvidia.com/gpu=present:NoSchedule"
```

Install the NVIDIA device plugin and deploy lean-ai-serve as a Kubernetes Deployment with GPU resource requests. Use the AKS Secrets Store CSI Driver to inject secrets from Key Vault.

> **SECURITY:** Pre-stage model files to Azure Blob Storage and copy them to the VM at deploy time, or use Private Link for HuggingFace Hub access. Do not allow GPU VMs to download models directly from the public internet unless network egress is tightly controlled.

---

## Managed Database

Use Azure Database for PostgreSQL Flexible Server with VNet integration. Never use SQLite for production deployments.

> **SECURITY:** Never use SQLite for production. SQLite does not support concurrent writes, lacks network-level access controls, and cannot be configured for high availability or automated backups. Always use a managed PostgreSQL instance.

```bash
# Create a private DNS zone for PostgreSQL
az network private-dns zone create \
  --resource-group rg-lean-ai \
  --name lean-ai.private.postgres.database.azure.com

az network private-dns zone virtual-network-link create \
  --resource-group rg-lean-ai \
  --zone-name lean-ai.private.postgres.database.azure.com \
  --name vnet-link \
  --virtual-network vnet-lean-ai \
  --registration-enabled false

# Create PostgreSQL Flexible Server (VNet-integrated, no public access)
az postgres flexible-server create \
  --resource-group rg-lean-ai \
  --name pg-lean-ai \
  --location eastus \
  --admin-user lean_ai_admin \
  --admin-password "$(az keyvault secret show --vault-name kv-lean-ai --name db-password --query value -o tsv)" \
  --sku-name Standard_B2ms \
  --tier Burstable \
  --storage-size 128 \
  --version 16 \
  --vnet vnet-lean-ai \
  --subnet snet-db \
  --private-dns-zone lean-ai.private.postgres.database.azure.com \
  --public-access Disabled

# Create the application database
az postgres flexible-server db create \
  --resource-group rg-lean-ai \
  --server-name pg-lean-ai \
  --database-name lean_ai_serve
```

Configure lean-ai-serve to connect:

```yaml
database:
  url: "ENV[DATABASE_URL]"
  pool_size: 5
  pool_max_overflow: 10
```

Set the environment variable:

```bash
export DATABASE_URL="postgresql+asyncpg://lean_ai_admin:PASSWORD@pg-lean-ai.postgres.database.azure.com:5432/lean_ai_serve"
```

Initialize the database:

```bash
lean-ai-serve db init --config /etc/lean-ai-serve/config.yaml
```

---

## Secrets Management

Store all sensitive values in Azure Key Vault. Never put secrets in plain text in `config.yaml`.

> **SECURITY:** Never store secrets in plain text in config.yaml. All secrets (JWT secret, database password, HuggingFace token, encryption key) must be stored in Azure Key Vault and injected as environment variables at runtime using `ENV[]` references.

> **SECURITY:** Never use auto-generated JWT secrets in production. An auto-generated JWT secret changes on every restart, invalidating all active sessions and tokens. Explicitly set `security.jwt_secret` via `ENV[]` or `ENC[]`.

### Create Key Vault

```bash
az keyvault create \
  --resource-group rg-lean-ai \
  --name kv-lean-ai \
  --location eastus \
  --enable-rbac-authorization \
  --enable-soft-delete \
  --enable-purge-protection
```

### Store Secrets

```bash
# Generate and store JWT secret
JWT_SECRET=$(openssl rand -hex 32)
az keyvault secret set --vault-name kv-lean-ai --name jwt-secret --value "$JWT_SECRET"

# Store database password
az keyvault secret set --vault-name kv-lean-ai --name db-password --value "YOUR_DB_PASSWORD"

# Store HuggingFace token
az keyvault secret set --vault-name kv-lean-ai --name hf-token --value "hf_YOUR_TOKEN"

# Generate and store encryption key
lean-ai-serve config generate-key /tmp/master.key
ENCRYPTION_KEY=$(xxd -p -c 64 /tmp/master.key)
az keyvault secret set --vault-name kv-lean-ai --name encryption-key --value "$ENCRYPTION_KEY"
rm /tmp/master.key  # Remove local copy after storing in Key Vault

# Store dashboard session secret
SESSION_SECRET=$(openssl rand -hex 32)
az keyvault secret set --vault-name kv-lean-ai --name session-secret --value "$SESSION_SECRET"
```

> **SECURITY:** HuggingFace tokens must be stored in Azure Key Vault, never in config.yaml or environment files committed to source control.

> **SECURITY:** The master encryption key must be backed up securely. Enable Key Vault soft-delete and purge protection to prevent accidental loss. For HIPAA workloads, consider HSM-backed keys (Azure Key Vault Premium or Managed HSM).

### Inject Secrets at Runtime

On the GPU VM, use a managed identity to fetch secrets at startup:

```bash
# Assign a system-managed identity to the VM
az vm identity assign \
  --resource-group rg-lean-ai \
  --name vm-lean-ai-gpu

# Grant the VM's identity access to Key Vault secrets
VM_IDENTITY=$(az vm show --resource-group rg-lean-ai --name vm-lean-ai-gpu --query identity.principalId -o tsv)
KV_ID=$(az keyvault show --name kv-lean-ai --query id -o tsv)

az role assignment create \
  --role "Key Vault Secrets User" \
  --assignee "$VM_IDENTITY" \
  --scope "$KV_ID"
```

Create a startup script (`/opt/lean-ai-serve/start.sh`):

```bash
#!/bin/bash
# Fetch secrets from Key Vault using managed identity
export JWT_SECRET=$(az keyvault secret show --vault-name kv-lean-ai --name jwt-secret --query value -o tsv)
export DATABASE_URL="postgresql+asyncpg://lean_ai_admin:$(az keyvault secret show --vault-name kv-lean-ai --name db-password --query value -o tsv)@pg-lean-ai.postgres.database.azure.com:5432/lean_ai_serve"
export HF_TOKEN=$(az keyvault secret show --vault-name kv-lean-ai --name hf-token --query value -o tsv)
export LEAN_AI_ENCRYPTION_KEY=$(az keyvault secret show --vault-name kv-lean-ai --name encryption-key --query value -o tsv)
export SESSION_SECRET=$(az keyvault secret show --vault-name kv-lean-ai --name session-secret --query value -o tsv)

exec /opt/lean-ai-serve/venv/bin/lean-ai-serve start --config /etc/lean-ai-serve/config.yaml
```

For AKS, use the [Secrets Store CSI Driver](https://learn.microsoft.com/en-us/azure/aks/csi-secrets-store-driver) with Azure Key Vault provider.

---

## Storage

### Model Cache

For air-gapped or restricted environments, pre-stage model files in Azure Blob Storage and sync them to the GPU VM at deploy time:

```bash
# Create storage account (deny public access)
az storage account create \
  --resource-group rg-lean-ai \
  --name stleanaimodels \
  --location eastus \
  --sku Standard_LRS \
  --default-action Deny \
  --bypass AzureServices

# Upload pre-downloaded model files
az storage blob upload-batch \
  --account-name stleanaimodels \
  --destination models \
  --source /local/models/Qwen3-Coder-30B-A3B/

# On the GPU VM, sync model files
az storage blob download-batch \
  --account-name stleanaimodels \
  --source models \
  --destination ~/.cache/lean-ai-serve/hub/
```

### Audit Log Exports

Schedule periodic audit exports to Blob Storage:

```bash
# Export audit logs and upload
lean-ai-serve admin audit-export --format json --output /tmp/audit-$(date +%Y%m%d).json
az storage blob upload \
  --account-name stleanaimodels \
  --container-name audit-exports \
  --name "audit-$(date +%Y%m%d).json" \
  --file /tmp/audit-$(date +%Y%m%d).json
rm /tmp/audit-$(date +%Y%m%d).json
```

Configure Blob Storage lifecycle policies to tier old exports to Cool or Archive storage.

---

## TLS and Load Balancing

Use Azure Application Gateway v2 for TLS termination, WAF, and path-based routing.

> **SECURITY:** Never expose the lean-ai-serve server without TLS. All client traffic must be encrypted in transit. Use Application Gateway or a reverse proxy for TLS termination.

### Application Gateway Setup

```bash
# Create a public IP for the Application Gateway
az network public-ip create \
  --resource-group rg-lean-ai \
  --name pip-appgw \
  --sku Standard \
  --allocation-method Static

# Create the Application Gateway with WAF
az network application-gateway create \
  --resource-group rg-lean-ai \
  --name appgw-lean-ai \
  --location eastus \
  --sku WAF_v2 \
  --capacity 2 \
  --vnet-name vnet-lean-ai \
  --subnet snet-appgw \
  --public-ip-address pip-appgw \
  --http-settings-port 8420 \
  --http-settings-protocol Http \
  --frontend-port 443 \
  --servers 10.0.1.4
```

Configure health probes to use `/health` on port 8420.

### Restricting Sensitive Endpoints

> **SECURITY:** The `/metrics` and `/health` endpoints must not be exposed through the Application Gateway to the public internet. Use path-based rules to block these paths, or restrict them to internal VNet traffic only.

> **SECURITY:** The web dashboard should be disabled in production (`dashboard.enabled: false`) or restricted to admin VPN CIDR ranges via Application Gateway path rules or NSG rules.

Create path-based rules on the Application Gateway to:
- Route `/v1/*` and `/api/*` to the backend (authenticated endpoints)
- Block `/metrics` and `/health` from external access
- Restrict `/dashboard/*` to admin VPN source IPs only

### WAF Policy

Enable the Azure WAF policy on the Application Gateway with OWASP 3.2 rule set for additional protection against common web attacks.

---

## Identity Integration

### Azure Active Directory (Entra ID) as OIDC Provider

Register lean-ai-serve as an application in Azure AD:

1. Go to Azure Portal > Microsoft Entra ID > App registrations > New registration
2. Set the redirect URI (if needed for dashboard OIDC flow)
3. Note the Application (client) ID and Directory (tenant) ID
4. Under "App roles," create roles that map to lean-ai-serve RBAC roles:
   - `admin`, `model-manager`, `trainer`, `user`, `auditor`, `service-account`

Configure lean-ai-serve:

```yaml
security:
  mode: "oidc"
  jwt_secret: "ENV[JWT_SECRET]"

  oidc:
    issuer_url: "https://login.microsoftonline.com/YOUR_TENANT_ID/v2.0"
    client_id: "YOUR_CLIENT_ID"
    audience: "api://YOUR_CLIENT_ID"
    roles_claim: "roles"
    role_mapping:
      "admin": "admin"
      "model-manager": "model-manager"
      "trainer": "trainer"
      "user": "user"
      "auditor": "auditor"
    default_role: "user"
    jwks_cache_ttl: 3600
```

> **SECURITY:** Never use `security.mode: none` in production. This disables all authentication and authorization, allowing unrestricted access to inference endpoints, model management, and audit logs.

For environments using Active Directory with LDAP, configure `security.mode: "ldap"` instead. See [Authentication](authentication.md) for full LDAP configuration.

---

## Monitoring and Observability

### Azure Monitor and Log Analytics

```bash
# Create a Log Analytics workspace
az monitor log-analytics workspace create \
  --resource-group rg-lean-ai \
  --workspace-name la-lean-ai \
  --location eastus

# Install the Azure Monitor Agent on the GPU VM
az vm extension set \
  --resource-group rg-lean-ai \
  --vm-name vm-lean-ai-gpu \
  --name AzureMonitorLinuxAgent \
  --publisher Microsoft.Azure.Monitor
```

Configure a Data Collection Rule to forward JSON-formatted lean-ai-serve logs from the systemd journal to Log Analytics.

### Prometheus and Grafana

For Prometheus metrics scraping:

- Deploy a self-hosted Prometheus instance within the VNet that scrapes the GPU VM's `/metrics` endpoint on port 8420
- Import the lean-ai-serve Grafana dashboard from `dashboards/lean-ai-serve.json`
- If using AKS, consider Azure Monitor managed service for Prometheus (Container Insights)

### Database Monitoring

Enable diagnostic settings on PostgreSQL Flexible Server to forward query performance and connection metrics to Log Analytics.

---

## Backup and Disaster Recovery

### Database Backups

Azure Database for PostgreSQL Flexible Server provides automated backups:

```bash
# Configure 35-day backup retention with geo-redundancy
az postgres flexible-server update \
  --resource-group rg-lean-ai \
  --name pg-lean-ai \
  --backup-retention 35 \
  --geo-redundant-backup Enabled
```

### Encryption Key Backup

> **SECURITY:** The master encryption key must be backed up securely. If lost, all encrypted audit data becomes unrecoverable. Key Vault soft-delete and purge protection (configured above) prevent accidental deletion. For additional safety, export a backup copy to a separate Key Vault in another region.

### Audit Log Archival

Schedule regular exports (daily or weekly) to Blob Storage as described in the Storage section. Configure Blob lifecycle policies for long-term retention matching your compliance requirements (default: 2190 days / 6 years for HIPAA).

---

## Cost Optimization

- **Idle sleep lifecycle:** Configure `lifecycle.idle_sleep_timeout` on models to free GPU memory when idle. Use `auto_wake_on_request: true` so models restart on demand.
- **Reserved Instances:** Purchase 1-year or 3-year reservations for GPU VMs in production (up to 60% savings).
- **Right-size GPU VMs:** Use `Standard_NC4as_T4_v3` (T4) for models under 14B parameters instead of A100 instances.
- **Storage tiering:** Use Blob lifecycle policies to move old audit exports to Cool (30+ days) or Archive (90+ days) storage.
- **Burstable database:** Use Burstable-tier PostgreSQL (`Standard_B2ms`) for low-traffic deployments.

> Spot VMs offer significant cost savings but are not recommended for HIPAA production workloads due to preemption risk. Use only for development and testing.

---

## Configuration Example

Complete `config.yaml` for an Azure deployment. All secrets reference environment variables injected from Key Vault at startup.

```yaml
server:
  host: "0.0.0.0"
  port: 8420
  tls:
    enabled: false                         # TLS terminated at Application Gateway

security:
  mode: "oidc"                             # NEVER use "none" in production
  jwt_secret: "ENV[JWT_SECRET]"            # NEVER leave empty -- set explicitly
  jwt_expiry_hours: 8.0

  oidc:
    issuer_url: "https://login.microsoftonline.com/YOUR_TENANT_ID/v2.0"
    client_id: "YOUR_CLIENT_ID"
    audience: "api://YOUR_CLIENT_ID"
    roles_claim: "roles"
    role_mapping:
      "admin": "admin"
      "model-manager": "model-manager"
      "trainer": "trainer"
      "user": "user"
      "auditor": "auditor"
    default_role: "user"
    jwks_cache_ttl: 3600

  content_filtering:
    enabled: true                          # Enable for PHI/PII workloads
    patterns:
      - name: "SSN"
        pattern: '\b\d{3}-\d{2}-\d{4}\b'
        action: "block"                    # Block requests containing SSNs
      - name: "MRN"
        pattern: '\bMRN[:\s]?\d{6,}\b'
        action: "redact"

audit:
  enabled: true
  log_prompts: true
  log_prompts_hash_only: true              # NEVER log full prompts with PHI
  retention_days: 2190                     # 6 years (HIPAA minimum)

encryption:
  at_rest:
    enabled: true                          # ALWAYS enable in production
    key_source: "env"
    key_env_var: "LEAN_AI_ENCRYPTION_KEY"  # Injected from Key Vault

database:
  url: "ENV[DATABASE_URL]"                 # NEVER use SQLite in production
  pool_size: 5
  pool_max_overflow: 10

cache:
  directory: "/opt/lean-ai-serve/cache"
  huggingface_token: "ENV[HF_TOKEN]"       # NEVER put tokens in plain text

models:
  # Configure your models here
  # qwen3-coder-30b:
  #   source: "Qwen/Qwen3-Coder-30B-A3B"
  #   gpu: [0]
  #   max_model_len: 131072
  #   autoload: true
  #   lifecycle:
  #     idle_sleep_timeout: 3600
  #     auto_wake_on_request: true

metrics:
  enabled: true
  gpu_poll_interval: 30

logging:
  json_output: true                        # Always JSON in production
  level: "INFO"

alerts:
  enabled: true
  evaluation_interval: 60

dashboard:
  enabled: false                           # Disable unless needed; restrict to VPN
  session_secret: "ENV[SESSION_SECRET]"    # NEVER leave empty if dashboard is enabled
  csrf_enabled: true
```

---

## Security Hardening Checklist

Verify every item before going to production:

- [ ] GPU VMs have no public IP addresses
- [ ] NSG denies all inbound from internet to GPU subnet
- [ ] NSG restricts outbound internet from GPU subnet
- [ ] Azure Private Link configured for Key Vault and Blob Storage
- [ ] PostgreSQL Flexible Server uses VNet integration (no public access)
- [ ] Application Gateway WAF enabled with OWASP 3.2 rule set
- [ ] `/metrics` and `/health` not routed through Application Gateway
- [ ] `/dashboard` disabled or restricted to admin VPN CIDR
- [ ] All secrets stored in Azure Key Vault (JWT, DB password, HF token, encryption key, session secret)
- [ ] `security.mode` set to `oidc` or `ldap` (never `none`)
- [ ] `security.jwt_secret` explicitly set via `ENV[]` (never auto-generated)
- [ ] `encryption.at_rest.enabled: true` with key sourced from Key Vault
- [ ] `database.url` points to PostgreSQL Flexible Server (never SQLite)
- [ ] `audit.log_prompts_hash_only: true` if handling PHI/PII
- [ ] `security.content_filtering.enabled: true` with PHI/PII patterns
- [ ] `logging.json_output: true` and `logging.level: "INFO"`
- [ ] Model files pre-staged or downloaded through controlled egress
- [ ] HuggingFace token stored in Key Vault, not in config files
- [ ] Key Vault soft-delete and purge protection enabled
- [ ] PostgreSQL geo-redundant backups configured
- [ ] `lean-ai-serve check --config config.yaml` passes without warnings
- [ ] Run `lean-ai-serve db init` to initialize the database schema
