# Deploying lean-ai-serve on Amazon Web Services

This guide covers deploying lean-ai-serve securely on AWS using GPU EC2 instances or Amazon EKS, Amazon RDS for PostgreSQL, AWS Secrets Manager, and Amazon Cognito / OIDC integration.

> See also: [Deployment Guide](deployment.md) for Docker, systemd, nginx, and generic production setup.
> [Security & Compliance](security-and-compliance.md) for HIPAA audit logging and encryption details.
> [Authentication](authentication.md) for OIDC, LDAP, and RBAC configuration.

---

## Prerequisites & Resource Planning

### Required Accounts and Permissions

- AWS account with GPU instance quotas approved (Service Quotas > EC2 > Running On-Demand P/G instances)
- AWS CLI v2 (`aws`) installed and configured with appropriate credentials
- A dedicated VPC for the deployment

Required IAM permissions:

| Permission | Purpose |
|------------|---------|
| ec2:* (scoped to VPC) | Create instances, security groups, subnets |
| rds:* | Provision and manage PostgreSQL |
| secretsmanager:* | Create and read secrets |
| s3:* (scoped to buckets) | Model staging, audit exports |
| elasticloadbalancing:* | Application Load Balancer |
| eks:* (optional) | EKS cluster management |
| iam:PassRole | Assign instance profiles |

### GPU Instance Selection

| Instance Type | GPUs | GPU Type | vCPUs | RAM | Use Case |
|---------------|------|----------|-------|----|----------|
| g5.xlarge | 1 | A10G 24 GB | 4 | 16 GB | Small models (7B parameters) |
| g5.12xlarge | 4 | A10G 24 GB | 48 | 192 GB | Multi-model or tensor parallel |
| p3.2xlarge | 1 | V100 16 GB | 8 | 61 GB | Budget option for smaller models |
| p4d.24xlarge | 8 | A100 40 GB | 96 | 1152 GB | Very large models (70B+) |
| p5.48xlarge | 8 | H100 80 GB | 192 | 2048 GB | Maximum performance |

### Estimated Resource Requirements

| Component | Recommended | Purpose |
|-----------|-------------|---------|
| EC2 | g5.xlarge or p3.2xlarge | Inference server |
| RDS | db.t4g.medium (2 vCPUs, 4 GB) | Metadata, audit logs, API keys |
| ALB | Application Load Balancer | TLS termination, WAF, routing |
| Secrets Manager | Standard | Secrets and encryption key storage |
| S3 | Standard | Audit exports, model staging |

---

## Network Architecture

All inference traffic must remain within the Amazon VPC. GPU instances must never have public IP addresses.

> **SECURITY:** Never allow inference traffic to traverse the public internet. All communication between clients, the load balancer, GPU instances, and the database must stay within the VPC or traverse encrypted private links.

### VPC and Subnet Design

