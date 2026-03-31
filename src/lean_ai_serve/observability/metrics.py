"""Dict-based Prometheus metrics — no external dependency required."""

from __future__ import annotations

import threading
import time
from collections import defaultdict

# Label key type: frozen tuple of (key, value) pairs for use as dict key
Labels = tuple[tuple[str, str], ...]


def _labels_key(**labels: str) -> Labels:
    """Convert keyword labels to a frozen tuple for use as dict key."""
    return tuple(sorted(labels.items()))


def _format_labels(labels: Labels) -> str:
    """Format labels for Prometheus text exposition."""
    if not labels:
        return ""
    parts = [f'{k}="{v}"' for k, v in labels]
    return "{" + ",".join(parts) + "}"


# ---------------------------------------------------------------------------
# Metric types
# ---------------------------------------------------------------------------


class Counter:
    """Monotonically increasing counter with labels."""

    def __init__(self, name: str, help_text: str, label_names: list[str]) -> None:
        self.name = name
        self.help = help_text
        self.label_names = label_names
        self._values: dict[Labels, float] = defaultdict(float)
        self._lock = threading.Lock()

    def inc(self, value: float = 1.0, **labels: str) -> None:
        """Increment the counter."""
        key = _labels_key(**labels)
        with self._lock:
            self._values[key] += value

    def get(self, **labels: str) -> float:
        """Get the current counter value."""
        key = _labels_key(**labels)
        with self._lock:
            return self._values.get(key, 0.0)

    def expose(self) -> str:
        """Render in Prometheus text exposition format."""
        lines = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} counter"]
        with self._lock:
            for labels, value in sorted(self._values.items()):
                lines.append(f"{self.name}{_format_labels(labels)} {value}")
        return "\n".join(lines)


class Histogram:
    """Histogram with configurable buckets."""

    DEFAULT_BUCKETS = (
        0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0,
    )

    def __init__(
        self,
        name: str,
        help_text: str,
        label_names: list[str],
        buckets: tuple[float, ...] | None = None,
    ) -> None:
        self.name = name
        self.help = help_text
        self.label_names = label_names
        self._buckets = buckets or self.DEFAULT_BUCKETS
        # Per-label-set: (bucket_counts, sum, count)
        self._data: dict[Labels, tuple[dict[float, int], float, int]] = {}
        self._lock = threading.Lock()

    def _ensure_key(self, key: Labels) -> tuple[dict[float, int], float, int]:
        if key not in self._data:
            bucket_counts = {b: 0 for b in self._buckets}
            self._data[key] = (bucket_counts, 0.0, 0)
        return self._data[key]

    def observe(self, value: float, **labels: str) -> None:
        """Record an observation (non-cumulative bucket counts)."""
        key = _labels_key(**labels)
        with self._lock:
            buckets, total, count = self._ensure_key(key)
            # Increment only the smallest bucket that fits (expose handles cumulation)
            for b in sorted(self._buckets):
                if value <= b:
                    buckets[b] += 1
                    break
            self._data[key] = (buckets, total + value, count + 1)

    def get_count(self, **labels: str) -> int:
        """Get total observation count for labels."""
        key = _labels_key(**labels)
        with self._lock:
            if key not in self._data:
                return 0
            return self._data[key][2]

    def get_sum(self, **labels: str) -> float:
        """Get sum of observations for labels."""
        key = _labels_key(**labels)
        with self._lock:
            if key not in self._data:
                return 0.0
            return self._data[key][1]

    def expose(self) -> str:
        """Render in Prometheus text exposition format."""
        lines = [
            f"# HELP {self.name} {self.help}",
            f"# TYPE {self.name} histogram",
        ]
        with self._lock:
            for labels, (buckets, total, count) in sorted(self._data.items()):
                fmt = _format_labels(labels)
                cumulative = 0
                for b in sorted(self._buckets):
                    cumulative += buckets[b]
                    le_labels = dict(labels)
                    le_labels = list(labels) + [("le", str(b))]
                    lines.append(
                        f"{self.name}_bucket{_format_labels(tuple(le_labels))} {cumulative}"
                    )
                # +Inf bucket
                inf_labels = list(labels) + [("le", "+Inf")]
                lines.append(
                    f"{self.name}_bucket{_format_labels(tuple(inf_labels))} {count}"
                )
                lines.append(f"{self.name}_sum{fmt} {total}")
                lines.append(f"{self.name}_count{fmt} {count}")
        return "\n".join(lines)


