"""Tests for the lean_ai DPO data ingestor."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import PlainTextResponse

from lean_ai_serve.config import Settings
from lean_ai_serve.db import Database
from lean_ai_serve.training.datasets import DatasetManager
from lean_ai_serve.training.lean_ai_ingest import (
    PAIR_KINDS,
    STREAM_DPO_TOOL_EXECUTIONS,
    STREAM_EVENTS,
    STREAM_KTO_DIFF_DECISIONS,
    STREAM_KTO_TRACES,
    STREAM_MEMORIES,
    STREAM_SFT_CLARIFICATIONS,
    STREAM_SFT_PHASE2,
    STREAM_SFT_TOOL_COMPRESSIONS,
    STREAM_SFT_TRACES,
    IngestError,
    LeanAiIngestor,
    _aux_dataset_name,
    _dataset_name,
)
from lean_ai_serve.training.schemas import DatasetFormat

# ---------------------------------------------------------------------------
# Fake lean_ai backend
# ---------------------------------------------------------------------------


class FakeLeanAi:
    """Minimal FastAPI app that mimics lean_ai's /api/export endpoints.

    Mirrors the real producer's contract: every endpoint requires
    ``?repo_root=<path>`` and ``/workspace-id`` returns the hash for that
    repo_root. Tests that pass a mismatched ``repo_root`` see the same
    422/mismatch the real backend would return.
    """

    def __init__(
        self, *, api_key: str = "test-key", repo_root: str = "/tmp/ws-abc",
        workspace_id: str = "ws-abc", schema_version: int = 1,
    ):
        self.api_key = api_key
        self.repo_root = repo_root
        self.workspace_id = workspace_id
        self.schema_version = schema_version
        self._rows: list[dict] = []
        # Aux-stream row buffers — one list per producer endpoint.
        self.sft_traces: list[dict] = []
        self.kto_traces: list[dict] = []
        self.tool_pairs: list[dict] = []
        self.tool_compressions: list[dict] = []
        self.phase2: list[dict] = []
        self.clarifications: list[dict] = []
        self.diff_decisions: list[dict] = []
        self.events: list[dict] = []
        self.memories: list[dict] = []
        self.received_diff_decisions: list[dict] = []
        # Every incoming request is recorded here so tests can assert on
        # traffic patterns (single fetch, query params, etc.).
        self.calls: list[dict] = []
        self.app = FastAPI()

        @self.app.middleware("http")
        async def record_call(request, call_next):
            self.calls.append({
                "path": request.url.path,
                "query": dict(request.query_params),
            })
            return await call_next(request)

        self._register_routes()

    def _register_routes(self) -> None:
        @self.app.get("/api/export/workspace-id")
        async def workspace_id_endpoint(
            repo_root: str, authorization: str = Header(""),
        ):
            self._auth(authorization)
            if repo_root != self.repo_root:
                # Real producer always hashes whatever path you pass, so return
                # a different id rather than an error — mismatch surfaces in
                # the ingestor's verification step.
                return {"workspace_id": f"other-{repo_root}"}
            return {"workspace_id": self.workspace_id}

        @self.app.get("/api/export/manifest")
        async def manifest(repo_root: str, authorization: str = Header("")):
            self._auth(authorization)
            # Mirror the real producer's count fields so the ingestor's
            # "skip when counts haven't moved" gate has something to read.
            # plan_decisions and validation_attempts map to their pair_kind
            # counts for a plausible approximation.
            plan_decisions = sum(
                1 for r in self._rows if r.get("pair_kind") == "plan_rejection"
            )
            validation_attempts = sum(
                1 for r in self._rows if r.get("pair_kind") == "validation_fix"
            )
            return {
                "schema_version": self.schema_version,
                "total_traces": len(self._rows),
                "plan_decisions": plan_decisions,
                "validation_attempts": validation_attempts,
                "tool_executions": len(self.tool_pairs),
                "tool_compressions": len(self.tool_compressions),
                "phase2_syntheses": len(self.phase2),
                "clarifications": len(self.clarifications),
                "diff_decisions": len(self.diff_decisions),
                "workflow_events": len(self.events),
                "memories": {"total": len(self.memories)},
                "workspace_id": self.workspace_id,
            }

        @self.app.get("/api/export/traces")
        async def traces(
            repo_root: str,
            authorization: str = Header(""),
            fmt: str = Query("dpo", alias="format"),
            cursor: int = Query(0),
            limit: int = Query(500),
        ):
            self._auth(authorization)
            if repo_root != self.repo_root:
                raise HTTPException(status_code=404, detail="unknown workspace")
            if fmt == "dpo":
                page = [r for r in self._rows if r["id"] > cursor][:limit]
            elif fmt == "sft":
                page = [r for r in self.sft_traces if r["id"] > cursor][:limit]
            elif fmt == "kto":
                page = [r for r in self.kto_traces if r["id"] > cursor][:limit]
            else:
                raise HTTPException(
                    status_code=400, detail=f"format '{fmt}' not supported in fake",
                )
            body = "\n".join(json.dumps(r) for r in page)
            return PlainTextResponse(body, media_type="application/x-ndjson")

        # Aux endpoints — each supports ?since=<iso8601> as the cursor.

        @self.app.get("/api/export/tool-executions")
        async def tool_executions(
            repo_root: str,
            authorization: str = Header(""),
            fmt: str = Query("dpo_pairs", alias="format"),
            since: str | None = Query(None),
            limit: int = Query(1000),
        ):
            self._auth(authorization)
            self._check_repo(repo_root)
            src = self.tool_pairs
            if since:
                src = [r for r in src if r.get("_ts", "") >= since]
            return self._jsonl(src[:limit])

        @self.app.get("/api/export/tool-compressions")
        async def tool_compressions(
            repo_root: str, authorization: str = Header(""),
            since: str | None = Query(None), limit: int = Query(1000),
        ):
            self._auth(authorization)
            self._check_repo(repo_root)
            src = self.tool_compressions
            if since:
                src = [r for r in src if r.get("created_at", "") >= since]
            return self._jsonl(src[:limit])

        @self.app.get("/api/export/phase2-syntheses")
        async def phase2(
            repo_root: str, authorization: str = Header(""),
            since: str | None = Query(None), limit: int = Query(500),
        ):
            self._auth(authorization)
            self._check_repo(repo_root)
            src = self.phase2
            if since:
                src = [r for r in src if r.get("created_at", "") >= since]
            return self._jsonl(src[:limit])

        @self.app.get("/api/export/clarifications")
        async def clarifications(
            repo_root: str, authorization: str = Header(""),
            since: str | None = Query(None), limit: int = Query(1000),
        ):
            self._auth(authorization)
            self._check_repo(repo_root)
            src = self.clarifications
            if since:
                src = [r for r in src if r.get("created_at", "") >= since]
            return self._jsonl(src[:limit])

        @self.app.get("/api/export/diff-decisions")
        async def diff_decisions(
            repo_root: str, authorization: str = Header(""),
            since: str | None = Query(None), limit: int = Query(1000),
        ):
            self._auth(authorization)
            self._check_repo(repo_root)
            src = self.diff_decisions
            if since:
                src = [r for r in src if r.get("created_at", "") >= since]
            return self._jsonl(src[:limit])

        @self.app.get("/api/export/events")
        async def events(
            repo_root: str, authorization: str = Header(""),
            since: str | None = Query(None), limit: int = Query(1000),
        ):
            self._auth(authorization)
            self._check_repo(repo_root)
            src = self.events
            if since:
                src = [r for r in src if r.get("created_at", "") >= since]
            return self._jsonl(src[:limit])

        @self.app.post("/api/diffs/decision")
        async def post_diff_decision(body: dict):
            # Mirror the producer — no auth on this endpoint; body carries
            # ``repo_root`` directly. Record the received body for test
            # assertions.
            if body.get("repo_root") != self.repo_root:
                raise HTTPException(status_code=404, detail="unknown repo_root")
            self.received_diff_decisions.append(body)
            return {"stored": True, "id": len(self.received_diff_decisions)}

        @self.app.get("/api/export/memories")
        async def memories_endpoint(
            repo_root: str, authorization: str = Header(""),
            limit: int = Query(500),
        ):
            self._auth(authorization)
            self._check_repo(repo_root)
            return self._jsonl(self.memories[:limit])

    def _check_repo(self, repo_root: str) -> None:
        if repo_root != self.repo_root:
            raise HTTPException(status_code=404, detail="unknown workspace")

    def _jsonl(self, rows: list[dict]):
        body = "\n".join(json.dumps(r) for r in rows)
        return PlainTextResponse(body, media_type="application/x-ndjson")

    def _auth(self, header: str) -> None:
        if header != f"Bearer {self.api_key}":
            raise HTTPException(status_code=401, detail="bad key")

    def _max_id(self) -> int:
        return max((r["id"] for r in self._rows), default=0)

    def add_pair(
        self,
        *,
        pair_kind: str,
        pair_id: str,
        chosen: str = "good",
        rejected: str = "bad",
    ) -> None:
        self._rows.append({
            "id": len(self._rows) + 1,
            "pair_id": pair_id,
            "pair_kind": pair_kind,
            "prompt": [{"role": "user", "content": "do thing"}],
            "chosen": {"role": "assistant", "content": chosen},
            "rejected": {"role": "assistant", "content": rejected},
            "workspace_id": "ws-abc",
            "model_name": "qwen3-coder:30b",
            "phase": "plan",
        })


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db(tmp_path: Path) -> Database:
    d = Database(tmp_path / "test.db")
    await d.connect()
    yield d
    await d.close()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    s = Settings()
    s.training.dataset_directory = str(tmp_path / "datasets")
    s.training.max_dataset_size_mb = 1
    s.ingestion.enabled = True
    s.ingestion.page_limit = 100
    s.ingestion.max_concurrent_pulls = 2
    return s


@pytest_asyncio.fixture
async def fake_lean_ai() -> FakeLeanAi:
    return FakeLeanAi(api_key="test-key")


@pytest_asyncio.fixture
async def ingestor(db, settings, fake_lean_ai):
    transport = httpx.ASGITransport(app=fake_lean_ai.app)
    http = httpx.AsyncClient(transport=transport, base_url="http://fake")
    dm = DatasetManager(db, settings)
    ing = LeanAiIngestor(db, dm, settings, http_client=http)
    yield ing
    await http.aclose()


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_workspace_probes_manifest_and_creates_datasets(
    ingestor, db, fake_lean_ai
):
    info = await ingestor.register_workspace(
        workspace_id="ws-abc",
        display_name="my-workstation",
        backend_url="http://fake",
        repo_root="/tmp/ws-abc",
        export_key="test-key",
        registered_by="alice",
    )
    assert info.workspace_id == "ws-abc"
    assert info.display_name == "my-workstation"
    assert info.enabled is True
    # Two ingest state rows (one per pair_kind) seeded at cursor=0.
    assert len(info.ingest) == 2
    kinds = {s.pair_kind for s in info.ingest}
    assert kinds == set(PAIR_KINDS)
    for s in info.ingest:
        assert s.last_cursor == 0
        assert s.rows_imported == 0
    # Two empty DPO datasets exist on disk.
    for pair_kind in PAIR_KINDS:
        name = _dataset_name("ws-abc", pair_kind)
        ds = await ingestor._datasets.get(name)
        assert ds is not None
        assert ds.format == DatasetFormat.DPO
        assert ds.row_count == 0
        assert Path(ds.path).exists()


@pytest.mark.asyncio
async def test_register_workspace_rejects_bad_key(ingestor):
    with pytest.raises(IngestError, match="401"):
        await ingestor.register_workspace(
            workspace_id="ws-abc",
            display_name="x",
            backend_url="http://fake",
            repo_root="/tmp/ws-abc",
            export_key="wrong",
            registered_by="alice",
        )


@pytest.mark.asyncio
async def test_register_twice_updates_metadata_not_cursor(ingestor, fake_lean_ai):
    await ingestor.register_workspace(
        workspace_id="ws-abc", display_name="v1",
        backend_url="http://fake", repo_root="/tmp/ws-abc", export_key="test-key", registered_by="alice",
    )
    fake_lean_ai.add_pair(pair_kind="plan_rejection", pair_id="p-1")
    await ingestor.poll_workspace("ws-abc")

    info_before = await ingestor.get_workspace("ws-abc")
    assert info_before is not None
    before_cursor = max(s.last_cursor for s in info_before.ingest)
    assert before_cursor > 0

    # Re-register with different display name; cursors should survive.
    info = await ingestor.register_workspace(
        workspace_id="ws-abc", display_name="v2",
        backend_url="http://fake", repo_root="/tmp/ws-abc", export_key="test-key", registered_by="bob",
    )
    assert info.display_name == "v2"
    after_cursor = max(s.last_cursor for s in info.ingest)
    assert after_cursor == before_cursor


# ---------------------------------------------------------------------------
# Polling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poll_appends_dpo_rows_split_by_pair_kind(ingestor, fake_lean_ai):
    await ingestor.register_workspace(
        workspace_id="ws-abc", display_name="x",
        backend_url="http://fake", repo_root="/tmp/ws-abc", export_key="test-key", registered_by="alice",
    )
    fake_lean_ai.add_pair(pair_kind="plan_rejection", pair_id="p-1", chosen="A", rejected="B")
    fake_lean_ai.add_pair(pair_kind="validation_fix", pair_id="v-1", chosen="C", rejected="D")
    fake_lean_ai.add_pair(pair_kind="plan_rejection", pair_id="p-2", chosen="E", rejected="F")

    result = await ingestor.poll_workspace("ws-abc")
    assert result.rows_pulled == 3
    assert set(result.datasets_updated) == {
        _dataset_name("ws-abc", "plan_rejection"),
        _dataset_name("ws-abc", "validation_fix"),
    }

    # Verify on-disk files contain the right rows.
    plan_rows = await ingestor._datasets.preview(
        _dataset_name("ws-abc", "plan_rejection"), limit=100
    )
    assert len(plan_rows) == 2
    assert {r["pair_id"] for r in plan_rows} == {"p-1", "p-2"}
    # Envelope "id" stripped from persisted rows.
    assert "id" not in plan_rows[0]

    fix_rows = await ingestor._datasets.preview(
        _dataset_name("ws-abc", "validation_fix"), limit=100
    )
    assert len(fix_rows) == 1
    assert fix_rows[0]["pair_id"] == "v-1"


@pytest.mark.asyncio
async def test_poll_is_idempotent_when_no_new_rows(ingestor, fake_lean_ai):
    await ingestor.register_workspace(
        workspace_id="ws-abc", display_name="x",
        backend_url="http://fake", repo_root="/tmp/ws-abc", export_key="test-key", registered_by="alice",
    )
    fake_lean_ai.add_pair(pair_kind="plan_rejection", pair_id="p-1")
    await ingestor.poll_workspace("ws-abc")

    second = await ingestor.poll_workspace("ws-abc")
    assert second.rows_pulled == 0
    assert second.datasets_updated == []


@pytest.mark.asyncio
async def test_poll_resumes_from_cursor_on_new_rows(ingestor, fake_lean_ai):
    await ingestor.register_workspace(
        workspace_id="ws-abc", display_name="x",
        backend_url="http://fake", repo_root="/tmp/ws-abc", export_key="test-key", registered_by="alice",
    )
    fake_lean_ai.add_pair(pair_kind="plan_rejection", pair_id="p-1")
    first = await ingestor.poll_workspace("ws-abc")
    assert first.rows_pulled == 1

    # New data arrives after first poll.
    fake_lean_ai.add_pair(pair_kind="plan_rejection", pair_id="p-2")
    fake_lean_ai.add_pair(pair_kind="validation_fix", pair_id="v-1")

    second = await ingestor.poll_workspace("ws-abc")
    assert second.rows_pulled == 2  # Only the 2 new rows.

    rows = await ingestor._datasets.preview(
        _dataset_name("ws-abc", "plan_rejection"), limit=100
    )
    assert {r["pair_id"] for r in rows} == {"p-1", "p-2"}


@pytest.mark.asyncio
async def test_poll_paginates_when_more_than_page_limit(ingestor, fake_lean_ai, settings):
    settings.ingestion.page_limit = 2  # Force multiple pages

    await ingestor.register_workspace(
        workspace_id="ws-abc", display_name="x",
        backend_url="http://fake", repo_root="/tmp/ws-abc", export_key="test-key", registered_by="alice",
    )
    for i in range(5):
        fake_lean_ai.add_pair(pair_kind="plan_rejection", pair_id=f"p-{i}")

    result = await ingestor.poll_workspace("ws-abc")
    assert result.rows_pulled == 5


@pytest.mark.asyncio
async def test_poll_deduplicates_by_pair_id(ingestor, fake_lean_ai):
    """If lean_ai republishes a pair_id we already have, it should not re-append."""
    await ingestor.register_workspace(
        workspace_id="ws-abc", display_name="x",
        backend_url="http://fake", repo_root="/tmp/ws-abc", export_key="test-key", registered_by="alice",
    )
    fake_lean_ai.add_pair(pair_kind="plan_rejection", pair_id="p-1")
    await ingestor.poll_workspace("ws-abc")

    # Simulate lean_ai re-emitting the same pair_id with a new id (hypothetical).
    fake_lean_ai._rows.append({
        "id": 999,
        "pair_id": "p-1",
        "pair_kind": "plan_rejection",
        "prompt": [{"role": "user", "content": "x"}],
        "chosen": {"role": "assistant", "content": "y"},
        "rejected": {"role": "assistant", "content": "z"},
    })
    result = await ingestor.poll_workspace("ws-abc")
    assert result.rows_pulled == 0

    rows = await ingestor._datasets.preview(
        _dataset_name("ws-abc", "plan_rejection"), limit=100
    )
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_poll_records_last_error_and_keeps_going(ingestor, fake_lean_ai, db):
    await ingestor.register_workspace(
        workspace_id="ws-abc", display_name="x",
        backend_url="http://fake", repo_root="/tmp/ws-abc", export_key="test-key", registered_by="alice",
    )
    # Rotate the key on the remote side so subsequent polls fail with 401.
    fake_lean_ai.api_key = "different-key"

    result = await ingestor.poll_workspace("ws-abc")
    assert result.rows_pulled == 0
    assert any("401" in e for e in result.errors)

    info = await ingestor.get_workspace("ws-abc")
    assert info is not None
    assert info.last_error and "401" in info.last_error
    assert info.last_polled_at is not None


# ---------------------------------------------------------------------------
# poll_all
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poll_all_hits_every_enabled_workspace(db, settings, tmp_path):
    backend_a = FakeLeanAi(api_key="key-a", workspace_id="ws-a", repo_root="/tmp/a")
    backend_b = FakeLeanAi(api_key="key-b", workspace_id="ws-b", repo_root="/tmp/b")
    backend_a.add_pair(pair_kind="plan_rejection", pair_id="a-1")
    backend_b.add_pair(pair_kind="validation_fix", pair_id="b-1")

    # Build a single httpx client routed by URL prefix.
    async def _dispatch(request: httpx.Request) -> httpx.Response:
        if request.url.host == "a":
            app = backend_a.app
        elif request.url.host == "b":
            app = backend_b.app
        else:
            return httpx.Response(404)
        transport = httpx.ASGITransport(app=app)
        return await transport.handle_async_request(request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(_dispatch))
    dm = DatasetManager(db, settings)
    ing = LeanAiIngestor(db, dm, settings, http_client=http)

    await ing.register_workspace(
        workspace_id="ws-a", display_name="a",
        backend_url="http://a", repo_root="/tmp/a",
        export_key="key-a", registered_by="u",
    )
    await ing.register_workspace(
        workspace_id="ws-b", display_name="b",
        backend_url="http://b", repo_root="/tmp/b",
        export_key="key-b", registered_by="u",
    )

    # Disable one — poll_all should skip it.
    await ing.delete_workspace("ws-b", hard=False)

    results = await ing.poll_all()
    assert len(results) == 1
    assert results[0].workspace_id == "ws-a"
    assert results[0].rows_pulled == 1

    await http.aclose()


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_soft_disables(ingestor, fake_lean_ai):
    await ingestor.register_workspace(
        workspace_id="ws-abc", display_name="x",
        backend_url="http://fake", repo_root="/tmp/ws-abc", export_key="test-key", registered_by="alice",
    )
    assert await ingestor.delete_workspace("ws-abc", hard=False) is True
    info = await ingestor.get_workspace("ws-abc")
    assert info is not None
    assert info.enabled is False
    # Datasets still exist.
    for pair_kind in PAIR_KINDS:
        ds = await ingestor._datasets.get(_dataset_name("ws-abc", pair_kind))
        assert ds is not None


@pytest.mark.asyncio
async def test_delete_hard_removes_everything(ingestor, fake_lean_ai):
    await ingestor.register_workspace(
        workspace_id="ws-abc", display_name="x",
        backend_url="http://fake", repo_root="/tmp/ws-abc", export_key="test-key", registered_by="alice",
    )
    assert await ingestor.delete_workspace("ws-abc", hard=True) is True
    assert await ingestor.get_workspace("ws-abc") is None
    for pair_kind in PAIR_KINDS:
        ds = await ingestor._datasets.get(_dataset_name("ws-abc", pair_kind))
        assert ds is None


@pytest.mark.asyncio
async def test_delete_missing_returns_false(ingestor):
    assert await ingestor.delete_workspace("nope", hard=False) is False


# ---------------------------------------------------------------------------
# Encryption wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_export_key_encrypted_when_encryption_enabled(
    db, settings, fake_lean_ai
):
    class StubEncryption:
        def encrypt(self, plaintext: str) -> str:
            return f"enc::{plaintext}"

        def decrypt(self, stored: str) -> str:
            assert stored.startswith("enc::")
            return stored[5:]

    transport = httpx.ASGITransport(app=fake_lean_ai.app)
    http = httpx.AsyncClient(transport=transport, base_url="http://fake")
    dm = DatasetManager(db, settings)
    ing = LeanAiIngestor(
        db, dm, settings, http_client=http, encryption=StubEncryption()
    )

    await ing.register_workspace(
        workspace_id="ws-abc", display_name="x",
        backend_url="http://fake", repo_root="/tmp/ws-abc", export_key="test-key", registered_by="alice",
    )

    row = await db.fetchone(
        "SELECT export_key_encrypted FROM lean_ai_workspaces WHERE workspace_id = ?",
        ("ws-abc",),
    )
    assert row is not None
    assert row["export_key_encrypted"] == "enc::test-key"

    fake_lean_ai.add_pair(pair_kind="plan_rejection", pair_id="p-1")
    result = await ing.poll_workspace("ws-abc")
    assert result.rows_pulled == 1

    await http.aclose()


# ---------------------------------------------------------------------------
# repo_root / workspace_id verification (P0)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_rejects_workspace_id_mismatch(ingestor):
    """User-supplied workspace_id must match hash(salt, repo_root) on the remote."""
    with pytest.raises(IngestError, match="workspace_id mismatch"):
        await ingestor.register_workspace(
            workspace_id="ws-abc",
            display_name="x",
            backend_url="http://fake",
            repo_root="/tmp/something-else",
            export_key="test-key",
            registered_by="alice",
        )


@pytest.mark.asyncio
async def test_register_requires_repo_root(ingestor):
    with pytest.raises(IngestError, match="repo_root is required"):
        await ingestor.register_workspace(
            workspace_id="ws-abc",
            display_name="x",
            backend_url="http://fake",
            repo_root="",
            export_key="test-key",
            registered_by="alice",
        )


@pytest.mark.asyncio
async def test_repo_root_sent_on_every_poll_request(ingestor, fake_lean_ai):
    """Every /api/export/* call must include ?repo_root= or the real backend 422s."""
    await ingestor.register_workspace(
        workspace_id="ws-abc", display_name="x",
        backend_url="http://fake", repo_root="/tmp/ws-abc",
        export_key="test-key", registered_by="alice",
    )
    fake_lean_ai.add_pair(pair_kind="plan_rejection", pair_id="p-1")
    await ingestor.poll_workspace("ws-abc")

    assert all(
        c["query"].get("repo_root") == "/tmp/ws-abc" for c in fake_lean_ai.calls
    ), fake_lean_ai.calls
    paths = {c["path"] for c in fake_lean_ai.calls}
    assert "/api/export/workspace-id" in paths
    assert "/api/export/traces" in paths
    assert "/api/export/manifest" in paths


@pytest.mark.asyncio
async def test_workspace_info_exposes_repo_root(ingestor, fake_lean_ai):
    info = await ingestor.register_workspace(
        workspace_id="ws-abc", display_name="x",
        backend_url="http://fake", repo_root="/tmp/ws-abc",
        export_key="test-key", registered_by="alice",
    )
    assert info.repo_root == "/tmp/ws-abc"
    fetched = await ingestor.get_workspace("ws-abc")
    assert fetched is not None
    assert fetched.repo_root == "/tmp/ws-abc"


# ---------------------------------------------------------------------------
# Single-fetch multiplex + discovery (P1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_fetch_per_cycle_instead_of_one_per_pair_kind(
    ingestor, fake_lean_ai
):
    """DPO pull must hit /traces?format=dpo once per cycle, not once per pair_kind.

    Note: since SFT + KTO were added as separate streams (Chunk A), there
    are now three /traces hits per cycle (one per format) — but the DPO
    format in particular must not fan out by pair_kind.
    """
    await ingestor.register_workspace(
        workspace_id="ws-abc", display_name="x",
        backend_url="http://fake", repo_root="/tmp/ws-abc",
        export_key="test-key", registered_by="alice",
    )
    fake_lean_ai.add_pair(pair_kind="plan_rejection", pair_id="p-1")
    fake_lean_ai.add_pair(pair_kind="validation_fix", pair_id="v-1")
    fake_lean_ai.calls.clear()

    await ingestor.poll_workspace("ws-abc")
    dpo_hits = [
        c for c in fake_lean_ai.calls
        if c["path"] == "/api/export/traces" and c["query"].get("format") == "dpo"
    ]
    assert len(dpo_hits) == 1, fake_lean_ai.calls


@pytest.mark.asyncio
async def test_unknown_pair_kind_gets_its_own_dataset(ingestor, fake_lean_ai):
    """New pair_kinds must be discovered and materialised, not silently dropped."""
    await ingestor.register_workspace(
        workspace_id="ws-abc", display_name="x",
        backend_url="http://fake", repo_root="/tmp/ws-abc",
        export_key="test-key", registered_by="alice",
    )
    fake_lean_ai.add_pair(pair_kind="tool_call_refinement", pair_id="t-1")

    result = await ingestor.poll_workspace("ws-abc")
    assert result.rows_pulled == 1
    discovered_name = _dataset_name("ws-abc", "tool_call_refinement")
    assert discovered_name in result.datasets_updated

    ds = await ingestor._datasets.get(discovered_name)
    assert ds is not None
    assert ds.row_count == 1

    info = await ingestor.get_workspace("ws-abc")
    assert info is not None
    kinds = {s.pair_kind for s in info.ingest}
    assert "tool_call_refinement" in kinds


@pytest.mark.asyncio
async def test_manifest_gate_skips_poll_when_counts_unchanged(
    ingestor, fake_lean_ai
):
    """Second poll with no new rows must not hit /traces at all."""
    await ingestor.register_workspace(
        workspace_id="ws-abc", display_name="x",
        backend_url="http://fake", repo_root="/tmp/ws-abc",
        export_key="test-key", registered_by="alice",
    )
    fake_lean_ai.add_pair(pair_kind="plan_rejection", pair_id="p-1")
    await ingestor.poll_workspace("ws-abc")
    fake_lean_ai.calls.clear()

    result = await ingestor.poll_workspace("ws-abc")
    assert result.rows_pulled == 0
    traces_hits = [c for c in fake_lean_ai.calls if c["path"] == "/api/export/traces"]
    assert traces_hits == [], "manifest gate should have short-circuited"


@pytest.mark.asyncio
async def test_manifest_gate_skips_only_when_counts_match(
    ingestor, fake_lean_ai
):
    """Once a new row arrives, the gate must re-open and poll runs."""
    await ingestor.register_workspace(
        workspace_id="ws-abc", display_name="x",
        backend_url="http://fake", repo_root="/tmp/ws-abc",
        export_key="test-key", registered_by="alice",
    )
    fake_lean_ai.add_pair(pair_kind="plan_rejection", pair_id="p-1")
    await ingestor.poll_workspace("ws-abc")

    # New data.
    fake_lean_ai.add_pair(pair_kind="plan_rejection", pair_id="p-2")
    result = await ingestor.poll_workspace("ws-abc")
    assert result.rows_pulled == 1


@pytest.mark.asyncio
async def test_schema_version_downgrade_is_rejected(db, settings, tmp_path):
    """A producer older than this consumer supports must fail fast."""
    fake = FakeLeanAi(schema_version=0)  # older than SUPPORTED_SCHEMA_VERSION=1
    transport = httpx.ASGITransport(app=fake.app)
    http = httpx.AsyncClient(transport=transport, base_url="http://fake")
    dm = DatasetManager(db, settings)
    ing = LeanAiIngestor(db, dm, settings, http_client=http)
    await ing.register_workspace(
        workspace_id="ws-abc", display_name="x",
        backend_url="http://fake", repo_root="/tmp/ws-abc",
        export_key="test-key", registered_by="alice",
    )
    result = await ing.poll_workspace("ws-abc")
    assert result.rows_pulled == 0
    assert any("schema_version" in e for e in result.errors)

    await http.aclose()


@pytest.mark.asyncio
async def test_schema_version_ahead_logs_warning_but_proceeds(
    db, settings, tmp_path, caplog,
):
    """A newer producer must not block polling — log and keep going."""
    fake = FakeLeanAi(schema_version=99)
    fake.add_pair(pair_kind="plan_rejection", pair_id="p-1")
    transport = httpx.ASGITransport(app=fake.app)
    http = httpx.AsyncClient(transport=transport, base_url="http://fake")
    dm = DatasetManager(db, settings)
    ing = LeanAiIngestor(db, dm, settings, http_client=http)
    await ing.register_workspace(
        workspace_id="ws-abc", display_name="x",
        backend_url="http://fake", repo_root="/tmp/ws-abc",
        export_key="test-key", registered_by="alice",
    )
    with caplog.at_level("WARNING"):
        result = await ing.poll_workspace("ws-abc")
    assert result.rows_pulled == 1
    assert any("schema_version=99" in r.message for r in caplog.records)

    await http.aclose()


@pytest.mark.asyncio
async def test_poll_fails_cleanly_when_repo_root_missing(ingestor, db, fake_lean_ai):
    """A legacy row that predates the repo_root column must be re-registered."""
    await ingestor.register_workspace(
        workspace_id="ws-abc", display_name="x",
        backend_url="http://fake", repo_root="/tmp/ws-abc",
        export_key="test-key", registered_by="alice",
    )
    # Simulate a pre-migration row by nulling out repo_root.
    await db.execute(
        "UPDATE lean_ai_workspaces SET repo_root = NULL WHERE workspace_id = ?",
        ("ws-abc",),
    )
    await db.commit()

    with pytest.raises(IngestError, match="missing repo_root"):
        await ingestor.poll_workspace("ws-abc")


# ---------------------------------------------------------------------------
# Aux-stream ingestion (P2)
# ---------------------------------------------------------------------------


def _add_aux_rows(fake: FakeLeanAi) -> None:
    """Seed every aux stream with a couple of representative rows."""
    fake.tool_pairs.append({
        "_ts": "2026-04-23T12:00:00+00:00",
        "prompt_hint": "edit_file", "session_id": "s1", "phase": "implementation",
        "rejected": {"arguments": {"path": "/ws/a", "search": "old"}, "result_preview": "ERR"},
        "chosen": {"arguments": {"path": "/ws/a", "search": "oldval"}, "result_preview": "OK"},
        "workspace_id": fake.workspace_id,
    })
    fake.phase2.append({
        "session_id": "s1", "task": "add audit log",
        "scope": "scope", "observations": [], "scratchpad": "", "journal": "",
        "exploration_output": "", "file_summary": {},
        "trace_uuid": "uuid-p2-1", "created_at": "2026-04-23T12:01:00+00:00",
        "workspace_id": fake.workspace_id,
    })
    fake.clarifications.append({
        "session_id": "s1", "phase": "planning.phase1",
        "task": "add audit log", "question": "split db?", "answer": "main",
        "outcome": "answered", "trace_uuid": "uuid-clar-1",
        "created_at": "2026-04-23T12:02:00+00:00",
        "workspace_id": fake.workspace_id,
    })
    fake.diff_decisions.append({
        "session_id": "s1", "file_path": "/ws/a", "accepted": 0,
        "diff_hash": "abc123", "note": "regression",
        "trace_uuid": "uuid-diff-1",
        "created_at": "2026-04-23T12:03:00+00:00",
        "workspace_id": fake.workspace_id,
    })
    fake.events.append({
        "session_id": "s1", "event_type": "session_start",
        "payload": {"primary_model": "qwen3-coder:30b"},
        "created_at": "2026-04-23T12:04:00+00:00",
        "workspace_id": fake.workspace_id,
    })
    fake.memories.append({
        "category": "fix_pattern", "content": "prefer TEXT over VARCHAR",
        "curation_status": "user_confirmed",
        "workspace_id": fake.workspace_id,
    })


@pytest.mark.asyncio
async def test_register_creates_aux_datasets(ingestor):
    """All six aux datasets should exist as empty placeholders post-registration."""
    await ingestor.register_workspace(
        workspace_id="ws-abc", display_name="x",
        backend_url="http://fake", repo_root="/tmp/ws-abc",
        export_key="test-key", registered_by="alice",
    )
    for stream_key in (
        STREAM_DPO_TOOL_EXECUTIONS, STREAM_SFT_PHASE2,
        STREAM_SFT_CLARIFICATIONS, STREAM_KTO_DIFF_DECISIONS,
        STREAM_EVENTS, STREAM_MEMORIES,
    ):
        ds = await ingestor._datasets.get(
            _aux_dataset_name("ws-abc", stream_key)
        )
        assert ds is not None, stream_key
        assert ds.row_count == 0


@pytest.mark.asyncio
async def test_aux_streams_pull_rows_from_each_endpoint(ingestor, fake_lean_ai):
    """Single poll should drain every aux stream into its dataset."""
    await ingestor.register_workspace(
        workspace_id="ws-abc", display_name="x",
        backend_url="http://fake", repo_root="/tmp/ws-abc",
        export_key="test-key", registered_by="alice",
    )
    _add_aux_rows(fake_lean_ai)
    result = await ingestor.poll_workspace("ws-abc")

    # Per-stream ingestion assertions.
    for stream_key, expected in (
        (STREAM_DPO_TOOL_EXECUTIONS, 1),
        (STREAM_SFT_PHASE2, 1),
        (STREAM_SFT_CLARIFICATIONS, 1),
        (STREAM_KTO_DIFF_DECISIONS, 1),
        (STREAM_EVENTS, 1),
        (STREAM_MEMORIES, 1),
    ):
        ds = await ingestor._datasets.get(
            _aux_dataset_name("ws-abc", stream_key)
        )
        assert ds is not None
        assert ds.row_count == expected, (stream_key, ds.row_count)

    # Each aux dataset should show in datasets_updated.
    assert _aux_dataset_name("ws-abc", STREAM_DPO_TOOL_EXECUTIONS) in result.datasets_updated
    assert _aux_dataset_name("ws-abc", STREAM_SFT_PHASE2) in result.datasets_updated
    assert _aux_dataset_name("ws-abc", STREAM_MEMORIES) in result.datasets_updated


@pytest.mark.asyncio
async def test_aux_stream_dedup_prevents_double_write_on_since_clip(
    ingestor, fake_lean_ai
):
    """Since-cursor is inclusive, so one row gets re-sent — dedup must drop it."""
    await ingestor.register_workspace(
        workspace_id="ws-abc", display_name="x",
        backend_url="http://fake", repo_root="/tmp/ws-abc",
        export_key="test-key", registered_by="alice",
    )
    fake_lean_ai.phase2.append({
        "session_id": "s1", "trace_uuid": "uuid-p2-once",
        "task": "t", "scope": "", "observations": [], "scratchpad": "",
        "journal": "", "exploration_output": "", "file_summary": {},
        "created_at": "2026-04-23T12:01:00+00:00",
        "workspace_id": "ws-abc",
    })
    await ingestor.poll_workspace("ws-abc")
    # Second poll: producer still has the same row, fake returns it again
    # because our since filter is >=. The dedup set should keep the file
    # at row_count=1.
    await ingestor.poll_workspace("ws-abc")
    ds = await ingestor._datasets.get(_aux_dataset_name("ws-abc", STREAM_SFT_PHASE2))
    assert ds is not None
    assert ds.row_count == 1


@pytest.mark.asyncio
async def test_memories_snapshot_replaces_when_changed(ingestor, fake_lean_ai):
    """Memories has no cursor — on change, the dataset content must mirror the remote."""
    await ingestor.register_workspace(
        workspace_id="ws-abc", display_name="x",
        backend_url="http://fake", repo_root="/tmp/ws-abc",
        export_key="test-key", registered_by="alice",
    )
    fake_lean_ai.memories.append({"category": "fix_pattern", "content": "v1", "workspace_id": "ws-abc"})
    await ingestor.poll_workspace("ws-abc")
    ds = await ingestor._datasets.get(_aux_dataset_name("ws-abc", STREAM_MEMORIES))
    assert ds is not None
    assert ds.row_count == 1

    # Producer rewrites the memory (v1 → v2).
    fake_lean_ai.memories[:] = [
        {"category": "fix_pattern", "content": "v2", "workspace_id": "ws-abc"},
    ]
    await ingestor.poll_workspace("ws-abc")
    ds = await ingestor._datasets.get(_aux_dataset_name("ws-abc", STREAM_MEMORIES))
    rows = await ingestor._datasets.preview(
        _aux_dataset_name("ws-abc", STREAM_MEMORIES), limit=10
    )
    assert ds is not None
    assert ds.row_count == 1
    assert rows[0]["content"] == "v2"


@pytest.mark.asyncio
async def test_memories_snapshot_skips_when_hash_unchanged(ingestor, fake_lean_ai):
    """Identical memories payload must not trigger a re-write."""
    await ingestor.register_workspace(
        workspace_id="ws-abc", display_name="x",
        backend_url="http://fake", repo_root="/tmp/ws-abc",
        export_key="test-key", registered_by="alice",
    )
    fake_lean_ai.memories.append({"category": "x", "content": "v1", "workspace_id": "ws-abc"})
    await ingestor.poll_workspace("ws-abc")

    mem_path = await ingestor._datasets.get_path(
        _aux_dataset_name("ws-abc", STREAM_MEMORIES)
    )
    assert mem_path is not None
    mtime_before = Path(mem_path).stat().st_mtime_ns

    await ingestor.poll_workspace("ws-abc")
    # mtime should not have advanced on the second call.
    assert Path(mem_path).stat().st_mtime_ns == mtime_before


@pytest.mark.asyncio
async def test_aux_stream_skipped_when_count_unchanged(ingestor, fake_lean_ai):
    """Per-stream manifest gate: phase2 count holds → no /phase2-syntheses hit."""
    await ingestor.register_workspace(
        workspace_id="ws-abc", display_name="x",
        backend_url="http://fake", repo_root="/tmp/ws-abc",
        export_key="test-key", registered_by="alice",
    )
    fake_lean_ai.phase2.append({
        "session_id": "s1", "trace_uuid": "uuid-p2-gate",
        "task": "t", "scope": "", "observations": [], "scratchpad": "",
        "journal": "", "exploration_output": "", "file_summary": {},
        "created_at": "2026-04-23T12:01:00+00:00", "workspace_id": "ws-abc",
    })
    await ingestor.poll_workspace("ws-abc")

    fake_lean_ai.calls.clear()
    await ingestor.poll_workspace("ws-abc")
    phase2_hits = [c for c in fake_lean_ai.calls if c["path"] == "/api/export/phase2-syntheses"]
    assert phase2_hits == []


@pytest.mark.asyncio
async def test_delete_hard_removes_all_aux_datasets(ingestor, fake_lean_ai):
    await ingestor.register_workspace(
        workspace_id="ws-abc", display_name="x",
        backend_url="http://fake", repo_root="/tmp/ws-abc",
        export_key="test-key", registered_by="alice",
    )
    _add_aux_rows(fake_lean_ai)
    await ingestor.poll_workspace("ws-abc")

    assert await ingestor.delete_workspace("ws-abc", hard=True) is True
    for stream_key in (
        STREAM_DPO_TOOL_EXECUTIONS, STREAM_SFT_PHASE2,
        STREAM_SFT_CLARIFICATIONS, STREAM_KTO_DIFF_DECISIONS,
        STREAM_EVENTS, STREAM_MEMORIES,
    ):
        assert await ingestor._datasets.get(
            _aux_dataset_name("ws-abc", stream_key)
        ) is None, stream_key


# ---------------------------------------------------------------------------
# POST /diff-decision proxy + eval holdout (P3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_forward_diff_decision_injects_repo_root(
    ingestor, fake_lean_ai
):
    await ingestor.register_workspace(
        workspace_id="ws-abc", display_name="x",
        backend_url="http://fake", repo_root="/tmp/ws-abc",
        export_key="test-key", registered_by="alice",
    )
    resp = await ingestor.forward_diff_decision(
        "ws-abc",
        session_id="s1",
        file_path="/ws/a.py",
        accepted=False,
        diff_hash="abc123",
        note="breaks handler",
        trace_uuid="uuid-1",
    )
    assert resp.get("stored") is True

    assert len(fake_lean_ai.received_diff_decisions) == 1
    body = fake_lean_ai.received_diff_decisions[0]
    # Coordinator injects repo_root from registration.
    assert body["repo_root"] == "/tmp/ws-abc"
    assert body["session_id"] == "s1"
    assert body["accepted"] is False
    assert body["diff_hash"] == "abc123"


@pytest.mark.asyncio
async def test_forward_diff_decision_unknown_workspace(ingestor):
    with pytest.raises(IngestError, match="Unknown workspace"):
        await ingestor.forward_diff_decision(
            "nope",
            session_id="s1", file_path="/x", accepted=True,
        )


@pytest.mark.asyncio
async def test_forward_diff_decision_disabled_workspace(ingestor, fake_lean_ai):
    await ingestor.register_workspace(
        workspace_id="ws-abc", display_name="x",
        backend_url="http://fake", repo_root="/tmp/ws-abc",
        export_key="test-key", registered_by="alice",
    )
    await ingestor.delete_workspace("ws-abc", hard=False)
    with pytest.raises(IngestError, match="disabled"):
        await ingestor.forward_diff_decision(
            "ws-abc",
            session_id="s1", file_path="/x", accepted=True,
        )


@pytest.mark.asyncio
async def test_holdout_disabled_by_default(ingestor, fake_lean_ai):
    """Fraction=0.0 must land every row in the main dataset only."""
    await ingestor.register_workspace(
        workspace_id="ws-abc", display_name="x",
        backend_url="http://fake", repo_root="/tmp/ws-abc",
        export_key="test-key", registered_by="alice",
    )
    for i in range(20):
        fake_lean_ai.add_pair(pair_kind="plan_rejection", pair_id=f"p-{i}")
    await ingestor.poll_workspace("ws-abc")
    main = await ingestor._datasets.get(
        _dataset_name("ws-abc", "plan_rejection")
    )
    assert main is not None
    assert main.row_count == 20
    eval_ds = await ingestor._datasets.get(
        _dataset_name("ws-abc", "plan_rejection") + ":eval"
    )
    # :eval dataset should not exist when the feature is off.
    assert eval_ds is None


@pytest.mark.asyncio
async def test_holdout_splits_rows_deterministically(db, settings, tmp_path):
    settings.ingestion.holdout_fraction = 0.3
    settings.ingestion.holdout_salt = "seed-1"
    fake = FakeLeanAi()
    transport = httpx.ASGITransport(app=fake.app)
    http = httpx.AsyncClient(transport=transport, base_url="http://fake")
    dm = DatasetManager(db, settings)
    ing = LeanAiIngestor(db, dm, settings, http_client=http)
    await ing.register_workspace(
        workspace_id="ws-abc", display_name="x",
        backend_url="http://fake", repo_root="/tmp/ws-abc",
        export_key="test-key", registered_by="alice",
    )
    for i in range(200):
        fake.add_pair(pair_kind="plan_rejection", pair_id=f"p-{i}")
    await ing.poll_workspace("ws-abc")

    main = await ing._datasets.get(_dataset_name("ws-abc", "plan_rejection"))
    eval_ds = await ing._datasets.get(
        _dataset_name("ws-abc", "plan_rejection") + ":eval"
    )
    assert main is not None and eval_ds is not None
    assert main.row_count + eval_ds.row_count == 200
    # With fraction=0.3 we expect ~60 in eval — allow a ±20 band over 200.
    assert 40 <= eval_ds.row_count <= 80

    # Determinism is enforced by hashlib inside _row_in_holdout; spot-check
    # by running the classifier directly rather than polling again (the
    # fake backend resets ids on clear and the cursor doesn't).
    for i in range(50):
        probe_row = {"pair_id": f"p-{i}"}
        first = ing._row_in_holdout(
            "ws-abc",
            _dataset_name("ws-abc", "plan_rejection"),
            f"p-{i}", probe_row, 0.3,
        )
        second = ing._row_in_holdout(
            "ws-abc",
            _dataset_name("ws-abc", "plan_rejection"),
            f"p-{i}", probe_row, 0.3,
        )
        assert first == second, f"holdout bucket for p-{i} was not deterministic"

    await http.aclose()


@pytest.mark.asyncio
async def test_holdout_split_applies_to_aux_streams_too(db, settings, tmp_path):
    """Phase2 + memories must also split when holdout_fraction > 0."""
    settings.ingestion.holdout_fraction = 0.5  # max allowed
    settings.ingestion.holdout_salt = "s2"
    fake = FakeLeanAi()
    transport = httpx.ASGITransport(app=fake.app)
    http = httpx.AsyncClient(transport=transport, base_url="http://fake")
    dm = DatasetManager(db, settings)
    ing = LeanAiIngestor(db, dm, settings, http_client=http)
    await ing.register_workspace(
        workspace_id="ws-abc", display_name="x",
        backend_url="http://fake", repo_root="/tmp/ws-abc",
        export_key="test-key", registered_by="alice",
    )
    for i in range(50):
        fake.phase2.append({
            "session_id": f"s-{i}",
            "trace_uuid": f"uuid-{i}",
            "task": "t", "scope": "", "observations": [], "scratchpad": "",
            "journal": "", "exploration_output": "", "file_summary": {},
            "created_at": f"2026-04-23T12:00:{i:02d}+00:00",
            "workspace_id": "ws-abc",
        })
    await ing.poll_workspace("ws-abc")
    main = await ing._datasets.get(_aux_dataset_name("ws-abc", STREAM_SFT_PHASE2))
    eval_ds = await ing._datasets.get(
        _aux_dataset_name("ws-abc", STREAM_SFT_PHASE2) + ":eval"
    )
    assert main is not None and eval_ds is not None
    assert main.row_count + eval_ds.row_count == 50
    # Both sides must get some rows with fraction=0.5 and 50 rows.
    assert main.row_count > 0 and eval_ds.row_count > 0

    await http.aclose()


# ---------------------------------------------------------------------------
# Purge — "wipe data, keep registration" (Deferred item 2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_purge_empties_datasets_but_preserves_registration(
    ingestor, fake_lean_ai, db,
):
    """All ingested datasets empty; workspace row + encrypted key survive."""
    await ingestor.register_workspace(
        workspace_id="ws-abc", display_name="x",
        backend_url="http://fake", repo_root="/tmp/ws-abc",
        export_key="test-key", registered_by="alice",
    )
    for i in range(3):
        fake_lean_ai.add_pair(pair_kind="plan_rejection", pair_id=f"p-{i}")
    _add_aux_rows(fake_lean_ai)
    await ingestor.poll_workspace("ws-abc")

    # Snapshot the encrypted key before purge.
    before = await db.fetchone(
        "SELECT export_key_encrypted, display_name, repo_root "
        "FROM lean_ai_workspaces WHERE workspace_id = ?",
        ("ws-abc",),
    )
    assert before is not None
    key_before = before["export_key_encrypted"]

    result = await ingestor.purge_workspace_data("ws-abc")
    assert result is not None
    assert result.workspace_id == "ws-abc"
    assert result.rows_purged >= 3

    # Registration row still there, credentials untouched.
    after = await db.fetchone(
        "SELECT export_key_encrypted, display_name, repo_root, "
        "       last_manifest_snapshot "
        "FROM lean_ai_workspaces WHERE workspace_id = ?",
        ("ws-abc",),
    )
    assert after is not None
    assert after["export_key_encrypted"] == key_before
    assert after["display_name"] == "x"
    assert after["repo_root"] == "/tmp/ws-abc"
    # Snapshot must be cleared so the next poll's manifest gate re-opens.
    assert after["last_manifest_snapshot"] is None

    # Every dataset that had data is now empty.
    plan_ds = await ingestor._datasets.get(
        _dataset_name("ws-abc", "plan_rejection")
    )
    assert plan_ds is not None
    assert plan_ds.row_count == 0
    for stream_key in (
        STREAM_DPO_TOOL_EXECUTIONS, STREAM_SFT_PHASE2,
        STREAM_SFT_CLARIFICATIONS, STREAM_KTO_DIFF_DECISIONS,
        STREAM_EVENTS, STREAM_MEMORIES,
    ):
        ds = await ingestor._datasets.get(
            _aux_dataset_name("ws-abc", stream_key)
        )
        assert ds is not None, stream_key
        assert ds.row_count == 0, (stream_key, ds.row_count)


@pytest.mark.asyncio
async def test_purge_resets_cursors_so_next_poll_refetches(
    ingestor, fake_lean_ai, db,
):
    """After purge, cursors are zero/null and the next poll pulls everything."""
    await ingestor.register_workspace(
        workspace_id="ws-abc", display_name="x",
        backend_url="http://fake", repo_root="/tmp/ws-abc",
        export_key="test-key", registered_by="alice",
    )
    fake_lean_ai.add_pair(pair_kind="plan_rejection", pair_id="p-1")
    _add_aux_rows(fake_lean_ai)
    await ingestor.poll_workspace("ws-abc")

    await ingestor.purge_workspace_data("ws-abc")

    # DPO traces cursor and every since-cursor should be reset.
    stream_rows = await db.fetchall(
        "SELECT format, last_cursor, last_cursor_since, last_snapshot_hash "
        "FROM lean_ai_stream_cursor WHERE workspace_id = ?",
        ("ws-abc",),
    )
    for row in stream_rows:
        assert row["last_cursor"] == 0, row["format"]
        assert row["last_cursor_since"] is None, row["format"]
        assert row["last_snapshot_hash"] is None, row["format"]

    # Per-pair_kind counters reset.
    pair_rows = await db.fetchall(
        "SELECT rows_imported, last_cursor FROM lean_ai_ingest_state "
        "WHERE workspace_id = ?",
        ("ws-abc",),
    )
    for row in pair_rows:
        assert row["rows_imported"] == 0
        assert row["last_cursor"] == 0

    # Next poll should re-pull everything that's still on the fake producer.
    result = await ingestor.poll_workspace("ws-abc")
    assert result.rows_pulled >= 1  # at least the plan_rejection row


@pytest.mark.asyncio
async def test_purge_includes_eval_siblings_when_holdout_enabled(
    db, settings, tmp_path,
):
    """Both main and :eval datasets get truncated."""
    settings.ingestion.holdout_fraction = 0.3
    settings.ingestion.holdout_salt = "purge-test"
    fake = FakeLeanAi()
    transport = httpx.ASGITransport(app=fake.app)
    http = httpx.AsyncClient(transport=transport, base_url="http://fake")
    dm = DatasetManager(db, settings)
    ing = LeanAiIngestor(db, dm, settings, http_client=http)
    await ing.register_workspace(
        workspace_id="ws-abc", display_name="x",
        backend_url="http://fake", repo_root="/tmp/ws-abc",
        export_key="test-key", registered_by="alice",
    )
    for i in range(100):
        fake.add_pair(pair_kind="plan_rejection", pair_id=f"p-{i}")
    await ing.poll_workspace("ws-abc")

    main_name = _dataset_name("ws-abc", "plan_rejection")
    eval_name = main_name + ":eval"

    main = await ing._datasets.get(main_name)
    eval_ds = await ing._datasets.get(eval_name)
    assert main is not None and eval_ds is not None
    assert main.row_count + eval_ds.row_count == 100
    assert eval_ds.row_count > 0  # sanity: holdout actually split rows

    result = await ing.purge_workspace_data("ws-abc")
    assert result is not None
    assert main_name in result.datasets_cleared
    assert eval_name in result.datasets_cleared
    assert result.rows_purged == 100

    main = await ing._datasets.get(main_name)
    eval_ds = await ing._datasets.get(eval_name)
    assert main is not None and eval_ds is not None
    assert main.row_count == 0
    assert eval_ds.row_count == 0

    await http.aclose()


@pytest.mark.asyncio
async def test_purge_returns_none_for_unknown_workspace(ingestor):
    assert await ingestor.purge_workspace_data("nope") is None


@pytest.mark.asyncio
async def test_purge_handles_workspace_with_no_data(ingestor, fake_lean_ai):
    """Purging an idle workspace is a no-op that still resets cursors cleanly."""
    await ingestor.register_workspace(
        workspace_id="ws-abc", display_name="x",
        backend_url="http://fake", repo_root="/tmp/ws-abc",
        export_key="test-key", registered_by="alice",
    )
    result = await ingestor.purge_workspace_data("ws-abc")
    assert result is not None
    assert result.rows_purged == 0
    # Empty datasets shouldn't be listed — nothing to clear.
    assert result.datasets_cleared == []


@pytest.mark.asyncio
async def test_purge_discovered_pair_kind_dataset_is_also_cleared(
    ingestor, fake_lean_ai,
):
    """A pair_kind discovered at runtime must be cleared by purge too."""
    await ingestor.register_workspace(
        workspace_id="ws-abc", display_name="x",
        backend_url="http://fake", repo_root="/tmp/ws-abc",
        export_key="test-key", registered_by="alice",
    )
    fake_lean_ai.add_pair(pair_kind="tool_call_refinement", pair_id="t-1")
    await ingestor.poll_workspace("ws-abc")

    discovered = _dataset_name("ws-abc", "tool_call_refinement")
    ds = await ingestor._datasets.get(discovered)
    assert ds is not None and ds.row_count == 1

    result = await ingestor.purge_workspace_data("ws-abc")
    assert result is not None
    assert discovered in result.datasets_cleared

    ds = await ingestor._datasets.get(discovered)
    assert ds is not None
    assert ds.row_count == 0


# ---------------------------------------------------------------------------
# enable + optional workspace_id on register (Dashboard support)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enable_reverses_soft_disable_and_clears_last_error(
    ingestor, fake_lean_ai, db,
):
    await ingestor.register_workspace(
        workspace_id="ws-abc", display_name="x",
        backend_url="http://fake", repo_root="/tmp/ws-abc",
        export_key="test-key", registered_by="alice",
    )
    # Plant a last_error + soft-disable.
    await db.execute(
        "UPDATE lean_ai_workspaces SET last_error = ?, enabled = 0 "
        "WHERE workspace_id = ?",
        ("some earlier failure", "ws-abc"),
    )
    await db.commit()

    info = await ingestor.enable_workspace("ws-abc")
    assert info is not None
    assert info.enabled is True
    assert info.last_error is None


@pytest.mark.asyncio
async def test_enable_unknown_workspace_returns_none(ingestor):
    assert await ingestor.enable_workspace("nope") is None


@pytest.mark.asyncio
async def test_enable_is_idempotent_on_already_enabled(
    ingestor, fake_lean_ai,
):
    await ingestor.register_workspace(
        workspace_id="ws-abc", display_name="x",
        backend_url="http://fake", repo_root="/tmp/ws-abc",
        export_key="test-key", registered_by="alice",
    )
    info1 = await ingestor.enable_workspace("ws-abc")
    info2 = await ingestor.enable_workspace("ws-abc")
    assert info1 is not None and info2 is not None
    assert info1.enabled is True and info2.enabled is True


@pytest.mark.asyncio
async def test_register_adopts_remote_workspace_id_when_none_supplied(
    ingestor, fake_lean_ai,
):
    """Registration without workspace_id uses whatever the remote returns."""
    info = await ingestor.register_workspace(
        workspace_id=None,  # left to remote
        display_name="auto-detected",
        backend_url="http://fake",
        repo_root="/tmp/ws-abc",
        export_key="test-key",
        registered_by="alice",
    )
    # FakeLeanAi's default repo_root="/tmp/ws-abc" hashes to workspace_id="ws-abc".
    assert info.workspace_id == "ws-abc"


@pytest.mark.asyncio
async def test_register_still_rejects_explicit_mismatch(ingestor):
    """Supplying a workspace_id that doesn't match must still fail."""
    with pytest.raises(IngestError, match="workspace_id mismatch"):
        await ingestor.register_workspace(
            workspace_id="wrong-id",
            display_name="x",
            backend_url="http://fake",
            repo_root="/tmp/ws-abc",
            export_key="test-key",
            registered_by="alice",
        )


# ---------------------------------------------------------------------------
# Chunk A — SFT + KTO trace streams and tool-compressions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sft_trace_stream_lands_rows(ingestor, fake_lean_ai):
    await ingestor.register_workspace(
        workspace_id="ws-abc", display_name="x",
        backend_url="http://fake", repo_root="/tmp/ws-abc",
        export_key="test-key", registered_by="alice",
    )
    fake_lean_ai.sft_traces.append({
        "id": 1,
        "messages": [{"role": "user", "content": "hello"}],
        "phase": "planning.phase1",
        "model_name": "qwen3-coder:30b",
        "workspace_id": "ws-abc",
    })
    result = await ingestor.poll_workspace("ws-abc")
    assert _aux_dataset_name("ws-abc", STREAM_SFT_TRACES) in result.datasets_updated
    ds = await ingestor._datasets.get(_aux_dataset_name("ws-abc", STREAM_SFT_TRACES))
    assert ds is not None
    assert ds.row_count == 1


@pytest.mark.asyncio
async def test_kto_trace_stream_lands_rows(ingestor, fake_lean_ai):
    await ingestor.register_workspace(
        workspace_id="ws-abc", display_name="x",
        backend_url="http://fake", repo_root="/tmp/ws-abc",
        export_key="test-key", registered_by="alice",
    )
    fake_lean_ai.kto_traces.append({
        "id": 1,
        "prompt": [{"role": "user", "content": "x"}],
        "completion": {"role": "assistant", "content": "y"},
        "label": True,
        "pair_id": "k-1",
        "workspace_id": "ws-abc",
        "phase": "planning.phase1",
        "model_name": "qwen3-coder:30b",
        "pair_kind": "plan_rejection",
    })
    result = await ingestor.poll_workspace("ws-abc")
    assert _aux_dataset_name("ws-abc", STREAM_KTO_TRACES) in result.datasets_updated
    ds = await ingestor._datasets.get(_aux_dataset_name("ws-abc", STREAM_KTO_TRACES))
    assert ds is not None
    assert ds.row_count == 1


@pytest.mark.asyncio
async def test_sft_dedup_on_repoll_clip(ingestor, fake_lean_ai):
    """SFT has no pair_id — row-hash dedup must prevent re-writes when the
    cursor clip re-emits an already-seen row."""
    await ingestor.register_workspace(
        workspace_id="ws-abc", display_name="x",
        backend_url="http://fake", repo_root="/tmp/ws-abc",
        export_key="test-key", registered_by="alice",
    )
    fake_lean_ai.sft_traces.append({
        "id": 1,
        "messages": [{"role": "user", "content": "same row"}],
        "phase": "planning.phase1", "model_name": "m", "workspace_id": "ws-abc",
    })
    await ingestor.poll_workspace("ws-abc")
    # Simulate the producer re-emitting the same trace with a new id (e.g.
    # a backfill on the producer side).
    fake_lean_ai.sft_traces.append({
        "id": 999,
        "messages": [{"role": "user", "content": "same row"}],
        "phase": "planning.phase1", "model_name": "m", "workspace_id": "ws-abc",
    })
    await ingestor.poll_workspace("ws-abc")
    ds = await ingestor._datasets.get(_aux_dataset_name("ws-abc", STREAM_SFT_TRACES))
    # Content-identical rows should be deduped to a single line on disk.
    assert ds is not None
    assert ds.row_count == 1


@pytest.mark.asyncio
async def test_kto_dedup_by_pair_id(ingestor, fake_lean_ai):
    await ingestor.register_workspace(
        workspace_id="ws-abc", display_name="x",
        backend_url="http://fake", repo_root="/tmp/ws-abc",
        export_key="test-key", registered_by="alice",
    )
    row = {
        "id": 1, "prompt": [], "completion": {}, "label": True,
        "pair_id": "k-1", "workspace_id": "ws-abc",
        "phase": "x", "model_name": "m", "pair_kind": "plan_rejection",
    }
    fake_lean_ai.kto_traces.append(row)
    await ingestor.poll_workspace("ws-abc")
    # Producer re-emits the same pair with a new id.
    fake_lean_ai.kto_traces.append({**row, "id": 42})
    await ingestor.poll_workspace("ws-abc")
    ds = await ingestor._datasets.get(_aux_dataset_name("ws-abc", STREAM_KTO_TRACES))
    assert ds is not None
    assert ds.row_count == 1


@pytest.mark.asyncio
async def test_tool_compressions_stream_lands_rows(ingestor, fake_lean_ai):
    await ingestor.register_workspace(
        workspace_id="ws-abc", display_name="x",
        backend_url="http://fake", repo_root="/tmp/ws-abc",
        export_key="test-key", registered_by="alice",
    )
    fake_lean_ai.tool_compressions.append({
        "session_id": "s1", "tool_name": "read_file",
        "raw_output": "x" * 100, "raw_length": 100,
        "compressed_output": "short", "compressed_length": 5,
        "compression_ratio": 0.05, "worker_model": "qwen2.5-coder:7b",
        "worker_provider": "ollama", "followup_progress": None,
        "created_at": "2026-04-23T12:00:00+00:00",
        "workspace_id": "ws-abc",
    })
    result = await ingestor.poll_workspace("ws-abc")
    assert _aux_dataset_name("ws-abc", STREAM_SFT_TOOL_COMPRESSIONS) in result.datasets_updated
    ds = await ingestor._datasets.get(
        _aux_dataset_name("ws-abc", STREAM_SFT_TOOL_COMPRESSIONS)
    )
    assert ds is not None
    assert ds.row_count == 1


@pytest.mark.asyncio
async def test_tool_compressions_idle_workspace_has_placeholder_dataset(
    ingestor, fake_lean_ai,
):
    """Workspaces where compression is off upstream still pre-create the empty dataset."""
    await ingestor.register_workspace(
        workspace_id="ws-abc", display_name="x",
        backend_url="http://fake", repo_root="/tmp/ws-abc",
        export_key="test-key", registered_by="alice",
    )
    ds = await ingestor._datasets.get(
        _aux_dataset_name("ws-abc", STREAM_SFT_TOOL_COMPRESSIONS)
    )
    assert ds is not None
    assert ds.row_count == 0


@pytest.mark.asyncio
async def test_new_streams_also_purge(ingestor, fake_lean_ai):
    """purge_workspace_data must truncate the SFT/KTO/tool_compressions datasets."""
    await ingestor.register_workspace(
        workspace_id="ws-abc", display_name="x",
        backend_url="http://fake", repo_root="/tmp/ws-abc",
        export_key="test-key", registered_by="alice",
    )
    fake_lean_ai.sft_traces.append({
        "id": 1, "messages": [{"role": "user", "content": "z"}],
        "phase": "x", "model_name": "m", "workspace_id": "ws-abc",
    })
    fake_lean_ai.kto_traces.append({
        "id": 1, "prompt": [], "completion": {}, "label": True,
        "pair_id": "k-1", "workspace_id": "ws-abc",
        "phase": "x", "model_name": "m", "pair_kind": "plan_rejection",
    })
    fake_lean_ai.tool_compressions.append({
        "session_id": "s1", "tool_name": "read_file",
        "raw_output": "x", "raw_length": 1, "compressed_output": "y",
        "compressed_length": 1, "compression_ratio": 1.0,
        "worker_model": "m", "worker_provider": "p", "followup_progress": None,
        "created_at": "2026-04-23T12:00:00+00:00",
        "workspace_id": "ws-abc",
    })
    await ingestor.poll_workspace("ws-abc")

    result = await ingestor.purge_workspace_data("ws-abc")
    assert result is not None
    for stream_key in (
        STREAM_SFT_TRACES, STREAM_KTO_TRACES, STREAM_SFT_TOOL_COMPRESSIONS,
    ):
        name = _aux_dataset_name("ws-abc", stream_key)
        assert name in result.datasets_cleared
        ds = await ingestor._datasets.get(name)
        assert ds is not None
        assert ds.row_count == 0
