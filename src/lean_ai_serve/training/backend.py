"""Training backend abstraction — ABC + LLaMA-Factory implementation."""

from __future__ import annotations

import abc
import asyncio
import logging
import re
import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from lean_ai_serve.config import Settings
from lean_ai_serve.training.schemas import (
    TrainingProgress,
    TrainingSubmitRequest,
)

logger = logging.getLogger(__name__)


class TrainingBackend(abc.ABC):
    """Abstract base class for training backends."""

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Backend identifier."""

    @abc.abstractmethod
    async def validate_environment(self) -> tuple[bool, str]:
        """Check if the backend is available.

        Returns (ok, message).
        """

    @abc.abstractmethod
    async def build_config(
        self,
        request: TrainingSubmitRequest,
        dataset_path: str,
        model_source: str,
        output_dir: str,
    ) -> dict[str, Any]:
        """Build the backend-specific training configuration.

        Returns a config dict that can be serialized and passed to launch().
        """

    @abc.abstractmethod
    async def launch(
        self,
        config: dict[str, Any],
        output_dir: str,
        gpu_ids: list[int],
    ) -> AsyncIterator[TrainingProgress]:
        """Launch training and stream progress events.

        Yields TrainingProgress events suitable for SSE streaming.
        The implementation must handle subprocess lifecycle.
        """
        yield  # pragma: no cover — abstract async generator

    @abc.abstractmethod
    async def cancel(self, output_dir: str) -> bool:
        """Cancel a running training job.

        Returns True if cancellation was successful.
        """


class LlamaFactoryBackend(TrainingBackend):
    """LLaMA-Factory training backend.

    Wraps the `llamafactory-cli train` command, generating a YAML config
    file and streaming stdout/stderr for progress.
    """

    def __init__(self, settings: Settings) -> None:
        self._output_base = Path(settings.training.output_directory)
        self._processes: dict[str, asyncio.subprocess.Process] = {}

    @property
    def name(self) -> str:
        return "llama-factory"

    async def validate_environment(self) -> tuple[bool, str]:
        """Check if llamafactory-cli is available."""
        cli_path = shutil.which("llamafactory-cli")
        if cli_path is None:
            return False, "llamafactory-cli not found in PATH"
        return True, f"llamafactory-cli found at {cli_path}"

    async def build_config(
        self,
        request: TrainingSubmitRequest,
        dataset_path: str,
        model_source: str,
        output_dir: str,
    ) -> dict[str, Any]:
        """Build LLaMA-Factory YAML config from request parameters."""
        config: dict[str, Any] = {
            # Model
            "model_name_or_path": model_source,
            "stage": "sft",
            "finetuning_type": "lora",
            # Dataset
            "dataset_dir": str(Path(dataset_path).parent),
            "dataset": Path(dataset_path).stem,
            # LoRA
            "lora_rank": request.lora_rank,
            "lora_alpha": request.lora_alpha,
            # Training
            "num_train_epochs": request.num_epochs,
            "per_device_train_batch_size": request.batch_size,
            "gradient_accumulation_steps": request.gradient_accumulation_steps,
            "learning_rate": request.learning_rate,
            "warmup_ratio": request.warmup_ratio,
            "weight_decay": request.weight_decay,
            "max_length": request.max_seq_length,
            # Output
            "output_dir": output_dir,
            "logging_steps": request.logging_steps,
            "save_steps": request.save_steps,
            # Misc
            "do_train": True,
            "bf16": True,
            "report_to": "none",
        }

        if request.lora_target:
            config["lora_target"] = request.lora_target

        # Merge extra args (user can override anything)
        config.update(request.extra_args)

        return config

    async def launch(
        self,
        config: dict[str, Any],
        output_dir: str,
        gpu_ids: list[int],
    ) -> AsyncIterator[TrainingProgress]:
        """Launch llamafactory-cli train and stream progress."""
        import yaml

        # Write config YAML
        config_path = Path(output_dir) / "training_config.yaml"
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        with open(config_path, "w") as f:
            yaml.dump(config, f, default_flow_style=False)

        # Build environment with GPU assignment
        env_vars = {
            "CUDA_VISIBLE_DEVICES": ",".join(str(g) for g in gpu_ids),
        }

        # Merge with current environment
        import os
        full_env = {**os.environ, **env_vars}

        cmd = ["llamafactory-cli", "train", str(config_path)]

        yield TrainingProgress(
            status="running",
            message=f"Starting training with config: {config_path}",
        )

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=full_env,
            )
            self._processes[output_dir] = process

            total_steps = self._estimate_total_steps(config)

            async for event in self._stream_output(process, total_steps):
                yield event

            await process.wait()

            if process.returncode == 0:
                yield TrainingProgress(
                    status="complete",
                    progress_pct=100.0,
                    message="Training completed successfully",
                )
            elif process.returncode == -15:  # SIGTERM
                yield TrainingProgress(
                    status="cancelled",
                    message="Training was cancelled",
                )
            else:
                yield TrainingProgress(
                    status="error",
                    message=f"Training failed with exit code {process.returncode}",
                )

        except Exception as e:
            logger.exception("Training launch failed")
            yield TrainingProgress(
                status="error",
                message=f"Training launch failed: {e}",
            )
        finally:
            self._processes.pop(output_dir, None)

    async def cancel(self, output_dir: str) -> bool:
        """Cancel a running training process."""
        process = self._processes.get(output_dir)
        if process is None or process.returncode is not None:
            return False

        import signal
        process.send_signal(signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), timeout=30)
        except TimeoutError:
            process.kill()
            await process.wait()

        self._processes.pop(output_dir, None)
        return True

    async def _stream_output(
        self,
        process: asyncio.subprocess.Process,
        total_steps: int,
    ) -> AsyncIterator[TrainingProgress]:
        """Parse training output for progress events."""
        assert process.stdout is not None

        # Regex patterns for common training output
        step_pattern = re.compile(
            r"{'loss':\s*([\d.]+).*?'learning_rate':\s*([\d.e-]+).*?'epoch':\s*([\d.]+)"
        )
        step_num_pattern = re.compile(r"\[\s*(\d+)/(\d+)\s*\]")

        async for raw_line in process.stdout:
            line = raw_line.decode(errors="replace").strip()
            if not line:
                continue

            # Try to parse step progress
            step_match = step_num_pattern.search(line)
            metrics_match = step_pattern.search(line)

            if step_match:
                current = int(step_match.group(1))
                total = int(step_match.group(2))
                pct = (current / total * 100) if total > 0 else 0

                event = TrainingProgress(
                    status="step",
                    step=current,
                    total_steps=total,
                    progress_pct=round(pct, 1),
                )

                if metrics_match:
                    event.loss = float(metrics_match.group(1))
                    event.learning_rate = float(metrics_match.group(2))
                    event.epoch = float(metrics_match.group(3))

                yield event

            elif "eval_loss" in line.lower():
                # Try to parse evaluation results
                eval_match = re.search(r"eval_loss['\"]?:\s*([\d.]+)", line)
                if eval_match:
                    yield TrainingProgress(
                        status="eval",
                        eval_loss=float(eval_match.group(1)),
                        message=line[:200],
                    )

    @staticmethod
    def _estimate_total_steps(config: dict[str, Any]) -> int:
        """Estimate total training steps from config (rough)."""
        # This is a rough estimate — actual depends on dataset size
        epochs = config.get("num_train_epochs", 3)
        batch_size = config.get("per_device_train_batch_size", 4)
        grad_accum = config.get("gradient_accumulation_steps", 4)
        # Assume ~1000 samples if we don't know
        effective_batch = batch_size * grad_accum
        return int(1000 / effective_batch * epochs)


def create_backend(settings: Settings) -> TrainingBackend:
    """Factory function — create the configured training backend."""
    backend_name = settings.training.backend

    if backend_name == "llama-factory":
        return LlamaFactoryBackend(settings)
    else:
        raise ValueError(f"Unknown training backend: {backend_name}")
