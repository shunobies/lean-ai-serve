# Deployment Guide

This guide covers deploying lean-ai-serve in production, including systemd services, Docker containers, TLS, reverse proxy, and monitoring.

## Production Checklist

Before deploying to production, verify:

- [ ] Set `security.mode` to a real auth method (not `none`)
- [ ] Set `security.jwt_secret` explicitly (use `ENV[]` or `ENC[]`)
- [ ] Enable TLS or deploy behind a TLS-terminating reverse proxy
- [ ] Enable `encryption.at_rest` for audit data
- [ ] Set `logging.json_output: true` for structured log parsing
- [ ] Set `logging.level: "INFO"` (not DEBUG)
- [ ] Review `audit.retention_days` for your compliance requirements
- [ ] Configure alerting rules for GPU memory and error rates
- [ ] Run `lean-ai-serve check --config config.yaml` to validate
- [ ] Create API keys for all services and users
- [ ] Back up the master encryption key securely
- [ ] Set `dashboard.session_secret` explicitly (use `ENV[]` or `ENC[]`) so CSRF tokens survive restarts
- [ ] Disable the dashboard in headless/API-only deployments: `dashboard.enabled: false`

## TLS Configuration

### Direct TLS

```yaml
server:
  host: "0.0.0.0"
  port: 8420
  tls:
    enabled: true
    cert_file: "/etc/ssl/certs/lean-ai-serve.pem"
    key_file: "/etc/ssl/private/lean-ai-serve.key"
```

### TLS via reverse proxy (recommended)

Terminate TLS at the reverse proxy (nginx, Caddy, etc.) and run lean-ai-serve on a private interface. See [Reverse Proxy](#reverse-proxy-nginx) below.

## systemd Service

Create `/etc/systemd/system/lean-ai-serve.service`:

```ini
[Unit]
Description=lean-ai-serve LLM Inference Server
After=network.target
Wants=network-online.target

[Service]
Type=simple
User=lean-ai
Group=lean-ai
WorkingDirectory=/opt/lean-ai-serve
ExecStart=/opt/lean-ai-serve/venv/bin/lean-ai-serve start --config /etc/lean-ai-serve/config.yaml
Restart=on-failure
RestartSec=10
TimeoutStopSec=120

# Environment
Environment=LEAN_AI_ENCRYPTION_KEY=
EnvironmentFile=-/etc/lean-ai-serve/env

# Security hardening
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths=/var/cache/lean-ai-serve /var/log/lean-ai-serve
PrivateTmp=yes

# GPU access
SupplementaryGroups=video render

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable lean-ai-serve
sudo systemctl start lean-ai-serve
sudo journalctl -u lean-ai-serve -f
```

## Docker

### Dockerfile

```dockerfile
FROM nvidia/cuda:12.4.1-runtime-ubuntu22.04

RUN apt-get update && apt-get install -y python3.11 python3.11-venv python3-pip && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src/ ./src/

RUN python3.11 -m venv /app/venv && \
    /app/venv/bin/pip install --no-cache-dir -e ".[gpu,ldap,vault]"

# Install vLLM
RUN /app/venv/bin/pip install --no-cache-dir vllm

COPY config.example.yaml /etc/lean-ai-serve/config.yaml

ENV PATH="/app/venv/bin:$PATH"

EXPOSE 8420

ENTRYPOINT ["lean-ai-serve"]
CMD ["start", "--config", "/etc/lean-ai-serve/config.yaml"]
```

### Docker Compose

```yaml
services:
  lean-ai-serve:
    build: .
    ports:
      - "8420:8420"
    volumes:
      - ./config.yaml:/etc/lean-ai-serve/config.yaml:ro
      - model-cache:/root/.cache/lean-ai-serve
    environment:
      - HF_TOKEN=${HF_TOKEN}
      - LEAN_AI_ENCRYPTION_KEY=${LEAN_AI_ENCRYPTION_KEY}
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
    restart: unless-stopped

volumes:
  model-cache:
```

### GPU passthrough

Docker requires the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html):

```bash
# Install nvidia-container-toolkit
sudo apt-get install nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker

# Run with GPU access
docker run --gpus all -p 8420:8420 lean-ai-serve
```

## Reverse Proxy (nginx)

nginx configuration with WebSocket and SSE support:

```nginx
upstream lean_ai_serve {
    server 127.0.0.1:8420;
    keepalive 32;
}

server {
    listen 443 ssl http2;
    server_name ai.corp.com;

    ssl_certificate     /etc/ssl/certs/ai.corp.com.pem;
    ssl_certificate_key /etc/ssl/private/ai.corp.com.key;

    # SSE and streaming support
    proxy_buffering off;
    proxy_cache off;

    # Timeouts for long inference requests
    proxy_connect_timeout 10s;
    proxy_read_timeout 300s;
    proxy_send_timeout 300s;

    location / {
        proxy_pass http://lean_ai_serve;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Connection "";
        proxy_http_version 1.1;
    }

    # Web dashboard (session cookie auth, serves HTML + static assets)
    location /dashboard/ {
        proxy_pass http://lean_ai_serve;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # Dashboard static assets (cache-friendly)
    location /static/ {
        proxy_pass http://lean_ai_serve;
        expires 7d;
        add_header Cache-Control "public, immutable";
    }

    # Health check endpoint (no auth, for load balancer)
    location /health {
        proxy_pass http://lean_ai_serve/health;
        access_log off;
    }

    # Prometheus metrics (restrict to internal monitoring)
    location /metrics {
        proxy_pass http://lean_ai_serve/metrics;
        allow 10.0.0.0/8;
        deny all;
    }
}
```

## Monitoring Setup

### Prometheus scrape config

```yaml
scrape_configs:
  - job_name: "lean-ai-serve"
    scrape_interval: 15s
    static_configs:
      - targets: ["localhost:8420"]
    metrics_path: /metrics
```

### Grafana dashboard

Import the pre-built dashboard from `dashboards/lean-ai-serve.json`:

1. Open Grafana -> Dashboards -> Import
2. Upload the JSON file
3. Select your Prometheus data source

## Backup

### Database location

The SQLite database is at `{cache.directory}/lean_ai_serve.db` (default: `~/.cache/lean-ai-serve/lean_ai_serve.db`).

### Backup strategy

```bash
# Check DB size
lean-ai-serve admin db-stats

# Export audit logs for archival
lean-ai-serve admin audit-export \
  --format json \
  --output /backup/audit-$(date +%Y%m%d).json

# SQLite backup (while server is running)
sqlite3 ~/.cache/lean-ai-serve/lean_ai_serve.db ".backup /backup/lean_ai_serve_$(date +%Y%m%d).db"
```

### What to back up

| Item | Location | Frequency |
|------|----------|-----------|
| SQLite database | `{cache_dir}/lean_ai_serve.db` | Daily |
| Configuration | `config.yaml` | On change |
| Master encryption key | `/etc/lean-ai-serve/master.key` | Once (secure storage) |
| Audit exports | As needed | Per retention policy |

## Environment Variables

Common environment variables for deployment:

| Variable | Purpose |
|----------|---------|
| `HF_TOKEN` | HuggingFace token for gated models |
| `LEAN_AI_ENCRYPTION_KEY` | Master encryption key (hex/base64) |
| `LEAN_AI_LDAP_BIND_PASSWORD` | LDAP bind password |
| `VAULT_ADDR` | Vault server URL |
| `VAULT_TOKEN` | Vault authentication token |
| `VAULT_ROLE_ID` | Vault AppRole role ID |
| `VAULT_SECRET_ID` | Vault AppRole secret ID |
