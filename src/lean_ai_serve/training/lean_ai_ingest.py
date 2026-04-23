"""Coordinator for pulling DPO training data from lean_ai workspaces.

Registered workspaces expose ``GET /api/export/manifest`` and
``GET /api/export/traces?format=dpo&cursor=<id>`` (Bearer-auth). This module
polls those endpoints on a schedule and lands each ``pair_kind`` of DPO pair
into its own ``datasets`` row so training jobs can target them independently.

See ``/home/alex/Code/lean_ai/docs/training.md`` for the protocol lean_ai
emits.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import UTC, datetime
from typing import Any

import httpx

from lean_ai_serve.config import Settings
from lean_ai_serve.db import Database, lean_ai_ingest_state_table
from lean_ai_serve.training.datasets import DatasetManager
from lean_ai_serve.training.schemas import (
    DatasetFormat,
    IngestResult,
    IngestState,
    WorkspaceInfo,
)

logger = logging.getLogger(__name__)

# Pair kinds emitted by lean_ai's DPO export. Each maps to a separate dataset
# on the serve side so trainers can opt into one without the other.
PAIR_KINDS: tuple[str, ...] = ("plan_rejection", "validation_fix")


class IngestError(Exception):
    """Raised when a poll against a remote lean_ai workspace fails."""


class LeanAiIngestor:
    """Poll registered lean_ai workspaces and land DPO pairs as datasets."""

    def __init__(
        self,
        db: Database,
        datasets: DatasetManager,
        settings: Settings,
        *,
        encryption: Any | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._db = db
        self._datasets = datasets
        self._settings = settings
        self._encryption = encryption
        self._owns_client = http_client is None
        self._http = http_client or httpx.AsyncClient(
            timeout=settings.ingestion.http_timeout_seconds
        )
        self._poll_lock = asyncio.Lock()

    async def close(self) -> None:
        if self._owns_client:
            await self._http.aclose()

    # ------------------------------------------------------------------
    # Key encryption helpers (reuse encryption.at_rest if enabled)
    # ------------------------------------------------------------------

    def _encrypt_key(self, plaintext: str) -> str:
        if self._encryption is not None:
            return self._encryption.encrypt(plaintext)
        return plaintext

    def _decrypt_key(self, stored: str) -> str:
        if self._encryption is not None:
            return self._encryption.decrypt(stored)
        return stored

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    async def register_workspace(
        self,
        *,
        workspace_id: str,
        display_name: str,
        backend_url: str,
        repo_root: str,
        export_key: str,
        registered_by: str,
    ) -> WorkspaceInfo:
        """Validate the export key + workspace identity, then persist.

        Calls ``/api/export/workspace-id`` with ``repo_root`` to confirm the
        remote salt+path pair hashes to the claimed ``workspace_id``. Falls
        back to probing ``/api/export/manifest`` if ``/workspace-id`` is not
        available (older producer). On success, creates one dataset per
        ``pair_kind`` plus one ``lean_ai_ingest_state`` row per ``pair_kind``
        with ``last_cursor=0``. Re-registering the same workspace_id rotates
        the export key, display name, and repo_root but leaves cursors
        untouched.
        """
        backend_url = backend_url.rstrip("/")
        if not repo_root:
            raise IngestError("repo_root is required (passed as ?repo_root= on every /api/export/*)")
        await self._verify_workspace_id(
            backend_url, export_key, repo_root, claimed_id=workspace_id,
        )

        encrypted = self._encrypt_key(export_key)
        now = datetime.now(UTC).isoformat()

        existing = await self._db.fetchone(
            "SELECT workspace_id FROM lean_ai_workspaces WHERE workspace_id = ?",
            (workspace_id,),
        )

        if existing is None:
            await self._db.execute(
                """
                INSERT INTO lean_ai_workspaces
                    (workspace_id, display_name, backend_url, repo_root,
                     export_key_encrypted, registered_by, registered_at, enabled)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    workspace_id,
                    display_name,
                    backend_url,
                    repo_root,
                    encrypted,
                    registered_by,
                    now,
                ),
            )
        else:
            await self._db.execute(
                """
                UPDATE lean_ai_workspaces
                   SET display_name = ?, backend_url = ?, repo_root = ?,
                       export_key_encrypted = ?, enabled = 1, last_error = NULL
                 WHERE workspace_id = ?
                """,
                (display_name, backend_url, repo_root, encrypted, workspace_id),
            )

        # Ensure one dataset + state row per pair_kind exists.
        for pair_kind in PAIR_KINDS:
            dataset_name = _dataset_name(workspace_id, pair_kind)
            await self._ensure_dataset(
                name=dataset_name,
                workspace_id=workspace_id,
                pair_kind=pair_kind,
                registered_by=registered_by,
            )
            await self._db.upsert(
                lean_ai_ingest_state_table,
                values={
                    "workspace_id": workspace_id,
                    "format": DatasetFormat.DPO.value,
                    "pair_kind": pair_kind,
                    "last_cursor": 0,
                    "rows_imported": 0,
                    "dataset_name": dataset_name,
                    "updated_at": now,
                },
                conflict_columns=["workspace_id", "format", "pair_kind"],
                on_conflict="ignore",
            )

        await self._db.commit()
        logger.info(
            "Registered lean_ai workspace %s (%s) at %s",
            workspace_id, display_name, backend_url,
        )
        info = await self.get_workspace(workspace_id)
        assert info is not None
        return info

    async def _ensure_dataset(
        self,
        *,
        name: str,
        workspace_id: str,
        pair_kind: str,
        registered_by: str,
    ) -> None:
        existing = await self._datasets.get(name)
        if existing is not None:
            return
        with contextlib.suppress(ValueError):
            await self._datasets.create_empty_jsonl(
                name=name,
                fmt=DatasetFormat.DPO,
                uploaded_by=registered_by,
                source=f"lean_ai:{workspace_id}:{pair_kind}",
                description=(
                    f"DPO {pair_kind} pairs auto-ingested from lean_ai "
                    f"workspace {workspace_id}"
                ),
            )

    # ------------------------------------------------------------------
    # Listing / querying
    # ------------------------------------------------------------------

    async def list_workspaces(self) -> list[WorkspaceInfo]:
        rows = await self._db.fetchall(
            "SELECT * FROM lean_ai_workspaces ORDER BY registered_at DESC"
        )
        return [await self._row_to_info(r) for r in rows]

    async def get_workspace(self, workspace_id: str) -> WorkspaceInfo | None:
        row = await self._db.fetchone(
            "SELECT * FROM lean_ai_workspaces WHERE workspace_id = ?",
            (workspace_id,),
        )
        if row is None:
            return None
        return await self._row_to_info(row)

    async def _row_to_info(self, row: Any) -> WorkspaceInfo:
        state_rows = await self._db.fetchall(
            "SELECT format, pair_kind, last_cursor, rows_imported, "
            "dataset_name, updated_at "
            "FROM lean_ai_ingest_state WHERE workspace_id = ?",
            (row["workspace_id"],),
        )
        ingest = [
            IngestState(
                format=DatasetFormat(s["format"]),
                pair_kind=s["pair_kind"],
                last_cursor=s["last_cursor"],
                rows_imported=s["rows_imported"],
                dataset_name=s["dataset_name"],
                updated_at=datetime.fromisoformat(s["updated_at"]),
            )
            for s in state_rows
        ]
        last_polled = (
            datetime.fromisoformat(row["last_polled_at"])
            if row["last_polled_at"] else None
        )
        return WorkspaceInfo(
            workspace_id=row["workspace_id"],
            display_name=row["display_name"],
            backend_url=row["backend_url"],
            repo_root=row["repo_root"],
            registered_by=row["registered_by"],
            registered_at=datetime.fromisoformat(row["registered_at"]),
            enabled=bool(row["enabled"]),
            last_polled_at=last_polled,
            last_error=row["last_error"],
            ingest=ingest,
        )

    # ------------------------------------------------------------------
    # Remove workspace
    # ------------------------------------------------------------------

    async def delete_workspace(self, workspace_id: str, *, hard: bool = False) -> bool:
        """Disable or fully remove a workspace.

        ``hard=False``: sets ``enabled=0`` (keeps datasets and cursors so a
        future re-enable resumes cleanly).
        ``hard=True``: removes ingest state rows and underlying datasets.
        """
        existing = await self._db.fetchone(
            "SELECT workspace_id FROM lean_ai_workspaces WHERE workspace_id = ?",
            (workspace_id,),
        )
        if existing is None:
            return False

        if not hard:
            await self._db.execute(
                "UPDATE lean_ai_workspaces SET enabled = 0 WHERE workspace_id = ?",
                (workspace_id,),
            )
            await self._db.commit()
            return True

        state_rows = await self._db.fetchall(
            "SELECT dataset_name FROM lean_ai_ingest_state WHERE workspace_id = ?",
            (workspace_id,),
        )
        for s in state_rows:
            await self._datasets.delete(s["dataset_name"])
        await self._db.execute(
            "DELETE FROM lean_ai_ingest_state WHERE workspace_id = ?",
            (workspace_id,),
        )
        await self._db.execute(
            "DELETE FROM lean_ai_workspaces WHERE workspace_id = ?",
            (workspace_id,),
        )
        await self._db.commit()
        return True

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------

    async def poll_workspace(self, workspace_id: str) -> IngestResult:
        """Pull any new DPO rows from a single workspace and append to datasets."""
        row = await self._db.fetchone(
            "SELECT * FROM lean_ai_workspaces WHERE workspace_id = ?",
            (workspace_id,),
        )
        if row is None:
            raise IngestError(f"Unknown workspace: {workspace_id}")
        if not row["enabled"]:
            raise IngestError(f"Workspace disabled: {workspace_id}")

        backend_url = row["backend_url"]
        repo_root = row["repo_root"]
        if not repo_root:
            raise IngestError(
                f"Workspace {workspace_id} is missing repo_root — re-register it"
            )
        export_key = self._decrypt_key(row["export_key_encrypted"])
        result = IngestResult(workspace_id=workspace_id, rows_pulled=0)

        try:
            for pair_kind in PAIR_KINDS:
                pulled = await self._poll_pair_kind(
                    workspace_id=workspace_id,
                    backend_url=backend_url,
                    repo_root=repo_root,
                    export_key=export_key,
                    pair_kind=pair_kind,
                    result=result,
                )
                if pulled:
                    result.datasets_updated.append(
                        _dataset_name(workspace_id, pair_kind)
                    )
        except IngestError as exc:
            result.errors.append(str(exc))
            await self._record_poll_result(workspace_id, error=str(exc))
            return result
        except httpx.HTTPError as exc:
            msg = f"HTTP error polling {backend_url}: {exc!r}"
            result.errors.append(msg)
            await self._record_poll_result(workspace_id, error=msg)
            return result

        await self._record_poll_result(workspace_id, error=None)
        return result

    async def poll_all(self) -> list[IngestResult]:
        """Poll every enabled workspace, bounded by ``max_concurrent_pulls``."""
        async with self._poll_lock:
            rows = await self._db.fetchall(
                "SELECT workspace_id FROM lean_ai_workspaces WHERE enabled = 1"
            )
            if not rows:
                return []

            sem = asyncio.Semaphore(self._settings.ingestion.max_concurrent_pulls)

            async def _one(ws_id: str) -> IngestResult:
                async with sem:
                    try:
                        return await self.poll_workspace(ws_id)
                    except Exception as exc:  # noqa: BLE001
                        logger.exception("Poll failed for workspace %s", ws_id)
                        return IngestResult(
                            workspace_id=ws_id,
                            rows_pulled=0,
                            errors=[repr(exc)],
                        )

            return await asyncio.gather(*(_one(r["workspace_id"]) for r in rows))

    # ------------------------------------------------------------------
    # Internal: single pair_kind pull loop
    # ------------------------------------------------------------------

    async def _poll_pair_kind(
        self,
        *,
        workspace_id: str,
        backend_url: str,
        repo_root: str,
        export_key: str,
        pair_kind: str,
        result: IngestResult,
    ) -> int:
        state = await self._db.fetchone(
            "SELECT last_cursor, rows_imported, dataset_name "
            "FROM lean_ai_ingest_state "
            "WHERE workspace_id = ? AND format = ? AND pair_kind = ?",
            (workspace_id, DatasetFormat.DPO.value, pair_kind),
        )
        if state is None:
            return 0

        cursor: int = state["last_cursor"]
        dataset_name: str = state["dataset_name"]
        already_imported: set[str] = await self._load_pair_ids(dataset_name)

        page_limit = max(1, int(self._settings.ingestion.page_limit))
        total_pulled = 0

        while True:
            rows = await self._fetch_page(
                backend_url=backend_url,
                repo_root=repo_root,
                export_key=export_key,
                cursor=cursor,
                limit=page_limit,
            )
            if not rows:
                break

            new_rows: list[dict] = []
            max_id_in_page = cursor
            for raw in rows:
                row_id = _coerce_int(raw.get("id"))
                if row_id is not None and row_id > max_id_in_page:
                    max_id_in_page = row_id

                if raw.get("pair_kind") != pair_kind:
                    continue
                pair_id = raw.get("pair_id")
                if pair_id and pair_id in already_imported:
                    continue
                # Strip the envelope "id" (bookkeeping only) from the persisted row.
                row_to_write = {k: v for k, v in raw.items() if k != "id"}
                new_rows.append(row_to_write)
                if pair_id:
                    already_imported.add(pair_id)

            if new_rows:
                appended = await self._datasets.append_jsonl(dataset_name, new_rows)
                total_pulled += appended
                result.rows_pulled += appended

            # Advance cursor to the max id seen (whether or not we kept each row).
            if max_id_in_page > cursor:
                cursor = max_id_in_page
                await self._db.execute(
                    "UPDATE lean_ai_ingest_state "
                    "SET last_cursor = ?, rows_imported = ?, updated_at = ? "
                    "WHERE workspace_id = ? AND format = ? AND pair_kind = ?",
                    (
                        cursor,
                        state["rows_imported"] + total_pulled,
                        datetime.now(UTC).isoformat(),
                        workspace_id,
                        DatasetFormat.DPO.value,
                        pair_kind,
                    ),
                )
                await self._db.commit()

            if len(rows) < page_limit:
                break

        return total_pulled

    async def _load_pair_ids(self, dataset_name: str) -> set[str]:
        """Read existing pair_ids from the dataset file so we can dedup."""
        path = await self._datasets.get_path(dataset_name)
        if not path:
            return set()
        ids: set[str] = set()
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    pid = obj.get("pair_id")
                    if pid:
                        ids.add(pid)
        except FileNotFoundError:
            pass
        return ids

    # ------------------------------------------------------------------
    # Internal: HTTP
    # ------------------------------------------------------------------

    async def _verify_workspace_id(
        self,
        backend_url: str,
        export_key: str,
        repo_root: str,
        *,
        claimed_id: str,
    ) -> None:
        """Confirm ``workspace_id`` matches ``hash(salt, repo_root)`` on the remote."""
        try:
            resp = await self._http.get(
                f"{backend_url}/api/export/workspace-id",
                params={"repo_root": repo_root},
                headers={"Authorization": f"Bearer {export_key}"},
            )
        except httpx.HTTPError as exc:
            raise IngestError(
                f"Cannot reach {backend_url}/api/export/workspace-id: {exc!r}"
            ) from exc
        if resp.status_code == 401:
            raise IngestError("Export key rejected (401)")
        if resp.status_code == 403:
            raise IngestError("Export key lacks permission (403)")
        if resp.status_code >= 400:
            raise IngestError(
                f"workspace-id probe failed ({resp.status_code}): {resp.text[:200]}"
            )
        try:
            returned = resp.json().get("workspace_id")
        except ValueError as exc:
            raise IngestError(
                f"workspace-id endpoint returned non-JSON: {resp.text[:200]}"
            ) from exc
        if not returned:
            raise IngestError("workspace-id response missing 'workspace_id' field")
        if returned != claimed_id:
            raise IngestError(
                "workspace_id mismatch — remote computed "
                f"'{returned}' from repo_root='{repo_root}' but the registration "
                f"claimed '{claimed_id}'. Use the value returned by "
                f"GET /api/export/workspace-id on the remote."
            )

    async def _fetch_page(
        self,
        *,
        backend_url: str,
        repo_root: str,
        export_key: str,
        cursor: int,
        limit: int,
    ) -> list[dict]:
        resp = await self._http.get(
            f"{backend_url}/api/export/traces",
            params={
                "repo_root": repo_root,
                "format": "dpo",
                "cursor": cursor,
                "limit": limit,
            },
            headers={"Authorization": f"Bearer {export_key}"},
        )
        if resp.status_code == 401:
            raise IngestError("Export key rejected (401)")
        if resp.status_code >= 400:
            raise IngestError(
                f"Fetch failed ({resp.status_code}): {resp.text[:200]}"
            )
        return _parse_jsonl_response(resp)

    async def _record_poll_result(
        self, workspace_id: str, *, error: str | None
    ) -> None:
        await self._db.execute(
            "UPDATE lean_ai_workspaces "
            "SET last_polled_at = ?, last_error = ? "
            "WHERE workspace_id = ?",
            (datetime.now(UTC).isoformat(), error, workspace_id),
        )
        await self._db.commit()


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _dataset_name(workspace_id: str, pair_kind: str) -> str:
    return f"lean_ai:{workspace_id}:dpo:{pair_kind}"


def _coerce_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_jsonl_response(resp: httpx.Response) -> list[dict]:
    """Parse a lean_ai export response as JSONL (fallback: JSON array)."""
    ctype = resp.headers.get("content-type", "")
    text = resp.text
    if "json" in ctype and text.lstrip().startswith("["):
        data = resp.json()
        return [r for r in data if isinstance(r, dict)]
    out: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


__all__ = ["IngestError", "LeanAiIngestor", "PAIR_KINDS"]
