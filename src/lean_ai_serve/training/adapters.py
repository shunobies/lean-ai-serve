"""Adapter registry — register, deploy, undeploy LoRA adapters."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from lean_ai_serve.db import Database
from lean_ai_serve.training.schemas import AdapterInfo, AdapterState

logger = logging.getLogger(__name__)


class AdapterError(Exception):
    """Raised on adapter operation failures."""


class AdapterRegistry:
    """Manages LoRA adapter lifecycle — register, deploy, undeploy, delete."""

    def __init__(self, db: Database) -> None:
        self._db = db
        self._http = httpx.AsyncClient(timeout=30.0)

    async def close(self) -> None:
        """Clean up HTTP client."""
        await self._http.aclose()

    async def register(
        self,
        name: str,
        base_model: str,
        source_path: str,
        training_job_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AdapterInfo:
        """Register a new adapter in the registry.

        Raises ValueError if name already exists.
        Raises AdapterError if adapter path doesn't exist.
        """
        # Check for duplicate
        existing = await self._db.fetchone(
            "SELECT name FROM adapters WHERE name = ?", (name,)
        )
        if existing:
            raise ValueError(f"Adapter '{name}' already exists")

        # Validate path exists
        adapter_path = Path(source_path)
        if not adapter_path.exists():
            raise AdapterError(
                f"Adapter path does not exist: {source_path}"
            )

        now = datetime.now(UTC)
        meta_json = json.dumps(metadata or {})

        await self._db.execute(
            """
            INSERT INTO adapters
                (name, base_model, source_path, state, training_job_id,
                 created_at, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name,
                base_model,
                source_path,
                AdapterState.AVAILABLE.value,
                training_job_id,
                now.isoformat(),
                meta_json,
            ),
        )
        await self._db.commit()

        logger.info("Adapter registered: %s (base_model=%s)", name, base_model)
        return AdapterInfo(
            name=name,
            base_model=base_model,
            source_path=source_path,
            state=AdapterState.AVAILABLE,
            training_job_id=training_job_id,
            created_at=now,
            metadata=metadata or {},
        )

    async def list_adapters(
        self, base_model: str | None = None
    ) -> list[AdapterInfo]:
        """List adapters, optionally filtered by base model."""
        if base_model:
            rows = await self._db.fetchall(
                "SELECT * FROM adapters WHERE base_model = ? ORDER BY created_at DESC",
                (base_model,),
            )
        else:
            rows = await self._db.fetchall(
                "SELECT * FROM adapters ORDER BY created_at DESC"
            )
        return [self._row_to_info(row) for row in rows]

    async def get(self, name: str) -> AdapterInfo | None:
        """Get adapter by name."""
        row = await self._db.fetchone(
            "SELECT * FROM adapters WHERE name = ?", (name,)
        )
        if row is None:
            return None
        return self._row_to_info(row)

    async def deploy(self, name: str, vllm_port: int) -> None:
        """Deploy an adapter to a running vLLM instance via dynamic LoRA loading.

        Uses vLLM's /v1/load_lora_adapter endpoint.

        Raises AdapterError on failure.
        """
        adapter = await self.get(name)
        if adapter is None:
            raise AdapterError(f"Adapter '{name}' not found")

        if adapter.state == AdapterState.DEPLOYED:
            raise AdapterError(f"Adapter '{name}' is already deployed")

        try:
            resp = await self._http.post(
                f"http://127.0.0.1:{vllm_port}/v1/load_lora_adapter",
                json={
                    "lora_name": name,
                    "lora_path": adapter.source_path,
                },
            )
            if resp.status_code != 200:
                detail = resp.text[:500]
                raise AdapterError(
                    f"vLLM rejected LoRA load: {resp.status_code} — {detail}"
                )
        except httpx.RequestError as e:
            raise AdapterError(f"Failed to connect to vLLM: {e}") from e

        now = datetime.now(UTC).isoformat()
        await self._db.execute(
            "UPDATE adapters SET state = ?, deployed_at = ? WHERE name = ?",
            (AdapterState.DEPLOYED.value, now, name),
        )
        await self._db.commit()
        logger.info("Adapter deployed: %s → port %d", name, vllm_port)

    async def undeploy(self, name: str, vllm_port: int) -> None:
        """Undeploy an adapter from a running vLLM instance.

        Uses vLLM's /v1/unload_lora_adapter endpoint.

        Raises AdapterError on failure.
        """
        adapter = await self.get(name)
        if adapter is None:
            raise AdapterError(f"Adapter '{name}' not found")

        if adapter.state != AdapterState.DEPLOYED:
            raise AdapterError(f"Adapter '{name}' is not deployed")

        try:
            resp = await self._http.post(
                f"http://127.0.0.1:{vllm_port}/v1/unload_lora_adapter",
                json={"lora_name": name},
            )
            if resp.status_code != 200:
                detail = resp.text[:500]
                raise AdapterError(
                    f"vLLM rejected LoRA unload: {resp.status_code} — {detail}"
                )
        except httpx.RequestError as e:
            raise AdapterError(f"Failed to connect to vLLM: {e}") from e

        await self._db.execute(
            "UPDATE adapters SET state = ?, deployed_at = NULL WHERE name = ?",
            (AdapterState.AVAILABLE.value, name),
        )
        await self._db.commit()
        logger.info("Adapter undeployed: %s", name)

    async def delete(self, name: str) -> bool:
        """Delete an adapter from the registry.

        Raises AdapterError if adapter is currently deployed.
        """
        adapter = await self.get(name)
        if adapter is None:
            return False

        if adapter.state == AdapterState.DEPLOYED:
            raise AdapterError(
                f"Cannot delete deployed adapter '{name}' — undeploy first"
            )

        await self._db.execute("DELETE FROM adapters WHERE name = ?", (name,))
        await self._db.commit()
        logger.info("Adapter deleted: %s", name)
        return True

    async def set_state(
        self, name: str, state: AdapterState, error_msg: str | None = None
    ) -> None:
        """Update adapter state directly (used by orchestrator)."""
        updates = ["state = ?"]
        params: list[Any] = [state.value]

        if state == AdapterState.DEPLOYED:
            updates.append("deployed_at = ?")
            params.append(datetime.now(UTC).isoformat())
        elif state == AdapterState.AVAILABLE:
            updates.append("deployed_at = NULL")

        if error_msg:
            meta_row = await self._db.fetchone(
                "SELECT metadata_json FROM adapters WHERE name = ?", (name,)
            )
            meta = (
                json.loads(meta_row["metadata_json"])
                if meta_row and meta_row["metadata_json"]
                else {}
            )
            meta["error"] = error_msg
            updates.append("metadata_json = ?")
            params.append(json.dumps(meta))

        params.append(name)
        set_clause = ", ".join(updates)
        await self._db.execute(
            f"UPDATE adapters SET {set_clause} WHERE name = ?",
            tuple(params),
        )
        await self._db.commit()

    @staticmethod
    def _row_to_info(row) -> AdapterInfo:
        """Convert DB row to AdapterInfo."""
        meta = json.loads(row["metadata_json"]) if row["metadata_json"] else {}
        return AdapterInfo(
            name=row["name"],
            base_model=row["base_model"],
            source_path=row["source_path"],
            state=AdapterState(row["state"]),
            training_job_id=row["training_job_id"],
            created_at=datetime.fromisoformat(row["created_at"]),
            deployed_at=(
                datetime.fromisoformat(row["deployed_at"])
                if row["deployed_at"]
                else None
            ),
            metadata=meta,
        )
