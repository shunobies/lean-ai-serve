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
import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import Any

import httpx

from lean_ai_serve.config import Settings
from lean_ai_serve.db import (
    Database,
    lean_ai_ingest_state_table,
    lean_ai_stream_cursor_table,
)
from lean_ai_serve.training.datasets import DatasetManager
from lean_ai_serve.training.schemas import (
    DatasetFormat,
    IngestResult,
    IngestState,
    PurgeResult,
    WorkspaceInfo,
)

logger = logging.getLogger(__name__)

# Pair kinds emitted by lean_ai's DPO export. Seeded datasets/state rows are
# pre-created at registration for these so operators see placeholders before
# any data arrives. New pair_kinds discovered in the stream are added on
# first sighting — per the protocol's "fall through on unknown values" rule
# (see /home/alex/Code/lean_ai/docs/training-ingestion.md §6).
KNOWN_PAIR_KINDS: tuple[str, ...] = ("plan_rejection", "validation_fix")

# Public alias retained for callers that imported the original symbol.
PAIR_KINDS = KNOWN_PAIR_KINDS

# Maximum producer ``schema_version`` this consumer understands. The producer
# does not emit schema_version today; when it starts, we error on a *lower*
# value (downgrade = breaking removal by contract) and log on a higher one.
SUPPORTED_SCHEMA_VERSION = 1

# Stream identifiers (values stored in lean_ai_stream_cursor.format).
STREAM_DPO_TRACES = "dpo_traces"
STREAM_DPO_TOOL_EXECUTIONS = "dpo_tool_executions"
STREAM_SFT_PHASE2 = "sft_phase2"
STREAM_SFT_CLARIFICATIONS = "sft_clarifications"
STREAM_KTO_DIFF_DECISIONS = "kto_diff_decisions"
STREAM_EVENTS = "events"
STREAM_MEMORIES = "memories"

# Per-stream dataset name suffix. DPO traces keep per-pair_kind datasets (see
# _dataset_name) so they're not in this map.
_AUX_DATASET_SUFFIX: dict[str, str] = {
    STREAM_DPO_TOOL_EXECUTIONS: "dpo:tool_calls",
    STREAM_SFT_PHASE2: "sft:phase2",
    STREAM_SFT_CLARIFICATIONS: "sft:clarifications",
    STREAM_KTO_DIFF_DECISIONS: "kto:diff_decisions",
    STREAM_EVENTS: "events",
    STREAM_MEMORIES: "memories",
}

# Dataset format per aux stream.
_AUX_DATASET_FORMAT: dict[str, DatasetFormat] = {
    STREAM_DPO_TOOL_EXECUTIONS: DatasetFormat.DPO,
    STREAM_SFT_PHASE2: DatasetFormat.JSONL,
    STREAM_SFT_CLARIFICATIONS: DatasetFormat.JSONL,
    STREAM_KTO_DIFF_DECISIONS: DatasetFormat.JSONL,
    STREAM_EVENTS: DatasetFormat.JSONL,
    STREAM_MEMORIES: DatasetFormat.JSONL,
}

