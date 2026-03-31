"""Request router — resolves model names to vLLM ports."""

from __future__ import annotations

import logging

from lean_ai_serve.engine.process import ProcessManager
from lean_ai_serve.models.registry import ModelRegistry
from lean_ai_serve.models.schemas import ModelState

logger = logging.getLogger(__name__)


class Router:
    """Routes inference requests to the correct vLLM backend port."""

    def __init__(self, registry: ModelRegistry, process_manager: ProcessManager):
        self._registry = registry
        self._pm = process_manager

    async def resolve(self, model_name: str) -> int | None:
        """Resolve a model name to a vLLM port.

        Returns the port number if the model is loaded, None otherwise.
        Checks the process manager first (live state), falls back to registry.
        """
        # Check live process manager
        port = self._pm.get_port(model_name)
        if port is not None:
            return port

        # Check registry (process might have been started before this instance)
        db_port = await self._registry.get_port(model_name)
        if db_port is not None:
            # Verify the port is actually responding
            healthy = await self._pm.health_check(model_name)
            if healthy:
                return db_port

        return None

    async def list_available(self) -> list[str]:
        """List all model names that are currently serving."""
        models = await self._registry.list_models()
        available = []
        for m in models:
            if m.state == ModelState.LOADED:
                port = self._pm.get_port(m.name) or m.port
                if port:
                    available.append(m.name)
        return available