```bash
# Create VPC
VPC_ID=$(aws ec2 create-vpc \
  --cidr-block 10.0.0.0/16 \
  --tag-specifications 'ResourceType=vpc,Tags=[{Key=Name,Value=vpc-lean-ai}]' \
  --query 'Vpc.VpcId' --output text)

# Enable DNS hostname resolution
aws ec2 modify-vpc-attribute --vpc-id $VPC_ID --enable-dns-hostnames

# Private subnets for GPU instances (two AZs for HA)
GPU_SUBNET_A=$(aws ec2 create-subnet \
  --vpc-id $VPC_ID \
  --cidr-block 10.0.1.0/24 \
  --availability-zone us-east-1a \
  --tag-specifications 'ResourceType=subnet,Tags=[{Key=Name,Value=snet-gpu-a}]' \
  --query 'Subnet.SubnetId' --output text)

GPU_SUBNET_B=$(aws ec2 create-subnet \
  --vpc-id $VPC_ID \
  --cidr-block 10.0.2.0/24 \
  --availability-zone us-east-1b \
  --tag-specifications 'ResourceType=subnet,Tags=[{Key=Name,Value=snet-gpu-b}]' \
  --query 'Subnet.SubnetId' --output text)

# Public subnets for ALB (two AZs required)
LB_SUBNET_A=$(aws ec2 create-subnet \
  --vpc-id $VPC_ID \
  --cidr-block 10.0.10.0/24 \
  --availability-zone us-east-1a \
  --tag-specifications 'ResourceType=subnet,Tags=[{Key=Name,Value=snet-lb-a}]' \
  --query 'Subnet.SubnetId' --output text)

LB_SUBNET_B=$(aws ec2 create-subnet \
  --vpc-id $VPC_ID \
  --cidr-block 10.0.11.0/24 \
  --availability-zone us-east-1b \
  --tag-specifications 'ResourceType=subnet,Tags=[{Key=Name,Value=snet-lb-b}]' \
  --query 'Subnet.SubnetId' --output text)

# Private subnets for RDS (two AZs required)
DB_SUBNET_A=$(aws ec2 create-subnet \
  --vpc-id $VPC_ID \
  --cidr-block 10.0.20.0/24 \
  --availability-zone us-east-1a \
  --tag-specifications 'ResourceType=subnet,Tags=[{Key=Name,Value=snet-db-a}]' \
  --query 'Subnet.SubnetId' --output text)

DB_SUBNET_B=$(aws ec2 create-subnet \
  --vpc-id $VPC_ID \
  --cidr-block 10.0.21.0/24 \
  --availability-zone us-east-1b \
  --tag-specifications 'ResourceType=subnet,Tags=[{Key=Name,Value=snet-db-b}]' \
  --query 'Subnet.SubnetId' --output text)

# Internet Gateway (for ALB subnets only)
IGW_ID=$(aws ec2 create-internet-gateway \
  --tag-specifications 'ResourceType=internet-gateway,Tags=[{Key=Name,Value=igw-lean-ai}]' \
  --query 'InternetGateway.InternetGatewayId' --output text)
aws ec2 attach-internet-gateway --vpc-id $VPC_ID --internet-gateway-id $IGW_ID

# NAT Gateway for GPU subnet outbound (restricted)
EIP_ID=$(aws ec2 allocate-address --domain vpc --query 'AllocationId' --output text)
NAT_ID=$(aws ec2 create-nat-gateway \
  --subnet-id $LB_SUBNET_A \
  --allocation-id $EIP_ID \
  --tag-specifications 'ResourceType=natgateway,Tags=[{Key=Name,Value=nat-lean-ai}]' \
  --query 'NatGateway.NatGatewayId' --output text)
```

Create route tables: public subnets route `0.0.0.0/0` to the Internet Gateway; private GPU subnets route `0.0.0.0/0` to the NAT Gateway (or remove the default route entirely for air-gapped deployments).

### Security Groups

```bash
# Security group for ALB
SG_ALB=$(aws ec2 create-security-group \
  --group-name sg-alb-lean-ai \
  --description "ALB for lean-ai-serve" \
  --vpc-id $VPC_ID \
  --query 'GroupId' --output text)

aws ec2 authorize-security-group-ingress \
  --group-id $SG_ALB \
  --protocol tcp --port 443 \
  --cidr 0.0.0.0/0  # Or restrict to corporate CIDR

# Security group for GPU instances
SG_GPU=$(aws ec2 create-security-group \
  --group-name sg-gpu-lean-ai \
  --description "GPU instances for lean-ai-serve" \
  --vpc-id $VPC_ID \
  --query 'GroupId' --output text)

# Allow inbound from ALB only
aws ec2 authorize-security-group-ingress \
  --group-id $SG_GPU \
  --protocol tcp --port 8420 \
  --source-group $SG_ALB

# Security group for RDS
SG_DB=$(aws ec2 create-security-group \
  --group-name sg-db-lean-ai \
  --description "RDS for lean-ai-serve" \
  --vpc-id $VPC_ID \
  --query 'GroupId' --output text)

# Allow inbound from GPU instances only
aws ec2 authorize-security-group-ingress \
  --group-id $SG_DB \
  --protocol tcp --port 5432 \
  --source-group $SG_GPU
```

