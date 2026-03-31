"""OpenTelemetry integration — optional, fully graceful when not installed."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from lean_ai_serve.config import TracingConfig

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# Module-level flag indicating whether OTEL is available
_otel_available = False
_tracer = None


class _NoOpSpan:
    """No-op span returned when OTEL is not available."""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def set_attribute(self, key: str, value: Any) -> None:
        pass

    def add_event(self, name: str, attributes: dict | None = None) -> None:
        pass


class _NoOpTracer:
    """No-op tracer returned when OTEL is not available."""

    def start_as_current_span(self, name: str, **kwargs) -> _NoOpSpan:
        return _NoOpSpan()


def setup_tracing(config: TracingConfig) -> bool:
    """Configure OpenTelemetry tracing if enabled and dependencies are installed.

    Returns True if tracing was successfully enabled, False otherwise.
    All imports are lazy — if opentelemetry packages are not installed,
    this logs a warning and returns False.
    """
    global _otel_available, _tracer

    if not config.enabled:
        logger.debug("Tracing is disabled in config")
        return False

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter as GRPCExporter,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        logger.warning(
            "OpenTelemetry packages not installed. "
            "Install with: pip install lean-ai-serve[tracing]"
        )
        return False

    try:
        # Resource identifies this service
        resource = Resource.create({"service.name": config.service_name})
        provider = TracerProvider(resource=resource)

        # Configure exporter
        if config.endpoint:
            if config.protocol == "http":
                try:
                    from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                        OTLPSpanExporter as HTTPExporter,
                    )

                    exporter = HTTPExporter(endpoint=config.endpoint)
                except ImportError:
                    logger.warning("HTTP OTLP exporter not available, falling back to gRPC")
                    exporter = GRPCExporter(endpoint=config.endpoint)
            else:
                exporter = GRPCExporter(endpoint=config.endpoint)

            provider.add_span_processor(BatchSpanProcessor(exporter))

        trace.set_tracer_provider(provider)
        _tracer = trace.get_tracer(config.service_name)
        _otel_available = True

        logger.info(
            "OpenTelemetry tracing enabled (endpoint=%s, protocol=%s)",
            config.endpoint or "none",
            config.protocol,
        )
        return True

    except Exception:
        logger.exception("Failed to initialize OpenTelemetry tracing")
        return False


def instrument_app(app) -> None:
    """Instrument a FastAPI app with OpenTelemetry if available."""
    if not _otel_available:
        return
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app)
        logger.info("FastAPI instrumented with OpenTelemetry")
    except ImportError:
        pass
    except Exception:
        logger.exception("Failed to instrument FastAPI with OpenTelemetry")


def get_tracer(name: str = "lean-ai-serve") -> Any:
    """Get a tracer instance. Returns a no-op tracer if OTEL is not available."""
    if _otel_available and _tracer:
        return _tracer
    return _NoOpTracer()


def trace_inference(
    model: str, prompt_tokens: int, completion_tokens: int
) -> None:
    """Add inference span attributes to the current span if OTEL is active."""
    if not _otel_available:
        return
    try:
        from opentelemetry import trace

        span = trace.get_current_span()
        if span and span.is_recording():
            span.set_attribute("inference.model", model)
            span.set_attribute("inference.prompt_tokens", prompt_tokens)
            span.set_attribute("inference.completion_tokens", completion_tokens)
            span.set_attribute("inference.total_tokens", prompt_tokens + completion_tokens)
    except Exception:
        pass  # Never fail inference due to tracing
