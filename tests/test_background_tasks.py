"""Tests for background maintenance scheduler."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from lean_ai_serve.config import Settings, set_settings
from lean_ai_serve.observability.tasks import BackgroundScheduler


@pytest.fixture()
def settings():
    s = Settings()
    set_settings(s)
    return s


@pytest.fixture()
def mock_db():
    db = AsyncMock()
    db.execute = AsyncMock(return_value=MagicMock(rowcount=0))
    db.commit = AsyncMock()
    db.fetchall = AsyncMock(return_value=[])
    return db


# ---------------------------------------------------------------------------
# Start / stop lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_stop_lifecycle(settings, mock_db):
    """start() creates tasks, stop() cancels them cleanly."""
    scheduler = BackgroundScheduler(mock_db, settings)
    await scheduler.start()
    assert len(scheduler._tasks) == 7  # 7 task types
    assert all(not t.done() for t in scheduler._tasks)

    await scheduler.stop()
    assert len(scheduler._tasks) == 0


@pytest.mark.asyncio
async def test_stop_idempotent(settings, mock_db):
    """stop() can be called even if not started."""
    scheduler = BackgroundScheduler(mock_db, settings)
    await scheduler.stop()  # Should not raise


# ---------------------------------------------------------------------------
# Individual task execution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_token_cleanup_called(settings, mock_db):
    """Token cleanup task calls cleanup_revoked_tokens."""
    scheduler = BackgroundScheduler(mock_db, settings)
    with patch(
        "lean_ai_serve.security.auth.cleanup_revoked_tokens",
        new_callable=AsyncMock,
        return_value=5,
    ) as mock_cleanup:
        await scheduler._cleanup_tokens()
        mock_cleanup.assert_called_once_with(mock_db)


@pytest.mark.asyncio
async def test_rate_limiter_cleanup_called(settings, mock_db):
    """Rate limiter cleanup calls rate_limiter.cleanup()."""
    mock_rl = MagicMock()
    mock_rl.cleanup.return_value = 3
    scheduler = BackgroundScheduler(mock_db, settings, rate_limiter=mock_rl)
    await scheduler._cleanup_rate_limiter()
    mock_rl.cleanup.assert_called_once()


@pytest.mark.asyncio
async def test_rate_limiter_cleanup_skipped_when_none(settings, mock_db):
    """Rate limiter cleanup is no-op when rate_limiter is None."""
    scheduler = BackgroundScheduler(mock_db, settings, rate_limiter=None)
    await scheduler._cleanup_rate_limiter()  # Should not raise


@pytest.mark.asyncio
async def test_audit_retention_deletes_old_entries(settings, mock_db):
    """Audit retention deletes entries older than retention_days."""
    mock_db.execute = AsyncMock(return_value=MagicMock(rowcount=10))
    scheduler = BackgroundScheduler(mock_db, settings)
    await scheduler._cleanup_audit()
    mock_db.execute.assert_called_once()
    call_args = mock_db.execute.call_args
    assert "DELETE FROM audit_log WHERE timestamp <" in call_args[0][0]
    mock_db.commit.assert_called()


@pytest.mark.asyncio
async def test_usage_cleanup_called(settings, mock_db):
    """Usage cleanup calls usage_tracker.cleanup()."""
    mock_ut = AsyncMock()
    scheduler = BackgroundScheduler(mock_db, settings, usage_tracker=mock_ut)
    await scheduler._cleanup_usage()
    mock_ut.cleanup.assert_called_once()


@pytest.mark.asyncio
async def test_gpu_snapshot_called(settings, mock_db):
    """GPU snapshot calls metrics.record_gpu_snapshot()."""
    mock_metrics = MagicMock()
    scheduler = BackgroundScheduler(mock_db, settings, metrics=mock_metrics)
    with patch("lean_ai_serve.utils.gpu.get_gpu_info", return_value=[]):
        await scheduler._snapshot_gpu()
    mock_metrics.record_gpu_snapshot.assert_called_once_with([])


@pytest.mark.asyncio
async def test_gpu_snapshot_skipped_when_no_metrics(settings, mock_db):
    """GPU snapshot is no-op when metrics is None."""
    scheduler = BackgroundScheduler(mock_db, settings, metrics=None)
    await scheduler._snapshot_gpu()  # Should not raise


@pytest.mark.asyncio
async def test_alert_evaluation_called(settings, mock_db):
    """Alert evaluation calls alert_evaluator.evaluate()."""
    mock_alert = MagicMock()
    scheduler = BackgroundScheduler(mock_db, settings, alert_evaluator=mock_alert)
    await scheduler._evaluate_alerts()
    mock_alert.evaluate.assert_called_once()


# ---------------------------------------------------------------------------
# Error resilience
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_task_error_doesnt_crash_loop(settings, mock_db):
    """Exception in a task doesn't stop the periodic loop."""
    call_count = 0

    async def failing_task():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("test error")

    scheduler = BackgroundScheduler(mock_db, settings)
    task = asyncio.create_task(scheduler._run_periodic("test", 0.05, failing_task))

    # Wait for at least 2 iterations
    await asyncio.sleep(0.15)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Should have been called multiple times despite the error
    assert call_count >= 2


@pytest.fixture(autouse=True)
def _clear_settings():
    yield
    set_settings(None)