> **SECURITY:** GPU instances must reside in private subnets with no public IP addresses. Use security groups that allow only the ALB to reach port 8420, and only GPU instances to reach the database on port 5432.

> **SECURITY:** Never expose `/metrics` or `/health` endpoints to the public internet. These endpoints should only be accessible from within the VPC or via restricted ALB listener rules.

### VPC Endpoints

Create VPC endpoints to keep traffic to AWS services off the public internet:

```bash
# S3 Gateway Endpoint (free)
aws ec2 create-vpc-endpoint \
  --vpc-id $VPC_ID \
  --service-name com.amazonaws.us-east-1.s3 \
  --route-table-ids <GPU_ROUTE_TABLE_ID>

# Secrets Manager Interface Endpoint
aws ec2 create-vpc-endpoint \
  --vpc-id $VPC_ID \
  --service-name com.amazonaws.us-east-1.secretsmanager \
  --vpc-endpoint-type Interface \
  --subnet-ids $GPU_SUBNET_A $GPU_SUBNET_B \
  --security-group-ids $SG_GPU \
  --private-dns-enabled
```

> **SECURITY:** Restrict outbound internet access from GPU instances. Use VPC endpoints for AWS services (S3, Secrets Manager, ECR) to avoid routing through the NAT Gateway. If full air-gapping is required, remove the NAT Gateway route and pre-stage all models via S3.

---

## Compute Setup

### Option A: GPU EC2 Instance

```bash
# Launch a GPU instance in the private subnet
INSTANCE_ID=$(aws ec2 run-instances \
  --image-id ami-0abcdef1234567890 \  # Use AWS Deep Learning AMI (Ubuntu)
  --instance-type g5.xlarge \
  --subnet-id $GPU_SUBNET_A \
  --security-group-ids $SG_GPU \
  --no-associate-public-ip-address \
  --iam-instance-profile Name=lean-ai-serve-role \
  --block-device-mappings '[{"DeviceName":"/dev/sda1","Ebs":{"VolumeSize":200,"VolumeType":"gp3"}}]' \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=lean-ai-gpu}]' \
  --query 'Instances[0].InstanceId' --output text)
```

Use the AWS Deep Learning AMI which comes with NVIDIA drivers and Docker pre-installed. SSH to the instance via SSM Session Manager (no SSH keys or bastion needed):

```bash
aws ssm start-session --target $INSTANCE_ID
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

### Option B: Amazon EKS

```bash
# Create an EKS cluster with private networking
eksctl create cluster \
  --name lean-ai-eks \
  --region us-east-1 \
  --vpc-private-subnets $GPU_SUBNET_A,$GPU_SUBNET_B \
  --vpc-public-subnets $LB_SUBNET_A,$LB_SUBNET_B \
  --without-nodegroup

# Add a GPU node group
eksctl create nodegroup \
  --cluster lean-ai-eks \
  --name gpu-nodes \
  --node-type g5.xlarge \
  --nodes 1 \
  --nodes-min 0 \
  --nodes-max 2 \
  --node-private-networking
```

Install the NVIDIA device plugin and deploy lean-ai-serve as a Kubernetes Deployment with GPU resource requests. Use the AWS Secrets and Configuration Provider (ASCP) for the Secrets Store CSI Driver to inject secrets from Secrets Manager.

> **SECURITY:** Pre-stage model files to Amazon S3 and sync them to instances at deploy time, or download via NAT Gateway with restricted outbound rules. Do not allow GPU instances unrestricted internet access for model downloads.

---

## Managed Database

Use Amazon RDS for PostgreSQL with private subnet placement. Never use SQLite for production deployments.

> **SECURITY:** Never use SQLite for production. SQLite does not support concurrent writes, lacks network-level access controls, and cannot be configured for high availability or automated backups. Always use a managed PostgreSQL instance.

```bash
# Create RDS subnet group
aws rds create-db-subnet-group \
  --db-subnet-group-name lean-ai-db-subnets \
  --db-subnet-group-description "Private subnets for lean-ai-serve RDS" \
  --subnet-ids $DB_SUBNET_A $DB_SUBNET_B

