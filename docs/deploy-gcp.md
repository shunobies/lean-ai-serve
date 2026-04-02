# Deploying lean-ai-serve on Google Cloud Platform

This guide covers deploying lean-ai-serve securely on GCP using GPU Compute Engine instances or Google Kubernetes Engine (GKE), Cloud SQL for PostgreSQL, Secret Manager, and Google Cloud Identity / OIDC integration.

> See also: [Deployment Guide](deployment.md) for Docker, systemd, nginx, and generic production setup.
> [Security & Compliance](security-and-compliance.md) for HIPAA audit logging and encryption details.
> [Authentication](authentication.md) for OIDC, LDAP, and RBAC configuration.

---

## Prerequisites & Resource Planning

### Required Accounts and Permissions

- GCP project with GPU quota approved (IAM & Admin > Quotas > Compute Engine API > GPUs)
- `gcloud` CLI installed and authenticated
- Billing enabled on the project

Required IAM roles:

| Role | Purpose |
|------|---------|
| Compute Admin | Create VMs, firewall rules, networks |
| Kubernetes Engine Admin | Create and manage GKE clusters |
| Cloud SQL Admin | Provision and manage PostgreSQL |
| Secret Manager Admin | Create and manage secrets |
| Storage Admin | Create buckets, manage objects |
| Service Account Admin | Create service accounts for workloads |

### GPU Instance Selection

| Machine Type | GPUs | GPU Type | vCPUs | RAM | Use Case |
|-------------|------|----------|-------|----|----------|
| g2-standard-4 | 1 | L4 24 GB | 4 | 16 GB | Small models (7B parameters) |
| g2-standard-48 | 4 | L4 24 GB | 48 | 192 GB | Multi-model or tensor parallel |
| a2-highgpu-1g | 1 | A100 40 GB | 12 | 85 GB | Large models (30B+) |
| a2-highgpu-4g | 4 | A100 40 GB | 48 | 340 GB | Very large models (70B+), tensor parallel |
| a2-ultragpu-1g | 1 | A100 80 GB | 12 | 170 GB | Maximum GPU memory per card |

### Estimated Resource Requirements

| Component | Recommended | Purpose |
|-----------|-------------|---------|
| Compute Engine | a2-highgpu-1g | Inference server |
| Cloud SQL | db-custom-2-7680 (2 vCPUs, 7.5 GB) | Metadata, audit logs, API keys |
| Cloud Load Balancer | External HTTPS LB | TLS termination, routing |
| Secret Manager | Standard | Secrets and encryption key storage |
| Cloud Storage | Standard | Audit exports, model staging |

---

## Network Architecture

All inference traffic must remain within the GCP VPC network. GPU VMs must never have external IP addresses.

> **SECURITY:** Never allow inference traffic to traverse the public internet. All communication between clients, the load balancer, GPU VMs, and the database must stay within the VPC or traverse private connections.

### VPC and Subnet Design

```bash
# Create a custom-mode VPC
gcloud compute networks create vpc-lean-ai \
  --subnet-mode=custom

# GPU subnet (private, no external IPs)
gcloud compute networks subnets create snet-gpu \
  --network=vpc-lean-ai \
  --region=us-central1 \
  --range=10.0.1.0/24 \
  --enable-private-ip-google-access

# Proxy-only subnet (for internal/external HTTPS LB)
gcloud compute networks subnets create snet-proxy \
  --network=vpc-lean-ai \
  --region=us-central1 \
  --range=10.0.2.0/24 \
  --purpose=REGIONAL_MANAGED_PROXY \
  --role=ACTIVE
```

Private IP Google Access is enabled on the GPU subnet so instances can reach Google APIs (Cloud Storage, Secret Manager, Container Registry) without external IPs.

### Firewall Rules

