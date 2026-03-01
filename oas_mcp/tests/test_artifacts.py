"""
Unit tests for ArtifactStore.

No OpenAeroStruct or OpenMDAO required — these tests only exercise
oas_mcp/core/artifacts.py and run instantly.
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from oas_mcp.core.artifacts import ArtifactStore


@pytest.fixture
def store(tmp_path):
    return ArtifactStore(data_dir=tmp_path)


# ---------------------------------------------------------------------------
# save
# ---------------------------------------------------------------------------


def test_save_returns_non_empty_run_id(store):
    run_id = store.save("s1", "aero", "run_aero_analysis", ["wing"], {}, {"CL": 0.5})
    assert run_id and isinstance(run_id, str)


def test_save_creates_artifact_file(store, tmp_path):
    run_id = store.save("s1", "aero", "run_aero_analysis", ["wing"], {}, {"CL": 0.5})
    artifact_file = tmp_path / "s1" / f"{run_id}.json"
    assert artifact_file.exists()


def test_save_artifact_contains_metadata_and_results(store, tmp_path):
    run_id = store.save(
        "s1", "aero", "run_aero_analysis", ["wing"], {"alpha": 5.0}, {"CL": 0.5, "CD": 0.02}
    )
    with (tmp_path / "s1" / f"{run_id}.json").open() as f:
        data = json.load(f)

    assert data["metadata"]["run_id"] == run_id
    assert data["metadata"]["analysis_type"] == "aero"
    assert data["metadata"]["tool_name"] == "run_aero_analysis"
    assert data["metadata"]["surfaces"] == ["wing"]
    assert data["metadata"]["parameters"] == {"alpha": 5.0}
    assert data["results"]["CL"] == pytest.approx(0.5)
    assert data["results"]["CD"] == pytest.approx(0.02)


def test_save_creates_index_entry(store, tmp_path):
    run_id = store.save("s1", "aero", "run_aero_analysis", ["wing"], {}, {})
    index_file = tmp_path / "s1" / "index.json"
    assert index_file.exists()
    with index_file.open() as f:
        index = json.load(f)
    assert any(e["run_id"] == run_id for e in index)


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def test_list_all_sessions(store):
    r1 = store.save("s1", "aero", "run_aero_analysis", ["wing"], {}, {})
    r2 = store.save("s1", "drag_polar", "compute_drag_polar", ["wing"], {}, {})
    r3 = store.save("s2", "aerostruct", "run_aerostruct_analysis", ["wing"], {}, {})

    ids = {e["run_id"] for e in store.list()}
    assert {r1, r2, r3} <= ids


def test_list_filter_by_session(store):
    r1 = store.save("s1", "aero", "run_aero_analysis", ["wing"], {}, {})
    r2 = store.save("s2", "aero", "run_aero_analysis", ["wing"], {}, {})

    ids = {e["run_id"] for e in store.list(session_id="s1")}
    assert r1 in ids
    assert r2 not in ids


def test_list_filter_by_analysis_type(store):
    r1 = store.save("s1", "aero", "run_aero_analysis", ["wing"], {}, {})
    r2 = store.save("s1", "drag_polar", "compute_drag_polar", ["wing"], {}, {})

    ids = {e["run_id"] for e in store.list(session_id="s1", analysis_type="aero")}
    assert r1 in ids
    assert r2 not in ids


def test_list_empty_session(store):
    entries = store.list(session_id="no_such_session")
    assert entries == []


# ---------------------------------------------------------------------------
# get / get_summary
# ---------------------------------------------------------------------------


def test_get_full_artifact(store):
    run_id = store.save("s1", "aero", "run_aero_analysis", ["wing"], {"alpha": 5.0}, {"CL": 0.5})
    artifact = store.get(run_id, session_id="s1")

    assert artifact is not None
    assert artifact["metadata"]["run_id"] == run_id
    assert artifact["results"]["CL"] == pytest.approx(0.5)


def test_get_without_session_hint(store):
    r1 = store.save("s1", "aero", "run_aero_analysis", ["wing"], {}, {"CL": 0.3})
    r2 = store.save("s2", "aero", "run_aero_analysis", ["wing"], {}, {"CL": 0.7})

    assert store.get(r1) is not None
    assert store.get(r2) is not None


def test_get_not_found(store):
    assert store.get("nonexistent_run_id", session_id="s1") is None


def test_get_summary_contains_metadata_only(store):
    run_id = store.save("s1", "aero", "run_aero_analysis", ["wing"], {"alpha": 5.0}, {"CL": 0.5})
    summary = store.get_summary(run_id, session_id="s1")

    assert summary is not None
    assert summary["run_id"] == run_id
    assert "results" not in summary


def test_get_summary_not_found(store):
    assert store.get_summary("bad_id", session_id="s1") is None


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


def test_delete_removes_file_and_index_entry(store, tmp_path):
    run_id = store.save("s1", "aero", "run_aero_analysis", ["wing"], {}, {})
    artifact_file = tmp_path / "s1" / f"{run_id}.json"
    assert artifact_file.exists()

    deleted = store.delete(run_id, session_id="s1")
    assert deleted is True
    assert not artifact_file.exists()

    with (tmp_path / "s1" / "index.json").open() as f:
        index = json.load(f)
    assert all(e["run_id"] != run_id for e in index)


def test_delete_without_session_hint(store, tmp_path):
    run_id = store.save("s2", "aero", "run_aero_analysis", ["wing"], {}, {})
    deleted = store.delete(run_id)
    assert deleted is True
    assert not (tmp_path / "s2" / f"{run_id}.json").exists()


def test_delete_not_found(store):
    assert store.delete("nonexistent_id", session_id="s1") is False


# ---------------------------------------------------------------------------
# numpy serialisation
# ---------------------------------------------------------------------------


def test_numpy_array_serialised_to_list(store, tmp_path):
    run_id = store.save(
        "s1", "aero", "run_aero_analysis", ["wing"], {},
        {"array": np.array([1.0, 2.0, 3.0])},
    )
    with (tmp_path / "s1" / f"{run_id}.json").open() as f:
        data = json.load(f)
    assert data["results"]["array"] == [1.0, 2.0, 3.0]


def test_numpy_scalar_serialised(store, tmp_path):
    run_id = store.save(
        "s1", "aero", "run_aero_analysis", ["wing"], {},
        {"scalar": np.float64(0.42), "int_val": np.int32(7)},
    )
    with (tmp_path / "s1" / f"{run_id}.json").open() as f:
        data = json.load(f)
    assert data["results"]["scalar"] == pytest.approx(0.42)
    assert data["results"]["int_val"] == 7


# ---------------------------------------------------------------------------
# index self-healing
# ---------------------------------------------------------------------------


def test_index_rebuilt_when_missing(store, tmp_path):
    run_id = store.save("s1", "aero", "run_aero_analysis", ["wing"], {}, {"CL": 0.6})
    # Delete the index file
    (tmp_path / "s1" / "index.json").unlink()

    entries = store.list(session_id="s1")
    assert any(e["run_id"] == run_id for e in entries)


def test_index_rebuilt_when_corrupt(store, tmp_path):
    run_id = store.save("s1", "aero", "run_aero_analysis", ["wing"], {}, {"CL": 0.6})
    # Corrupt the index
    (tmp_path / "s1" / "index.json").write_text("not valid json {{{")

    entries = store.list(session_id="s1")
    assert any(e["run_id"] == run_id for e in entries)
