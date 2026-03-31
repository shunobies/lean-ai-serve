"""Model lifecycle — idle sleep/wake management."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import UTC, datetime

from lean_ai_serve.config import get_settings
from lean_ai_serve.engine.process import ProcessManager
from lean_ai_serve.models.registry import ModelRegistry
from lean_ai_serve.models.schemas import ModelState

logger = logging.getLogger(__name__)

POLL_INTERVAL = 60  # seconds


class RequestTracker:
    """Lightweight in-memory tracker of last-request time per model.

    Called from the OpenAI-compat layer on every inference request.
    Thread-safe: single asyncio event loop, no locking needed.
    """

    def __init__(self) -> None:
        self._last_seen: dict[str, float] = {}

    def touch(self, model_name: str) -> None:
        """Record that a request was just made to this model."""
        self._last_seen[model_name] = time.monotonic()

    def last_seen(self, model_name: str) -> float | None:
        """Return monotonic timestamp of last request, or ``None`` if never seen."""
        return self._last_seen.get(model_name)

    def idle_seconds(self, model_name: str) -> float | None:
        """Seconds since last request, or ``None`` if no requests recorded."""
        ts = self._last_seen.get(model_name)
        if ts is None:
            return None
        return time.monotonic() - ts

    def clear(self, model_name: str) -> None:
        """Remove tracking for a model (e.g., after unload or sleep)."""
        self._last_seen.pop(model_name, None)

    @property
    def tracked_models(self) -> list[str]:
        """Return list of model names currently being tracked."""
        return list(self._last_seen.keys())


class LifecycleManager:
    """Background daemon that sleeps idle models.

    Polls every ``POLL_INTERVAL`` seconds.  For each LOADED model with
    ``lifecycle.idle_sleep_timeout > 0``, checks if the model has been idle
    longer than the timeout.

    Sleep levels:
      - Level 1: process stopped, state → SLEEPING (auto-wakeable on request)
      - Level 2: process stopped, state → DOWNLOADED (manual reload required)
    """

    def __init__(
        self,
        registry: ModelRegistry,
        process_manager: ProcessManager,
        tracker: RequestTracker,
    ):
        self._registry = registry
        self._pm = process_manager
        self._tracker = tracker
        self._task: asyncio.Task | None = None
        self._wake_locks: dict[str, asyncio.Lock] = {}

    async def start(self) -> None:
        """Start the background poll loop."""
        self._task = asyncio.create_task(self._poll_loop())
        logger.info("Lifecycle manager started (poll interval=%ds)", POLL_INTERVAL)

    async def stop(self) -> None:
        """Cancel the background task."""
        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        logger.info("Lifecycle manager stopped")

    async def _poll_loop(self) -> None:
        """Main loop: check idle models every POLL_INTERVAL."""
        while True:
            try:
                await self._check_idle_models()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Lifecycle poll error")
            await asyncio.sleep(POLL_INTERVAL)

    async def _check_idle_models(self) -> None:
        """Check all loaded models for idle timeout."""
        settings = get_settings()
        models = await self._registry.list_models()

        for model in models:
            if model.state != ModelState.LOADED:
                continue

            config = settings.models.get(model.name)
            if config is None:
                continue

            timeout = config.lifecycle.idle_sleep_timeout
            if timeout <= 0:
                continue

            idle = self._tracker.idle_seconds(model.name)
            # If never seen a request, use loaded_at as reference
            if idle is None and model.loaded_at:
                loaded_secs_ago = (
                    datetime.now(UTC) - model.loaded_at
                ).total_seconds()
                idle = loaded_secs_ago

            if idle is not None and idle >= timeout:
                await self._sleep_model(model.name, config.lifecycle.sleep_level)

    async def _sleep_model(self, name: str, level: int) -> None:
        """Put a model to sleep."""
        logger.info(
            "Sleeping model '%s' (level=%d, idle timeout exceeded)", name, level
        )
        await self._pm.stop(name)
        self._tracker.clear(name)

        if level == 1:
            await self._registry.set_state(name, ModelState.SLEEPING)
        else:
            await self._registry.set_state(name, ModelState.DOWNLOADED)

    async def wake_model(self, name: str) -> None:
        """Wake a sleeping model — restart the vLLM process.

        Uses a per-model lock to prevent concurrent wake attempts.
        Raises ``ValueError`` if model not in SLEEPING state.
        """
        # Per-model lock to prevent concurrent wake attempts
        lock = self._wake_locks.setdefault(name, asyncio.Lock())
        async with lock:
            model = await self._registry.get_model(name)
            if model is None:
                raise ValueError(f"Model not found: {name}")
            # If already loaded (concurrent wake beat us), return silently
            if model.state == ModelState.LOADED:
                return
            if model.state != ModelState.SLEEPING:
                raise ValueError(
                    f"Model '{name}' is not sleeping (state={model.state})"
                )

            config = await self._registry.get_config(name)
            if config is None:
                raise ValueError(f"No config for model '{name}'")

            from lean_ai_serve.models.downloader import ModelDownloader

            downloader = ModelDownloader()
            model_path = downloader.get_local_path(config.source)
            if model_path is None:
                raise ValueError(f"Model files not found for '{name}'")

            await self._registry.set_state(name, ModelState.LOADING)
            try:
                info = await self._pm.start(name, config, model_path)
                await self._registry.set_state(
                    name, ModelState.LOADED, port=info.port, pid=info.pid
                )
                logger.info("Model '%s' woken up (port=%d)", name, info.port)
            except Exception:
                logger.exception("Failed to wake model '%s'", name)
                await self._registry.set_state(
                    name, ModelState.ERROR, error_message="Wake failed"
                )
                raise

    def get_idle_times(self) -> dict[str, float | None]:
        """Return idle seconds for all tracked models."""
        return {
            name: self._tracker.idle_seconds(name)
            for name in self._tracker.tracked_models
        }
