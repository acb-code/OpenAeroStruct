"""Shared helpers used by multiple tool modules.

All functions that need ``sessions`` or ``artifacts`` import them from
``._state`` to avoid circular imports with ``server.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import time
import warnings
from typing import Any

import numpy as np

from ..core.artifacts import _make_run_id  # noqa: F401 – re-export for convenience
from ..core.auth import get_current_user
from ..core.envelope import make_envelope
from ..core.plotting import PLOT_TYPES, generate_plot
from ..core.requirements import check_requirements
from ..core.summary import (
    summarize_aero,
    summarize_aerostruct,
    summarize_drag_polar,
    summarize_optimization,
    summarize_stability,
)
from ..core.telemetry import make_telemetry
from ..core.validation import ValidationFinding, findings_to_dict
from ._state import artifacts as _artifacts

# ---------------------------------------------------------------------------
# Sentinel values for run_id resolution
# ---------------------------------------------------------------------------

_LATEST_SENTINELS = {"latest", "last"}


async def _resolve_run_id(
    run_id: str, session_id: str | None = None
) -> str:
    """Resolve ``"latest"``/``"last"`` to the most recent run_id for the current user."""
    if run_id.lower() in _LATEST_SENTINELS:
        user = get_current_user()
        resolved = await asyncio.to_thread(
            _artifacts.get_latest, user, None, session_id
        )
        if resolved is None:
            raise ValueError(
                "No runs found for the current user. Run an analysis first."
            )
        return resolved
    return run_id


def _get_viewer_base_url() -> str | None:
    """Compute the base URL for the viewer/dashboard HTTP endpoints.

    Uses RESOURCE_SERVER_URL (set on VPS deployments, e.g. https://mcp.lakesideai.dev)
    if available.  Falls back to the local daemon thread viewer port for stdio transport.
    Returns None if no viewer is reachable.
    """
    resource_url = os.environ.get("RESOURCE_SERVER_URL")
    if resource_url:
        return resource_url.rstrip("/")
    prov_port = os.environ.get("OAS_PROV_PORT", "7654")
    if os.environ.get("OAS_PROV_VIEWER", "").lower() != "off":
        return f"http://localhost:{prov_port}"
    return None


def _suppress_output(func, *args, **kwargs):
    """Run func(*args, **kwargs) while suppressing stdout/stderr and OpenMDAO warnings."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return func(*args, **kwargs)


async def _apply_auto_plots(
    envelope: dict,
    session,
    run_id: str,
    results: dict,
    standard_detail: dict | None = None,
) -> None:
    """Generate auto-visualize plots and attach their hashes to the envelope.

    Modifies *envelope* in place, adding an ``auto_plots`` key that maps each
    configured plot_type to its image_hash string.  The full image data is NOT
    included — callers use ``visualize(run_id, plot_type)`` to retrieve it.

    Silently skips any plot_type that fails (e.g. drag_polar requested for an
    aero run that has no alpha sweep data).
    """
    plot_types = session.defaults.auto_visualize
    if not plot_types:
        return

    if standard_detail is None:
        standard_detail = {}

    # Enrich results with sectional_data (mirrors visualize() logic)
    plot_results = dict(results)
    if standard_detail.get("sectional_data"):
        for surf_name, sect in standard_detail["sectional_data"].items():
            if surf_name in plot_results.get("surfaces", {}):
                plot_results["surfaces"][surf_name]["sectional_data"] = sect
        plot_results["sectional_data"] = standard_detail.get("sectional_data", {})

    mesh_snap = standard_detail.get("mesh_snapshot", {})
    mesh_data: dict = {}
    for surf_mesh in mesh_snap.values():
        le = surf_mesh.get("leading_edge")
        te = surf_mesh.get("trailing_edge")
        if le and te:
            mesh_data["mesh"] = np.array([le, te]).tolist()
        break

    auto_plots: dict[str, str | None] = {}
    for plot_type in plot_types:
        try:
            plot_result = await asyncio.to_thread(
                generate_plot, plot_type, run_id, plot_results, {}, mesh_data, ""
            )
            auto_plots[plot_type] = plot_result.metadata["image_hash"]
        except Exception:
            pass  # don't let auto-plot errors block analysis results

    if auto_plots:
        envelope["auto_plots"] = auto_plots


# ---------------------------------------------------------------------------
# Summary dispatch table
# ---------------------------------------------------------------------------

_SUMMARIZERS = {
    "aero":         lambda r, sd, ctx, prev: summarize_aero(r, sd, ctx, prev),
    "aerostruct":   lambda r, sd, ctx, prev: summarize_aerostruct(r, sd, ctx, prev),
    "drag_polar":   lambda r, _sd, ctx, prev: summarize_drag_polar(r, ctx, prev),
    "stability":    lambda r, _sd, ctx, prev: summarize_stability(r, ctx, prev),
    "optimization": lambda r, sd, ctx, _prev: summarize_optimization(r, sd, ctx),
}


async def _finalize_analysis(
    tool_name: str,
    run_id: str,
    session,
    session_id: str,
    surfaces: list[str],
    analysis_type: str,
    inputs: dict,
    results: dict,
    standard_detail: dict | None,
    findings: list,
    t0: float,
    cache_hit: bool,
    run_name: str | None = None,
    surface_dicts: list[dict] | None = None,
    auto_plots: bool = True,
) -> dict:
    """Shared post-analysis: validate requirements, build telemetry, save artifact, build envelope."""
    # Inject failed requirements as validation findings
    if session.requirements:
        req_report = check_requirements(session.requirements, results)
        for outcome in req_report.get("results", []):
            if not outcome.get("passed"):
                findings.append(ValidationFinding(
                    check_id=f"requirements.{outcome['label']}",
                    category="constraints",
                    severity="error",
                    confidence="high",
                    passed=False,
                    message=(
                        f"Requirement '{outcome['label']}': {outcome['path']} "
                        f"{outcome['operator']} {outcome['target']} "
                        f"(actual: {outcome.get('actual')})"
                    ),
                    remediation="Adjust design or requirements.",
                ))

    validation = findings_to_dict(findings)
    elapsed = time.perf_counter() - t0

    mesh_shape = None
    if surface_dicts:
        m = surface_dicts[0].get("mesh")
        if m is not None:
            mesh_shape = tuple(m.shape)
    telem = make_telemetry(elapsed, cache_hit, len(surfaces), mesh_shape)

    # Physics summary (narrative + derived metrics + delta vs previous run)
    previous_results = session.get_last_results(surfaces, analysis_type)
    summarize_fn = _SUMMARIZERS.get(analysis_type)
    summary = summarize_fn(results, standard_detail, inputs, previous_results) if summarize_fn else None
    session.store_last_results(surfaces, analysis_type, results)

    # Build results payload for artifact storage
    results_to_save = dict(results)
    if standard_detail:
        results_to_save["standard_detail"] = standard_detail
    conv_data = session.get_convergence(run_id)
    if conv_data:
        results_to_save["convergence"] = conv_data

    user = get_current_user()
    _artifacts.save(
        session_id=session_id,
        analysis_type=analysis_type,
        tool_name=tool_name,
        surfaces=surfaces,
        parameters=inputs,
        results=results_to_save,
        user=user,
        project=session.project,
        name=run_name,
        validation=validation,
        telemetry=telem,
        run_id=run_id,
    )

    # Auto-prune oldest artifacts if retention limit is configured
    if session.defaults.retention_max_count is not None:
        _artifacts.cleanup(
            user=user,
            project=session.project,
            session_id=session_id,
            max_count=session.defaults.retention_max_count,
            protected_run_ids=set(session._pinned),
        )

    envelope = make_envelope(tool_name, run_id, inputs, results, validation, telem)
    if summary is not None:
        envelope["summary"] = summary
    if auto_plots:
        await _apply_auto_plots(envelope, session, run_id, results, standard_detail)
    return envelope


# ---------------------------------------------------------------------------
# Control-point ordering helpers
# ---------------------------------------------------------------------------
# All *_cp arrays at the MCP boundary are ordered ROOT-to-TIP (standard
# aerospace convention: cp[0]=root, cp[-1]=tip).  OAS internally uses
# TIP-to-ROOT ordering (cp[0]=tip, cp[-1]=root).  These helpers convert.

_CP_KEYS = frozenset({
    "twist_cp", "chord_cp", "t_over_c_cp", "thickness_cp",
    "spar_thickness_cp", "skin_thickness_cp",
})


def _to_oas_order(arr):
    """Root-to-tip (MCP user convention) → tip-to-root (OAS internal)."""
    return arr[::-1].copy()


def _from_oas_order(arr):
    """Tip-to-root (OAS internal) → root-to-tip (MCP user convention)."""
    return arr[::-1].copy()


def _is_cp_dv(name: str) -> bool:
    """Return True if *name* refers to a spanwise control-point DV array."""
    # DV names passed by users omit the _cp suffix (e.g. "twist", "thickness")
    return (name + "_cp") in _CP_KEYS or name in _CP_KEYS
