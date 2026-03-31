"""vLLM subprocess manager — start, stop, health check, sleep."""

from __future__ import annotations

import asyncio
import logging
import signal
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import psutil

from lean_ai_serve.config import ModelConfig, get_settings
from lean_ai_serve.utils.gpu import get_free_port

logger = logging.getLogger(__name__)

# How long to wait for vLLM to become healthy before giving up
HEALTH_TIMEOUT_SECONDS = 600
HEALTH_POLL_INTERVAL = 3.0
STOP_TIMEOUT_SECONDS = 30


@dataclass
class ProcessInfo:
    """Tracks a running vLLM subprocess."""

    name: str
    port: int
    pid: int
    process: asyncio.subprocess.Process
    config: ModelConfig
    model_path: str
    healthy: bool = False
    _health_task: asyncio.Task | None = field(default=None, repr=False)


class ProcessManager:
    """Manages vLLM subprocesses — one per model."""

    def __init__(self) -> None:
        self._processes: dict[str, ProcessInfo] = {}
        self._http = httpx.AsyncClient(timeout=10.0)

    async def close(self) -> None:
        """Stop all managed processes and clean up."""
        names = list(self._processes.keys())
        for name in names:
            await self.stop(name)
        await self._http.aclose()

    def get_info(self, name: str) -> ProcessInfo | None:
        return self._processes.get(name)

    def get_port(self, name: str) -> int | None:
        info = self._processes.get(name)
        return info.port if info and info.healthy else None

    @property
    def running_models(self) -> list[str]:
        return [n for n, p in self._processes.items() if p.healthy]

    async def start(
        self,
        name: str,
        config: ModelConfig,
        model_path: str | Path,
    ) -> ProcessInfo:
        """Start a vLLM process for the given model.

        Returns ProcessInfo once the process is spawned (health checked async).
        Validates the model config before launching.
        """
        if name in self._processes:
            existing = self._processes[name]
            if existing.process.returncode is None:
                logger.warning("Model %s already running (pid=%d)", name, existing.pid)
                return existing

        # Pre-start validation
        from lean_ai_serve.engine.validators import validate_model_config

        validate_model_config(config)

        port = get_free_port()
        cmd = self._build_command(name, config, str(model_path), port)

        logger.info("Starting vLLM for '%s' on port %d: %s", name, port, " ".join(cmd))

        # Scope GPU visibility to the assigned GPUs
        import os

        env = os.environ.copy()
        env["CUDA_VISIBLE_DEVICES"] = ",".join(str(g) for g in config.gpu)

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        info = ProcessInfo(
            name=name,
            port=port,
            pid=process.pid,
            process=process,
            config=config,
            model_path=str(model_path),
        )
        self._processes[name] = info

        # Start background health check
        info._health_task = asyncio.create_task(self._wait_for_health(info))

        return info

    async def stop(self, name: str) -> bool:
        """Stop a running vLLM process."""
        info = self._processes.pop(name, None)
        if info is None:
            return False

        if info._health_task and not info._health_task.done():
            info._health_task.cancel()

        if info.process.returncode is not None:
            logger.info("Process for '%s' already exited", name)
            return True

        logger.info("Stopping vLLM for '%s' (pid=%d)", name, info.pid)

        # Graceful shutdown
        try:
            info.process.send_signal(signal.SIGTERM)
            await asyncio.wait_for(info.process.wait(), timeout=STOP_TIMEOUT_SECONDS)
        except TimeoutError:
            logger.warning("SIGTERM timeout for '%s' — sending SIGKILL", name)
            info.process.kill()
            await info.process.wait()

        # Also kill any child processes (vLLM may spawn workers)
        try:
            parent = psutil.Process(info.pid)
            for child in parent.children(recursive=True):
                child.kill()
        except psutil.NoSuchProcess:
            pass

        logger.info("Stopped vLLM for '%s'", name)
        return True

    async def health_check(self, name: str) -> bool:
        """Check if a model's vLLM process is healthy."""
        info = self._processes.get(name)
        if info is None:
            return False

        if info.process.returncode is not None:
            return False

        try:
            resp = await self._http.get(f"http://127.0.0.1:{info.port}/health")
            return resp.status_code == 200
        except httpx.RequestError:
            return False

    async def _wait_for_health(self, info: ProcessInfo) -> None:
        """Poll vLLM health endpoint until ready or timeout."""
        elapsed = 0.0
        while elapsed < HEALTH_TIMEOUT_SECONDS:
            # Check if process died
            if info.process.returncode is not None:
                stderr = b""
                if info.process.stderr:
                    stderr = await info.process.stderr.read()
                logger.error(
                    "vLLM for '%s' exited with code %d: %s",
                    info.name,
                    info.process.returncode,
                    stderr.decode(errors="replace")[-500:],
                )
                return

            try:
                resp = await self._http.get(f"http://127.0.0.1:{info.port}/health")
                if resp.status_code == 200:
                    info.healthy = True
                    logger.info(
                        "vLLM for '%s' is healthy (port=%d, pid=%d)",
                        info.name,
                        info.port,
                        info.pid,
                    )
                    return
            except httpx.RequestError:
                pass

            await asyncio.sleep(HEALTH_POLL_INTERVAL)
            elapsed += HEALTH_POLL_INTERVAL

        logger.error("vLLM health timeout for '%s' after %ds", info.name, HEALTH_TIMEOUT_SECONDS)

    def _build_command(
        self, name: str, config: ModelConfig, model_path: str, port: int
    ) -> list[str]:
        """Build the vLLM serve command."""
        settings = get_settings()

        cmd = [
            "python", "-m", "vllm.entrypoints.openai.api_server",
            "--model", model_path,
            "--served-model-name", name,
            "--host", "127.0.0.1",
            "--port", str(port),
            "--dtype", config.dtype,
            "--gpu-memory-utilization",
            str(config.gpu_memory_utilization or settings.defaults.gpu_memory_utilization),
            "--guided-decoding-backend", config.guided_decoding_backend,
        ]

        # Tensor parallelism
        if config.tensor_parallel_size > 1:
            cmd.extend(["--tensor-parallel-size", str(config.tensor_parallel_size)])

        # Pipeline parallelism
        if config.pipeline_parallel_size > 1:
            cmd.extend(["--pipeline-parallel-size", str(config.pipeline_parallel_size)])

        # Max model length
        max_len = config.max_model_len or config.context.max_model_len
        if max_len:
            cmd.extend(["--max-model-len", str(max_len)])

        # Quantization
        if config.quantization:
            cmd.extend(["--quantization", config.quantization])

        # Tool call parser
        if config.tool_call_parser:
            cmd.extend(["--tool-call-parser", config.tool_call_parser])
            cmd.append("--enable-auto-tool-choice")

        # Reasoning parser
        if config.reasoning_parser:
            cmd.extend(["--reasoning-parser", config.reasoning_parser])

        # LoRA support
        if config.enable_lora:
            cmd.append("--enable-lora")
            cmd.extend(["--max-loras", str(config.max_loras)])
            cmd.extend(["--max-lora-rank", str(config.max_lora_rank)])

        # KV cache dtype
        if config.kv_cache.dtype != "auto":
            cmd.extend(["--kv-cache-dtype", config.kv_cache.dtype])

        # KV cache scale calculation (for FP8 quantized caches)
        if config.kv_cache.calculate_scales:
            cmd.append("--calculate-kv-scales")

        # Prefix caching
        if config.context.enable_prefix_caching:
            cmd.append("--enable-prefix-caching")

        # CPU offload
        if config.context.cpu_offload_gb > 0:
            cmd.extend(["--cpu-offload-gb", str(config.context.cpu_offload_gb)])

        # Swap space
        if config.context.swap_space:
            cmd.extend(["--swap-space", str(config.context.swap_space)])

        # Chunked prefill batch tokens
        if config.context.max_num_batched_tokens:
            cmd.extend(
                ["--max-num-batched-tokens", str(config.context.max_num_batched_tokens)]
            )

        # RoPE scaling
        if config.context.rope_scaling:
            import json
            cmd.extend(["--rope-scaling", json.dumps(config.context.rope_scaling)])

        if config.context.rope_theta:
            cmd.extend(["--rope-theta", str(config.context.rope_theta)])

        # Speculative decoding
        if config.speculative.enabled:
            if config.speculative.strategy == "draft" and config.speculative.draft_model:
                cmd.extend(["--speculative-model", config.speculative.draft_model])
                cmd.extend(
                    ["--num-speculative-tokens", str(config.speculative.num_tokens)]
                )
                if config.speculative.draft_tensor_parallel_size:
                    cmd.extend(
                        [
                            "--speculative-draft-tensor-parallel-size",
                            str(config.speculative.draft_tensor_parallel_size),
                        ]
                    )
            elif config.speculative.strategy == "ngram":
                cmd.extend(["--speculative-model", "[ngram]"])
                cmd.extend(
                    ["--num-speculative-tokens", str(config.speculative.num_tokens)]
                )

        # Task (embed, generate, chat)
        if config.task and config.task != "chat":
            cmd.extend(["--task", config.task])

        return cmd
