"""Shared execution logic for oas-cli.

Provides a tool registry and run_tool() function used by all three CLI modes.
Tool functions are imported from server.py (which creates FastMCP as a side
effect — harmless since the CLI never calls mcp.run()).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable

import numpy as np

# ---------------------------------------------------------------------------
# JSON serialization
# ---------------------------------------------------------------------------


class _NumpyEncoder(json.JSONEncoder):
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


def json_dumps(obj: Any, pretty: bool = False) -> str:
    """Serialize obj to JSON, handling numpy types."""
    indent = 2 if pretty else None
    return json.dumps(obj, cls=_NumpyEncoder, indent=indent)


# ---------------------------------------------------------------------------
# Tool registry — populated lazily on first access
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, Callable] | None = None


def _build_registry() -> dict[str, Callable]:
    """Import tool functions from server.py and build name → function map."""
    # Import here to defer the heavy OpenAeroStruct import until needed.
    from oas_mcp import server  # noqa: F401  (side-effect: registers tools)
    from oas_mcp.provenance import tools as _prov_tools

    # Analysis tools defined directly in server.py
    analysis_tools = [
        "create_surface",
        "run_aero_analysis",
        "run_aerostruct_analysis",
        "compute_drag_polar",
        "compute_stability_derivatives",
        "run_optimization",
        "reset",
        "list_artifacts",
        "get_artifact",
        "get_artifact_summary",
        "delete_artifact",
        "get_run",
        "pin_run",
        "unpin_run",
        "get_detailed_results",
        "visualize",
        "get_n2_html",
        "get_last_logs",
        "configure_session",
        "set_requirements",
    ]

    registry: dict[str, Callable] = {}
    for name in analysis_tools:
        fn = getattr(server, name, None)
        if fn is not None:
            registry[name] = fn

    # Provenance tools registered separately
    registry["start_session"] = _prov_tools.start_session
    registry["log_decision"] = _prov_tools.log_decision
    registry["export_session_graph"] = _prov_tools.export_session_graph

    return registry


def get_registry() -> dict[str, Callable]:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = _build_registry()
    return _REGISTRY


# ---------------------------------------------------------------------------
# run_tool — call a tool by name, return JSON-serializable dict
# ---------------------------------------------------------------------------


async def run_tool(name: str, args: dict) -> dict:
    """Call a tool function by name with the given args.

    Returns a dict with either ``{"ok": True, "result": ...}`` or
    ``{"ok": False, "error": {"code": ..., "message": ...}}``.
    """
    registry = get_registry()
    fn = registry.get(name)
    if fn is None:
        return {
            "ok": False,
            "error": {
                "code": "USER_INPUT_ERROR",
                "message": f"Unknown tool: {name!r}. Available: {sorted(registry)}",
            },
        }

    try:
        result = await fn(**args)
        # visualize() returns a list (image + metadata) — serialize to JSON-safe form
        if isinstance(result, list):
            result = _serialize_list(result)
        return {"ok": True, "result": result}
    except Exception as exc:
        from oas_mcp.core.errors import OASMCPError

        if isinstance(exc, OASMCPError):
            return {"ok": False, "error": exc.to_dict()}
        return {
            "ok": False,
            "error": {
                "code": "INTERNAL_ERROR",
                "message": str(exc),
            },
        }


def run_tool_sync(name: str, args: dict) -> dict:
    """Synchronous wrapper around run_tool()."""
    return asyncio.run(run_tool(name, args))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _serialize_list(items: list) -> list:
    """Convert a mixed list (may contain MCP ImageContent / TextContent) to JSON-safe form."""
    out = []
    for item in items:
        if hasattr(item, "model_dump"):
            out.append(item.model_dump())
        elif hasattr(item, "__dict__"):
            out.append(vars(item))
        else:
            out.append(item)
    return out


def list_tools() -> list[str]:
    """Return sorted list of available tool names."""
    return sorted(get_registry())