# Create PostgreSQL instance (private, no public access)
aws rds create-db-instance \
  --db-instance-identifier lean-ai-postgres \
  --db-instance-class db.t4g.medium \
  --engine postgres \
  --engine-version 16.4 \
  --master-username lean_ai_admin \
  --master-user-password "$(aws secretsmanager get-secret-value --secret-id lean-ai-serve/db-password --query SecretString --output text)" \
  --allocated-storage 100 \
  --storage-type gp3 \
  --vpc-security-group-ids $SG_DB \
  --db-subnet-group-name lean-ai-db-subnets \
  --no-publicly-accessible \
  --multi-az \
  --backup-retention-period 35 \
  --storage-encrypted
```

After the instance is available, create the application database:

```bash
# Connect via the GPU instance or a bastion
psql -h lean-ai-postgres.xxxx.us-east-1.rds.amazonaws.com -U lean_ai_admin -c "CREATE DATABASE lean_ai_serve;"
```

Configure lean-ai-serve:

```yaml
database:
  url: "ENV[DATABASE_URL]"
  pool_size: 5
  pool_max_overflow: 10
```

Set the environment variable:

```bash
export DATABASE_URL="postgresql+asyncpg://lean_ai_admin:PASSWORD@lean-ai-postgres.xxxx.us-east-1.rds.amazonaws.com:5432/lean_ai_serve"
```

Initialize the database:

```bash
lean-ai-serve db init --config /etc/lean-ai-serve/config.yaml
```

---

## Secrets Management

Store all sensitive values in AWS Secrets Manager. Never put secrets in plain text in `config.yaml`.

> **SECURITY:** Never store secrets in plain text in config.yaml. All secrets (JWT secret, database password, HuggingFace token, encryption key) must be stored in AWS Secrets Manager and injected as environment variables at runtime using `ENV[]` references.

> **SECURITY:** Never use auto-generated JWT secrets in production. An auto-generated JWT secret changes on every restart, invalidating all active sessions and tokens. Explicitly set `security.jwt_secret` via `ENV[]` or `ENC[]`.

### Store Secrets

```bash
# Generate and store JWT secret
JWT_SECRET=$(openssl rand -hex 32)
aws secretsmanager create-secret \
  --name lean-ai-serve/jwt-secret \
  --secret-string "$JWT_SECRET"

# Store database password
aws secretsmanager create-secret \
  --name lean-ai-serve/db-password \
  --secret-string "YOUR_DB_PASSWORD"

# Store HuggingFace token
aws secretsmanager create-secret \
  --name lean-ai-serve/hf-token \
  --secret-string "hf_YOUR_TOKEN"

# Generate and store encryption key
lean-ai-serve config generate-key /tmp/master.key
ENCRYPTION_KEY=$(xxd -p -c 64 /tmp/master.key)
aws secretsmanager create-secret \
  --name lean-ai-serve/encryption-key \
  --secret-string "$ENCRYPTION_KEY"
rm /tmp/master.key  # Remove local copy after storing in Secrets Manager

# Store dashboard session secret
SESSION_SECRET=$(openssl rand -hex 32)
aws secretsmanager create-secret \
  --name lean-ai-serve/session-secret \
  --secret-string "$SESSION_SECRET"
```

> **SECURITY:** HuggingFace tokens must be stored in AWS Secrets Manager, never in config.yaml or environment files committed to source control.

> **SECURITY:** The master encryption key must be backed up securely. Enable secret versioning in Secrets Manager for recovery. For HIPAA workloads, consider using AWS KMS with a customer-managed key (CMK) for HSM-backed encryption.

### IAM Instance Profile

Create an IAM role for the GPU instances that grants access to Secrets Manager:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "secretsmanager:GetSecretValue",
      "Resource": "arn:aws:secretsmanager:us-east-1:ACCOUNT_ID:secret:lean-ai-serve/*"
    }
  ]
}
```