```bash
# Allow health checks from Google's health check ranges
gcloud compute firewall-rules create allow-health-checks \
  --network=vpc-lean-ai \
  --direction=INGRESS \
  --action=ALLOW \
  --rules=tcp:8420 \
  --source-ranges=130.211.0.0/22,35.191.0.0/16 \
  --target-tags=gpu-instance

# Allow load balancer proxy to reach GPU instances
gcloud compute firewall-rules create allow-lb-to-gpu \
  --network=vpc-lean-ai \
  --direction=INGRESS \
  --action=ALLOW \
  --rules=tcp:8420 \
  --source-ranges=10.0.2.0/24 \
  --target-tags=gpu-instance

# Allow SSH via Identity-Aware Proxy only (no direct SSH)
gcloud compute firewall-rules create allow-iap-ssh \
  --network=vpc-lean-ai \
  --direction=INGRESS \
  --action=ALLOW \
  --rules=tcp:22 \
  --source-ranges=35.235.240.0/20 \
  --target-tags=gpu-instance

# Default deny all other ingress
gcloud compute firewall-rules create deny-all-ingress \
  --network=vpc-lean-ai \
  --direction=INGRESS \
  --action=DENY \
  --rules=all \
  --source-ranges=0.0.0.0/0 \
  --priority=65534

# Restrict egress (allow only Google APIs and internal)
gcloud compute firewall-rules create allow-egress-google-apis \
  --network=vpc-lean-ai \
  --direction=EGRESS \
  --action=ALLOW \
  --rules=tcp:443 \
  --destination-ranges=199.36.153.8/30 \
  --target-tags=gpu-instance

gcloud compute firewall-rules create allow-egress-internal \
  --network=vpc-lean-ai \
  --direction=EGRESS \
  --action=ALLOW \
  --rules=all \
  --destination-ranges=10.0.0.0/16 \
  --target-tags=gpu-instance

gcloud compute firewall-rules create deny-egress-internet \
  --network=vpc-lean-ai \
  --direction=EGRESS \
  --action=DENY \
  --rules=all \
  --destination-ranges=0.0.0.0/0 \
  --priority=65534 \
  --target-tags=gpu-instance
```

> **SECURITY:** GPU VMs must reside in private subnets with no external IP addresses. Use firewall rules to restrict ingress to only the load balancer and IAP, and restrict egress to only Google APIs and internal traffic.

> **SECURITY:** Never expose `/metrics` or `/health` endpoints to the public internet. These endpoints should only be accessible within the VPC or via restricted load balancer rules.

### Private Services Connection for Cloud SQL

```bash
# Allocate a private IP range for Cloud SQL
gcloud compute addresses create google-managed-services-range \
  --global \
  --purpose=VPC_PEERING \
  --addresses=10.0.20.0 \
  --prefix-length=24 \
  --network=vpc-lean-ai

# Create the private services connection
gcloud services vpc-peerings connect \
  --service=servicenetworking.googleapis.com \
  --ranges=google-managed-services-range \
  --network=vpc-lean-ai
```

> **SECURITY:** Restrict outbound internet access from GPU instances. Private IP Google Access allows access to Google APIs (Cloud Storage, Secret Manager) without external IPs. For HuggingFace model downloads, pre-stage models via Cloud Storage or configure a Cloud NAT with restricted routes.

---

## Compute Setup

### Option A: GPU Compute Engine Instance

```bash
# Create a service account for the GPU instance
gcloud iam service-accounts create sa-lean-ai-gpu \
  --display-name="lean-ai-serve GPU instance"

# Grant Secret Manager access
gcloud projects add-iam-policy-binding PROJECT_ID \
  --member="serviceAccount:sa-lean-ai-gpu@PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"

# Grant Cloud Storage access (for model staging and audit exports)
gcloud projects add-iam-policy-binding PROJECT_ID \
  --member="serviceAccount:sa-lean-ai-gpu@PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/storage.objectUser"

# Create GPU instance with no external IP
gcloud compute instances create vm-lean-ai-gpu \
  --zone=us-central1-a \
  --machine-type=a2-highgpu-1g \
  --accelerator=type=nvidia-tesla-a100,count=1 \
  --maintenance-policy=TERMINATE \
  --image-family=common-cu124-ubuntu-2204 \
  --image-project=deeplearning-platform-release \
  --boot-disk-size=200GB \
  --boot-disk-type=pd-ssd \
  --network-interface=subnet=snet-gpu,no-address \
  --service-account=sa-lean-ai-gpu@PROJECT_ID.iam.gserviceaccount.com \
  --scopes=cloud-platform \
  --tags=gpu-instance \
  --metadata=install-nvidia-driver=True
```

