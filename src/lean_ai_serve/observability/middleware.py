"""Request metrics middleware — records HTTP request counts and latency."""

from __future__ import annotations

import re
import time

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from lean_ai_serve.observability.metrics import MetricsCollector

# Paths excluded from metrics (avoid metric-recording loops and noisy health probes)
_EXCLUDED_PATHS = {"/health", "/metrics"}

# Patterns to normalize high-cardinality path segments
_ID_PATTERNS = [
    (re.compile(r"/models/[^/]+"), "/models/{name}"),
    (re.compile(r"/jobs/[^/]+"), "/jobs/{id}"),
    (re.compile(r"/datasets/[^/]+"), "/datasets/{id}"),
    (re.compile(r"/adapters/[^/]+"), "/adapters/{id}"),
    (re.compile(r"/keys/[^/]+"), "/keys/{id}"),
]


def _normalize_path(path: str) -> str:
    """Replace dynamic path segments with placeholders for cardinality control."""
    for pattern, replacement in _ID_PATTERNS:
        path = pattern.sub(replacement, path)
    return path


class MetricsMiddleware(BaseHTTPMiddleware):
    """ASGI middleware that records request metrics (count, latency).

    Follows the same BaseHTTPMiddleware pattern as ContentFilterMiddleware.
    """

    def __init__(self, app, metrics: MetricsCollector) -> None:
        super().__init__(app)
        self._metrics = metrics

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        path = request.url.path

        # Skip excluded paths
        if path in _EXCLUDED_PATHS:
            return await call_next(request)

        method = request.method
        start = time.monotonic()

        response = await call_next(request)

        duration = time.monotonic() - start
        normalized = _normalize_path(path)
        self._metrics.record_request(method, normalized, response.status_code, duration)

        return response
