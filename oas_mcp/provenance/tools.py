"""Three MCP tools for provenance management.

These are plain async functions registered via ``mcp.tool()(_prov_tools.X)``
in server.py — no @mcp.tool() decorator here to avoid double-registration.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from .capture import _get_session_id, _prov_session_id, set_server_session_id
from .db import (
    _dumps,
    _next_seq,
    get_session_graph,
    list_sessions,
    record_decision,
    record_session,
)


async def start_session(
    notes: Annotated[str, "Optional notes describing this provenance session"] = "",
) -> dict:
    """Start a new provenance session and set it as the current session.

    Returns ``{session_id, started_at}``.  Call this at the beginning of a
    workflow to group all subsequent tool calls under a named session.
    """
    from oas_mcp.core.auth import get_current_user

    session_id = f"sess-{uuid.uuid4().hex[:12]}"
    started_at = datetime.now(timezone.utc).isoformat()
    record_session(session_id, notes=notes, user=get_current_user())
    # Set module-level var so all subsequent tool calls (separate asyncio tasks)
    # are recorded under this session.
    set_server_session_id(session_id)
    # Also set ContextVar for test isolation (has priority over module-level).
    _prov_session_id.set(session_id)
    return {"session_id": session_id, "started_at": started_at}


async def log_decision(
    decision_type: Annotated[
        str,
        "Category of decision (e.g. 'dv_selection', 'mesh_resolution', 'constraint_choice', 'result_interpretation')",
    ],
    reasoning: Annotated[str, "Explanation of why this decision was made"],
    selected_action: Annotated[str, "The action or value chosen"],
    prior_call_id: Annotated[
        str | None,
        "call_id from the _provenance field of a preceding tool result that informed this decision",
    ] = None,
    confidence: Annotated[
        str, "Confidence level: 'high', 'medium', or 'low'"
    ] = "medium",
) -> dict:
    """Record a reasoning/decision step in the provenance log.

    Use this before major steps (choosing design variables, setting mesh
    resolution, interpreting unexpected results) to create an audit trail.
    Returns ``{decision_id}``.
    """
    session_id = _get_session_id()
    decision_id = str(uuid.uuid4())
    seq = _next_seq(session_id)
    record_decision(
        decision_id=decision_id,
        session_id=session_id,
        seq=seq,
        decision_type=decision_type,
        reasoning=reasoning,
        prior_call_id=prior_call_id,
        selected_action=selected_action,
        confidence=confidence,
    )
    return {"decision_id": decision_id}


async def export_session_graph(
    session_id: Annotated[
        str | None,
        "Session ID to export (None = current session)",
    ] = None,
    output_path: Annotated[
        str | None,
        "File path to write the JSON graph (None = return only, don't write file)",
    ] = None,
) -> dict:
    """Export the provenance graph for a session as a JSON dict.

    Returns ``{session, nodes, edges, path}`` where *path* is the output file
    path (or null if not written).  Load the JSON into
    ``oas_mcp/provenance/viewer/index.html`` to visualise the DAG.
    """
    sid = session_id or _get_session_id()
    graph = get_session_graph(sid)

    written_path: str | None = None
    if output_path is not None:
        p = Path(output_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(_dumps(graph), encoding="utf-8")
        written_path = str(p)

    # Include viewer URL so agents can surface it to users
    viewer_url: str | None = None
    dashboard_hint: str | None = None
    try:
        from oas_mcp.server import _get_viewer_base_url
        base = _get_viewer_base_url()
        if base:
            viewer_url = f"{base}/viewer?session_id={sid}"
            dashboard_hint = (
                f"View this session's provenance graph at: {viewer_url}\n"
                f"Run dashboards are at: {base}/dashboard?run_id=<run_id>"
            )
    except Exception:
        pass

    return {**graph, "path": written_path, "viewer_url": viewer_url, "dashboard_hint": dashboard_hint}