SSH to the instance via Identity-Aware Proxy (no external IP needed):

```bash
gcloud compute ssh vm-lean-ai-gpu --zone=us-central1-a --tunnel-through-iap
```

On the instance:

```bash
# Install Python 3.11 and create a virtual environment
sudo apt update && sudo apt install -y python3.11 python3.11-venv
python3.11 -m venv /opt/lean-ai-serve/venv

# Install lean-ai-serve with GPU, LDAP, and PostgreSQL support
/opt/lean-ai-serve/venv/bin/pip install \
  "lean-ai-serve[gpu,ldap,postgres]" vllm

# Deploy config
sudo mkdir -p /etc/lean-ai-serve
sudo cp config.yaml /etc/lean-ai-serve/config.yaml
sudo chmod 640 /etc/lean-ai-serve/config.yaml
```

See [Deployment Guide](deployment.md) for the systemd service file template.

### Option B: Google Kubernetes Engine (GKE)

```bash
# Create a private GKE cluster
gcloud container clusters create gke-lean-ai \
  --region=us-central1 \
  --network=vpc-lean-ai \
  --subnetwork=snet-gpu \
  --enable-private-nodes \
  --master-ipv4-cidr=172.16.0.0/28 \
  --enable-master-authorized-networks \
  --master-authorized-networks=YOUR_ADMIN_CIDR/32 \
  --num-nodes=1 \
  --machine-type=e2-standard-4 \
  --workload-pool=PROJECT_ID.svc.id.goog

# Add a GPU node pool
gcloud container node-pools create gpu-pool \
  --cluster=gke-lean-ai \
  --region=us-central1 \
  --machine-type=a2-highgpu-1g \
  --accelerator=type=nvidia-tesla-a100,count=1 \
  --num-nodes=1 \
  --enable-autoscaling \
  --min-nodes=0 \
  --max-nodes=2 \
  --node-taints="nvidia.com/gpu=present:NoSchedule"
```

GKE automatically installs the NVIDIA GPU device driver via a DaemonSet. Use Workload Identity to grant the lean-ai-serve pods access to Secret Manager and Cloud Storage without static credentials.

> **SECURITY:** Pre-stage model files to Cloud Storage and sync them to instances at deploy time, or use Private IP Google Access for HuggingFace Hub downloads. Cloud NAT is required if instances need to reach external endpoints (HuggingFace, PyPI).

---

## Managed Database

Use Cloud SQL for PostgreSQL with private IP only. Never use SQLite for production deployments.

> **SECURITY:** Never use SQLite for production. SQLite does not support concurrent writes, lacks network-level access controls, and cannot be configured for high availability or automated backups. Always use a managed PostgreSQL instance.

```bash
# Create Cloud SQL instance (private IP only, no public IP)
gcloud sql instances create sql-lean-ai \
  --database-version=POSTGRES_16 \
  --tier=db-custom-2-7680 \
  --region=us-central1 \
  --network=vpc-lean-ai \
  --no-assign-ip \
  --storage-type=SSD \
  --storage-size=100GB \
  --storage-auto-increase \
  --backup-start-time=02:00 \
  --enable-point-in-time-recovery \
  --availability-type=REGIONAL \
  --root-password="$(gcloud secrets versions access latest --secret=lean-ai-db-password)"

# Create the application database
gcloud sql databases create lean_ai_serve \
  --instance=sql-lean-ai

# Create an application user
gcloud sql users create lean_ai_app \
  --instance=sql-lean-ai \
  --password="$(gcloud secrets versions access latest --secret=lean-ai-db-password)"
```

