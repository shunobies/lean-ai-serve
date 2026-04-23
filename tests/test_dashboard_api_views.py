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


# ---------------------------------------------------------------------------
# Workspace (lean_ai ingestion) partials — Item 1
# ---------------------------------------------------------------------------


def _make_workspace_info(workspace_id: str = "ws-abc", **overrides):
    from datetime import UTC, datetime

    from lean_ai_serve.training.schemas import WorkspaceInfo

    defaults = {
        "workspace_id": workspace_id,
        "display_name": "alice-workstation",
        "backend_url": "http://fake:8422",
        "repo_root": "/tmp/ws-abc",
        "registered_by": "admin",
        "registered_at": datetime.now(UTC),
        "enabled": True,
        "last_polled_at": None,
        "last_error": None,
        "ingest": [],
    }
    defaults.update(overrides)
    return WorkspaceInfo(**defaults)


def _install_ingestor(app, **methods):
    """Wire a fully-mocked ingestor onto app.state. Each kwarg is a coroutine."""
    ingestor = AsyncMock()
    for name, value in methods.items():
        setattr(ingestor, name, value)
    app.state.lean_ai_ingestor = ingestor
    return ingestor


class TestWorkspacePartials:
    def test_workspace_list_partial_requires_auth(self, client):
        resp = client.get("/dashboard/api/partials/workspace-list")
        assert resp.status_code == 401

    def test_workspace_list_partial_empty_when_ingestion_disabled(
        self, client, auth_headers,
    ):
        """When app.state has no ingestor, return an empty body (not a 500)."""
        resp = client.get(
            "/dashboard/api/partials/workspace-list",
            cookies=auth_headers["cookies"],
        )
        assert resp.status_code == 200
        assert resp.text.strip() == ""

    def test_workspace_list_partial_renders_rows(
        self, app, client, auth_headers,
    ):
        info = _make_workspace_info()
        _install_ingestor(
            app, list_workspaces=AsyncMock(return_value=[info]),
        )
        resp = client.get(
            "/dashboard/api/partials/workspace-list",
            cookies=auth_headers["cookies"],
        )
        assert resp.status_code == 200
        assert 'id="workspace-row-ws-abc"' in resp.text
        assert "alice-workstation" in resp.text

    def test_register_requires_csrf(self, client, auth_headers):
        resp = client.post(
            "/dashboard/api/partials/workspaces",
            cookies=auth_headers["cookies"],
            data={"display_name": "x"},
        )
        assert resp.status_code == 403

    def test_register_requires_auth(self, client):
        resp = client.post(
            "/dashboard/api/partials/workspaces",
            data={"display_name": "x"},
        )
        assert resp.status_code == 401

    def test_register_returns_row_fragment_on_success(
        self, app, client, auth_headers,
    ):
        info = _make_workspace_info()
        _install_ingestor(
            app, register_workspace=AsyncMock(return_value=info),
        )
        resp = client.post(
            "/dashboard/api/partials/workspaces",
            data={
                "display_name": "alice-workstation",
                "backend_url": "http://fake:8422",
                "repo_root": "/tmp/ws-abc",
                "export_key": "test-key",
                "workspace_id": "",
            },
            cookies=auth_headers["cookies"],
            headers=auth_headers["headers"],
        )
        assert resp.status_code == 200
        assert 'id="workspace-row-ws-abc"' in resp.text
        assert "alice-workstation" in resp.text

    def test_register_surfaces_ingestor_error_inline(
        self, app, client, auth_headers,
    ):
        from lean_ai_serve.training.lean_ai_ingest import IngestError

        _install_ingestor(
            app, register_workspace=AsyncMock(
                side_effect=IngestError("Export key rejected (401)"),
            ),
        )
        resp = client.post(
            "/dashboard/api/partials/workspaces",
            data={
                "display_name": "x", "backend_url": "http://fake",
                "repo_root": "/tmp/ws-abc", "export_key": "wrong",
            },
            cookies=auth_headers["cookies"],
            headers=auth_headers["headers"],
        )
        assert resp.status_code == 400
        assert "Export key rejected" in resp.text

    def test_poll_returns_updated_row(self, app, client, auth_headers):
        from datetime import UTC, datetime

        info = _make_workspace_info(last_polled_at=datetime.now(UTC))
        _install_ingestor(
            app,
            poll_workspace=AsyncMock(return_value=None),
            get_workspace=AsyncMock(return_value=info),
        )
        resp = client.post(
            "/dashboard/api/partials/workspaces/ws-abc/poll",
            cookies=auth_headers["cookies"],
            headers=auth_headers["headers"],
        )
        assert resp.status_code == 200
        assert 'id="workspace-row-ws-abc"' in resp.text

    def test_poll_requires_csrf(self, client, auth_headers):
        resp = client.post(
            "/dashboard/api/partials/workspaces/ws-abc/poll",
            cookies=auth_headers["cookies"],
        )
        assert resp.status_code == 403

    def test_enable_returns_row_or_404(self, app, client, auth_headers):
        info = _make_workspace_info(enabled=True)
        _install_ingestor(
            app, enable_workspace=AsyncMock(return_value=info),
        )
        resp = client.post(
            "/dashboard/api/partials/workspaces/ws-abc/enable",
            cookies=auth_headers["cookies"],
            headers=auth_headers["headers"],
        )
        assert resp.status_code == 200
        assert "Enabled" in resp.text

    def test_enable_missing_workspace_404(self, app, client, auth_headers):
        _install_ingestor(
            app, enable_workspace=AsyncMock(return_value=None),
        )
        resp = client.post(
            "/dashboard/api/partials/workspaces/nope/enable",
            cookies=auth_headers["cookies"],
            headers=auth_headers["headers"],
        )
        assert resp.status_code == 404

    def test_disable_returns_disabled_row(self, app, client, auth_headers):
        disabled = _make_workspace_info(enabled=False)
        _install_ingestor(
            app,
            delete_workspace=AsyncMock(return_value=True),
            get_workspace=AsyncMock(return_value=disabled),
        )
        resp = client.post(
            "/dashboard/api/partials/workspaces/ws-abc/disable",
            cookies=auth_headers["cookies"],
            headers=auth_headers["headers"],
        )
        assert resp.status_code == 200
        assert "Disabled" in resp.text
        # Disabled row should offer the Re-enable action.
        assert "Re-enable" in resp.text

    def test_purge_data_returns_row_and_hx_trigger(
        self, app, client, auth_headers,
    ):
        from lean_ai_serve.training.schemas import PurgeResult

        result = PurgeResult(
            workspace_id="ws-abc",
            datasets_cleared=["lean_ai:ws-abc:dpo:plan_rejection"],
            rows_purged=42,
        )
        info = _make_workspace_info()
        _install_ingestor(
            app,
            purge_workspace_data=AsyncMock(return_value=result),
            get_workspace=AsyncMock(return_value=info),
        )
        resp = client.request(
            "DELETE",
            "/dashboard/api/partials/workspaces/ws-abc/data",
            cookies=auth_headers["cookies"],
            headers=auth_headers["headers"],
        )
        assert resp.status_code == 200
        assert "HX-Trigger" in resp.headers
        assert "rows_purged" in resp.headers["HX-Trigger"]
        assert "42" in resp.headers["HX-Trigger"]

    def test_hard_delete_returns_empty_for_row_removal(
        self, app, client, auth_headers,
    ):
        _install_ingestor(
            app, delete_workspace=AsyncMock(return_value=True),
        )
        resp = client.request(
            "DELETE",
            "/dashboard/api/partials/workspaces/ws-abc?hard=true",
            cookies=auth_headers["cookies"],
            headers=auth_headers["headers"],
        )
        assert resp.status_code == 200
        assert resp.text == ""

    def test_hard_delete_missing_workspace_404(
        self, app, client, auth_headers,
    ):
        _install_ingestor(
            app, delete_workspace=AsyncMock(return_value=False),
        )
        resp = client.request(
            "DELETE",
            "/dashboard/api/partials/workspaces/nope?hard=true",
            cookies=auth_headers["cookies"],
            headers=auth_headers["headers"],
        )
        assert resp.status_code == 404

    def test_register_without_manage_permission_forbidden(
        self, app, client,
    ):
        """A user without workspace:manage gets 403 on register."""
        from lean_ai_serve.dashboard.dependencies import generate_csrf_token
        from lean_ai_serve.security.auth import decode_jwt, issue_jwt

        token, _jti, _exp = issue_jwt(
            "viewer", "Viewer", ["viewer"], ["training:read"],
        )
        payload = decode_jwt(token)
        csrf = generate_csrf_token(payload["jti"])

        _install_ingestor(app)
        resp = client.post(
            "/dashboard/api/partials/workspaces",
            data={"display_name": "x"},
            cookies={"las_session": token},
            headers={"X-CSRF-Token": csrf},
        )
        assert resp.status_code == 403
