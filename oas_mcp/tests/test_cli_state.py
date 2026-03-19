"""Tests for oas_mcp.cli_state — state file save/load/clear."""

import json
from pathlib import Path

import pytest

from oas_mcp.cli_state import (
    STATE_DIR,
    clear_state,
    load_surfaces,
    save_surfaces,
    _state_path,
)


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Redirect STATE_DIR to a per-test temp directory."""
    import oas_mcp.cli_state as cs

    monkeypatch.setattr(cs, "STATE_DIR", tmp_path / "state")
    yield tmp_path / "state"


def test_save_and_load_surfaces(isolated_state):
    args = {"name": "wing", "wing_type": "rect", "num_y": 7}
    save_surfaces("default", {"wing": args})
    loaded = load_surfaces("default")
    assert loaded == {"wing": args}


def test_load_missing_workspace():
    loaded = load_surfaces("nonexistent_workspace_xyz")
    assert loaded == {}


def test_save_merges_existing(isolated_state):
    save_surfaces("ws", {"wing": {"name": "wing"}})
    save_surfaces("ws", {"tail": {"name": "tail"}})
    loaded = load_surfaces("ws")
    assert "wing" in loaded
    assert "tail" in loaded


def test_save_overwrites_same_name(isolated_state):
    save_surfaces("ws", {"wing": {"name": "wing", "num_y": 7}})
    save_surfaces("ws", {"wing": {"name": "wing", "num_y": 9}})
    loaded = load_surfaces("ws")
    assert loaded["wing"]["num_y"] == 9


def test_clear_state(isolated_state):
    save_surfaces("ws", {"wing": {"name": "wing"}})
    clear_state("ws")
    loaded = load_surfaces("ws")
    assert loaded == {}


def test_clear_missing_state_is_noop(isolated_state):
    # Should not raise
    clear_state("does_not_exist")


def test_workspace_isolation(isolated_state):
    save_surfaces("ws1", {"wing": {"name": "wing"}})
    save_surfaces("ws2", {"tail": {"name": "tail"}})
    assert "wing" in load_surfaces("ws1")
    assert "tail" not in load_surfaces("ws1")
    assert "tail" in load_surfaces("ws2")
    assert "wing" not in load_surfaces("ws2")


def test_numpy_arrays_serialized(isolated_state):
    import numpy as np

    args = {"name": "wing", "twist_cp": np.array([1.0, 2.0, 3.0])}
    save_surfaces("ws", {"wing": args})

    # Read raw file to verify numpy array was serialized as list
    import oas_mcp.cli_state as cs
    path = cs._state_path("ws")
    raw = json.loads(path.read_text())
    assert raw["surfaces"]["wing"]["twist_cp"] == [1.0, 2.0, 3.0]


def test_load_corrupt_state_returns_empty(isolated_state):
    import oas_mcp.cli_state as cs

    cs.STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = cs._state_path("ws")
    path.write_text("not valid json", encoding="utf-8")
    loaded = load_surfaces("ws")
    assert loaded == {}
