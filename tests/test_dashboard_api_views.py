"""Tests for dashboard HTMX API endpoints."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from lean_ai_serve.config import DashboardConfig, Settings, set_settings
from lean_ai_serve.dashboard.dependencies import generate_csrf_token
from lean_ai_serve.security.auth import decode_jwt, issue_jwt


@pytest.fixture(autouse=True)
def _configure():
    settings = Settings(
        security={"mode": "api_key", "jwt_secret": "test-secret-key-for-jwt-signing"},
        dashboard=DashboardConfig(enabled=True, session_secret="test-csrf-secret"),
    )
    set_settings(settings)
    yield
    set_settings(None)


@pytest.fixture()
def app():
    from lean_ai_serve.main import create_app

    test_app = create_app()

    db = AsyncMock()
    db.fetchall = AsyncMock(return_value=[])
    db.execute = AsyncMock()
    db.commit = AsyncMock()

    registry = AsyncMock()
    registry.list_models = AsyncMock(return_value=[])
    registry.get_model = AsyncMock(return_value=None)

    test_app.state.db = db
    test_app.state.registry = registry
    test_app.state.start_time = 0.0

    return test_app


@pytest.fixture()
def client(app):
    return TestClient(app)


@pytest.fixture()
def auth_headers():
    """Generate auth cookie and CSRF token header."""
    token, _jti, _exp = issue_jwt("testuser", "Test User", ["admin"], ["*"])
    payload = decode_jwt(token)
    csrf = generate_csrf_token(payload["jti"])
    return {"cookies": {"las_session": token}, "headers": {"X-CSRF-Token": csrf}}


class TestModelPartials:
    def test_model_list_requires_auth(self, client):
        resp = client.get("/dashboard/api/partials/model-list")
        assert resp.status_code == 401

    def test_model_list_with_auth(self, client, auth_headers):
        resp = client.get(
            "/dashboard/api/partials/model-list",
            cookies=auth_headers["cookies"],
        )
        assert resp.status_code == 200

    def test_load_requires_csrf(self, client, auth_headers):
        resp = client.post(
            "/dashboard/api/models/test-model/load",
            cookies=auth_headers["cookies"],
            # Missing CSRF header
        )
        assert resp.status_code == 403

    def test_unload_requires_csrf(self, client, auth_headers):
        resp = client.post(
            "/dashboard/api/models/test-model/unload",
            cookies=auth_headers["cookies"],
        )
        assert resp.status_code == 403


class TestMetricsPartials:
    def test_metrics_requires_auth(self, client):
        resp = client.get("/dashboard/api/partials/metrics")
        assert resp.status_code == 401

    def test_metrics_with_auth(self, client, auth_headers):
        resp = client.get(
            "/dashboard/api/partials/metrics",
            cookies=auth_headers["cookies"],
        )
        assert resp.status_code == 200

    def test_alerts_with_auth(self, client, auth_headers):
        resp = client.get(
            "/dashboard/api/partials/alerts",
            cookies=auth_headers["cookies"],
        )
        assert resp.status_code == 200
        assert "No active alerts" in resp.text


class TestAuditPartials:
    def test_audit_requires_auth(self, client):
        resp = client.get("/dashboard/api/partials/audit")
        assert resp.status_code == 401

    def test_audit_with_auth(self, client, auth_headers):
        resp = client.get(
            "/dashboard/api/partials/audit",
            cookies=auth_headers["cookies"],
        )
        assert resp.status_code == 200


class TestKeyManagement:
    def test_create_key_requires_csrf(self, client, auth_headers):
        resp = client.post(
            "/dashboard/api/keys/create",
            data={"name": "test", "role": "user"},
            cookies=auth_headers["cookies"],
        )
        assert resp.status_code == 403

    def test_delete_key_requires_csrf(self, client, auth_headers):
        resp = client.request(
            "DELETE",
            "/dashboard/api/keys/some-id",
            cookies=auth_headers["cookies"],
        )
        assert resp.status_code == 403
