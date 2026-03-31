"""Pre-start validation for model configurations."""

from __future__ import annotations

import logging

from lean_ai_serve.config import ModelConfig

logger = logging.getLogger(__name__)


def validate_gpu_config(config: ModelConfig) -> list[str]:
    """Validate GPU assignments against tensor/pipeline parallel config.

    Returns list of error strings. Empty = valid.
    """
    errors: list[str] = []
    gpu_count = len(config.gpu)

    if config.tensor_parallel_size > gpu_count:
        errors.append(
            f"tensor_parallel_size ({config.tensor_parallel_size}) exceeds "
            f"assigned GPU count ({gpu_count})"
        )

    total_parallel = config.tensor_parallel_size * config.pipeline_parallel_size
    if total_parallel > gpu_count:
        errors.append(
            f"tensor_parallel_size * pipeline_parallel_size ({total_parallel}) "
            f"exceeds assigned GPU count ({gpu_count})"
        )

    return errors


def validate_gpu_existence(config: ModelConfig) -> list[str]:
    """Check that requested GPU indices actually exist on this machine.

    Uses nvidia-ml-py (pynvml). Returns errors if GPUs missing.
    Falls back gracefully if pynvml unavailable.
    """
    errors: list[str] = []
    try:
        import pynvml

        pynvml.nvmlInit()
        try:
            device_count = pynvml.nvmlDeviceGetCount()
            for idx in config.gpu:
                if idx >= device_count:
                    errors.append(
                        f"GPU index {idx} does not exist "
                        f"(system has {device_count} GPU(s))"
                    )
        finally:
            pynvml.nvmlShutdown()
    except ImportError:
        logger.debug("pynvml not available — skipping GPU existence check")
    except Exception as exc:
        logger.debug("GPU existence check failed: %s", exc)

    return errors


def validate_speculative_config(config: ModelConfig) -> list[str]:
    """Validate speculative decoding settings.

    Returns list of error/warning strings.
    """
    errors: list[str] = []
    spec = config.speculative

    if not spec.enabled:
        return errors

    if spec.strategy == "draft" and not spec.draft_model:
        errors.append("Speculative strategy 'draft' requires draft_model to be set")

    if spec.strategy == "eagle":
        errors.append(
            "Speculative strategy 'eagle' is not yet supported in vLLM command building"
        )

    if not 1 <= spec.num_tokens <= 20:
        errors.append(
            f"speculative.num_tokens must be 1-20, got {spec.num_tokens}"
        )

    return errors


def validate_model_config(config: ModelConfig) -> list[str]:
    """Run all validators. Called before ProcessManager.start().

    Returns combined list of errors. Raises ValueError if any found.
    """
    errors: list[str] = []
    errors.extend(validate_gpu_config(config))
    errors.extend(validate_gpu_existence(config))
    errors.extend(validate_speculative_config(config))

    if errors:
        raise ValueError(
            f"Model config validation failed: {'; '.join(errors)}"
        )

    return errors
