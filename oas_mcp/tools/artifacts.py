"""Tools: list_artifacts, get_artifact, get_artifact_summary, delete_artifact."""

from __future__ import annotations

import asyncio
from typing import Annotated

from ..core.auth import get_current_user
from ._state import artifacts as _artifacts


async def list_artifacts(
    session_id: Annotated[str | None, "Filter by session ID, or None to list all sessions"] = None,
    analysis_type: Annotated[
        str | None,
        "Filter by type: 'aero', 'aerostruct', 'drag_polar', 'stability', 'optimization'",
    ] = None,
    project: Annotated[str | None, "Filter by project name (default: all projects)"] = None,
) -> dict:
    """List saved analysis artifacts with optional filters.

    Returns a count and a list of index entries (run_id, session_id,
    analysis_type, timestamp, surfaces, tool_name).  Does not load the
    full results payload — use get_artifact for that.

    Results are scoped to the authenticated user — you cannot list other users' artifacts.
    """
    user = get_current_user()
    entries = await asyncio.to_thread(_artifacts.list, session_id, analysis_type, user, project)
    return {"count": len(entries), "artifacts": entries}


async def get_artifact(
    run_id: Annotated[str, "Run ID returned by an analysis tool"],
    session_id: Annotated[
        str | None, "Session that owns this artifact — speeds up lookup when provided"
    ] = None,
) -> dict:
    """Retrieve a saved artifact (metadata + full results) by run_id.

    Scoped to the authenticated user — you cannot access other users' artifacts.
    """
    user = get_current_user()
    artifact = await asyncio.to_thread(_artifacts.get, run_id, session_id, user)
    if artifact is None:
        raise ValueError(f"Artifact '{run_id}' not found")
    return artifact


async def get_artifact_summary(
    run_id: Annotated[str, "Run ID returned by an analysis tool"],
    session_id: Annotated[str | None, "Session that owns this artifact"] = None,
) -> dict:
    """Retrieve artifact metadata only (no results payload) — much smaller response.

    Returns: run_id, session_id, analysis_type, timestamp, surfaces,
    tool_name, parameters.

    Scoped to the authenticated user.
    """
    user = get_current_user()
    summary = await asyncio.to_thread(_artifacts.get_summary, run_id, session_id, user)
    if summary is None:
        raise ValueError(f"Artifact '{run_id}' not found")
    return summary


async def delete_artifact(
    run_id: Annotated[str, "Run ID to delete"],
    session_id: Annotated[str | None, "Session that owns this artifact"] = None,
) -> dict:
    """Permanently delete a saved artifact from disk.

    Scoped to the authenticated user — you cannot delete other users' artifacts.
    """
    user = get_current_user()
    deleted = await asyncio.to_thread(_artifacts.delete, run_id, session_id, user)
    if not deleted:
        raise ValueError(f"Artifact '{run_id}' not found")
    return {"status": "deleted", "run_id": run_id}
