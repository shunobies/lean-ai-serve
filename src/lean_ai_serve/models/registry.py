"""Model registry — tracks model state and persists to the database."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from lean_ai_serve.config import ModelConfig
from lean_ai_serve.db import Database, models_table
from lean_ai_serve.models.schemas import ModelInfo, ModelState

logger = logging.getLogger(__name__)


class ModelRegistry:
    """Manages model lifecycle state in the database."""

    def __init__(self, db: Database):
        self._db = db

    async def sync_from_config(self, models: dict[str, ModelConfig]) -> None:
        """Sync config-defined models into the registry.

        Models in config but not in DB are added as NOT_DOWNLOADED.
        Models in DB but not in config are left as-is (manually managed).
        """
        for name, config in models.items():
            existing = await self._db.fetchone(
                "SELECT name, state FROM models WHERE name = ?", (name,)
            )
            if existing is None:
                await self._db.execute(
                    """
                    INSERT INTO models (name, source, state, gpu_assignment, config_json)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        name,
                        config.source,
                        ModelState.NOT_DOWNLOADED,
                        json.dumps(config.gpu),
                        config.model_dump_json(),
                    ),
                )
                logger.info("Registered model from config: %s (%s)", name, config.source)
            else:
                # Update config but preserve state
                await self._db.execute(
                    """
                    UPDATE models SET source = ?, gpu_assignment = ?, config_json = ?
                    WHERE name = ?
                    """,
                    (config.source, json.dumps(config.gpu), config.model_dump_json(), name),
                )
        await self._db.commit()

    async def list_models(self) -> list[ModelInfo]:
        """List all registered models."""
        rows = await self._db.fetchall("SELECT * FROM models")
        result = []
        for row in rows:
            config = ModelConfig(**json.loads(row["config_json"])) if row["config_json"] else None
            result.append(
                ModelInfo(
                    name=row["name"],
                    source=row["source"],
                    state=ModelState(row["state"]),
                    gpu=json.loads(row["gpu_assignment"]) if row["gpu_assignment"] else [],
                    tensor_parallel_size=config.tensor_parallel_size if config else 1,
                    max_model_len=config.max_model_len if config else None,
                    task=config.task if config else "chat",
                    port=row["port"],
                    enable_lora=config.enable_lora if config else False,
                    autoload=config.autoload if config else False,
                    downloaded_at=(
                        datetime.fromisoformat(row["downloaded_at"])
                        if row["downloaded_at"]
                        else None
                    ),
                    loaded_at=(
                        datetime.fromisoformat(row["loaded_at"]) if row["loaded_at"] else None
                    ),
                    error_message=row["error_message"],
                )
            )
        return result

    async def get_model(self, name: str) -> ModelInfo | None:
        """Get a single model by name."""
        row = await self._db.fetchone("SELECT * FROM models WHERE name = ?", (name,))
        if row is None:
            return None
        config = ModelConfig(**json.loads(row["config_json"])) if row["config_json"] else None
        return ModelInfo(
            name=row["name"],
            source=row["source"],
            state=ModelState(row["state"]),
            gpu=json.loads(row["gpu_assignment"]) if row["gpu_assignment"] else [],
            tensor_parallel_size=config.tensor_parallel_size if config else 1,
            max_model_len=config.max_model_len if config else None,
            task=config.task if config else "chat",
            port=row["port"],
            enable_lora=config.enable_lora if config else False,
            autoload=config.autoload if config else False,
            downloaded_at=(
                datetime.fromisoformat(row["downloaded_at"]) if row["downloaded_at"] else None
            ),
            loaded_at=datetime.fromisoformat(row["loaded_at"]) if row["loaded_at"] else None,
            error_message=row["error_message"],
        )

    async def get_config(self, name: str) -> ModelConfig | None:
        """Get the full ModelConfig for a model."""
        row = await self._db.fetchone(
            "SELECT config_json FROM models WHERE name = ?", (name,)
        )
        if row is None or row["config_json"] is None:
            return None
        return ModelConfig(**json.loads(row["config_json"]))

    async def set_state(
        self,
        name: str,
        state: ModelState,
        *,
        port: int | None = None,
        pid: int | None = None,
        error_message: str | None = None,
    ) -> None:
        """Update model state and optional fields."""
        updates = ["state = ?"]
        params: list = [state.value]

        if port is not None:
            updates.append("port = ?")
            params.append(port)
        if pid is not None:
            updates.append("pid = ?")
            params.append(pid)
        if error_message is not None:
            updates.append("error_message = ?")
            params.append(error_message)
        elif state != ModelState.ERROR:
            updates.append("error_message = NULL")

        now = datetime.now(UTC).isoformat()
        if state == ModelState.DOWNLOADED:
            updates.append("downloaded_at = ?")
            params.append(now)
        elif state == ModelState.LOADED:
            updates.append("loaded_at = ?")
            params.append(now)
        elif state in (ModelState.NOT_DOWNLOADED, ModelState.UNLOADING, ModelState.SLEEPING):
            updates.append("port = NULL")
            updates.append("pid = NULL")

        set_clause = ", ".join(updates)
        params.append(name)
        await self._db.execute(f"UPDATE models SET {set_clause} WHERE name = ?", tuple(params))
        await self._db.commit()
        logger.info("Model '%s' → %s", name, state.value)

    async def register_model(
        self,
        name: str,
        source: str,
        config: ModelConfig,
        state: ModelState = ModelState.NOT_DOWNLOADED,
    ) -> None:
        """Register a new model (e.g., after pull)."""
        await self._db.upsert(
            models_table,
            {
                "name": name,
                "source": source,
                "state": state.value,
                "gpu_assignment": json.dumps(config.gpu),
                "config_json": config.model_dump_json(),
            },
            on_conflict="replace",
        )
        await self._db.commit()

    async def delete_model(self, name: str) -> bool:
        """Remove a model from the registry."""
        result = await self._db.execute("DELETE FROM models WHERE name = ?", (name,))
        await self._db.commit()
        return result.rowcount > 0

    async def get_port(self, name: str) -> int | None:
        """Get the port for a loaded model."""
        row = await self._db.fetchone(
            "SELECT port FROM models WHERE name = ? AND state = ?",
            (name, ModelState.LOADED.value),
        )
        return row["port"] if row else None