# Manifest count key that gates each aux stream. If the current manifest
# reports the same count as the last-pulled snapshot, we skip the fetch.
# ``memories`` is special: its count lives at ``manifest['memories']['total']``.
_AUX_MANIFEST_COUNT_KEY: dict[str, str] = {
    STREAM_DPO_TOOL_EXECUTIONS: "tool_executions",
    STREAM_SFT_PHASE2: "phase2_syntheses",
    STREAM_SFT_CLARIFICATIONS: "clarifications",
    STREAM_KTO_DIFF_DECISIONS: "diff_decisions",
    STREAM_EVENTS: "workflow_events",
    STREAM_MEMORIES: "memories.total",
}


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
        workspace_id: str | None = None,
        display_name: str,
        backend_url: str,
        repo_root: str,
        export_key: str,
        registered_by: str,
    ) -> WorkspaceInfo:
        """Validate the export key + workspace identity, then persist.

        If ``workspace_id`` is supplied, calls ``/api/export/workspace-id``
        and rejects on mismatch. If it's omitted, calls the same endpoint
        and adopts whatever id the remote returns — removing the most
        common registration footgun (the user hashing repo_root with a
        different salt than the remote). On success, creates one dataset
        per ``pair_kind`` plus one ``lean_ai_ingest_state`` row per
        ``pair_kind`` with ``last_cursor=0``. Re-registering the same
        workspace_id rotates the export key, display name, and repo_root
        but leaves cursors untouched.
        """
        backend_url = backend_url.rstrip("/")
        if not repo_root:
            raise IngestError("repo_root is required (passed as ?repo_root= on every /api/export/*)")
        resolved_id = await self._resolve_workspace_id(
            backend_url, export_key, repo_root, claimed_id=workspace_id,
        )
        workspace_id = resolved_id

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

        # Ensure one dataset + state row per known pair_kind exists for the
        # DPO traces stream (discovery will add more on first sighting).
        for pair_kind in KNOWN_PAIR_KINDS:
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

        # Pre-create placeholder datasets for each aux stream so operators
        # can see the full registry for the workspace before data arrives.
        for stream_key in _AUX_DATASET_SUFFIX:
            await self._ensure_aux_dataset(
                workspace_id=workspace_id,
                stream_key=stream_key,
                registered_by=registered_by,
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
            await self._datasets.delete(_eval_dataset_name(s["dataset_name"]))
        # Aux-stream datasets (memories, events, phase2, clarifications,
        # diff-decisions, tool-call DPO) — delete even if ingest never wrote.
        for stream_key in _AUX_DATASET_SUFFIX:
            main = _aux_dataset_name(workspace_id, stream_key)
            await self._datasets.delete(main)
            await self._datasets.delete(_eval_dataset_name(main))
        await self._db.execute(
            "DELETE FROM lean_ai_ingest_state WHERE workspace_id = ?",
            (workspace_id,),
        )
        await self._db.execute(
            "DELETE FROM lean_ai_stream_cursor WHERE workspace_id = ?",
            (workspace_id,),
        )
        await self._db.execute(
            "DELETE FROM lean_ai_workspaces WHERE workspace_id = ?",
            (workspace_id,),
        )
        await self._db.commit()
        return True

    async def enable_workspace(self, workspace_id: str) -> WorkspaceInfo | None:
        """Re-enable a soft-disabled workspace.

        Symmetric with ``delete_workspace(hard=False)``. Clears
        ``last_error`` so the row returns to a clean state. Returns the
        updated :class:`WorkspaceInfo`, or None if the workspace doesn't
        exist. Calling this on an already-enabled workspace is a no-op.
        """
        existing = await self._db.fetchone(
            "SELECT workspace_id FROM lean_ai_workspaces WHERE workspace_id = ?",
            (workspace_id,),
        )
        if existing is None:
            return None
        await self._db.execute(
            "UPDATE lean_ai_workspaces "
            "SET enabled = 1, last_error = NULL WHERE workspace_id = ?",
            (workspace_id,),
        )
        await self._db.commit()
        return await self.get_workspace(workspace_id)

    async def purge_workspace_data(
        self, workspace_id: str,
    ) -> PurgeResult | None:
        """Wipe ingested data for a workspace but keep the registration.

        Third delete mode between soft-disable (which stops pulls but
        preserves data) and hard-delete (which removes the workspace
        entirely). Use for data rotation or a user's right-to-revoke
        request: the workspace row, encrypted export key, display name,
        and repo_root all survive, but every dataset file is truncated
        and every cursor reset so the next poll re-pulls from scratch.

        Returns None if the workspace doesn't exist (caller should 404).
        Acquires the same poll lock the background scheduler uses, so
        purges and in-flight polls can't interleave.
        """
        existing = await self._db.fetchone(
            "SELECT workspace_id FROM lean_ai_workspaces WHERE workspace_id = ?",
            (workspace_id,),
        )
        if existing is None:
            return None

        async with self._poll_lock:
            result = PurgeResult(workspace_id=workspace_id, rows_purged=0)

            # Collect every dataset that could hold this workspace's data.
            # Per-pair_kind rows live in lean_ai_ingest_state (including any
            # discovered pair_kinds from the fall-through rule); aux streams
            # are named deterministically from _AUX_DATASET_SUFFIX.
            pair_rows = await self._db.fetchall(
                "SELECT dataset_name FROM lean_ai_ingest_state "
                "WHERE workspace_id = ?",
                (workspace_id,),
            )
            main_names = [r["dataset_name"] for r in pair_rows]
            main_names.extend(
                _aux_dataset_name(workspace_id, k) for k in _AUX_DATASET_SUFFIX
            )

            for name in main_names:
                if await self._truncate_if_exists(name, result):
                    result.datasets_cleared.append(name)
                eval_name = _eval_dataset_name(name)
                if await self._truncate_if_exists(eval_name, result):
                    result.datasets_cleared.append(eval_name)

            now = datetime.now(UTC).isoformat()
            # Reset per-pair_kind counters and cursors.
            await self._db.execute(
                "UPDATE lean_ai_ingest_state "
                "SET rows_imported = 0, last_cursor = 0, updated_at = ? "
                "WHERE workspace_id = ?",
                (now, workspace_id),
            )
            # Reset stream cursors (both id and since forms, plus the memories
            # snapshot hash).
            await self._db.execute(
                "UPDATE lean_ai_stream_cursor "
                "SET last_cursor = 0, last_cursor_since = NULL, "
                "    last_snapshot_hash = NULL, updated_at = ? "
                "WHERE workspace_id = ?",
                (now, workspace_id),
            )
            # Drop the manifest snapshot so the next poll's gate doesn't
            # short-circuit on pre-purge counts.
            await self._db.execute(
                "UPDATE lean_ai_workspaces "
                "SET last_manifest_snapshot = NULL, last_error = NULL "
                "WHERE workspace_id = ?",
                (workspace_id,),
            )
            await self._db.commit()

        logger.info(
            "Purged %d rows across %d datasets for workspace %s",
            result.rows_purged, len(result.datasets_cleared), workspace_id,
        )
        return result

    async def _truncate_if_exists(
        self, dataset_name: str, result: PurgeResult,
    ) -> bool:
        """Empty a dataset file in place if it exists and has rows.

        Returns True iff rows were actually purged — so ``datasets_cleared``
        only lists datasets whose content meaningfully changed. An already-
        empty placeholder is left untouched (no mtime bump, not reported).
        """
        ds = await self._datasets.get(dataset_name)
        if ds is None:
            return False
        count = ds.row_count or 0
        if count == 0:
            return False
        result.rows_purged += count
        await self._datasets.replace_jsonl(dataset_name, [])
        return True

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------

    async def poll_workspace(self, workspace_id: str) -> IngestResult:
        """Pull any new DPO rows from a single workspace and append to datasets.

        Does one paginated fetch of ``/traces?format=dpo`` and fans rows out
        to their per-``pair_kind`` datasets, creating new datasets for
        unknown pair_kinds (per protocol §6 "fall through on unknown"). Skips
        the fetch entirely if ``/manifest`` shows no new DPO rows since the
        previous poll — saves one round-trip per idle workspace × cycle.
        """
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
            manifest = await self._fetch_manifest(backend_url, repo_root, export_key)
            self._check_schema_version(manifest)
            prev_snapshot = self._load_prev_manifest(row)

            # DPO traces (per-pair_kind fan-out, id cursor).
            if self._dpo_traces_changed(prev_snapshot, manifest):
                datasets_written = await self._poll_dpo_stream(
                    workspace_id=workspace_id,
                    backend_url=backend_url,
                    repo_root=repo_root,
                    export_key=export_key,
                    registered_by=row["registered_by"],
                    result=result,
                )
                result.datasets_updated.extend(sorted(datasets_written))

            # Aux streams that share the since-cursor+append pattern.
            for stream_key, endpoint_path, extra_params, dedup_fn in (
                (
                    STREAM_DPO_TOOL_EXECUTIONS,
                    "/api/export/tool-executions",
                    {"format": "dpo_pairs"},
                    _dedup_tool_pair,
                ),
                (
                    STREAM_SFT_PHASE2,
                    "/api/export/phase2-syntheses",
                    {},
                    _dedup_trace_uuid_required,
                ),
                (
                    STREAM_SFT_CLARIFICATIONS,
                    "/api/export/clarifications",
                    {},
                    _dedup_clarification,
                ),
                (
                    STREAM_KTO_DIFF_DECISIONS,
                    "/api/export/diff-decisions",
                    {},
                    _dedup_diff_decision,
                ),
                (
                    STREAM_EVENTS,
                    "/api/export/events",
                    {},
                    _dedup_event,
                ),
            ):
                if not self._aux_count_changed(
                    prev_snapshot, manifest, stream_key,
                ):
                    continue
                updated = await self._poll_since_stream(
                    workspace_id=workspace_id,
                    backend_url=backend_url,
                    repo_root=repo_root,
                    export_key=export_key,
                    registered_by=row["registered_by"],
                    stream_key=stream_key,
                    endpoint_path=endpoint_path,
                    extra_params=extra_params,
                    dedup_fn=dedup_fn,
                    result=result,
                )
                if updated:
                    result.datasets_updated.append(updated)

            # Memories is snapshot-only — no cursor. The count gate is
            # unreliable here because an edit to an existing memory doesn't
            # change the total; the internal payload-hash check is the real
            # skip mechanism, and it's cheap (the endpoint caps at 5000 rows).
            mem_updated = await self._poll_memories_snapshot(
                workspace_id=workspace_id,
                backend_url=backend_url,
                repo_root=repo_root,
                export_key=export_key,
                registered_by=row["registered_by"],
                result=result,
            )
            if mem_updated:
                result.datasets_updated.append(mem_updated)

            await self._persist_manifest(workspace_id, manifest)
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

    async def forward_diff_decision(
        self,
        workspace_id: str,
        *,
        session_id: str,
        file_path: str,
        accepted: bool,
        diff_hash: str | None = None,
        note: str | None = None,
        trace_uuid: str | None = None,
    ) -> dict:
        """POST a user accept/reject to the workspace's /api/diffs/decision.

        The coordinator can act as a single ingress for extensions that
        don't know (or shouldn't know) the individual workspace URLs.
        The producer's endpoint takes ``repo_root`` in the body and does
        not require auth — we supply the registered repo_root.
        """
        row = await self._db.fetchone(
            "SELECT backend_url, repo_root, enabled FROM lean_ai_workspaces "
            "WHERE workspace_id = ?",
            (workspace_id,),
        )
        if row is None:
            raise IngestError(f"Unknown workspace: {workspace_id}")
        if not row["enabled"]:
            raise IngestError(f"Workspace disabled: {workspace_id}")
        if not row["repo_root"]:
            raise IngestError(
                f"Workspace {workspace_id} is missing repo_root — re-register it"
            )

        body = {
            "repo_root": row["repo_root"],
            "session_id": session_id,
            "file_path": file_path,
            "accepted": accepted,
        }
        if diff_hash is not None:
            body["diff_hash"] = diff_hash
        if note is not None:
            body["note"] = note
        if trace_uuid is not None:
            body["trace_uuid"] = trace_uuid

        try:
            resp = await self._http.post(
                f"{row['backend_url']}/api/diffs/decision", json=body,
            )
        except httpx.HTTPError as exc:
            raise IngestError(
                f"Cannot reach {row['backend_url']}/api/diffs/decision: {exc!r}"
            ) from exc
        if resp.status_code >= 400:
            raise IngestError(
                f"Forward failed ({resp.status_code}): {resp.text[:200]}"
            )
        try:
            return resp.json()
        except ValueError:
            return {"stored": True}

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
    # Internal: single-fetch DPO multiplex
    # ------------------------------------------------------------------

    async def _poll_dpo_stream(
        self,
        *,
        workspace_id: str,
        backend_url: str,
        repo_root: str,
        export_key: str,
        registered_by: str,
        result: IngestResult,
    ) -> set[str]:
        """Pull /traces?format=dpo once and route rows to the right dataset.

        Advances a single per-(workspace_id, format) cursor in
        ``lean_ai_stream_cursor`` regardless of how many pair_kinds the
        stream contains. Per-pair_kind ``rows_imported`` totals are kept in
        ``lean_ai_ingest_state`` so the UI can show per-kind progress.
        """
        fmt = DatasetFormat.DPO.value
        cursor = await self._get_stream_cursor(workspace_id, fmt)
        existing_state = await self._load_pair_kind_state(workspace_id, fmt)
        dedup_cache: dict[str, set[str]] = {}
        kinds_written: set[str] = set()
        page_limit = max(1, int(self._settings.ingestion.page_limit))

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

            by_kind: dict[str, list[dict]] = {}
            unknown_kinds: set[str] = set()
            max_id_in_page = cursor

            for raw in rows:
                row_id = _coerce_int(raw.get("id"))
                if row_id is not None and row_id > max_id_in_page:
                    max_id_in_page = row_id

                pair_kind = raw.get("pair_kind")
                if not pair_kind:
                    continue
                if pair_kind not in existing_state:
                    unknown_kinds.add(pair_kind)

                pair_id = raw.get("pair_id")
                if pair_id:
                    seen = dedup_cache.get(pair_kind)
                    if seen is None:
                        ds_name = existing_state.get(pair_kind, {}).get(
                            "dataset_name"
                        ) or _dataset_name(workspace_id, pair_kind)
                        seen = await self._load_pair_ids(ds_name)
                        dedup_cache[pair_kind] = seen
                    if pair_id in seen:
                        continue
                    seen.add(pair_id)

                by_kind.setdefault(pair_kind, []).append(
                    {k: v for k, v in raw.items() if k != "id"}
                )

            # Create datasets / state rows for any newly-seen pair_kinds
            # before writing so the append+bookkeeping step can assume they
            # exist. Unknown kinds get a seeded dataset name matching the
            # known-kind convention.
            for pair_kind in unknown_kinds:
                await self._ensure_pair_kind_state(
                    workspace_id=workspace_id,
                    pair_kind=pair_kind,
                    registered_by=registered_by,
                )
                existing_state[pair_kind] = {
                    "dataset_name": _dataset_name(workspace_id, pair_kind),
                    "rows_imported": 0,
                }
                logger.info(
                    "Discovered new pair_kind '%s' for workspace %s — created "
                    "dataset and state row",
                    pair_kind, workspace_id,
                )

            for pair_kind, new_rows in by_kind.items():
                if not new_rows:
                    continue
                dataset_name = existing_state[pair_kind]["dataset_name"]
                appended = await self._append_with_holdout(
                    workspace_id,
                    dataset_name,
                    new_rows,
                    _dedup_pair_id,
                    registered_by=registered_by,
                )
                if appended:
                    existing_state[pair_kind]["rows_imported"] += appended
                    result.rows_pulled += appended
                    kinds_written.add(dataset_name)
                    await self._db.execute(
                        "UPDATE lean_ai_ingest_state "
                        "SET rows_imported = ?, updated_at = ? "
                        "WHERE workspace_id = ? AND format = ? AND pair_kind = ?",
                        (
                            existing_state[pair_kind]["rows_imported"],
                            datetime.now(UTC).isoformat(),
                            workspace_id,
                            fmt,
                            pair_kind,
                        ),
                    )

            if max_id_in_page > cursor:
                cursor = max_id_in_page
                await self._save_stream_cursor(workspace_id, fmt, cursor)
                # Mirror the shared cursor onto each pair_kind state row so
                # WorkspaceInfo.ingest[].last_cursor stays informative.
                await self._db.execute(
                    "UPDATE lean_ai_ingest_state "
                    "SET last_cursor = ? WHERE workspace_id = ? AND format = ?",
                    (cursor, workspace_id, fmt),
                )
                await self._db.commit()

            if len(rows) < page_limit:
                break

        return kinds_written

    async def _get_stream_cursor(self, workspace_id: str, fmt: str) -> int:
        row = await self._db.fetchone(
            "SELECT last_cursor FROM lean_ai_stream_cursor "
            "WHERE workspace_id = ? AND format = ?",
            (workspace_id, fmt),
        )
        if row is not None:
            return int(row["last_cursor"])
        # Backfill from the max per-pair_kind cursor when migrating from the
        # pre-P1 schema — keeps no-op polls after upgrade.
        legacy = await self._db.fetchone(
            "SELECT MAX(last_cursor) AS c FROM lean_ai_ingest_state "
            "WHERE workspace_id = ? AND format = ?",
            (workspace_id, fmt),
        )
        initial = int(legacy["c"] or 0) if legacy is not None else 0
        await self._db.upsert(
            lean_ai_stream_cursor_table,
            values={
                "workspace_id": workspace_id,
                "format": fmt,
                "last_cursor": initial,
                "updated_at": datetime.now(UTC).isoformat(),
            },
            conflict_columns=["workspace_id", "format"],
            on_conflict="ignore",
        )
        return initial

    async def _save_stream_cursor(
        self, workspace_id: str, fmt: str, cursor: int
    ) -> None:
        await self._db.execute(
            "UPDATE lean_ai_stream_cursor "
            "SET last_cursor = ?, updated_at = ? "
            "WHERE workspace_id = ? AND format = ?",
            (cursor, datetime.now(UTC).isoformat(), workspace_id, fmt),
        )

    async def _load_pair_kind_state(
        self, workspace_id: str, fmt: str
    ) -> dict[str, dict]:
        rows = await self._db.fetchall(
            "SELECT pair_kind, dataset_name, rows_imported "
            "FROM lean_ai_ingest_state "
            "WHERE workspace_id = ? AND format = ?",
            (workspace_id, fmt),
        )
        return {
            r["pair_kind"]: {
                "dataset_name": r["dataset_name"],
                "rows_imported": r["rows_imported"],
            }
            for r in rows
        }

    # ------------------------------------------------------------------
    # Aux-stream polling (tool-executions, phase2, clarifications,
    # diff-decisions, events) — all share since=<iso8601> pagination.
    # ------------------------------------------------------------------

    async def _poll_since_stream(
        self,
        *,
        workspace_id: str,
        backend_url: str,
        repo_root: str,
        export_key: str,
        registered_by: str,
        stream_key: str,
        endpoint_path: str,
        extra_params: dict[str, str],
        dedup_fn,
        result: IngestResult,
    ) -> str | None:
        """Paginate a ``since=<iso8601>`` stream and append to its dataset.

        ``dedup_fn(row) -> key`` computes a dedup key per row. Rows whose
        key is already present in the on-disk dataset are skipped so
        re-polls after a ``since=`` cursor that clips at the start of a
        second don't double-write. Returns the dataset name if any rows
        were appended, else None.
        """
        dataset_name = _aux_dataset_name(workspace_id, stream_key)
        await self._ensure_aux_dataset(
            workspace_id=workspace_id,
            stream_key=stream_key,
            registered_by=registered_by,
        )
        seen_keys = await self._load_dedup_keys(dataset_name, dedup_fn)

        cursor_row = await self._db.fetchone(
            "SELECT last_cursor_since FROM lean_ai_stream_cursor "
            "WHERE workspace_id = ? AND format = ?",
            (workspace_id, stream_key),
        )
        since: str | None = cursor_row["last_cursor_since"] if cursor_row else None

        page_limit = _stream_page_limit(stream_key, self._settings.ingestion.page_limit)
        appended_total = 0
        latest_seen = since

        while True:
            params: dict[str, Any] = dict(extra_params)
            params["repo_root"] = repo_root
            params["limit"] = page_limit
            if since:
                params["since"] = since

            resp = await self._http.get(
                f"{backend_url}{endpoint_path}",
                params=params,
                headers={"Authorization": f"Bearer {export_key}"},
            )
            if resp.status_code == 401:
                raise IngestError("Export key rejected (401)")
            if resp.status_code >= 400:
                raise IngestError(
                    f"{endpoint_path} failed ({resp.status_code}): "
                    f"{resp.text[:200]}"
                )
            rows = _parse_jsonl_response(resp)
            if not rows:
                break

            new_rows: list[dict] = []
            for raw in rows:
                created = raw.get("created_at")
                if isinstance(created, str) and (
                    latest_seen is None or created > latest_seen
                ):
                    latest_seen = created
                key = dedup_fn(raw)
                if key is None:
                    # Rows without a dedup key still count — ambiguous rows
                    # are rare and leaving them dedup-free means one extra
                    # copy after a cursor clip, which is preferable to
                    # dropping real data.
                    new_rows.append({k: v for k, v in raw.items() if k != "id"})
                    continue
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                new_rows.append({k: v for k, v in raw.items() if k != "id"})

            if new_rows:
                appended = await self._append_with_holdout(
                    workspace_id,
                    dataset_name,
                    new_rows,
                    dedup_fn,
                    registered_by=registered_by,
                )
                appended_total += appended
                result.rows_pulled += appended

            if len(rows) < page_limit:
                break
            # Advance since-cursor so the next page doesn't resend the clip
            # we just consumed. Producer uses ``created_at >= since`` so the
            # cursor boundary is inclusive; dedup handles the re-sent row.
            if latest_seen and latest_seen != since:
                since = latest_seen
            else:
                # No forward progress — bail to avoid a tight loop.
                break

        # Persist cursor — use the pair_kind-less "dpo_tool_executions"
        # case as an anchor: its "since" is the wall-clock of this poll
        # because the producer's dpo_pairs stream doesn't echo created_at.
        next_cursor = latest_seen
        if stream_key == STREAM_DPO_TOOL_EXECUTIONS:
            next_cursor = datetime.now(UTC).isoformat()
        await self._save_since_cursor(workspace_id, stream_key, next_cursor)
        return dataset_name if appended_total else None

    async def _poll_memories_snapshot(
        self,
        *,
        workspace_id: str,
        backend_url: str,
        repo_root: str,
        export_key: str,
        registered_by: str,
        result: IngestResult,
    ) -> str | None:
        """Pull all curated memories and atomically replace the dataset.

        ``/api/export/memories`` has no cursor — it's a snapshot endpoint.
        We hash the payload and skip the replace if it's identical to the
        last pulled snapshot, so idle polls are cheap even without a
        server-side gate.
        """
        dataset_name = _aux_dataset_name(workspace_id, STREAM_MEMORIES)
        await self._ensure_aux_dataset(
            workspace_id=workspace_id,
            stream_key=STREAM_MEMORIES,
            registered_by=registered_by,
        )

        resp = await self._http.get(
            f"{backend_url}/api/export/memories",
            params={"repo_root": repo_root, "limit": 5000},
            headers={"Authorization": f"Bearer {export_key}"},
        )
        if resp.status_code == 401:
            raise IngestError("Export key rejected (401)")
        if resp.status_code >= 400:
            raise IngestError(
                f"/api/export/memories failed ({resp.status_code}): "
                f"{resp.text[:200]}"
            )
        rows = _parse_jsonl_response(resp)

        # Hash the rendered body so we can skip an unchanged snapshot.
        digest = hashlib.sha256(
            "".join(
                json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n"
                for r in rows
            ).encode("utf-8")
        ).hexdigest()

        prev = await self._db.fetchone(
            "SELECT last_snapshot_hash FROM lean_ai_stream_cursor "
            "WHERE workspace_id = ? AND format = ?",
            (workspace_id, STREAM_MEMORIES),
        )
        prev_hash = prev["last_snapshot_hash"] if prev else None
        if prev_hash == digest:
            return None

        written = await self._replace_with_holdout(
            workspace_id,
            dataset_name,
            rows,
            _dedup_memory,
            registered_by=registered_by,
        )
        result.rows_pulled += written
        await self._save_snapshot_hash(workspace_id, STREAM_MEMORIES, digest)
        # Only list as updated if the snapshot actually has rows — an
        # empty-to-empty first poll shouldn't show up in datasets_updated.
        return dataset_name if written else None

    async def _save_since_cursor(
        self, workspace_id: str, stream_key: str, since: str | None,
    ) -> None:
        await self._db.upsert(
            lean_ai_stream_cursor_table,
            values={
                "workspace_id": workspace_id,
                "format": stream_key,
                "last_cursor": 0,
                "last_cursor_since": since,
                "updated_at": datetime.now(UTC).isoformat(),
            },
            conflict_columns=["workspace_id", "format"],
            on_conflict="update",
            update_columns=["last_cursor_since", "updated_at"],
        )
        await self._db.commit()

    async def _save_snapshot_hash(
        self, workspace_id: str, stream_key: str, digest: str,
    ) -> None:
        await self._db.upsert(
            lean_ai_stream_cursor_table,
            values={
                "workspace_id": workspace_id,
                "format": stream_key,
                "last_cursor": 0,
                "last_snapshot_hash": digest,
                "updated_at": datetime.now(UTC).isoformat(),
            },
            conflict_columns=["workspace_id", "format"],
            on_conflict="update",
            update_columns=["last_snapshot_hash", "updated_at"],
        )
        await self._db.commit()

    async def _append_with_holdout(
        self,
        workspace_id: str,
        dataset_name: str,
        rows: list[dict],
        dedup_fn,
        *,
        registered_by: str,
    ) -> int:
        """Append rows, routing a deterministic fraction to the :eval sibling.

        Returns the total number of rows appended (train + eval). When
        ``holdout_fraction`` is 0, behaves identically to
        ``_datasets.append_jsonl`` on the main dataset.
        """
        fraction = self._holdout_fraction()
        if fraction <= 0.0 or not rows:
            return await self._datasets.append_jsonl(dataset_name, rows)

        train_rows: list[dict] = []
        eval_rows: list[dict] = []
        for row in rows:
            key = dedup_fn(row) if dedup_fn else None
            if self._row_in_holdout(workspace_id, dataset_name, key, row, fraction):
                eval_rows.append(row)
            else:
                train_rows.append(row)

        total = 0
        if train_rows:
            total += await self._datasets.append_jsonl(dataset_name, train_rows)
        if eval_rows:
            eval_name = _eval_dataset_name(dataset_name)
            await self._ensure_eval_dataset(
                main_name=dataset_name,
                eval_name=eval_name,
                registered_by=registered_by,
            )
            total += await self._datasets.append_jsonl(eval_name, eval_rows)
        return total

    async def _replace_with_holdout(
        self,
        workspace_id: str,
        dataset_name: str,
        rows: list[dict],
        dedup_fn,
        *,
        registered_by: str,
    ) -> int:
        """Snapshot-replace variant used by memories. Splits atomically."""
        fraction = self._holdout_fraction()
        if fraction <= 0.0:
            return await self._datasets.replace_jsonl(dataset_name, rows)

        train_rows: list[dict] = []
        eval_rows: list[dict] = []
        for row in rows:
            key = dedup_fn(row) if dedup_fn else None
            if self._row_in_holdout(workspace_id, dataset_name, key, row, fraction):
                eval_rows.append(row)
            else:
                train_rows.append(row)

        total = await self._datasets.replace_jsonl(dataset_name, train_rows)
        eval_name = _eval_dataset_name(dataset_name)
        await self._ensure_eval_dataset(
            main_name=dataset_name,
            eval_name=eval_name,
            registered_by=registered_by,
        )
        total += await self._datasets.replace_jsonl(eval_name, eval_rows)
        return total

    def _holdout_fraction(self) -> float:
        raw = float(self._settings.ingestion.holdout_fraction or 0.0)
        # Clamp to [0.0, 0.5] — more than half the data going to eval means
        # the coordinator is the wrong place for the split.
        return max(0.0, min(0.5, raw))

    def _row_in_holdout(
        self,
        workspace_id: str,
        dataset_name: str,
        dedup_key: str | None,
        row: dict,
        fraction: float,
    ) -> bool:
        salt = self._settings.ingestion.holdout_salt or ""
        # A stable bucketing identity: workspace + row identity. If the row
        # has no dedup key (rare), fall back to a sorted-json digest so the
        # decision is at least content-stable.
        identity = dedup_key or json.dumps(row, sort_keys=True, default=str)
        digest = hashlib.sha256(
            f"{salt}|{workspace_id}|{dataset_name}|{identity}".encode("utf-8")
        ).digest()
        # Take 8 bytes as an integer, map to [0.0, 1.0).
        bucket = int.from_bytes(digest[:8], "big") / float(1 << 64)
        return bucket < fraction

    async def _ensure_eval_dataset(
        self, *, main_name: str, eval_name: str, registered_by: str,
    ) -> None:
        existing = await self._datasets.get(eval_name)
        if existing is not None:
            return
        main = await self._datasets.get(main_name)
        fmt = main.format if main else DatasetFormat.JSONL
        with contextlib.suppress(ValueError):
            await self._datasets.create_empty_jsonl(
                name=eval_name,
                fmt=fmt,
                uploaded_by=registered_by,
                source=f"holdout-of:{main_name}",
                description=(
                    f"Eval holdout rows (deterministic) for {main_name}"
                ),
            )

    async def _ensure_aux_dataset(
        self, *, workspace_id: str, stream_key: str, registered_by: str,
    ) -> None:
        name = _aux_dataset_name(workspace_id, stream_key)
        existing = await self._datasets.get(name)
        if existing is not None:
            return
        fmt = _AUX_DATASET_FORMAT[stream_key]
        with contextlib.suppress(ValueError):
            await self._datasets.create_empty_jsonl(
                name=name,
                fmt=fmt,
                uploaded_by=registered_by,
                source=f"lean_ai:{workspace_id}:{stream_key}",
                description=(
                    f"{fmt.value} rows auto-ingested from lean_ai "
                    f"workspace {workspace_id} ({stream_key})"
                ),
            )

    async def _load_dedup_keys(
        self, dataset_name: str, dedup_fn,
    ) -> set[str]:
        path = await self._datasets.get_path(dataset_name)
        if not path:
            return set()
        keys: set[str] = set()
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
                    k = dedup_fn(obj)
                    if k is not None:
                        keys.add(k)
        except FileNotFoundError:
            pass
        return keys

    async def _ensure_pair_kind_state(
        self, *, workspace_id: str, pair_kind: str, registered_by: str,
    ) -> None:
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
                "updated_at": datetime.now(UTC).isoformat(),
            },
            conflict_columns=["workspace_id", "format", "pair_kind"],
            on_conflict="ignore",
        )

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

    async def _fetch_manifest(
        self, backend_url: str, repo_root: str, export_key: str,
    ) -> dict:
        """Pull /api/export/manifest for gating + schema-version checks."""
        try:
            resp = await self._http.get(
                f"{backend_url}/api/export/manifest",
                params={"repo_root": repo_root},
                headers={"Authorization": f"Bearer {export_key}"},
            )
        except httpx.HTTPError as exc:
            raise IngestError(
                f"Cannot reach {backend_url}/api/export/manifest: {exc!r}"
            ) from exc
        if resp.status_code == 401:
            raise IngestError("Export key rejected (401)")
        if resp.status_code >= 400:
            raise IngestError(
                f"Manifest fetch failed ({resp.status_code}): {resp.text[:200]}"
            )
        try:
            body = resp.json()
        except ValueError as exc:
            raise IngestError(
                f"Manifest returned non-JSON: {resp.text[:200]}"
            ) from exc
        if not isinstance(body, dict):
            raise IngestError("Manifest body must be a JSON object")
        return body

    def _check_schema_version(self, manifest: dict) -> None:
        """Warn on ahead-of-consumer schema; error on a known-breaking downgrade."""
        version = manifest.get("schema_version")
        if version is None:
            # Producer does not emit schema_version yet — treat as v1.
            return
        try:
            version_int = int(version)
        except (TypeError, ValueError):
            logger.warning(
                "Manifest schema_version is not an integer: %r", version
            )
            return
        if version_int < SUPPORTED_SCHEMA_VERSION:
            raise IngestError(
                f"Producer schema_version={version_int} is older than this "
                f"consumer supports (min={SUPPORTED_SCHEMA_VERSION}) — "
                "either upgrade the producer or pin an older lean_ai_serve."
            )
        if version_int > SUPPORTED_SCHEMA_VERSION:
            logger.warning(
                "Producer schema_version=%d is newer than this consumer "
                "understands (%d) — unknown fields will be preserved on raw "
                "exports but new columns will be ignored until upgrade.",
                version_int, SUPPORTED_SCHEMA_VERSION,
            )

    def _load_prev_manifest(self, workspace_row: Any) -> dict | None:
        """Return the previously-persisted manifest snapshot, or None."""
        raw = workspace_row["last_manifest_snapshot"]
        if not raw:
            return None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None

    def _dpo_traces_changed(
        self, prev: dict | None, manifest: dict,
    ) -> bool:
        """True if DPO-relevant counts differ from the prior snapshot.

        A missing snapshot or absent integer counts defaults to True so the
        first poll after upgrade actually runs.
        """
        if prev is None:
            return True
        keys = ("plan_decisions", "validation_attempts", "total_traces")
        current = {k: manifest.get(k) for k in keys}
        previous = {k: prev.get(k) for k in keys}
        has_counts = any(isinstance(v, int) for v in current.values()) and any(
            isinstance(v, int) for v in previous.values()
        )
        if not has_counts:
            return True
        return current != previous

    def _aux_count_changed(
        self, prev: dict | None, manifest: dict, stream_key: str,
    ) -> bool:
        """Gate for a single aux stream. Uses the stream's manifest count key."""
        count_key = _AUX_MANIFEST_COUNT_KEY[stream_key]
        current = _nested_get(manifest, count_key)
        if prev is None or not isinstance(current, int):
            return True
        previous = _nested_get(prev, count_key)
        if not isinstance(previous, int):
            return True
        return current != previous

    async def _persist_manifest(
        self, workspace_id: str, manifest: dict,
    ) -> None:
        # Persist just the per-table counts we gate on — small, stable, and
        # enough to drive the per-stream change-detection logic.
        snapshot_body: dict[str, Any] = {
            k: manifest.get(k)
            for k in (
                "plan_decisions", "validation_attempts", "total_traces",
                "tool_executions", "clarifications", "phase2_syntheses",
                "diff_decisions", "workflow_events",
            )
            if manifest.get(k) is not None
        }
        mem = manifest.get("memories")
        if isinstance(mem, dict) and isinstance(mem.get("total"), int):
            snapshot_body["memories"] = {"total": mem["total"]}
        snapshot = json.dumps(snapshot_body, separators=(",", ":"))
        schema_version = manifest.get("schema_version")
        try:
            schema_int: int | None = int(schema_version) if schema_version is not None else None
        except (TypeError, ValueError):
            schema_int = None
        await self._db.execute(
            "UPDATE lean_ai_workspaces "
            "SET last_manifest_snapshot = ?, last_schema_version = ? "
            "WHERE workspace_id = ?",
            (snapshot, schema_int, workspace_id),
        )
        await self._db.commit()

    async def _resolve_workspace_id(
        self,
        backend_url: str,
        export_key: str,
        repo_root: str,
        *,
        claimed_id: str | None,
    ) -> str:
        """Look up the remote's workspace_id and verify or adopt it.

        Calls ``GET /api/export/workspace-id?repo_root=...``. If
        ``claimed_id`` is set, rejects on mismatch; if it's None, adopts
        whatever the remote returns. Returns the authoritative id.
        """
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
        if claimed_id is None:
            return returned
        if returned != claimed_id:
            raise IngestError(
                "workspace_id mismatch — remote computed "
                f"'{returned}' from repo_root='{repo_root}' but the registration "
                f"claimed '{claimed_id}'. Use the value returned by "
                f"GET /api/export/workspace-id on the remote."
            )
        return returned

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


