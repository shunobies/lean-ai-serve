"""GPU discovery and memory queries via nvidia-ml-py."""

from __future__ import annotations

import logging

from lean_ai_serve.models.schemas import GPUInfo

logger = logging.getLogger(__name__)


def get_gpu_info() -> list[GPUInfo]:
    """Query all NVIDIA GPUs. Returns empty list if nvidia-ml-py unavailable."""
    try:
        import pynvml

        pynvml.nvmlInit()
        count = pynvml.nvmlDeviceGetCount()
        gpus = []
        for i in range(count):
            handle = pynvml.nvmlDeviceGetHandleByIndex(i)
            name = pynvml.nvmlDeviceGetName(handle)
            if isinstance(name, bytes):
                name = name.decode()
            mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
            util = pynvml.nvmlDeviceGetUtilizationRates(handle)
            try:
                temp = pynvml.nvmlDeviceGetTemperature(
                    handle, pynvml.NVML_TEMPERATURE_GPU
                )
            except pynvml.NVMLError:
                temp = None

            gpus.append(
                GPUInfo(
                    index=i,
                    name=name,
                    memory_total_mb=mem.total // (1024 * 1024),
                    memory_used_mb=mem.used // (1024 * 1024),
                    memory_free_mb=mem.free // (1024 * 1024),
                    utilization_pct=float(util.gpu),
                    temperature_c=temp,
                )
            )
        pynvml.nvmlShutdown()
        return gpus
    except ImportError:
        logger.debug("nvidia-ml-py not installed — GPU queries unavailable")
        return []
    except Exception:
        logger.debug("NVML init failed — no NVIDIA GPUs available")
        return []


def get_free_port(start: int = 8430, end: int = 8530) -> int:
    """Find an available port in the given range."""
    import socket

    for port in range(start, end):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"No free ports in range {start}-{end}")
