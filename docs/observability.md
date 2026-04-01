# Observability

lean-ai-serve provides comprehensive observability with Prometheus metrics, structured logging, OpenTelemetry tracing, and configurable alerting. All features are independently toggleable.

## Structured Logging

### Configuration

```yaml
logging:
  json_output: true   # true = JSON lines (production), false = colored console (dev)
  level: "INFO"       # DEBUG, INFO, WARNING, ERROR, CRITICAL
```

### JSON output (production)

When `json_output: true`, each log line is a JSON object:

```json
{"event": "lean-ai-serve ready on 0.0.0.0:8420", "level": "info", "timestamp": "2026-04-01T12:00:00Z", "logger": "lean_ai_serve.main"}
```

### Console output (development)

When `json_output: false`, logs use colored console output with timestamps, ideal for local development.

### Request IDs

Every HTTP request gets a unique `X-Request-ID` header (generated or preserved from the incoming request). The request ID is bound to the structlog context for all log entries during that request, and returned in the response headers.

## Prometheus Metrics

lean-ai-serve includes a built-in Prometheus metrics collector with **zero external dependencies** — no prometheus_client library needed.

### Configuration

```yaml
metrics:
  enabled: true
  gpu_poll_interval: 30   # Seconds between GPU metric snapshots
```

### Scrape endpoint

```bash
curl http://localhost:8420/metrics
```

No authentication required. Returns Prometheus text exposition format.

### Available metrics

#### Counters

| Metric | Labels | Description |
|--------|--------|-------------|
| `lean_ai_serve_requests_total` | method, path, status | Total HTTP requests |
| `lean_ai_serve_inference_tokens_total` | model, type (prompt/completion) | Total inference tokens |
| `lean_ai_serve_auth_failures_total` | method | Total authentication failures |

#### Histograms

| Metric | Labels | Description |
|--------|--------|-------------|
| `lean_ai_serve_request_duration_seconds` | method, path | Request duration |
| `lean_ai_serve_inference_latency_seconds` | model | Inference latency |

Default buckets: 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0 seconds.

#### Gauges

| Metric | Labels | Description |
|--------|--------|-------------|
| `lean_ai_serve_models_loaded` | — | Number of currently loaded models |
| `lean_ai_serve_gpu_memory_used_bytes` | gpu | GPU memory used (bytes) |
| `lean_ai_serve_gpu_utilization_pct` | gpu | GPU utilization percentage |
| `lean_ai_serve_training_jobs_active` | — | Active training jobs |

### JSON metrics summary

```bash
curl http://localhost:8420/api/metrics/summary \
  -H "Authorization: Bearer las-..."
```

```json
{
  "uptime_seconds": 3600.5,
  "total_requests": 15432,
  "total_inference_tokens": 2500000,
  "models_loaded": 2,
  "training_jobs_active": 0
}
```

### Prometheus scrape config

Add to your `prometheus.yml`:

```yaml
scrape_configs:
  - job_name: "lean-ai-serve"
    scrape_interval: 15s
    static_configs:
      - targets: ["localhost:8420"]
    metrics_path: /metrics
```

## Alerting

The built-in alert evaluator checks metrics against configurable rules at regular intervals.

### Configuration

```yaml
alerts:
  enabled: true
  evaluation_interval: 60   # Seconds between evaluations
  rules: []                  # Empty = use default rules
```

### Default rules

| Rule | Metric | Condition | Threshold | Severity |
|------|--------|-----------|-----------|----------|
| `high_gpu_memory` | `gpu_memory_used_pct` | > | 90% | warning |
| `high_error_rate` | Error rate (computed) | > | 5% | warning |

### Custom rules

```yaml
alerts:
  enabled: true
  rules:
    - name: "critical_gpu_memory"
      metric: "gpu_memory_used_pct"
      condition: "gt"
      threshold: 95.0
      severity: "critical"

    - name: "low_model_count"
      metric: "models_loaded"
      condition: "lt"
      threshold: 1.0
      severity: "warning"
      message: "No models loaded"
```

Conditions: `gt`, `lt`, `gte`, `lte`, `eq`

Severities: `info`, `warning`, `critical`

### View active alerts

```bash
curl http://localhost:8420/api/metrics/alerts \
  -H "Authorization: Bearer las-..."
```

## OpenTelemetry Tracing

Optional distributed tracing via OpenTelemetry.

### Prerequisites

```bash
pip install lean-ai-serve[tracing]
```

### Configuration

```yaml
tracing:
  enabled: true
  endpoint: "http://otel-collector:4317"   # OTLP endpoint
  protocol: "grpc"                          # grpc or http
  service_name: "lean-ai-serve"
```

### What is traced

- All FastAPI HTTP requests (auto-instrumented)
- Inference requests with model name, token counts, and latency as span attributes
- Custom spans for model loading, training job execution, etc.

### Collector setup

Example OpenTelemetry Collector config for forwarding to Jaeger:

```yaml
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: "0.0.0.0:4317"

exporters:
  jaeger:
    endpoint: "jaeger:14250"

service:
  pipelines:
    traces:
      receivers: [otlp]
      exporters: [jaeger]
```

## Usage Tracking

Token usage is tracked per-user, per-model in hourly buckets.

### Query usage

```bash
# Current user's usage (last 24 hours)
curl http://localhost:8420/api/usage/me?period_hours=24 \
  -H "Authorization: Bearer las-..."

# Per-model usage
curl http://localhost:8420/api/usage/models/my-model?period_hours=168 \
  -H "Authorization: Bearer las-..."

# All usage (admin)
curl "http://localhost:8420/api/usage?model=my-model&limit=168" \
  -H "Authorization: Bearer las-..."
```

## Background Scheduler

The background scheduler runs periodic maintenance tasks:

| Task | Interval | Description |
|------|----------|-------------|
| GPU snapshot | 30s (configurable) | Poll GPU metrics via nvidia-ml-py |
| Alert evaluation | 60s (configurable) | Check metrics against alert rules |
| Token cleanup | 1 hour | Remove expired revoked JWT tokens |
| Rate limiter cleanup | 5 minutes | Clean stale rate limit buckets |
| Audit retention | Daily | Purge audit entries beyond retention_days |
| Usage retention | Daily | Prune old usage records |
| Zombie reaper | 5 minutes | Kill orphaned vLLM subprocesses |

All tasks run independently — a failure in one task does not affect others.

## Grafana Dashboard

A pre-built Grafana dashboard is available in the `dashboards/` directory:

```bash
# Import the dashboard
# 1. Open Grafana → Dashboards → Import
# 2. Upload dashboards/lean-ai-serve.json
# 3. Select your Prometheus data source
```

The dashboard includes panels for:

- Request rate and latency percentiles
- Inference token throughput
- GPU memory and utilization per device
- Model status overview
- Authentication failure rate
- Active training jobs