Configure lean-ai-serve:

```yaml
database:
  url: "ENV[DATABASE_URL]"
  pool_size: 5
  pool_max_overflow: 10
```

Set the environment variable (using the Cloud SQL private IP):

```bash
export DATABASE_URL="postgresql+asyncpg://lean_ai_app:PASSWORD@CLOUD_SQL_PRIVATE_IP:5432/lean_ai_serve"
```

Initialize the database:

```bash
lean-ai-serve db init --config /etc/lean-ai-serve/config.yaml
```

---

## Secrets Management

Store all sensitive values in Google Cloud Secret Manager. Never put secrets in plain text in `config.yaml`.

> **SECURITY:** Never store secrets in plain text in config.yaml. All secrets (JWT secret, database password, HuggingFace token, encryption key) must be stored in Secret Manager and injected as environment variables at runtime using `ENV[]` references.

> **SECURITY:** Never use auto-generated JWT secrets in production. An auto-generated JWT secret changes on every restart, invalidating all active sessions and tokens. Explicitly set `security.jwt_secret` via `ENV[]` or `ENC[]`.

### Store Secrets

```bash
# Generate and store JWT secret
JWT_SECRET=$(openssl rand -hex 32)
echo -n "$JWT_SECRET" | gcloud secrets create lean-ai-jwt-secret --data-file=-

# Store database password
echo -n "YOUR_DB_PASSWORD" | gcloud secrets create lean-ai-db-password --data-file=-

# Store HuggingFace token
echo -n "hf_YOUR_TOKEN" | gcloud secrets create lean-ai-hf-token --data-file=-

# Generate and store encryption key
lean-ai-serve config generate-key /tmp/master.key
ENCRYPTION_KEY=$(xxd -p -c 64 /tmp/master.key)
echo -n "$ENCRYPTION_KEY" | gcloud secrets create lean-ai-encryption-key --data-file=-
rm /tmp/master.key  # Remove local copy after storing

# Store dashboard session secret
SESSION_SECRET=$(openssl rand -hex 32)
echo -n "$SESSION_SECRET" | gcloud secrets create lean-ai-session-secret --data-file=-
```

> **SECURITY:** HuggingFace tokens must be stored in Secret Manager, never in config.yaml or environment files committed to source control.

> **SECURITY:** The master encryption key must be backed up securely. Secret Manager automatically versions secrets for recovery. For HIPAA workloads, consider using Cloud KMS with an HSM protection level for the master encryption key.

### Inject Secrets at Runtime

The GPU instance's service account already has `roles/secretmanager.secretAccessor`. Create a startup script (`/opt/lean-ai-serve/start.sh`):

```bash
#!/bin/bash

get_secret() {
  gcloud secrets versions access latest --secret="$1"
}

export JWT_SECRET=$(get_secret lean-ai-jwt-secret)
export DATABASE_URL="postgresql+asyncpg://lean_ai_app:$(get_secret lean-ai-db-password)@CLOUD_SQL_PRIVATE_IP:5432/lean_ai_serve"
export HF_TOKEN=$(get_secret lean-ai-hf-token)
export LEAN_AI_ENCRYPTION_KEY=$(get_secret lean-ai-encryption-key)
export SESSION_SECRET=$(get_secret lean-ai-session-secret)

exec /opt/lean-ai-serve/venv/bin/lean-ai-serve start --config /etc/lean-ai-serve/config.yaml
```

