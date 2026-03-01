"""Filesystem-backed, thread-safe artifact store for the OAS MCP Server.

Each analysis run is saved as a single JSON file:

    $OAS_DATA_DIR/{session_id}/{run_id}.json   — metadata + results
    $OAS_DATA_DIR/{session_id}/index.json      — [{run_id, analysis_type, …}, …]

Run IDs are formatted as ``YYYYMMDDTHHMMSS_{4hex}`` — human-readable,
chronologically sortable, and collision-resistant.

The index is rebuilt from artifact files automatically if it is missing or
corrupt (self-healing).  All public methods are thread-safe via a single lock.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np


def _default_data_dir() -> Path:
    return Path(os.environ.get("OAS_DATA_DIR", "./oas_data/artifacts"))


class _NumpyEncoder(json.JSONEncoder):
    """Extend JSONEncoder to handle numpy scalars and arrays."""

    def default(self, obj: Any) -> Any:
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        if isinstance(obj, np.integer):
            return int(obj)
        if isinstance(obj, np.floating):
            return float(obj)
        if isinstance(obj, np.bool_):
            return bool(obj)
        return super().default(obj)


def _make_run_id() -> str:
    """Return a sortable, collision-resistant run ID: ``YYYYMMDDTHHMMSS_xxxx``."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    suffix = secrets.token_hex(2)  # 4 hex chars
    return f"{ts}_{suffix}"


