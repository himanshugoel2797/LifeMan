"""Shared test fixtures.

A per-test temp DB is set up by pointing `settings.db_path` at a fresh file
and resetting the `_db` module global, so each test gets an isolated
connection that runs the real schema + migrations.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import pytest_asyncio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest_asyncio.fixture
async def temp_db(tmp_path: Path):
    from lifeman import db as db_mod
    from lifeman.config import settings

    prev_path = settings.db_path
    prev_data_dir = settings.data_dir
    settings.db_path = tmp_path / "test.db"
    settings.data_dir = tmp_path

    # Reset the cached connection so get_db() opens a fresh one.
    if db_mod._db is not None:
        await db_mod._db.close()
        db_mod._db = None

    conn = await db_mod.get_db()
    # Register built-in output channels so emit_output has somewhere to deliver.
    from lifeman.outputs.registry import install_builtin_channels
    from lifeman.inputs import install_handlers as install_input_handlers
    from lifeman.memory import install_handlers as install_memory_handlers
    from lifeman.observations import install_handlers as install_observation_handlers
    install_builtin_channels()
    install_input_handlers()
    install_memory_handlers()
    install_observation_handlers()
    try:
        yield conn
    finally:
        await db_mod.close_db()
        settings.db_path = prev_path
        settings.data_dir = prev_data_dir
