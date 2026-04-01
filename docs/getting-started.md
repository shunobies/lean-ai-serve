# Getting Started

This guide walks you through installing lean-ai-serve, pulling your first model, and making your first inference request.

## Prerequisites

- **Python 3.11+**
- **NVIDIA GPU** with CUDA drivers (for inference; optional for config/key management)
- **vLLM** installed in the same Python environment (`pip install vllm`)
- **HuggingFace account** (optional, for gated models)

## Installation

### From PyPI

```bash
pip install lean-ai-serve
```

### With optional features

```bash
# GPU monitoring
pip install lean-ai-serve[gpu]

# All enterprise features
pip install lean-ai-serve[gpu,ldap,vault,compression,training,tracing]
```

### From source (development)

```bash
git clone https://github.com/your-org/lean-ai-serve.git
cd lean-ai-serve
pip install -e ".[dev,gpu]"
```

### Optional dependency groups

| Extra | What it enables |
|-------|----------------|
| `gpu` | GPU metrics and monitoring via nvidia-ml-py |
| `ldap` | LDAP/Active Directory authentication |
| `vault` | HashiCorp Vault encryption key management |
| `compression` | LLMlingua2 context compression |
| `training` | Dataset utilities for fine-tuning (pandas) |
| `tracing` | OpenTelemetry distributed tracing |
| `dev` | pytest, ruff, and other dev tools |

## Pre-Flight Check

Validate your environment before starting:

```bash
lean-ai-serve check --config config.yaml
```

Example output:

```
  ✓ Config loaded successfully
  ✓ GPUs detected: 2
  ✓ Python available in PATH
  ✓ nvidia-ml-py available (GPU monitoring)
  ⚠ Context compression enabled but llmlingua not installed

All checks passed
```

## Minimal Configuration

Create a `config.yaml` with just the essentials:

```yaml
server:
  host: "0.0.0.0"
  port: 8420

security:
  mode: "api_key"

models:
  my-model:
    source: "Qwen/Qwen2.5-7B-Instruct"
    gpu: [0]
    autoload: true
```

See [config.example.yaml](../config.example.yaml) for the full configuration reference with all options commented, or [docs/configuration.md](configuration.md) for detailed explanations.

## Create an API Key

Before starting the server, create an API key for authentication:

```bash
lean-ai-serve keys create --name "dev-key" --role admin
```

Output:

```
API Key Created
  Name:  dev-key
  Role:  admin
  ID:    a1b2c3d4-...
  Key:   las-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

Save this key — it cannot be retrieved later.
```

Save the key value (`las-...`) somewhere secure. You'll use it in the `Authorization` header for all API requests.

## Pull a Model

Download a model from HuggingFace Hub:

```bash
lean-ai-serve pull Qwen/Qwen2.5-7B-Instruct --name my-model
```

This downloads the model weights to `~/.cache/lean-ai-serve/models/`. For gated models, configure your HuggingFace token in `config.yaml`:

```yaml
cache:
  huggingface_token: "ENV[HF_TOKEN]"
```

Then set the environment variable:

```bash
export HF_TOKEN="hf_..."
```

## Start the Server

```bash
lean-ai-serve start --config config.yaml
```

The server will:

1. Load configuration
2. Connect to the SQLite database
3. Initialize authentication, audit logging, and metrics
4. Start background tasks (GPU monitoring, cleanup, alerts)
5. Autoload models with `autoload: true`
6. Listen on `http://0.0.0.0:8420`

If a model has `autoload: true`, it will be loaded into vLLM automatically. Otherwise, load it manually:

```bash
lean-ai-serve load my-model
```

## Make Your First Request

### Chat completion

```bash
curl http://localhost:8420/v1/chat/completions \
  -H "Authorization: Bearer las-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "my-model",
    "messages": [
      {"role": "system", "content": "You are a helpful assistant."},
      {"role": "user", "content": "What is the capital of France?"}
    ],
    "max_tokens": 100,
    "temperature": 0.7
  }'
```

### Streaming

```bash
curl http://localhost:8420/v1/chat/completions \
  -H "Authorization: Bearer las-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "my-model",
    "messages": [{"role": "user", "content": "Write a haiku about code"}],
    "stream": true
  }'
```

### Using the OpenAI Python SDK

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8420/v1",
    api_key="las-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
)

response = client.chat.completions.create(
    model="my-model",
    messages=[{"role": "user", "content": "Hello!"}],
    max_tokens=256,
)

print(response.choices[0].message.content)
```

## Check Status

### List models

```bash
lean-ai-serve models
```

```
                    Models
┌──────────┬──────────────────────┬────────┬─────┬──────┬──────┬──────┐
│ Name     │ Source               │ State  │ GPU │ Port │ Task │ LoRA │
├──────────┼──────────────────────┼────────┼─────┼──────┼──────┼──────┤
│ my-model │ Qwen/Qwen2.5-7B-... │ loaded │ 0   │ 8001 │ chat │ no   │
└──────────┴──────────────────────┴────────┴─────┴──────┴──────┴──────┘
```

### GPU status

```bash
lean-ai-serve status
```

```
                           GPUs
┌───────┬───────────────────┬─────────────────────┬─────────────┬──────┬──────────┐
│ Index │ Name              │ Memory (Used/Total) │ Utilization │ Temp │ Model    │
├───────┼───────────────────┼─────────────────────┼─────────────┼──────┼──────────┤
│ 0     │ NVIDIA RTX 4090   │ 14201 / 24564 MB    │ 45%         │ 62C  │ my-model │
│ 1     │ NVIDIA RTX 4090   │ 512 / 24564 MB      │ 0%          │ 38C  │ -        │
└───────┴───────────────────┴─────────────────────┴─────────────┴──────┴──────────┘
```

### Health check

```bash
curl http://localhost:8420/health
```

```json
{
  "status": "ok",
  "version": "0.1.0",
  "models_loaded": 1,
  "ready": true,
  "checks": {
    "db": "ok",
    "gpu": "ok",
    "metrics": "ok",
    "scheduler": "ok"
  }
}
```

## Next Steps

- [Configuration](configuration.md) — Full config reference with all options
- [Authentication](authentication.md) — Set up LDAP, OIDC, or manage API keys
- [Model Management](model-management.md) — Multi-GPU, sleep/wake, speculative decoding
- [API Reference](api-reference.md) — Complete HTTP API with examples
- [CLI Reference](cli-reference.md) — All CLI commands
- [Training Guide](training-guide.md) — Fine-tune models with LoRA
- [Deployment](deployment.md) — Production setup with TLS, systemd, Docker
