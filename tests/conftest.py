"""Shared test fixtures."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from lean_ai_serve.config import Settings, set_settings
from lean_ai_serve.db import Database


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Create test settings with temp cache directory."""
    s = Settings(
        cache={"directory": str(tmp_path / "cache")},
        security={"mode": "none"},
    )
    set_settings(s)
    return s


@pytest.fixture
async def db(tmp_path: Path) -> Database:
    """Create a test database."""
    db = Database(tmp_path / "test.db")
    await db.connect()
    yield db
    await db.close()
