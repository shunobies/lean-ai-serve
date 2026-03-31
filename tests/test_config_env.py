"""Tests for environment variable config overrides and new config models."""

from __future__ import annotations

import pytest

from lean_ai_serve.config import (
    AlertConfig,
    AlertRuleConfig,
    LoggingConfig,
    MetricsConfig,
    OIDCConfig,
    Settings,
    TracingConfig,
    load_settings,
    set_settings,
)

# ---------------------------------------------------------------------------
# New config model defaults
# ---------------------------------------------------------------------------


def test_metrics_config_defaults():
    """MetricsConfig has correct defaults."""
    mc = MetricsConfig()
    assert mc.enabled is True
    assert mc.gpu_poll_interval == 30


def test_logging_config_defaults():
    """LoggingConfig has correct defaults."""
    lc = LoggingConfig()
    assert lc.json_output is True
    assert lc.level == "INFO"


def test_alert_config_defaults():
    """AlertConfig has correct defaults."""
    ac = AlertConfig()
    assert ac.enabled is True
    assert ac.evaluation_interval == 60
    assert ac.rules == []


def test_alert_rule_config():
    """AlertRuleConfig can be constructed with all fields."""
    rule = AlertRuleConfig(
        name="high_gpu_memory",
        metric="gpu_memory_used_pct",
        condition="gt",
        threshold=90.0,
        severity="warning",
        message="GPU memory above 90%",
    )
    assert rule.name == "high_gpu_memory"
    assert rule.threshold == 90.0


def test_tracing_config_defaults():
    """TracingConfig has correct defaults (disabled by default)."""
    tc = TracingConfig()
    assert tc.enabled is False
    assert tc.endpoint == ""
    assert tc.protocol == "grpc"
    assert tc.service_name == "lean-ai-serve"


def test_oidc_config_extensions():
    """OIDCConfig has new fields for role mapping and JWKS caching."""
    oc = OIDCConfig()
    assert oc.role_mapping == {}
    assert oc.default_role == "user"
    assert oc.jwks_cache_ttl == 3600


def test_oidc_config_with_role_mapping():
    """OIDCConfig accepts custom role_mapping."""
    oc = OIDCConfig(
        issuer_url="https://keycloak.example.com/realms/ai",
        client_id="test",
        audience="test",
        role_mapping={"ai-admin": "admin", "ai-user": "user"},
        default_role="user",
    )
    assert oc.role_mapping["ai-admin"] == "admin"


# ---------------------------------------------------------------------------
# Settings includes new fields
# ---------------------------------------------------------------------------


def test_settings_has_new_fields():
    """Settings includes metrics, logging, alerts, tracing fields."""
    s = Settings()
    assert isinstance(s.metrics, MetricsConfig)
    assert isinstance(s.logging, LoggingConfig)
    assert isinstance(s.alerts, AlertConfig)
    assert isinstance(s.tracing, TracingConfig)


def test_settings_backward_compat():
    """Settings() without any args still works (backward compatibility)."""
    s = Settings()
    assert s.server.port == 8420
    assert s.security.mode == "api_key"
    assert s.audit.enabled is True
    assert s.models == {}


# ---------------------------------------------------------------------------
# Environment variable overrides
# ---------------------------------------------------------------------------


def test_env_overrides_simple(monkeypatch):
    """Environment variable overrides a simple top-level nested value."""
    monkeypatch.setenv("LEAN_AI_SERVE_SERVER__PORT", "9000")
    s = Settings()
    assert s.server.port == 9000


def test_env_overrides_security_mode(monkeypatch):
    """Nested env var overrides security mode."""
    monkeypatch.setenv("LEAN_AI_SERVE_SECURITY__MODE", "oidc+api_key")
    s = Settings()
    assert s.security.mode == "oidc+api_key"


def test_env_overrides_metrics_enabled(monkeypatch):
    """Env var can disable metrics."""
    monkeypatch.setenv("LEAN_AI_SERVE_METRICS__ENABLED", "false")
    s = Settings()
    assert s.metrics.enabled is False


def test_env_overrides_logging_level(monkeypatch):
    """Env var can change logging level."""
    monkeypatch.setenv("LEAN_AI_SERVE_LOGGING__LEVEL", "DEBUG")
    s = Settings()
    assert s.logging.level == "DEBUG"


def test_env_overrides_tracing_enabled(monkeypatch):
    """Env var can enable tracing."""
    monkeypatch.setenv("LEAN_AI_SERVE_TRACING__ENABLED", "true")
    monkeypatch.setenv("LEAN_AI_SERVE_TRACING__ENDPOINT", "http://localhost:4317")
    s = Settings()
    assert s.tracing.enabled is True
    assert s.tracing.endpoint == "http://localhost:4317"


def test_env_precedence_over_yaml(monkeypatch, tmp_path):
    """Env var takes precedence over YAML file value."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text("server:\n  port: 8420\n")
    monkeypatch.setenv("LEAN_AI_SERVE_SERVER__PORT", "9999")
    s = load_settings(str(config_file))
    assert s.server.port == 9999


def test_yaml_values_loaded(tmp_path):
    """YAML values are loaded when no env overrides."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "server:\n  port: 7777\nmetrics:\n  gpu_poll_interval: 15\n"
    )
    s = load_settings(str(config_file))
    assert s.server.port == 7777
    assert s.metrics.gpu_poll_interval == 15


def test_defaults_preserved_no_yaml_no_env():
    """Without YAML or env, defaults are preserved."""
    s = Settings()
    assert s.server.host == "0.0.0.0"
    assert s.server.port == 8420
    assert s.metrics.enabled is True
    assert s.logging.json_output is True
    assert s.tracing.enabled is False


def test_load_settings_missing_file(tmp_path):
    """load_settings with nonexistent file falls back to defaults."""
    s = load_settings(str(tmp_path / "nonexistent.yaml"))
    assert s.server.port == 8420


@pytest.fixture(autouse=True)
def _clear_settings():
    """Reset the global settings singleton between tests."""
    set_settings(None)
    yield
    set_settings(None)
