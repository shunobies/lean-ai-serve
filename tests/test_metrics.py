"""Tests for dict-based Prometheus metrics."""

from __future__ import annotations

from lean_ai_serve.observability.metrics import (
    Counter,
    Gauge,
    Histogram,
    MetricsCollector,
)

# ---------------------------------------------------------------------------
# Counter tests
# ---------------------------------------------------------------------------


def test_counter_inc():
    """Increment counter and verify value."""
    c = Counter("test_total", "test", [])
    c.inc()
    c.inc(5.0)
    assert c.get() == 6.0


def test_counter_inc_with_labels():
    """Separate label sets tracked independently."""
    c = Counter("req_total", "test", ["method"])
    c.inc(method="GET")
    c.inc(method="GET")
    c.inc(method="POST")
    assert c.get(method="GET") == 2.0
    assert c.get(method="POST") == 1.0
    assert c.get(method="PUT") == 0.0


def test_counter_expose():
    """Counter Prometheus text format."""
    c = Counter("http_requests_total", "Total requests", ["method"])
    c.inc(3.0, method="GET")
    c.inc(1.0, method="POST")
    text = c.expose()
    assert "# HELP http_requests_total Total requests" in text
    assert "# TYPE http_requests_total counter" in text
    assert 'http_requests_total{method="GET"} 3.0' in text
    assert 'http_requests_total{method="POST"} 1.0' in text


# ---------------------------------------------------------------------------
# Histogram tests
# ---------------------------------------------------------------------------


def test_histogram_observe():
    """Observe values and verify count/sum."""
    h = Histogram("duration", "test", [])
    h.observe(0.1)
    h.observe(0.5)
    h.observe(1.0)
    assert h.get_count() == 3
    assert abs(h.get_sum() - 1.6) < 0.001


def test_histogram_buckets():
    """Observations fall into correct buckets."""
    h = Histogram("latency", "test", [], buckets=(0.1, 0.5, 1.0))
    h.observe(0.05)  # <= 0.1, <= 0.5, <= 1.0
    h.observe(0.3)   # <= 0.5, <= 1.0
    h.observe(0.8)   # <= 1.0
    text = h.expose()
    assert 'le="0.1"} 1' in text
    assert 'le="0.5"} 2' in text
    assert 'le="1.0"} 3' in text
    assert 'le="+Inf"} 3' in text


def test_histogram_with_labels():
    """Histogram tracks per-label observations."""
    h = Histogram("req_dur", "test", ["path"])
    h.observe(0.1, path="/api")
    h.observe(0.2, path="/api")
    h.observe(0.5, path="/health")
    assert h.get_count(path="/api") == 2
    assert h.get_count(path="/health") == 1


def test_histogram_expose():
    """Histogram Prometheus text format includes HELP and TYPE."""
    h = Histogram("test_hist", "A histogram", [])
    h.observe(0.5)
    text = h.expose()
    assert "# HELP test_hist A histogram" in text
    assert "# TYPE test_hist histogram" in text
    assert "test_hist_sum" in text
    assert "test_hist_count" in text


# ---------------------------------------------------------------------------
# Gauge tests
# ---------------------------------------------------------------------------


def test_gauge_set():
    """Set gauge and verify value."""
    g = Gauge("temp", "test", [])
    g.set(42.0)
    assert g.get() == 42.0


def test_gauge_inc_dec():
    """Increment and decrement gauge."""
    g = Gauge("active", "test", [])
    g.inc()
    g.inc()
    g.dec()
    assert g.get() == 1.0


def test_gauge_with_labels():
    """Gauge tracks per-label values."""
    g = Gauge("gpu_mem", "test", ["gpu"])
    g.set(1000.0, gpu="0")
    g.set(2000.0, gpu="1")
    assert g.get(gpu="0") == 1000.0
    assert g.get(gpu="1") == 2000.0


def test_gauge_expose():
    """Gauge Prometheus text format."""
    g = Gauge("models_loaded", "Loaded models", [])
    g.set(3.0)
    text = g.expose()
    assert "# HELP models_loaded Loaded models" in text
    assert "# TYPE models_loaded gauge" in text
    assert "models_loaded 3.0" in text


# ---------------------------------------------------------------------------
# MetricsCollector tests
# ---------------------------------------------------------------------------


def test_record_request():
    """record_request updates request counter and duration histogram."""
    mc = MetricsCollector()
    mc.record_request("GET", "/api/health", 200, 0.05)
    mc.record_request("POST", "/v1/chat", 200, 0.5)
    assert mc.requests_total.get(method="GET", path="/api/health", status="200") == 1.0
    assert mc.requests_total.get(method="POST", path="/v1/chat", status="200") == 1.0


def test_record_inference():
    """record_inference updates token counters and latency histogram."""
    mc = MetricsCollector()
    mc.record_inference("qwen3", prompt_tokens=100, completion_tokens=50, latency=0.8)
    assert mc.inference_tokens_total.get(model="qwen3", type="prompt") == 100.0
    assert mc.inference_tokens_total.get(model="qwen3", type="completion") == 50.0
    assert mc.inference_latency_seconds.get_count(model="qwen3") == 1


def test_record_gpu_snapshot():
    """record_gpu_snapshot sets GPU gauges."""

    class FakeGPU:
        def __init__(self, idx, used, total):
            self.index = idx
            self.memory_used = used
            self.memory_total = total

    mc = MetricsCollector()
    gpus = [FakeGPU(0, 4000, 8000), FakeGPU(1, 6000, 8000)]
    mc.record_gpu_snapshot(gpus)
    assert mc.gpu_memory_used_bytes.get(gpu="0") == 4000
    assert mc.gpu_memory_used_bytes.get(gpu="1") == 6000
    assert mc.gpu_utilization_pct.get(gpu="0") == 50.0
    assert mc.gpu_utilization_pct.get(gpu="1") == 75.0


def test_expose_full():
    """expose() renders all metrics in Prometheus format."""
    mc = MetricsCollector()
    mc.record_request("GET", "/health", 200, 0.01)
    mc.models_loaded.set(2)
    text = mc.expose()
    assert "lean_ai_serve_requests_total" in text
    assert "lean_ai_serve_models_loaded" in text
    assert "# HELP" in text
    assert "# TYPE" in text


def test_summary():
    """summary() returns JSON-friendly dict."""
    mc = MetricsCollector()
    mc.record_request("GET", "/health", 200, 0.01)
    mc.models_loaded.set(3)
    s = mc.summary()
    assert s["total_requests"] == 1.0
    assert s["models_loaded"] == 3.0
    assert "uptime_seconds" in s
