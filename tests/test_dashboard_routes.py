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

    def test_workspace_detail_renders_with_history_and_datasets(
        self, app, client, auth_cookie,
    ):
        """The drill-down page renders metadata + streams + poll history."""
        from datetime import UTC, datetime

        from lean_ai_serve.config import (
            IngestionConfig,
            TrainingConfig,
            get_settings,
            set_settings,
        )
        from lean_ai_serve.training.schemas import (
            DatasetFormat,
            DatasetInfo,
            PollHistoryEntry,
            WorkspaceInfo,
        )

        settings = get_settings()
        settings.training = TrainingConfig(enabled=True)
        settings.ingestion = IngestionConfig(enabled=True)
        set_settings(settings)

        now = datetime.now(UTC)
        workspace = WorkspaceInfo(
            workspace_id="ws-abc",
            display_name="alice-workstation",
            backend_url="http://fake:8422",
            repo_root="/tmp/ws-abc",
            registered_by="admin",
            registered_at=now,
            enabled=True,
        )
        ds = DatasetInfo(
            name="lean_ai:ws-abc:dpo:plan_rejection",
            path="/tmp/x.jsonl",
            format=DatasetFormat.DPO,
            row_count=42,
            size_bytes=2048,
            uploaded_by="admin",
            created_at=now,
            description="",
        )
        history = [
            PollHistoryEntry(
                started_at=now,
                finished_at=now,
                rows_pulled=5,
                datasets_updated_count=2,
                error=None,
                duration_ms=120,
            ),
            PollHistoryEntry(
                started_at=now,
                finished_at=now,
                rows_pulled=0,
                datasets_updated_count=0,
                error="boom",
                duration_ms=80,
            ),
        ]

        ingestor = AsyncMock()
        ingestor.get_workspace = AsyncMock(return_value=workspace)
        ingestor.list_workspace_datasets = AsyncMock(return_value=[
            {
                "stream_key": "dpo_traces:plan_rejection",
                "dataset": ds,
                "eval_dataset": None,
            },
        ])
        ingestor.get_poll_history = AsyncMock(return_value=history)
        app.state.lean_ai_ingestor = ingestor

        resp = client.get(
            "/dashboard/training/workspaces/ws-abc", cookies=auth_cookie,
        )
        assert resp.status_code == 200
        assert "alice-workstation" in resp.text
        assert "dpo_traces:plan_rejection" in resp.text
        assert "Recent polls" in resp.text
        assert "boom" in resp.text  # error from second history row
        assert "42" in resp.text  # dataset row_count

    def test_workspace_detail_redirects_when_ingestion_disabled(
        self, app, client, auth_cookie,
    ):
        from lean_ai_serve.config import (
            IngestionConfig,
            TrainingConfig,
            get_settings,
            set_settings,
        )

        settings = get_settings()
        settings.training = TrainingConfig(enabled=True)
        settings.ingestion = IngestionConfig(enabled=False)
        set_settings(settings)
        # No ingestor on app.state simulates ingestion=disabled at boot.
        if hasattr(app.state, "lean_ai_ingestor"):
            delattr(app.state, "lean_ai_ingestor")

        resp = client.get(
            "/dashboard/training/workspaces/ws-abc",
            cookies=auth_cookie,
            follow_redirects=False,
        )
        assert resp.status_code == 303

    def test_workspace_detail_redirects_on_unknown_workspace(
        self, app, client, auth_cookie,
    ):
        from lean_ai_serve.config import (
            IngestionConfig,
            TrainingConfig,
            get_settings,
            set_settings,
        )

        settings = get_settings()
        settings.training = TrainingConfig(enabled=True)
        settings.ingestion = IngestionConfig(enabled=True)
        set_settings(settings)

        ingestor = AsyncMock()
        ingestor.get_workspace = AsyncMock(return_value=None)
        app.state.lean_ai_ingestor = ingestor

        resp = client.get(
            "/dashboard/training/workspaces/nope",
            cookies=auth_cookie,
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/dashboard/training"

    def test_ingestion_config_block_renders_with_masked_salt(
        self, app, client, auth_cookie,
    ):
        """Config display must show the salt presence but never its value."""
        from lean_ai_serve.config import (
            IngestionConfig,
            TrainingConfig,
            get_settings,
            set_settings,
        )

        secret = "top-secret-salt-do-not-leak"
        settings = get_settings()
        settings.training = TrainingConfig(enabled=True)
        settings.ingestion = IngestionConfig(
            enabled=True,
            poll_interval_seconds=300,
            holdout_fraction=0.15,
            holdout_salt=secret,
        )
        set_settings(settings)

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
        assert "Ingestion settings" in resp.text
        # Poll interval formatted as "every 5 min" for 300 seconds.
        assert "every 5 min" in resp.text
        # Holdout fraction shown as percent.
        assert "15% routed" in resp.text
        # Salt value itself MUST NOT appear anywhere on the page.
        assert secret not in resp.text

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