For GKE, use [Workload Identity](https://cloud.google.com/kubernetes-engine/docs/how-to/workload-identity) with the External Secrets Operator or the GKE Secret Manager add-on.

---

## Storage

### Model Cache

For air-gapped or restricted environments, pre-stage model files in Cloud Storage and sync them to the GPU instance at deploy time:

```bash
# Create a bucket (uniform access, no public access)
gcloud storage buckets create gs://lean-ai-models-PROJECT_ID \
  --location=us-central1 \
  --uniform-bucket-level-access

# Upload pre-downloaded model files
gcloud storage cp -r /local/models/Qwen3-Coder-30B-A3B/ \
  gs://lean-ai-models-PROJECT_ID/models/Qwen3-Coder-30B-A3B/

# On the GPU instance, sync model files (traffic stays in VPC via Private Google Access)
gcloud storage cp -r gs://lean-ai-models-PROJECT_ID/models/ \
  /opt/lean-ai-serve/cache/hub/
```

### Audit Log Exports

Schedule periodic audit exports to Cloud Storage:

```bash
# Export audit logs and upload
lean-ai-serve admin audit-export --format json --output /tmp/audit-$(date +%Y%m%d).json
gcloud storage cp /tmp/audit-$(date +%Y%m%d).json \
  gs://lean-ai-audit-PROJECT_ID/exports/audit-$(date +%Y%m%d).json
rm /tmp/audit-$(date +%Y%m%d).json
```

Configure Object Lifecycle Management to transition old exports to Nearline (30+ days) or Coldline (90+ days) storage classes.

---

## TLS and Load Balancing

Use a Google Cloud External HTTPS Load Balancer for TLS termination with Google-managed SSL certificates.

> **SECURITY:** Never expose the lean-ai-serve server without TLS. All client traffic must be encrypted in transit. Use the Cloud Load Balancer for TLS termination.

### Load Balancer Setup

```bash
# Create an instance group for the GPU VM
gcloud compute instance-groups unmanaged create ig-lean-ai \
  --zone=us-central1-a

gcloud compute instance-groups unmanaged add-instances ig-lean-ai \
  --zone=us-central1-a \
  --instances=vm-lean-ai-gpu

gcloud compute instance-groups unmanaged set-named-ports ig-lean-ai \
  --zone=us-central1-a \
  --named-ports=http:8420

# Create health check
gcloud compute health-checks create http hc-lean-ai \
  --port=8420 \
  --request-path=/health \
  --check-interval=30s

# Create backend service
gcloud compute backend-services create bs-lean-ai \
  --protocol=HTTP \
  --port-name=http \
  --health-checks=hc-lean-ai \
  --global

gcloud compute backend-services add-backend bs-lean-ai \
  --instance-group=ig-lean-ai \
  --instance-group-zone=us-central1-a \
  --global

# Create URL map
gcloud compute url-maps create urlmap-lean-ai \
  --default-service=bs-lean-ai

# Create Google-managed SSL certificate
gcloud compute ssl-certificates create cert-lean-ai \
  --domains=lean-ai.corp.com \
  --global

# Create HTTPS proxy
gcloud compute target-https-proxies create proxy-lean-ai \
  --url-map=urlmap-lean-ai \
  --ssl-certificates=cert-lean-ai

# Create forwarding rule
gcloud compute forwarding-rules create fr-lean-ai \
  --global \
  --target-https-proxy=proxy-lean-ai \
  --ports=443
```

### Restricting Sensitive Endpoints

> **SECURITY:** The `/metrics` and `/health` endpoints must not be exposed through the external load balancer. Use URL map path rules to return 403 for these paths or direct them to a non-existent backend.

> **SECURITY:** The web dashboard should be disabled in production (`dashboard.enabled: false`) or restricted to admin VPN CIDR ranges via Cloud Armor security policies.

### Cloud Armor WAF

Attach a Cloud Armor security policy for protection against common web attacks:

```bash
# Create security policy
gcloud compute security-policies create policy-lean-ai \
  --description="WAF policy for lean-ai-serve"

# Add preconfigured WAF rules
gcloud compute security-policies rules create 1000 \
  --security-policy=policy-lean-ai \
  --expression="evaluatePreconfiguredExpr('xss-v33-stable')" \
  --action=deny-403

gcloud compute security-policies rules create 1001 \
  --security-policy=policy-lean-ai \
  --expression="evaluatePreconfiguredExpr('sqli-v33-stable')" \
  --action=deny-403

# Block /metrics from external access
gcloud compute security-policies rules create 900 \
  --security-policy=policy-lean-ai \
  --expression="request.path.matches('/metrics.*') || request.path.matches('/health.*')" \
  --action=deny-403

# Apply to backend service
gcloud compute backend-services update bs-lean-ai \
  --security-policy=policy-lean-ai \
  --global
```

---

## Identity Integration

### Google Cloud Identity / Workspace as OIDC Provider

Register lean-ai-serve as an OAuth 2.0 application:

1. Go to Google Cloud Console > APIs & Services > Credentials > Create OAuth client ID
2. Application type: Web application
3. Note the Client ID

> Google ID tokens do not natively include custom roles. You have two options:
> - **Option A:** Use Google Groups mapped to lean-ai-serve roles (requires a middleware or custom claim mapper)
> - **Option B (Recommended):** Deploy Keycloak on GKE, federate with Google Cloud Identity, and configure rich role mappings via Keycloak realm roles

#### Option A: Google as Direct OIDC Provider

```yaml
security:
  mode: "oidc"
  jwt_secret: "ENV[JWT_SECRET]"

  oidc:
    issuer_url: "https://accounts.google.com"
    client_id: "YOUR_CLIENT_ID.apps.googleusercontent.com"
    audience: "YOUR_CLIENT_ID.apps.googleusercontent.com"
    roles_claim: "groups"              # Requires Google Workspace Directory API
    role_mapping:
      "lean-ai-admins@corp.com": "admin"
      "lean-ai-managers@corp.com": "model-manager"
      "lean-ai-trainers@corp.com": "trainer"
      "lean-ai-users@corp.com": "user"
      "lean-ai-auditors@corp.com": "auditor"
    default_role: "user"
    jwks_cache_ttl: 3600
```

#### Option B: Keycloak on GKE (Recommended for RBAC)

Deploy Keycloak on GKE, configure Google as an identity provider in Keycloak, and map Keycloak realm roles to lean-ai-serve roles:

```yaml
security:
  mode: "oidc"
  jwt_secret: "ENV[JWT_SECRET]"

  oidc:
    issuer_url: "https://keycloak.internal.corp.com/realms/lean-ai"
    client_id: "lean-ai-serve"
    audience: "lean-ai-serve"
    roles_claim: "realm_access.roles"
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

### Cloud Monitoring and Cloud Logging

GCP Compute Engine VMs with the Ops Agent automatically forward logs and metrics:

```bash
# Install the Ops Agent on the GPU instance
curl -sSO https://dl.google.com/cloudagents/add-google-cloud-ops-agent-repo.sh
sudo bash add-google-cloud-ops-agent-repo.sh --also-install
```

Configure the Ops Agent to collect JSON-formatted lean-ai-serve logs from the systemd journal.

### Prometheus and Grafana

For Prometheus metrics:

- **GKE:** Use Google Cloud Managed Service for Prometheus, which auto-discovers and scrapes `/metrics` endpoints
- **Compute Engine:** Deploy a self-hosted Prometheus within the VPC that scrapes port 8420
- Import the lean-ai-serve Grafana dashboard from `dashboards/lean-ai-serve.json`

### Database Monitoring

Cloud SQL provides built-in monitoring dashboards in the Cloud Console showing query performance, connection counts, and storage usage.

---

## Backup and Disaster Recovery

### Database Backups

Cloud SQL provides automated daily backups with point-in-time recovery:

```bash
# Verify backup configuration
gcloud sql instances describe sql-lean-ai \
  --format="value(settings.backupConfiguration)"
```

Regional availability type (configured above) provides automatic failover for high availability across zones.

### Encryption Key Backup

> **SECURITY:** The master encryption key must be backed up securely. If lost, all encrypted audit data becomes unrecoverable. Secret Manager automatically versions secrets for recovery. For additional safety, replicate the secret across regions or use Cloud KMS with multi-region key rings.

```bash
# Add a replication policy to the secret
gcloud secrets update lean-ai-encryption-key \
  --replication-policy="user-managed" \
  --locations="us-central1,us-east1"
```

### Audit Log Archival

Schedule regular exports (daily or weekly) to Cloud Storage as described in the Storage section. Configure Object Lifecycle Management for long-term retention matching your compliance requirements (default: 2190 days / 6 years for HIPAA).

---

## Cost Optimization

- **Idle sleep lifecycle:** Configure `lifecycle.idle_sleep_timeout` on models to free GPU memory when idle. Use `auto_wake_on_request: true` so models restart on demand.
- **Committed Use Discounts (CUDs):** Purchase 1-year or 3-year commitments for GPU instances (up to 57% savings).
- **Right-size instances:** Use `g2-standard-4` (L4) for models under 20B parameters instead of A100 instances.
- **Sustained use discounts:** Automatic discounts for instances running more than 25% of the month.
- **Storage class lifecycle:** Use Nearline (30+ days) and Coldline (90+ days) classes for audit exports.
- **GKE cluster autoscaler:** Scale GPU node pools to 0 nodes during off-hours in non-production environments.
- **Burstable database:** Use `db-f1-micro` or `db-custom-1-3840` for Cloud SQL in development.

> Spot VMs (formerly preemptible) offer significant cost savings but are not recommended for HIPAA production workloads due to preemption risk. Use only for development and testing.

---

## Configuration Example

Complete `config.yaml` for a GCP deployment. All secrets reference environment variables injected from Secret Manager at startup.

```yaml
server:
  host: "0.0.0.0"
  port: 8420
  tls:
    enabled: false                         # TLS terminated at Cloud HTTPS LB

security:
  mode: "oidc"                             # NEVER use "none" in production
  jwt_secret: "ENV[JWT_SECRET]"            # NEVER leave empty -- set explicitly
  jwt_expiry_hours: 8.0

  oidc:
    issuer_url: "https://keycloak.internal.corp.com/realms/lean-ai"
    client_id: "lean-ai-serve"
    audience: "lean-ai-serve"
    roles_claim: "realm_access.roles"
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
    key_env_var: "LEAN_AI_ENCRYPTION_KEY"  # Injected from Secret Manager

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

- [ ] GPU VMs have no external IP addresses
- [ ] Firewall rules deny all ingress except from the load balancer and IAP
- [ ] Firewall rules restrict egress to Google APIs and internal VPC traffic only
- [ ] Private IP Google Access enabled on the GPU subnet
- [ ] Private Services Connection configured for Cloud SQL (no public IP)
- [ ] Cloud Armor WAF enabled on the HTTPS Load Balancer
- [ ] `/metrics` and `/health` blocked via Cloud Armor or URL map rules
- [ ] `/dashboard` disabled or restricted to admin VPN CIDR
- [ ] All secrets in Secret Manager (JWT, DB password, HF token, encryption key, session secret)
- [ ] `security.mode` set to `oidc` or `ldap` (never `none`)
- [ ] `security.jwt_secret` explicitly set via `ENV[]` (never auto-generated)
- [ ] `encryption.at_rest.enabled: true` with key from Secret Manager or Cloud KMS
- [ ] `database.url` points to Cloud SQL PostgreSQL (never SQLite)
- [ ] `audit.log_prompts_hash_only: true` if handling PHI/PII
- [ ] `security.content_filtering.enabled: true` with PHI/PII patterns
- [ ] `logging.json_output: true` and `logging.level: "INFO"`
- [ ] Model files pre-staged in Cloud Storage or downloaded through controlled egress
- [ ] HuggingFace token stored in Secret Manager, not in config files
- [ ] Cloud Storage buckets use uniform access and default encryption
- [ ] Cloud SQL Regional HA and automated backups enabled with point-in-time recovery
- [ ] VPC Service Controls perimeter configured (optional, for high-security environments)
- [ ] `lean-ai-serve check --config config.yaml` passes without warnings
- [ ] Run `lean-ai-serve db init` to initialize the database schema
