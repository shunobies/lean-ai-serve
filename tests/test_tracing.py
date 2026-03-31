"""Tests for OpenTelemetry tracing module."""

from __future__ import annotations

from unittest.mock import patch

from lean_ai_serve.config import TracingConfig
from lean_ai_serve.observability import tracing


def test_setup_tracing_disabled():
    """setup_tracing returns False when disabled."""
    config = TracingConfig(enabled=False)
    result = tracing.setup_tracing(config)
    assert result is False


def test_setup_tracing_no_otel_packages():
    """setup_tracing returns False when OTEL packages not installed."""
    config = TracingConfig(enabled=True, endpoint="http://localhost:4317")
    with patch.dict("sys.modules", {"opentelemetry": None}):
        # Reset module state
        tracing._otel_available = False
        tracing._tracer = None
        result = tracing.setup_tracing(config)
        assert result is False


def test_get_tracer_returns_noop_when_disabled():
    """get_tracer returns NoOpTracer when OTEL is not available."""
    tracing._otel_available = False
    tracing._tracer = None
    tracer = tracing.get_tracer()
    assert isinstance(tracer, tracing._NoOpTracer)


def test_noop_tracer_works():
    """NoOpTracer and NoOpSpan don't raise errors."""
    tracer = tracing._NoOpTracer()
    span = tracer.start_as_current_span("test")
    with span:
        span.set_attribute("key", "value")
        span.add_event("test_event")


def test_trace_inference_noop_when_disabled():
    """trace_inference is silent when OTEL is disabled."""
    tracing._otel_available = False
    # Should not raise
    tracing.trace_inference("test-model", 100, 50)


def test_instrument_app_noop_when_disabled():
    """instrument_app is silent when OTEL is disabled."""
    tracing._otel_available = False
    # Should not raise
    tracing.instrument_app(None)