def _aux_dataset_name(workspace_id: str, stream_key: str) -> str:
    return f"lean_ai:{workspace_id}:{_AUX_DATASET_SUFFIX[stream_key]}"


def _eval_dataset_name(main_name: str) -> str:
    """Sibling dataset name used by the holdout split."""
    return f"{main_name}:eval"


def _nested_get(obj: dict, dotted_key: str) -> Any:
    """Look up ``foo.bar`` in a nested dict — used for manifest['memories']['total']."""
    parts = dotted_key.split(".")
    cur: Any = obj
    for p in parts:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(p)
    return cur


def _stream_page_limit(stream_key: str, default_limit: int) -> int:
    """Respect per-endpoint server-side caps documented in the producer."""
    caps: dict[str, int] = {
        STREAM_SFT_PHASE2: 2000,      # /phase2-syntheses enforces max 2000
        STREAM_SFT_CLARIFICATIONS: 5000,
        STREAM_KTO_DIFF_DECISIONS: 10000,
        STREAM_EVENTS: 10000,
        STREAM_DPO_TOOL_EXECUTIONS: 10000,
    }
    cap = caps.get(stream_key, default_limit)
    return max(1, min(default_limit, cap))


# ---- dedup key functions per stream ----


def _dedup_pair_id(row: dict) -> str | None:
    """DPO traces stream: pair_id is the stable identity."""
    pid = row.get("pair_id")
    return str(pid) if pid else None


