"""Tests for configuration loading."""

from __future__ import annotations

from pathlib import Path

import yaml

from lean_ai_serve.config import Settings, load_settings


def test_default_settings():
    """Default settings should have sane values."""
    s = Settings()
    assert s.server.port == 8420
    assert s.server.host == "0.0.0.0"
    assert s.security.mode == "api_key"
    assert s.audit.enabled is True
    assert s.defaults.gpu_memory_utilization == 0.90


def test_load_from_yaml(tmp_path: Path):
    """Should load settings from a YAML file."""
    config = {
        "server": {"port": 9999, "host": "127.0.0.1"},
        "security": {"mode": "none"},
        "models": {
            "test-model": {
                "source": "test/model",
                "gpu": [0],
            }
        },
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.dump(config))

    s = load_settings(config_path)
    assert s.server.port == 9999
    assert s.server.host == "127.0.0.1"
    assert "test-model" in s.models
    assert s.models["test-model"].source == "test/model"


def test_model_defaults_applied(tmp_path: Path):
    """Server defaults should be applied to models without explicit values."""
    config = {
        "defaults": {"gpu_memory_utilization": 0.85},
        "models": {
            "model-a": {"source": "org/model-a"},
        },
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.dump(config))

    s = load_settings(config_path)
    assert s.models["model-a"].gpu_memory_utilization == 0.85


def test_path_expansion():
    """~ should be expanded in cache directory."""
    s = Settings(cache={"directory": "~/test-cache"})
    assert "~" not in s.cache.directory
    assert str(Path.home()) in s.cache.directory


def test_missing_config_returns_defaults():
    """Loading from nonexistent file should return defaults."""
    s = load_settings("/nonexistent/config.yaml")
    assert s.server.port == 8420
