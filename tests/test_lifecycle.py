"""Tests for model lifecycle management — request tracking and idle sleep/wake."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from lean_ai_serve.config import LifecycleConfig, ModelConfig, Settings, set_settings
from lean_ai_serve.db import Database
from lean_ai_serve.engine.lifecycle import LifecycleManager, RequestTracker
from lean_ai_serve.models.registry import ModelRegistry
from lean_ai_serve.models.schemas import ModelState

# ---------------------------------------------------------------------------
# RequestTracker tests
# ---------------------------------------------------------------------------


def test_request_tracker_touch_and_idle():
    tracker = RequestTracker()
    tracker.touch("model-a")
    idle = tracker.idle_seconds("model-a")
    assert idle is not None
    assert idle < 1.0  # Should be nearly zero


def test_request_tracker_idle_increases():
    tracker = RequestTracker()
    # Manually set a past timestamp
    tracker._last_seen["model-a"] = time.monotonic() - 60
    idle = tracker.idle_seconds("model-a")
    assert idle is not None
    assert idle >= 59.0


def test_request_tracker_never_seen():
    tracker = RequestTracker()
    assert tracker.idle_seconds("unknown") is None
    assert tracker.last_seen("unknown") is None


def test_request_tracker_clear():
    tracker = RequestTracker()
    tracker.touch("model-a")
    assert tracker.idle_seconds("model-a") is not None
    tracker.clear("model-a")
    assert tracker.idle_seconds("model-a") is None


def test_request_tracker_clear_nonexistent():
    tracker = RequestTracker()
    # Should not raise
    tracker.clear("nonexistent")


def test_request_tracker_tracked_models():
    tracker = RequestTracker()
    tracker.touch("m1")
    tracker.touch("m2")
    assert set(tracker.tracked_models) == {"m1", "m2"}


def test_request_tracker_touch_updates():
    tracker = RequestTracker()
    tracker._last_seen["model-a"] = time.monotonic() - 120
    old_idle = tracker.idle_seconds("model-a")
    tracker.touch("model-a")
    new_idle = tracker.idle_seconds("model-a")
    assert new_idle < old_idle


# ---------------------------------------------------------------------------
# Fixtures for LifecycleManager tests
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db(tmp_path):
    db = Database(tmp_path / "test.db")
    await db.connect()
    yield db
    await db.close()


@pytest_asyncio.fixture
async def registry(db):
    return ModelRegistry(db)


@dataclass
class MockProcessInfo:
    name: str = "test-model"
    port: int = 8430
    pid: int = 12345


def _make_settings(models: dict[str, ModelConfig] | None = None) -> Settings:
    s = Settings(models=models or {})
    set_settings(s)
    return s


# ---------------------------------------------------------------------------
# LifecycleManager — sleep behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lifecycle_sleeps_idle_model(registry):
    """Model idle longer than timeout should be slept."""
    config = ModelConfig(
        source="org/model",
        lifecycle=LifecycleConfig(idle_sleep_timeout=60, sleep_level=1),
    )
    _make_settings({"idle-model": config})

    # Register and set to LOADED
    await registry.register_model("idle-model", "org/model", config)
    await registry.set_state(
        "idle-model", ModelState.LOADED, port=8430, pid=1234
    )

    tracker = RequestTracker()
    # Simulate 120 seconds idle
    tracker._last_seen["idle-model"] = time.monotonic() - 120

    pm = AsyncMock()
    pm.stop = AsyncMock(return_value=True)

    lifecycle = LifecycleManager(registry, pm, tracker)
    await lifecycle._check_idle_models()

    pm.stop.assert_called_once_with("idle-model")
    model = await registry.get_model("idle-model")
    assert model.state == ModelState.SLEEPING


@pytest.mark.asyncio
async def test_lifecycle_level_2_sets_downloaded(registry):
    """Level 2 sleep sets state to DOWNLOADED."""
    config = ModelConfig(
        source="org/model",
        lifecycle=LifecycleConfig(idle_sleep_timeout=60, sleep_level=2),
    )
    _make_settings({"model-l2": config})

    await registry.register_model("model-l2", "org/model", config)
    await registry.set_state("model-l2", ModelState.LOADED, port=8430, pid=1234)

    tracker = RequestTracker()
    tracker._last_seen["model-l2"] = time.monotonic() - 120

    pm = AsyncMock()
    pm.stop = AsyncMock(return_value=True)

    lifecycle = LifecycleManager(registry, pm, tracker)
    await lifecycle._check_idle_models()

    model = await registry.get_model("model-l2")
    assert model.state == ModelState.DOWNLOADED


@pytest.mark.asyncio
async def test_lifecycle_skips_zero_timeout(registry):
    """Models with idle_sleep_timeout=0 should never be slept."""
    config = ModelConfig(
        source="org/model",
        lifecycle=LifecycleConfig(idle_sleep_timeout=0),
    )
    _make_settings({"no-sleep": config})

    await registry.register_model("no-sleep", "org/model", config)
    await registry.set_state("no-sleep", ModelState.LOADED, port=8430, pid=1234)

    tracker = RequestTracker()
    tracker._last_seen["no-sleep"] = time.monotonic() - 9999

    pm = AsyncMock()
    lifecycle = LifecycleManager(registry, pm, tracker)
    await lifecycle._check_idle_models()

    pm.stop.assert_not_called()
    model = await registry.get_model("no-sleep")
    assert model.state == ModelState.LOADED


@pytest.mark.asyncio
async def test_lifecycle_skips_non_loaded(registry):
    """Only LOADED models should be checked for idle timeout."""
    config = ModelConfig(
        source="org/model",
        lifecycle=LifecycleConfig(idle_sleep_timeout=60),
    )
    _make_settings({"downloading": config})

    await registry.register_model("downloading", "org/model", config)
    await registry.set_state("downloading", ModelState.DOWNLOADING)

    tracker = RequestTracker()
    pm = AsyncMock()
    lifecycle = LifecycleManager(registry, pm, tracker)
    await lifecycle._check_idle_models()

    pm.stop.assert_not_called()


@pytest.mark.asyncio
async def test_lifecycle_uses_loaded_at_when_never_seen(registry):
    """If no request recorded, use loaded_at as idle reference."""
    config = ModelConfig(
        source="org/model",
        lifecycle=LifecycleConfig(idle_sleep_timeout=10),
    )
    _make_settings({"fresh": config})

    await registry.register_model("fresh", "org/model", config)
    await registry.set_state("fresh", ModelState.LOADED, port=8430, pid=1234)

    tracker = RequestTracker()
    # No touch() call — never seen

    pm = AsyncMock()
    pm.stop = AsyncMock(return_value=True)

    lifecycle = LifecycleManager(registry, pm, tracker)

    # The loaded_at is set to now, so idle should be ~0. Model should NOT be slept.
    await lifecycle._check_idle_models()
    pm.stop.assert_not_called()


# ---------------------------------------------------------------------------
# LifecycleManager — wake behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wake_model(registry):
    """Wake a sleeping model — should restart via ProcessManager."""
    config = ModelConfig(source="org/model")
    await registry.register_model("sleepy", "org/model", config)
    await registry.set_state("sleepy", ModelState.SLEEPING)

    mock_info = MockProcessInfo(name="sleepy", port=8431, pid=5678)
    pm = AsyncMock()
    pm.start = AsyncMock(return_value=mock_info)

    tracker = RequestTracker()
    lifecycle = LifecycleManager(registry, pm, tracker)

    with patch(
        "lean_ai_serve.models.downloader.ModelDownloader"
    ) as mock_dl:
        mock_dl.return_value.get_local_path.return_value = "/models/org/model"
        await lifecycle.wake_model("sleepy")

    pm.start.assert_called_once()
    model = await registry.get_model("sleepy")
    assert model.state == ModelState.LOADED
    assert model.port == 8431


@pytest.mark.asyncio
async def test_wake_non_sleeping_raises(registry):
    """Waking a model not in SLEEPING state should raise ValueError."""
    config = ModelConfig(source="org/model")
    await registry.register_model("downloaded", "org/model", config)
    await registry.set_state("downloaded", ModelState.DOWNLOADED)

    pm = AsyncMock()
    tracker = RequestTracker()
    lifecycle = LifecycleManager(registry, pm, tracker)

    with pytest.raises(ValueError, match="not sleeping"):
        await lifecycle.wake_model("downloaded")


@pytest.mark.asyncio
async def test_wake_nonexistent_raises(registry):
    """Waking a nonexistent model should raise ValueError."""
    pm = AsyncMock()
    tracker = RequestTracker()
    lifecycle = LifecycleManager(registry, pm, tracker)

    with pytest.raises(ValueError, match="not found"):
        await lifecycle.wake_model("nonexistent")


@pytest.mark.asyncio
async def test_wake_already_loaded_idempotent(registry):
    """Concurrent wake: if model already LOADED, should return silently."""
    config = ModelConfig(source="org/model")
    await registry.register_model("waking", "org/model", config)
    await registry.set_state("waking", ModelState.LOADED, port=8430, pid=1234)

    pm = AsyncMock()
    tracker = RequestTracker()
    lifecycle = LifecycleManager(registry, pm, tracker)

    # Should not raise even though state is LOADED (not SLEEPING)
    # The lock-based check treats LOADED as "already woken"
    await lifecycle.wake_model("waking")
    pm.start.assert_not_called()


# ---------------------------------------------------------------------------
# LifecycleManager — get_idle_times
# ---------------------------------------------------------------------------


def test_get_idle_times():
    tracker = RequestTracker()
    tracker.touch("m1")
    tracker._last_seen["m2"] = time.monotonic() - 300

    pm = AsyncMock()
    registry_mock = AsyncMock()
    lifecycle = LifecycleManager(registry_mock, pm, tracker)

    times = lifecycle.get_idle_times()
    assert "m1" in times
    assert times["m1"] < 1.0
    assert "m2" in times
    assert times["m2"] >= 299.0


# ---------------------------------------------------------------------------
# LifecycleManager — start/stop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lifecycle_start_stop():
    """Start creates a background task, stop cancels it."""
    pm = AsyncMock()
    registry_mock = AsyncMock()
    tracker = RequestTracker()
    lifecycle = LifecycleManager(registry_mock, pm, tracker)

    await lifecycle.start()
    assert lifecycle._task is not None
    assert not lifecycle._task.done()

    await lifecycle.stop()
    # Give a moment for cancellation to propagate
    await asyncio.sleep(0.05)
    assert lifecycle._task.done()