def _dedup_memory(row: dict) -> str | None:
    """Memories have no globally-stable id — bucket on (category, content)."""
    parts = [
        str(row.get("category") or ""),
        str(row.get("content") or ""),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _dedup_tool_pair(row: dict) -> str | None:
    """(session_id, prompt_hint, chosen.arguments, rejected.arguments) — hashed."""
    chosen = row.get("chosen") or {}
    rejected = row.get("rejected") or {}
    parts = [
        str(row.get("session_id") or ""),
        str(row.get("prompt_hint") or ""),
        json.dumps(chosen.get("arguments"), sort_keys=True, default=str),
        json.dumps(rejected.get("arguments"), sort_keys=True, default=str),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _dedup_trace_uuid_required(row: dict) -> str | None:
    trace_uuid = row.get("trace_uuid")
    return str(trace_uuid) if trace_uuid else None


def _dedup_clarification(row: dict) -> str | None:
    if row.get("trace_uuid"):
        return f"trace:{row['trace_uuid']}"
    session = row.get("session_id") or ""
    question = row.get("question") or ""
    return f"sess:{session}|q:{hashlib.sha256(question.encode()).hexdigest()[:16]}"


def _dedup_diff_decision(row: dict) -> str | None:
    if row.get("diff_hash"):
        return f"diff:{row['diff_hash']}"
    if row.get("trace_uuid"):
        return f"trace:{row['trace_uuid']}:{row.get('file_path') or ''}"
    return None


def _dedup_event(row: dict) -> str | None:
    session = row.get("session_id") or ""
    event_type = row.get("event_type") or ""
    created = row.get("created_at") or ""
    if not (event_type and created):
        return None
    return f"{session}|{event_type}|{created}"


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