class ArtifactStore:
    """Manages analysis artifacts on the local filesystem.

    Parameters
    ----------
    data_dir:
        Root directory for artifacts.  Falls back to the ``OAS_DATA_DIR``
        environment variable, or ``./oas_data/artifacts/`` if that is unset.
    """

    def __init__(self, data_dir: Path | str | None = None) -> None:
        self._data_dir = Path(data_dir) if data_dir is not None else _default_data_dir()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _session_dir(self, session_id: str) -> Path:
        return self._data_dir / session_id

    def _artifact_path(self, session_id: str, run_id: str) -> Path:
        return self._session_dir(session_id) / f"{run_id}.json"

    def _index_path(self, session_id: str) -> Path:
        return self._session_dir(session_id) / "index.json"

    def _load_index(self, session_id: str) -> list[dict]:
        """Load the index for *session_id*, rebuilding if missing or corrupt."""
        index_path = self._index_path(session_id)
        if not index_path.exists():
            return self._rebuild_index(session_id)
        try:
            with index_path.open() as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return self._rebuild_index(session_id)

    def _save_index(self, session_id: str, index: list[dict]) -> None:
        index_path = self._index_path(session_id)
        index_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = index_path.with_suffix(".tmp")
        try:
            with tmp.open("w") as f:
                json.dump(index, f, indent=2, cls=_NumpyEncoder)
            tmp.replace(index_path)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise

    def _rebuild_index(self, session_id: str) -> list[dict]:
        """Reconstruct the index by scanning artifact files on disk."""
        session_dir = self._session_dir(session_id)
        index: list[dict] = []
        if session_dir.exists():
            for path in sorted(session_dir.glob("*.json")):
                if path.name == "index.json":
                    continue
                try:
                    with path.open() as f:
                        artifact = json.load(f)
                    meta = artifact.get("metadata", {})
                    index.append({
                        "run_id": meta.get("run_id", path.stem),
                        "session_id": meta.get("session_id", session_id),
                        "analysis_type": meta.get("analysis_type", "unknown"),
                        "timestamp": meta.get("timestamp", ""),
                        "surfaces": meta.get("surfaces", []),
                        "tool_name": meta.get("tool_name", ""),
                    })
                except (json.JSONDecodeError, OSError):
                    continue
        self._save_index(session_id, index)
        return index

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def save(
        self,
        session_id: str,
        analysis_type: str,
        tool_name: str,
        surfaces: list[str],
        parameters: dict,
        results: Any,
    ) -> str:
        """Persist an analysis artifact and return its ``run_id``.

        Parameters
        ----------
        session_id:
            The session that produced this result.
        analysis_type:
            One of ``"aero"``, ``"aerostruct"``, ``"drag_polar"``,
            ``"stability"``, ``"optimization"``.
        tool_name:
            The MCP tool that was called (e.g. ``"run_aero_analysis"``).
        surfaces:
            List of surface names involved.
        parameters:
            Flight conditions and/or configuration dict.
        results:
            The tool return value (may contain numpy arrays).

        Returns
        -------
        str
            The new ``run_id``.
        """
        run_id = _make_run_id()
        timestamp = datetime.now(timezone.utc).isoformat()

        metadata = {
            "run_id": run_id,
            "session_id": session_id,
            "analysis_type": analysis_type,
            "timestamp": timestamp,
            "surfaces": surfaces,
            "tool_name": tool_name,
            "parameters": parameters,
        }
        artifact = {"metadata": metadata, "results": results}

        with self._lock:
            artifact_path = self._artifact_path(session_id, run_id)
            artifact_path.parent.mkdir(parents=True, exist_ok=True)
            with artifact_path.open("w") as f:
                json.dump(artifact, f, indent=2, cls=_NumpyEncoder)

            index = self._load_index(session_id)
            index.append({
                "run_id": run_id,
                "session_id": session_id,
                "analysis_type": analysis_type,
                "timestamp": timestamp,
                "surfaces": surfaces,
                "tool_name": tool_name,
            })
            self._save_index(session_id, index)

        return run_id

    def list(
        self,
        session_id: str | None = None,
        analysis_type: str | None = None,
    ) -> list[dict]:
        """Return index entries, optionally filtered.

        Parameters
        ----------
        session_id:
            If given, restrict results to that session.
        analysis_type:
            If given, restrict results to that analysis type.
        """
        with self._lock:
            if session_id is not None:
                entries = self._load_index(session_id)
            else:
                entries = []
                if self._data_dir.exists():
                    for session_dir in sorted(self._data_dir.iterdir()):
                        if session_dir.is_dir():
                            entries.extend(self._load_index(session_dir.name))

            if analysis_type is not None:
                entries = [e for e in entries if e.get("analysis_type") == analysis_type]

            return entries

    def get(self, run_id: str, session_id: str | None = None) -> dict | None:
        """Return the full artifact (metadata + results), or ``None`` if not found.

        Parameters
        ----------
        run_id:
            The run ID to look up.
        session_id:
            Optional hint.  If supplied the lookup is O(1); otherwise all
            known session directories are searched.
        """
        with self._lock:
            sessions = (
                [session_id]
                if session_id is not None
                else (
                    [d.name for d in sorted(self._data_dir.iterdir()) if d.is_dir()]
                    if self._data_dir.exists()
                    else []
                )
            )
            for sid in sessions:
                path = self._artifact_path(sid, run_id)
                if path.exists():
                    try:
                        with path.open() as f:
                            return json.load(f)
                    except (json.JSONDecodeError, OSError):
                        return None
        return None

    def get_summary(self, run_id: str, session_id: str | None = None) -> dict | None:
        """Return artifact metadata only (no results payload), or ``None``."""
        artifact = self.get(run_id, session_id)
        if artifact is None:
            return None
        return artifact.get("metadata")

    def delete(self, run_id: str, session_id: str | None = None) -> bool:
        """Delete an artifact from disk and its index entry.

        Returns ``True`` if the artifact was found and removed, ``False``
        if it did not exist.
        """
        with self._lock:
            sessions = (
                [session_id]
                if session_id is not None
                else (
                    [d.name for d in sorted(self._data_dir.iterdir()) if d.is_dir()]
                    if self._data_dir.exists()
                    else []
                )
            )
            for sid in sessions:
                path = self._artifact_path(sid, run_id)
                if path.exists():
                    path.unlink()
                    index = self._load_index(sid)
                    index = [e for e in index if e.get("run_id") != run_id]
                    self._save_index(sid, index)
                    return True
        return False
