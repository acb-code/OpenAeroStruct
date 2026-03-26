"""Fixtures for rectangular wing demonstration parity tests."""

import uuid

import pytest
import pytest_asyncio
from oas_mcp.server import _artifacts, reset


@pytest.fixture(autouse=True)
def isolate_artifacts(tmp_path):
    """Redirect artifact storage to a per-test temp directory."""
    original = _artifacts._data_dir
    _artifacts._data_dir = tmp_path / "artifacts"
    yield
    _artifacts._data_dir = original


@pytest.fixture(autouse=True)
def isolate_provenance(tmp_path):
    """Redirect provenance DB to a per-test temp file."""
    from oas_mcp.provenance.capture import _prov_session_id
    from oas_mcp.provenance.db import init_db

    init_db(tmp_path / "prov.db")
    token = _prov_session_id.set(f"demo-{uuid.uuid4().hex[:8]}")
    yield
    _prov_session_id.reset(token)


@pytest_asyncio.fixture(autouse=True)
async def clean_session(isolate_provenance):
    """Reset global session before every test."""
    await reset()
    yield
    await reset()