class Gauge:
    """Value that can go up and down."""

    def __init__(self, name: str, help_text: str, label_names: list[str]) -> None:
        self.name = name
        self.help = help_text
        self.label_names = label_names
        self._values: dict[Labels, float] = defaultdict(float)
        self._lock = threading.Lock()

    def set(self, value: float, **labels: str) -> None:
        """Set the gauge to a specific value."""
        key = _labels_key(**labels)
        with self._lock:
            self._values[key] = value

    def inc(self, value: float = 1.0, **labels: str) -> None:
        """Increment the gauge."""
        key = _labels_key(**labels)
        with self._lock:
            self._values[key] += value

    def dec(self, value: float = 1.0, **labels: str) -> None:
        """Decrement the gauge."""
        key = _labels_key(**labels)
        with self._lock:
            self._values[key] -= value

    def get(self, **labels: str) -> float:
        """Get the current gauge value."""
        key = _labels_key(**labels)
        with self._lock:
            return self._values.get(key, 0.0)

    def expose(self) -> str:
        """Render in Prometheus text exposition format."""
        lines = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} gauge"]
        with self._lock:
            for labels, value in sorted(self._values.items()):
                lines.append(f"{self.name}{_format_labels(labels)} {value}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Central collector
# ---------------------------------------------------------------------------


class MetricsCollector:
    """Central metrics registry with pre-defined application metrics."""

    def __init__(self) -> None:
        # Counters
        self.requests_total = Counter(
            "lean_ai_serve_requests_total",
            "Total HTTP requests",
            ["method", "path", "status"],
        )
        self.inference_tokens_total = Counter(
            "lean_ai_serve_inference_tokens_total",
            "Total inference tokens",
            ["model", "type"],
        )
        self.auth_failures_total = Counter(
            "lean_ai_serve_auth_failures_total",
            "Total authentication failures",
            ["method"],
        )

        # Histograms
        self.request_duration_seconds = Histogram(
            "lean_ai_serve_request_duration_seconds",
            "Request duration in seconds",
            ["method", "path"],
        )
        self.inference_latency_seconds = Histogram(
            "lean_ai_serve_inference_latency_seconds",
            "Inference latency in seconds",
            ["model"],
        )

        # Gauges
        self.models_loaded = Gauge(
            "lean_ai_serve_models_loaded",
            "Number of loaded models",
            [],
        )
        self.gpu_memory_used_bytes = Gauge(
            "lean_ai_serve_gpu_memory_used_bytes",
            "GPU memory used in bytes",
            ["gpu"],
        )
        self.gpu_utilization_pct = Gauge(
            "lean_ai_serve_gpu_utilization_pct",
            "GPU utilization percentage",
            ["gpu"],
        )
        self.training_jobs_active = Gauge(
            "lean_ai_serve_training_jobs_active",
            "Number of active training jobs",
            [],
        )

        self._start_time = time.monotonic()

    def record_request(
        self, method: str, path: str, status: int, duration: float
    ) -> None:
        """Record an HTTP request."""
        self.requests_total.inc(method=method, path=path, status=str(status))
        self.request_duration_seconds.observe(duration, method=method, path=path)

    def record_inference(
        self, model: str, prompt_tokens: int, completion_tokens: int, latency: float
    ) -> None:
        """Record an inference request with token counts."""
        self.inference_tokens_total.inc(prompt_tokens, model=model, type="prompt")
        self.inference_tokens_total.inc(
            completion_tokens, model=model, type="completion"
        )
        self.inference_latency_seconds.observe(latency, model=model)

    def record_gpu_snapshot(self, gpu_info_list: list) -> None:
        """Update GPU gauges from a list of GPUInfo objects."""
        for gpu in gpu_info_list:
            idx = str(gpu.index)
            self.gpu_memory_used_bytes.set(gpu.memory_used, gpu=idx)
            if gpu.memory_total > 0:
                pct = (gpu.memory_used / gpu.memory_total) * 100
                self.gpu_utilization_pct.set(pct, gpu=idx)

    def expose(self) -> str:
        """Render all metrics in Prometheus text exposition format."""
        sections = [
            self.requests_total.expose(),
            self.inference_tokens_total.expose(),
            self.auth_failures_total.expose(),
            self.request_duration_seconds.expose(),
            self.inference_latency_seconds.expose(),
            self.models_loaded.expose(),
            self.gpu_memory_used_bytes.expose(),
            self.gpu_utilization_pct.expose(),
            self.training_jobs_active.expose(),
        ]
        return "\n\n".join(s for s in sections if s) + "\n"

    def summary(self) -> dict:
        """Return a JSON-friendly summary of key metrics."""
        return {
            "uptime_seconds": round(time.monotonic() - self._start_time, 1),
            "total_requests": sum(self.requests_total._values.values()),
            "total_inference_tokens": sum(
                self.inference_tokens_total._values.values()
            ),
            "models_loaded": self.models_loaded.get(),
            "training_jobs_active": self.training_jobs_active.get(),
        }
