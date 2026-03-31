"""Tests for metrics middleware and API endpoints."""

from __future__ import annotations

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from lean_ai_serve.api.metrics import router as metrics_router
from lean_ai_serve.config import Settings, set_settings
from lean_ai_serve.models.schemas import AuthUser
from lean_ai_serve.observability.alerts import AlertEvaluator, AlertRule
from lean_ai_serve.observability.metrics import MetricsCollector
from lean_ai_serve.observability.middleware import MetricsMiddleware, _normalize_path
from lean_ai_serve.security.auth import authenticate


def _make_admin() -> AuthUser:
    return AuthUser(
        user_id="admin",
        display_name="Admin",
        roles=["admin"],
        allowed_models=["*"],
        auth_method="none",
    )


@pytest_asyncio.fixture
async def app():
    """Create a test app with metrics middleware and endpoints."""
    settings = Settings(security={"mode": "none"})
    set_settings(settings)

    metrics = MetricsCollector()

    test_app = FastAPI()
    test_app.include_router(metrics_router)

    # Simple test endpoint
    @test_app.get("/api/test")
    async def test_endpoint():
        return {"status": "ok"}

    # Health endpoint (should be excluded from metrics)
    @test_app.get("/health")
    async def health():
        return {"status": "ok"}

    # Override authenticate to admin
    test_app.dependency_overrides[authenticate] = lambda: _make_admin()

    # Add middleware
    test_app.add_middleware(MetricsMiddleware, metrics=metrics)

    # Inject state
    test_app.state.metrics = metrics

    return test_app


@pytest_asyncio.fixture
async def client(app) -> AsyncClient:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------------
# Middleware tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_counted(client, app):
    """Requests should be counted in metrics."""
    resp = await client.get("/api/test")
    assert resp.status_code == 200

    metrics = app.state.metrics
    assert metrics.requests_total.get(method="GET", path="/api/test", status="200") == 1.0


@pytest.mark.asyncio
async def test_latency_recorded(client, app):
    """Request latency should be recorded."""
    await client.get("/api/test")

    metrics = app.state.metrics
    assert metrics.request_duration_seconds.get_count(method="GET", path="/api/test") == 1
    assert metrics.request_duration_seconds.get_sum(method="GET", path="/api/test") > 0


@pytest.mark.asyncio
async def test_health_excluded(client, app):
    """Health endpoint should be excluded from metrics."""
    await client.get("/health")

    metrics = app.state.metrics
    assert metrics.requests_total.get(method="GET", path="/health", status="200") == 0.0


@pytest.mark.asyncio
async def test_metrics_excluded(client, app):
    """/metrics endpoint should be excluded from metrics."""
    await client.get("/metrics")

    metrics = app.state.metrics
    assert metrics.requests_total.get(method="GET", path="/metrics", status="200") == 0.0


@pytest.mark.asyncio
async def test_status_codes_tracked(client, app):
    """Different status codes should be tracked separately."""
    await client.get("/api/test")
    await client.get("/api/nonexistent")

    metrics = app.state.metrics
    assert metrics.requests_total.get(method="GET", path="/api/test", status="200") == 1.0
    assert metrics.requests_total.get(method="GET", path="/api/nonexistent", status="404") == 1.0


@pytest.mark.asyncio
async def test_multiple_requests_accumulated(client, app):
    """Multiple requests to the same endpoint should accumulate."""
    for _ in range(5):
        await client.get("/api/test")

    metrics = app.state.metrics
    assert metrics.requests_total.get(method="GET", path="/api/test", status="200") == 5.0


# ---------------------------------------------------------------------------
# Path normalization tests
# ---------------------------------------------------------------------------


def test_normalize_path_models():
    assert _normalize_path("/api/models/my-model") == "/api/models/{name}"


def test_normalize_path_jobs():
    assert _normalize_path("/api/training/jobs/abc-123") == "/api/training/jobs/{id}"


def test_normalize_path_no_match():
    assert _normalize_path("/api/test") == "/api/test"


def test_normalize_path_multiple_segments():
    assert _normalize_path("/api/models/my-model/adapters/adapter-1") == (
        "/api/models/{name}/adapters/{id}"
    )


# ---------------------------------------------------------------------------
# Endpoint tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prometheus_metrics_endpoint(client, app):
    """GET /metrics should return Prometheus text format."""
    # Record some data first
    app.state.metrics.record_request("GET", "/api/test", 200, 0.05)

    resp = await client.get("/metrics")
    assert resp.status_code == 200
    text = resp.text
    assert "lean_ai_serve_requests_total" in text
    assert "# TYPE" in text


@pytest.mark.asyncio
async def test_prometheus_metrics_disabled(client, app):
    """GET /metrics returns placeholder when metrics not configured."""
    app.state.metrics = None
    resp = await client.get("/metrics")
    assert resp.status_code == 200
    assert "not enabled" in resp.text


@pytest.mark.asyncio
async def test_metrics_summary_endpoint(client, app):
    """GET /api/metrics/summary should return JSON summary."""
    app.state.metrics.record_request("GET", "/api/test", 200, 0.05)

    resp = await client.get("/api/metrics/summary")
    assert resp.status_code == 200
    body = resp.json()
    assert "uptime_seconds" in body
    assert "total_requests" in body
    assert body["total_requests"] >= 1


@pytest.mark.asyncio
async def test_alerts_endpoint_empty(client, app):
    """GET /api/metrics/alerts should return empty when no evaluator."""
    resp = await client.get("/api/metrics/alerts")
    assert resp.status_code == 200
    body = resp.json()
    assert body["alerts"] == []


@pytest.mark.asyncio
async def test_alerts_endpoint_with_firing(client, app):
    """GET /api/metrics/alerts should return firing alerts."""
    metrics = app.state.metrics
    rule = AlertRule(name="test_alert", metric="models_loaded", condition="lt", threshold=1.0)
    evaluator = AlertEvaluator(metrics, rules=[rule])
    evaluator.evaluate()  # models_loaded defaults to 0, which is < 1 -> fires

    app.state.alert_evaluator = evaluator

    resp = await client.get("/api/metrics/alerts")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    assert body["alerts"][0]["rule_name"] == "test_alert"


@pytest.mark.asyncio
async def test_prometheus_includes_alerts(client, app):
    """GET /metrics should include alert metrics when evaluator is present."""
    metrics = app.state.metrics
    rule = AlertRule(name="prom_test", metric="models_loaded", condition="lt", threshold=1.0)
    evaluator = AlertEvaluator(metrics, rules=[rule])
    evaluator.evaluate()

    app.state.alert_evaluator = evaluator

    resp = await client.get("/metrics")
    assert resp.status_code == 200
    text = resp.text
    assert 'alertname="prom_test"' in text


@pytest.fixture(autouse=True)
def _clear_settings():
    yield
    set_settings(None)
