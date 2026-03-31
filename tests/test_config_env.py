"""Tests for configuration loading, config model defaults, and secret resolution."""

from __future__ import annotations

import os

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
# YAML loading
# ---------------------------------------------------------------------------


def test_yaml_values_loaded(tmp_path):
    """YAML values are loaded correctly."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "server:\n  port: 7777\nmetrics:\n  gpu_poll_interval: 15\n"
    )
    s = load_settings(str(config_file))
    assert s.server.port == 7777
    assert s.metrics.gpu_poll_interval == 15


def test_defaults_preserved_no_yaml():
    """Without YAML, defaults are preserved."""
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


# ---------------------------------------------------------------------------
# Secret resolution via load_settings (integration tests)
# ---------------------------------------------------------------------------


def test_env_ref_resolved_in_yaml(monkeypatch, tmp_path):
    """ENV[VAR] in YAML is resolved during load."""
    monkeypatch.setenv("MY_JWT", "resolved-jwt")
    config_file = tmp_path / "config.yaml"
    config_file.write_text('security:\n  jwt_secret: "ENV[MY_JWT]"\n')
    s = load_settings(str(config_file))
    assert s.security.jwt_secret == "resolved-jwt"


def test_enc_ref_resolved_in_yaml(tmp_path):
    """ENC[...] in YAML is decrypted during load."""
    from lean_ai_serve.security.secrets import encrypt_value

    key = os.urandom(32)
    key_path = tmp_path / "master.key"
    key_path.write_bytes(key)

    encrypted = encrypt_value("super-secret-jwt", key)
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        f'security:\n  jwt_secret: "{encrypted}"\n'
        f"encryption:\n  at_rest:\n    enabled: true\n    key_source: file\n"
        f'    key_file: "{key_path}"\n'
    )
    s = load_settings(str(config_file))
    assert s.security.jwt_secret == "super-secret-jwt"


def test_env_and_enc_combined(monkeypatch, tmp_path):
    """Both ENV[] and ENC[] work together through load_settings."""
    from lean_ai_serve.security.secrets import encrypt_value

    key = os.urandom(32)
    key_path = tmp_path / "master.key"
    key_path.write_bytes(key)
    monkeypatch.setenv("HF_TEST_TOKEN", "hf_abc123")

    encrypted_jwt = encrypt_value("my-jwt", key)
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        f'security:\n  jwt_secret: "{encrypted_jwt}"\n'
        f'cache:\n  huggingface_token: "ENV[HF_TEST_TOKEN]"\n'
        f"encryption:\n  at_rest:\n    enabled: true\n    key_source: file\n"
        f'    key_file: "{key_path}"\n'
    )
    s = load_settings(str(config_file))
    assert s.security.jwt_secret == "my-jwt"
    assert s.cache.huggingface_token == "hf_abc123"


def test_plain_yaml_no_secrets(tmp_path):
    """YAML without ENV[]/ENC[] works normally (no encryption config needed)."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "server:\n  port: 9999\nsecurity:\n  jwt_secret: plain-text-dev\n"
    )
    s = load_settings(str(config_file))
    assert s.server.port == 9999
    assert s.security.jwt_secret == "plain-text-dev"


@pytest.fixture(autouse=True)
def _clear_settings():
    """Reset the global settings singleton between tests."""
    set_settings(None)
    yield
    set_settings(None)
