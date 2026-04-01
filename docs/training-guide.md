# Training Guide

lean-ai-serve includes a fine-tuning subsystem for LoRA training via [LLaMA-Factory](https://github.com/hiyouga/LLaMA-Factory), with dataset management, job orchestration, and dynamic adapter deployment.

## Prerequisites

1. **Enable training** in `config.yaml`:

```yaml
training:
  enabled: true
  backend: "llama-factory"
  max_concurrent_jobs: 1
  default_gpu: [0]
  max_dataset_size_mb: 1024
```

2. **Install LLaMA-Factory** (separate from lean-ai-serve):

```bash
pip install llamafactory
# or follow https://github.com/hiyouga/LLaMA-Factory#installation
```

The `llamafactory-cli` command must be available in PATH.

3. **Install dataset utilities**:

```bash
pip install lean-ai-serve[training]
```

## Workflow Overview

```mermaid
flowchart LR
    A["Upload Dataset"] --> B["Submit Job"]
    B --> C["Start Training"]
    C --> D["Monitor Progress"]
    D --> E["Register Adapter"]
    E --> F["Deploy to Model"]
    F --> G["Inference with Adapter"]
```

## Dataset Management

### Supported formats

| Format | Description |
|--------|-------------|
| `sharegpt` | ShareGPT conversation format (JSON array of conversations) |
| `alpaca` | Alpaca instruction format (instruction, input, output) |
| `jsonl` | JSON Lines (one JSON object per line) |
| `csv` | CSV with header row |

### Upload a dataset

```bash
curl -X POST http://localhost:8420/api/training/datasets \
  -H "Authorization: Bearer las-..." \
  -F "file=@training_data.jsonl" \
  -F "name=medical-qa" \
  -F "format=sharegpt" \
  -F "description=Medical Q&A dataset for fine-tuning"
```

Response:

```json
{
  "name": "medical-qa",
  "format": "sharegpt",
  "size_bytes": 2048576,
  "row_count": 5000,
  "uploaded_by": "admin",
  "description": "Medical Q&A dataset for fine-tuning",
  "created_at": "2026-04-01T12:00:00Z"
}
```

### List datasets

```bash
curl http://localhost:8420/api/training/datasets \
  -H "Authorization: Bearer las-..."
```

### Preview dataset

```bash
curl "http://localhost:8420/api/training/datasets/medical-qa/preview?limit=3" \
  -H "Authorization: Bearer las-..."
```

### Delete dataset

```bash
curl -X DELETE http://localhost:8420/api/training/datasets/medical-qa \
  -H "Authorization: Bearer las-..."
```

## Submitting a Training Job

### Submit

```bash
curl -X POST http://localhost:8420/api/training/jobs \
  -H "Authorization: Bearer las-..." \
  -H "Content-Type: application/json" \
  -d '{
    "name": "medical-finetune-v1",
    "base_model": "qwen-7b",
    "dataset": "medical-qa",
    "num_epochs": 3,
    "learning_rate": 2e-4,
    "per_device_batch_size": 4,
    "lora_rank": 16,
    "lora_alpha": 32,
    "gpu": [1]
  }'
```

Response:

```json
{
  "job_id": "job-abc123",
  "name": "medical-finetune-v1",
  "base_model": "qwen-7b",
  "dataset": "medical-qa",
  "state": "queued",
  "submitted_by": "admin",
  "created_at": "2026-04-01T12:30:00Z"
}
```

### Job lifecycle

| State | Description |
|-------|-------------|
| `queued` | Job submitted, waiting to start |
| `running` | Training in progress |
| `completed` | Training finished, adapter registered |
| `failed` | Training failed (check error_message) |
| `cancelled` | Cancelled by user |

### Start and stream progress

```bash
curl http://localhost:8420/api/training/jobs/job-abc123/start \
  -H "Authorization: Bearer las-..."
```

Returns an SSE stream:

```
data: {"event": "step", "step": 100, "total_steps": 1500, "loss": 1.42, "learning_rate": 0.0002}
data: {"event": "step", "step": 200, "total_steps": 1500, "loss": 1.15, "learning_rate": 0.00019}
data: {"event": "eval", "step": 500, "eval_loss": 1.08}
...
data: {"event": "complete", "adapter_name": "medical-finetune-v1", "output_path": "/..."}
```

### Cancel a job

```bash
curl -X POST http://localhost:8420/api/training/jobs/job-abc123/cancel \
  -H "Authorization: Bearer las-..."
```

### List jobs

```bash
# All jobs
curl http://localhost:8420/api/training/jobs \
  -H "Authorization: Bearer las-..."

# Filter by state
curl "http://localhost:8420/api/training/jobs?state=completed" \
  -H "Authorization: Bearer las-..."
```

## GPU Scheduling

Training jobs require GPU resources. The orchestrator checks:

1. **Concurrent job limit** — `max_concurrent_jobs` (default: 1)
2. **GPU availability** — requested GPUs must not overlap with running training jobs
3. **Default GPU** — if no GPU specified, uses `training.default_gpu`

Check current GPU usage by training:

```bash
curl http://localhost:8420/api/training/gpu-status \
  -H "Authorization: Bearer las-..."
```

## LoRA Adapters

### Auto-registration

When a training job completes successfully, its LoRA adapter is automatically registered in the adapter registry.

### Import external adapters

Import adapters trained outside lean-ai-serve:

```bash
curl -X POST http://localhost:8420/api/training/adapters/import \
  -H "Authorization: Bearer las-..." \
  -H "Content-Type: application/json" \
  -d '{
    "name": "external-adapter",
    "base_model": "qwen-7b",
    "path": "/path/to/adapter/weights"
  }'
```

### List adapters

```bash
# All adapters
curl http://localhost:8420/api/training/adapters \
  -H "Authorization: Bearer las-..."

# Filter by base model
curl "http://localhost:8420/api/training/adapters?base_model=qwen-7b" \
  -H "Authorization: Bearer las-..."
```

### Deploy adapter to running model

The base model must have `enable_lora: true` in its config and be in the `loaded` state:

```yaml
models:
  qwen-7b:
    source: "Qwen/Qwen2.5-7B-Instruct"
    enable_lora: true
    max_loras: 4
```

Deploy:

```bash
curl -X POST http://localhost:8420/api/training/adapters/medical-finetune-v1/deploy \
  -H "Authorization: Bearer las-..." \
  -H "Content-Type: application/json" \
  -d '{"model_name": "qwen-7b"}'
```

This calls vLLM's `/v1/load_lora_adapter` endpoint to dynamically load the adapter.

### Undeploy adapter

```bash
curl -X POST http://localhost:8420/api/training/adapters/medical-finetune-v1/undeploy \
  -H "Authorization: Bearer las-..."
```

### Delete adapter

```bash
curl -X DELETE http://localhost:8420/api/training/adapters/medical-finetune-v1 \
  -H "Authorization: Bearer las-..."
```

## End-to-End Example

Complete workflow from dataset to inference with a fine-tuned adapter:

```bash
# 1. Upload training data
curl -X POST http://localhost:8420/api/training/datasets \
  -H "Authorization: Bearer las-..." \
  -F "file=@medical_qa.jsonl" \
  -F "name=medical-qa" \
  -F "format=sharegpt"

# 2. Submit training job
curl -X POST http://localhost:8420/api/training/jobs \
  -H "Authorization: Bearer las-..." \
  -H "Content-Type: application/json" \
  -d '{
    "name": "medical-v1",
    "base_model": "qwen-7b",
    "dataset": "medical-qa",
    "num_epochs": 3,
    "learning_rate": 2e-4,
    "lora_rank": 16
  }'
# Returns: {"job_id": "job-abc123", ...}

# 3. Start and monitor training
curl http://localhost:8420/api/training/jobs/job-abc123/start \
  -H "Authorization: Bearer las-..."
# SSE stream with progress events...

# 4. Deploy adapter (auto-registered on completion)
curl -X POST http://localhost:8420/api/training/adapters/medical-v1/deploy \
  -H "Authorization: Bearer las-..." \
  -H "Content-Type: application/json" \
  -d '{"model_name": "qwen-7b"}'

# 5. Inference using the adapter
curl http://localhost:8420/v1/chat/completions \
  -H "Authorization: Bearer las-..." \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen-7b",
    "messages": [{"role": "user", "content": "What are the symptoms of diabetes?"}],
    "max_tokens": 256
  }'
```

## CLI Commands

```bash
# List datasets
lean-ai-serve training datasets

# List jobs (optionally filter by state)
lean-ai-serve training jobs --state completed

# List adapters (optionally filter by model)
lean-ai-serve training adapters --model qwen-7b
```
