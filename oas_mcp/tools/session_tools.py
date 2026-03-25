"""Tools: reset, configure_session, set_requirements."""

from __future__ import annotations

from typing import Annotated

from ..core.plotting import PLOT_TYPES
from ..core.validators import validate_safe_name
from ._state import sessions as _sessions


async def reset(
    session_id: Annotated[str | None, "Session to reset, or None to reset all sessions"] = None,
) -> dict:
    """Reset sessions and cached OpenMDAO problems.

    If session_id is provided, only that session is cleared.  If None, all
    sessions are cleared.
    """
    if session_id is None:
        _sessions.reset()
        return {"status": "All sessions reset", "cleared": "all"}
    else:
        session = _sessions.get(session_id)
        session.clear()
        return {"status": f"Session '{session_id}' reset", "cleared": session_id}


async def configure_session(
    session_id: Annotated[str, "Session to configure"] = "default",
    default_detail_level: Annotated[
        str | None,
        "Default detail level for get_detailed_results: 'summary' | 'standard'",
    ] = None,
    validation_severity_threshold: Annotated[
        str | None,
        "Minimum severity to include in validation block: 'error' | 'warning' | 'info'",
    ] = None,
    auto_visualize: Annotated[
        list[str] | None,
        "Plot types to auto-generate after each analysis (empty list = none). "
        "E.g. ['lift_distribution', 'drag_polar']",
    ] = None,
    telemetry_mode: Annotated[
        str | None,
        "Override telemetry mode for this session: 'off' | 'logging' | 'otel'",
    ] = None,
    requirements: Annotated[
        list[dict] | None,
        "Set requirements checked against every analysis result. "
        "Each requirement: {path, operator, value, label}. "
        "Operators: ==, !=, <, <=, >, >=. "
        "Example: [{\"path\": \"CL\", \"operator\": \">=\", \"value\": 0.4, \"label\": \"min_CL\"}]",
    ] = None,
    project: Annotated[
        str | None,
        "Project name for organising artifacts under {OAS_DATA_DIR}/{user}/{project}/",
    ] = None,
    visualization_output: Annotated[
        str | None,
        "Default output mode for visualize(): "
        "'inline' = PNG image (default, best for claude.ai), "
        "'file' = save PNG to disk only (no [image] noise in CLI), "
        "'url' = return dashboard/plot URLs (best for remote/VPS CLI)",
    ] = None,
    retention_max_count: Annotated[
        int | None,
        "Maximum number of artifacts to keep per session. "
        "Oldest artifacts are automatically deleted after each analysis when exceeded. "
        "None = unlimited (default).",
    ] = None,
) -> dict:
    """Configure per-session defaults to reduce repeated arguments.

    Settings persist until reset() is called or the server restarts.

    Parameters
    ----------
    default_detail_level:
        Default detail level when get_detailed_results is called.
    validation_severity_threshold:
        Filter validation findings below this severity from responses.
        'error' = show only errors; 'warning' = show errors+warnings; 'info' = show all.
    auto_visualize:
        List of plot_type values to auto-generate after each analysis.
        Plots are returned in the 'auto_plots' key of the response envelope.
    telemetry_mode:
        Override the server-wide OAS_TELEMETRY_MODE for this session.
    requirements:
        Dot-path requirements checked after every analysis in this session.
        Failed requirements appear as "error" findings in the validation block.
    visualization_output:
        Default output mode for all visualize() calls in this session.
        'inline' = return metadata + ImageContent (default, for claude.ai).
        'file' = save PNG to disk, return metadata with file_path (CLI-friendly).
        'url' = return metadata with dashboard_url and plot_url (for VPS CLI).
    """
    session = _sessions.get(session_id)

    updates: dict = {}
    if default_detail_level is not None:
        if default_detail_level not in ("summary", "standard"):
            raise ValueError("default_detail_level must be 'summary' or 'standard'")
        updates["default_detail_level"] = default_detail_level

    if validation_severity_threshold is not None:
        if validation_severity_threshold not in ("error", "warning", "info"):
            raise ValueError("validation_severity_threshold must be 'error', 'warning', or 'info'")
        updates["validation_severity_threshold"] = validation_severity_threshold

    if auto_visualize is not None:
        unknown = [p for p in auto_visualize if p not in PLOT_TYPES]
        if unknown:
            raise ValueError(
                f"Unknown plot type(s) in auto_visualize: {unknown}. "
                f"Supported: {sorted(PLOT_TYPES)}"
            )
        updates["auto_visualize"] = auto_visualize

    if telemetry_mode is not None:
        if telemetry_mode not in ("off", "logging", "otel"):
            raise ValueError("telemetry_mode must be 'off', 'logging', or 'otel'")
        updates["telemetry_mode"] = telemetry_mode

    if project is not None:
        validate_safe_name(project, "project")
        updates["project"] = project

    if visualization_output is not None:
        if visualization_output not in ("inline", "file", "url"):
            raise ValueError(
                "visualization_output must be 'inline', 'file', or 'url'"
            )
        updates["visualization_output"] = visualization_output

    if retention_max_count is not None:
        if retention_max_count < 1:
            raise ValueError("retention_max_count must be >= 1")
        updates["retention_max_count"] = retention_max_count

    if updates:
        session.configure(**updates)

    if requirements is not None:
        session.set_requirements(requirements)

    return {
        "session_id": session_id,
        "project": session.project,
        "status": "configured",
        "current_defaults": session.defaults.to_dict(),
        "requirements_count": len(session.requirements),
    }


async def set_requirements(
    requirements: Annotated[
        list[dict],
        "List of requirement dicts: {path, operator, value, label}. "
        "Operators: ==, !=, <, <=, >, >=. "
        "Example: [{\"path\": \"surfaces.wing.failure\", \"operator\": \"<\", \"value\": 1.0}]",
    ],
    session_id: Annotated[str, "Session identifier"] = "default",
) -> dict:
    """Set requirements that are automatically checked against every analysis result.

    Requirements use dot-path notation to access nested result values and
    compare them using standard operators.  Failed requirements appear as
    'error' severity findings in the validation block of each response.

    Example requirements:
      {"path": "CL", "operator": ">=", "value": 0.4, "label": "min_CL"}
      {"path": "surfaces.wing.failure", "operator": "<", "value": 1.0, "label": "no_failure"}
      {"path": "L_over_D", "operator": ">", "value": 10.0, "label": "min_LD"}
    """
    session = _sessions.get(session_id)
    session.set_requirements(requirements)
    return {
        "session_id": session_id,
        "requirements_set": len(requirements),
        "requirements": requirements,
    }
