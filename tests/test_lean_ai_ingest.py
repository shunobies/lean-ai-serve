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
    IngestError,
    LeanAiIngestor,
    _dataset_name,
)
from lean_ai_serve.training.schemas import DatasetFormat

# ---------------------------------------------------------------------------
# Fake lean_ai backend
# ---------------------------------------------------------------------------


class FakeLeanAi:
    """Minimal FastAPI app that mimics lean_ai's /api/export endpoints."""

    def __init__(self, *, api_key: str = "test-key"):
        self.api_key = api_key
        self._rows: list[dict] = []
        self.app = FastAPI()
        self._register_routes()

    def _register_routes(self) -> None:
        @self.app.get("/api/export/manifest")
        async def manifest(authorization: str = Header("")):
            self._auth(authorization)
            return {"schema_version": 1, "max_cursor": self._max_id(), "rows": len(self._rows)}

        @self.app.get("/api/export/traces")
        async def traces(
            authorization: str = Header(""),
            fmt: str = Query("dpo", alias="format"),
            cursor: int = Query(0),
            limit: int = Query(500),
        ):
            self._auth(authorization)
            if fmt != "dpo":
                raise HTTPException(status_code=400, detail="only dpo supported in fake")
            page = [r for r in self._rows if r["id"] > cursor][:limit]
            body = "\n".join(json.dumps(r) for r in page)
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
            export_key="wrong",
            registered_by="alice",
        )


@pytest.mark.asyncio
async def test_register_twice_updates_metadata_not_cursor(ingestor, fake_lean_ai):
    await ingestor.register_workspace(
        workspace_id="ws-abc", display_name="v1",
        backend_url="http://fake", export_key="test-key", registered_by="alice",
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
        backend_url="http://fake", export_key="test-key", registered_by="bob",
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
        backend_url="http://fake", export_key="test-key", registered_by="alice",
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
        backend_url="http://fake", export_key="test-key", registered_by="alice",
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
        backend_url="http://fake", export_key="test-key", registered_by="alice",
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
        backend_url="http://fake", export_key="test-key", registered_by="alice",
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
        backend_url="http://fake", export_key="test-key", registered_by="alice",
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
        backend_url="http://fake", export_key="test-key", registered_by="alice",
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
    backend_a = FakeLeanAi(api_key="key-a")
    backend_b = FakeLeanAi(api_key="key-b")
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
        backend_url="http://a", export_key="key-a", registered_by="u",
    )
    await ing.register_workspace(
        workspace_id="ws-b", display_name="b",
        backend_url="http://b", export_key="key-b", registered_by="u",
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
        backend_url="http://fake", export_key="test-key", registered_by="alice",
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
        backend_url="http://fake", export_key="test-key", registered_by="alice",
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
        backend_url="http://fake", export_key="test-key", registered_by="alice",
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
