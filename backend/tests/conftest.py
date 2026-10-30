"""Pytest fixtures providing isolated temporary SQLite databases per test."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

from db.session import (
    create_db_engine,
    create_session_factory,
    init_db,
)
import pytest
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)


@pytest.fixture
async def isolated_engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    """Create a fresh SQLite database in tmp_path for each test function."""
    db_file = tmp_path / "test_operation_shadow.db"
    engine = create_db_engine(f"sqlite+aiosqlite:///{db_file}")
    await init_db(engine, seed_defaults=True)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def session_factory(
    isolated_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    """Return an async_sessionmaker bound to the test's isolated SQLite engine."""
    return create_session_factory(isolated_engine)


@pytest.fixture
async def db_session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Yield an isolated AsyncSession that rolls back uncommitted state on teardown."""
    async with session_factory() as session:
        yield session
