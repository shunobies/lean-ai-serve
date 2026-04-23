"""Tests for dashboard page routes."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from lean_ai_serve.config import DashboardConfig, Settings, set_settings
from lean_ai_serve.security.auth import issue_jwt


@pytest.fixture(autouse=True)
def _configure_dashboard():
    """Enable dashboard with a fixed JWT secret for tests."""
    settings = Settings(
        security={"mode": "api_key", "jwt_secret": "test-secret-key-for-jwt-signing"},
        dashboard=DashboardConfig(enabled=True, session_secret="test-csrf-secret"),
    )
    set_settings(settings)
    yield
    set_settings(None)


@pytest.fixture()
def app():
    """Create a test app with dashboard enabled."""
    from lean_ai_serve.main import create_app

    test_app = create_app()

    # Mock app state
    db = AsyncMock()
    db.fetchall = AsyncMock(return_value=[])

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
def auth_cookie():
    """Generate a valid session JWT cookie."""
    token, _jti, _exp = issue_jwt("testuser", "Test User", ["admin"], ["*"])
    return {"las_session": token}


class TestLoginPage:
    def test_login_renders(self, client):
        resp = client.get("/dashboard/login", follow_redirects=False)
        assert resp.status_code == 200
        assert "lean-ai-serve" in resp.text
        assert "Sign in" in resp.text

    def test_login_shows_api_key_form(self, client):
        resp = client.get("/dashboard/login")
        assert "api_key" in resp.text

    def test_login_with_invalid_key_redirects(self, client):
        resp = client.post(
            "/dashboard/login",
            data={"api_key": "las-invalid-key"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "error=invalid_key" in resp.headers["location"]


class TestAuthenticatedPages:
    def test_home_requires_auth(self, client):
        resp = client.get("/dashboard/", follow_redirects=False)
        assert resp.status_code == 303
        assert "/login" in resp.headers["location"]

    def test_home_with_auth(self, client, auth_cookie):
        resp = client.get("/dashboard/", cookies=auth_cookie)
        assert resp.status_code == 200
        assert "Dashboard" in resp.text

    def test_models_page(self, client, auth_cookie):
        resp = client.get("/dashboard/models", cookies=auth_cookie)
        assert resp.status_code == 200
        assert "Models" in resp.text

    def test_monitoring_page(self, client, auth_cookie):
        resp = client.get("/dashboard/monitoring", cookies=auth_cookie)
        assert resp.status_code == 200
        assert "Monitoring" in resp.text

    def test_security_page(self, client, auth_cookie):
        resp = client.get("/dashboard/security", cookies=auth_cookie)
        assert resp.status_code == 200
        assert "Security" in resp.text

    def test_settings_page(self, client, auth_cookie):
        resp = client.get("/dashboard/settings", cookies=auth_cookie)
        assert resp.status_code == 200
        assert "Settings" in resp.text

    def test_training_redirects_when_disabled(self, client, auth_cookie):
        resp = client.get(
            "/dashboard/training", cookies=auth_cookie, follow_redirects=False
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/dashboard/"

    def test_training_page_renders_workspaces_tab_when_ingestion_enabled(
        self, app, client, auth_cookie,
    ):
        """The new Workspaces tab must render with an empty-state when no workspaces exist."""
        from lean_ai_serve.config import (
            IngestionConfig,
            TrainingConfig,
            get_settings,
            set_settings,
        )

        # Enable training + ingestion.
        settings = get_settings()
        settings.training = TrainingConfig(enabled=True)
        settings.ingestion = IngestionConfig(enabled=True)
        set_settings(settings)

        # Minimal state for the training page; workspaces list is empty.
        orchestrator = AsyncMock()
        orchestrator.list_jobs = AsyncMock(return_value=[])
        app.state.training_orchestrator = orchestrator
        dm = AsyncMock()
        dm.list_datasets = AsyncMock(return_value=[])
        app.state.dataset_manager = dm
        adapters = AsyncMock()
        adapters.list_adapters = AsyncMock(return_value=[])
        app.state.adapter_registry = adapters
        ingestor = AsyncMock()
        ingestor.list_workspaces = AsyncMock(return_value=[])
        app.state.lean_ai_ingestor = ingestor

        resp = client.get("/dashboard/training", cookies=auth_cookie)
        assert resp.status_code == 200
        assert "Workspaces" in resp.text
        # Empty state text from the partial.
        assert "No workspaces registered yet" in resp.text


class TestLogout:
    def test_logout_clears_cookie(self, client, auth_cookie):
        resp = client.post(
            "/dashboard/logout", cookies=auth_cookie, follow_redirects=False
        )
        assert resp.status_code == 303
        assert "/login" in resp.headers["location"]


class TestStaticFiles:
    def test_css_served(self, client):
        resp = client.get("/static/css/dashboard.css")
        assert resp.status_code == 200
        assert "las-primary" in resp.text

    def test_js_served(self, client):
        resp = client.get("/static/js/dashboard.js")
        assert resp.status_code == 200

    def test_htmx_served(self, client):
        resp = client.get("/static/js/htmx.min.js")
        assert resp.status_code == 200

    def test_pico_css_served(self, client):
        resp = client.get("/static/css/pico.min.css")
        assert resp.status_code == 200