### Inject Secrets at Runtime

Create a startup script (`/opt/lean-ai-serve/start.sh`):

```bash
#!/bin/bash
REGION="us-east-1"

get_secret() {
  aws secretsmanager get-secret-value \
    --region $REGION \
    --secret-id "lean-ai-serve/$1" \
    --query SecretString --output text
}

export JWT_SECRET=$(get_secret jwt-secret)
export DATABASE_URL="postgresql+asyncpg://lean_ai_admin:$(get_secret db-password)@lean-ai-postgres.xxxx.us-east-1.rds.amazonaws.com:5432/lean_ai_serve"
export HF_TOKEN=$(get_secret hf-token)
export LEAN_AI_ENCRYPTION_KEY=$(get_secret encryption-key)
export SESSION_SECRET=$(get_secret session-secret)

exec /opt/lean-ai-serve/venv/bin/lean-ai-serve start --config /etc/lean-ai-serve/config.yaml
```

For EKS, use the [AWS Secrets and Configuration Provider](https://docs.aws.amazon.com/secretsmanager/latest/userguide/integrating_csi_driver.html) for the Kubernetes Secrets Store CSI Driver.

---

## Storage

### Model Cache

For air-gapped or restricted environments, pre-stage model files in Amazon S3 and sync them to the GPU instance at deploy time:

```bash
# Create S3 bucket (block all public access)
aws s3api create-bucket \
  --bucket lean-ai-models-ACCOUNT_ID \
  --region us-east-1

aws s3api put-public-access-block \
  --bucket lean-ai-models-ACCOUNT_ID \
  --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true

# Enable default encryption
aws s3api put-bucket-encryption \
  --bucket lean-ai-models-ACCOUNT_ID \
  --server-side-encryption-configuration \
    '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"aws:kms"}}]}'

# Upload pre-downloaded model files
aws s3 sync /local/models/Qwen3-Coder-30B-A3B/ \
  s3://lean-ai-models-ACCOUNT_ID/models/Qwen3-Coder-30B-A3B/

# On the GPU instance, sync model files (traffic stays in VPC via S3 endpoint)
aws s3 sync s3://lean-ai-models-ACCOUNT_ID/models/ \
  /opt/lean-ai-serve/cache/hub/
```

### Audit Log Exports

Schedule periodic audit exports to S3:

```bash
# Export audit logs and upload
lean-ai-serve admin audit-export --format json --output /tmp/audit-$(date +%Y%m%d).json
aws s3 cp /tmp/audit-$(date +%Y%m%d).json \
  s3://lean-ai-audit-ACCOUNT_ID/exports/audit-$(date +%Y%m%d).json
rm /tmp/audit-$(date +%Y%m%d).json
```

Configure S3 lifecycle policies to transition old exports to S3 Glacier for long-term retention.

---

## TLS and Load Balancing

Use an Application Load Balancer (ALB) for TLS termination with AWS Certificate Manager (ACM).

> **SECURITY:** Never expose the lean-ai-serve server without TLS. All client traffic must be encrypted in transit. Use an ALB or reverse proxy for TLS termination.

### ALB Setup

```bash
# Create the ALB in public subnets
ALB_ARN=$(aws elbv2 create-load-balancer \
  --name alb-lean-ai \
  --subnets $LB_SUBNET_A $LB_SUBNET_B \
  --security-groups $SG_ALB \
  --scheme internet-facing \
  --type application \
  --query 'LoadBalancers[0].LoadBalancerArn' --output text)

# Create target group
TG_ARN=$(aws elbv2 create-target-group \
  --name tg-lean-ai \
  --protocol HTTP \
  --port 8420 \
  --vpc-id $VPC_ID \
  --target-type instance \
  --health-check-path /health \
  --health-check-interval-seconds 30 \
  --query 'TargetGroups[0].TargetGroupArn' --output text)

# Register GPU instance
aws elbv2 register-targets \
  --target-group-arn $TG_ARN \
  --targets Id=$INSTANCE_ID

# Request ACM certificate
CERT_ARN=$(aws acm request-certificate \
  --domain-name "lean-ai.corp.com" \
  --validation-method DNS \
  --query 'CertificateArn' --output text)

# Create HTTPS listener
aws elbv2 create-listener \
  --load-balancer-arn $ALB_ARN \
  --protocol HTTPS \
  --port 443 \
  --certificates CertificateArn=$CERT_ARN \
  --default-actions Type=forward,TargetGroupArn=$TG_ARN
```

### Restricting Sensitive Endpoints

> **SECURITY:** The `/metrics` and `/health` endpoints must not be exposed through the ALB to the public internet. Use ALB listener rules to return 403 for these paths, or restrict them to internal VPC traffic only.

> **SECURITY:** The web dashboard should be disabled in production (`dashboard.enabled: false`) or restricted to admin VPN CIDR ranges via ALB listener rules.

Create ALB listener rules to:
- Block `/metrics` from external access (return fixed 403 response)
- Restrict `/dashboard/*` to admin VPN source IPs only
- Forward all other authenticated API traffic to the target group

### AWS WAF

Optionally attach AWS WAF to the ALB with AWS Managed Rules (Core Rule Set, Known Bad Inputs) for additional protection.

---

## Identity Integration

### Amazon Cognito as OIDC Provider

```bash
# Create a Cognito User Pool
POOL_ID=$(aws cognito-idp create-user-pool \
  --pool-name lean-ai-users \
  --auto-verified-attributes email \
  --query 'UserPool.Id' --output text)

# Create an app client
CLIENT_ID=$(aws cognito-idp create-user-pool-client \
  --user-pool-id $POOL_ID \
  --client-name lean-ai-serve \
  --no-generate-secret \
  --query 'UserPoolClient.ClientId' --output text)

# Create groups that map to lean-ai-serve roles
for role in admin model-manager trainer user auditor service-account; do
  aws cognito-idp create-group \
    --user-pool-id $POOL_ID \
    --group-name $role
done
```

Configure lean-ai-serve:

```yaml
security:
  mode: "oidc"
  jwt_secret: "ENV[JWT_SECRET]"

  oidc:
    issuer_url: "https://cognito-idp.us-east-1.amazonaws.com/POOL_ID"
    client_id: "YOUR_CLIENT_ID"
    audience: "YOUR_CLIENT_ID"
    roles_claim: "cognito:groups"
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

### CloudWatch Logs

Forward lean-ai-serve JSON logs to CloudWatch using the CloudWatch agent:

```bash
# Install CloudWatch agent on the GPU instance
sudo yum install -y amazon-cloudwatch-agent  # Amazon Linux
# or
sudo apt install -y amazon-cloudwatch-agent  # Ubuntu
```

Configure the agent to collect logs from the systemd journal or log files.

### Prometheus and Grafana

For Prometheus metrics:

- **Self-hosted:** Deploy Prometheus within the VPC that scrapes the GPU instance's `/metrics` endpoint on port 8420
- **Amazon Managed Prometheus (AMP):** Use with Amazon Managed Grafana for a fully managed monitoring stack
- Import the lean-ai-serve Grafana dashboard from `dashboards/lean-ai-serve.json`

### RDS Monitoring

Enable Enhanced Monitoring and Performance Insights on the RDS instance for database query analysis and resource utilization tracking.

---

## Backup and Disaster Recovery

### Database Backups

RDS provides automated daily backups with point-in-time recovery:

```bash
# Verify backup retention is set to 35 days
aws rds modify-db-instance \
  --db-instance-identifier lean-ai-postgres \
  --backup-retention-period 35
```

Multi-AZ deployment (configured above) provides automatic failover for high availability.

### Encryption Key Backup

> **SECURITY:** The master encryption key must be backed up securely. If lost, all encrypted audit data becomes unrecoverable. Secrets Manager automatically versions secrets for recovery. For additional safety, replicate the secret to another region.

```bash
# Replicate encryption key to a secondary region
aws secretsmanager replicate-secret-to-regions \
  --secret-id lean-ai-serve/encryption-key \
  --add-replica-regions Region=us-west-2
```

### Audit Log Archival

Schedule regular exports (daily or weekly) to S3 as described in the Storage section. Configure S3 lifecycle policies to transition exports to Glacier after 90 days. Retain for the full compliance period (default: 2190 days / 6 years for HIPAA).

---

## Cost Optimization

- **Idle sleep lifecycle:** Configure `lifecycle.idle_sleep_timeout` on models to free GPU memory when idle. Use `auto_wake_on_request: true` so models restart on demand.
- **Reserved Instances / Savings Plans:** Purchase 1-year or 3-year commitments for GPU instances (up to 60% savings).
- **Right-size instances:** Use `g5.xlarge` (A10G) for models under 20B parameters instead of p4d instances.
- **S3 Intelligent-Tiering:** Automatically moves audit exports to lower-cost tiers based on access patterns.
- **EKS Cluster Autoscaler:** Scale GPU node groups to 0 nodes during off-hours in non-production environments.
- **Burstable database:** Use `db.t4g.medium` for RDS in low-traffic deployments.

> Spot Instances offer significant cost savings but are not recommended for HIPAA production workloads due to interruption risk. Use only for development and testing.

---

## Configuration Example

Complete `config.yaml` for an AWS deployment. All secrets reference environment variables injected from Secrets Manager at startup.

```yaml
server:
  host: "0.0.0.0"
  port: 8420
  tls:
    enabled: false                         # TLS terminated at ALB

security:
  mode: "oidc"                             # NEVER use "none" in production
  jwt_secret: "ENV[JWT_SECRET]"            # NEVER leave empty -- set explicitly
  jwt_expiry_hours: 8.0

  oidc:
    issuer_url: "https://cognito-idp.us-east-1.amazonaws.com/YOUR_POOL_ID"
    client_id: "YOUR_CLIENT_ID"
    audience: "YOUR_CLIENT_ID"
    roles_claim: "cognito:groups"
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
    key_env_var: "LEAN_AI_ENCRYPTION_KEY"  # Injected from Secrets Manager

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

- [ ] GPU instances in private subnets with no public IP addresses
- [ ] Security groups follow least-privilege (`sg-gpu`, `sg-alb`, `sg-db`)
- [ ] No Internet Gateway route in GPU subnet route tables
- [ ] VPC Endpoints configured for Secrets Manager and S3
- [ ] NAT Gateway egress restricted or removed for air-gapped environments
- [ ] ALB WAF enabled with AWS Managed Rules
- [ ] `/metrics` and `/health` blocked or restricted via ALB listener rules
- [ ] `/dashboard` disabled or restricted to admin VPN CIDR
- [ ] All secrets in AWS Secrets Manager (JWT, DB password, HF token, encryption key, session secret)
- [ ] `security.mode` set to `oidc` or `ldap` (never `none`)
- [ ] `security.jwt_secret` explicitly set via `ENV[]` (never auto-generated)
- [ ] `encryption.at_rest.enabled: true` with key from Secrets Manager
- [ ] `database.url` points to RDS PostgreSQL (never SQLite)
- [ ] `audit.log_prompts_hash_only: true` if handling PHI/PII
- [ ] `security.content_filtering.enabled: true` with PHI/PII patterns
- [ ] `logging.json_output: true` and `logging.level: "INFO"`
- [ ] Model files pre-staged in S3 or downloaded through controlled egress
- [ ] HuggingFace token stored in Secrets Manager, not in config files
- [ ] S3 buckets block all public access, versioning and encryption enabled
- [ ] RDS Multi-AZ enabled with 35-day backup retention and storage encryption
- [ ] `lean-ai-serve check --config config.yaml` passes without warnings
- [ ] Run `lean-ai-serve db init` to initialize the database schema
