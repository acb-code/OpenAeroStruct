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
    from oas_mcp.provenance.db import init_db as _prov_init_db

    # Ensure provenance DB is ready (MCP server does this at startup;
    # CLI mode needs it here since server.__main__ is never executed).
    _prov_init_db()

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


_last_run_id: str | None = None


def _extract_run_id(response: dict) -> str | None:
    """Extract run_id from a successful tool response."""
    if not response.get("ok"):
        return None
    result = response.get("result")
    if isinstance(result, dict):
        return result.get("run_id")
    return None


def interpolate_args(args: dict, prev_results: list[dict]) -> dict:
    """Replace ``$prev.run_id`` and ``$N.run_id`` references in string arg values.

    - ``$prev.run_id`` → run_id from the most recent successful step
    - ``$1.run_id``    → run_id from step 1's result (1-indexed)
    - ``latest``/``last`` as a run_id value → left as-is (handled server-side)
    """
    out = {}
    for key, value in args.items():
        if isinstance(value, str):
            if value == "$prev.run_id":
                # Find most recent run_id walking backwards
                for prev in reversed(prev_results):
                    rid = _extract_run_id(prev)
                    if rid:
                        value = rid
                        break
            elif value.startswith("$") and value.endswith(".run_id"):
                # $N.run_id — step reference (1-indexed)
                try:
                    idx = int(value[1:].split(".")[0]) - 1
                    if 0 <= idx < len(prev_results):
                        rid = _extract_run_id(prev_results[idx])
                        if rid:
                            value = rid
                        else:
                            step_tool = prev_results[idx].get("tool", "unknown")
                            raise ValueError(
                                f"Cannot resolve {value!r}: step {idx + 1} "
                                f"({step_tool}) did not return a run_id. "
                                f"Use $prev.run_id to reference the most "
                                f"recent step that produced a run_id."
                            )
                    else:
                        raise ValueError(
                            f"Cannot resolve {value!r}: only "
                            f"{len(prev_results)} steps have completed so far."
                        )
                except ValueError:
                    raise
                except (IndexError,):
                    pass
        out[key] = value
    return out


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

    # In interactive mode, resolve "latest"/"last" run_id to the tracked value
    global _last_run_id
    if "run_id" in args and isinstance(args["run_id"], str):
        if args["run_id"].lower() in ("latest", "last") and _last_run_id is not None:
            args = {**args, "run_id": _last_run_id}

    try:
        result = await fn(**args)
        # visualize() returns a list (image + metadata) — serialize to JSON-safe form
        if isinstance(result, list):
            result = _serialize_list(result)
        response = {"ok": True, "result": result}
        # Track the last run_id for interactive chaining
        rid = _extract_run_id(response)
        if rid:
            _last_run_id = rid
        return response
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
