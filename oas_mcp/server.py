"""
OAS MCP Server — FastMCP entry point.

All @mcp.tool() registrations live here.  Heavy OpenMDAO work is dispatched
to a thread pool via asyncio.to_thread() so the event loop stays responsive.
"""

from __future__ import annotations

# Load .env before any module-level env var reads.
# FastMCP() is constructed at module level — dotenv must run first.
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv()
except ImportError:
    pass

import asyncio
import contextlib
import io
import json
import os
import time
import warnings
from typing import Annotated, Any

import numpy as np
from mcp.server.fastmcp import FastMCP

from .core.builders import (
    build_aero_problem,
    build_aerostruct_problem,
    build_optimization_problem,
    build_multipoint_optimization_problem,
)
from .core.defaults import (
    DEFAULT_AEROSTRUCT_CONDITIONS,
    DEFAULT_AERO_CONDITIONS,
    DEFAULT_WINGBOX_UPPER_X,
    DEFAULT_WINGBOX_UPPER_Y,
    DEFAULT_WINGBOX_LOWER_X,
    DEFAULT_WINGBOX_LOWER_Y,
)
from .core.envelope import make_envelope, make_error_envelope
from .core.errors import OASMCPError, UserInputError, SolverConvergenceError
from .core.mesh import apply_dihedral, apply_sweep, apply_taper, build_mesh
from .core.plotting import PLOT_TYPES, generate_plot, generate_n2
from .core.widget import DASHBOARD_HTML, extract_plot_data
from .core.requirements import check_requirements
from .core.results import (
    extract_aero_results,
    extract_aerostruct_results,
    extract_multipoint_results,
    extract_stability_results,
    extract_standard_detail,
)
from .core.telemetry import get_run_logs, make_telemetry
from .core.summary import (
    summarize_aero,
    summarize_aerostruct,
    summarize_drag_polar,
    summarize_stability,
    summarize_optimization,
)
from .core.validation import (
    findings_to_dict,
    validate_aero,
    validate_aerostruct,
    validate_drag_polar,
    validate_optimization,
    validate_stability,
)
from .core.artifacts import ArtifactStore, _make_run_id
from .core.auth import build_auth_settings, build_token_verifier, get_current_user, _env as _auth_env
from .provenance.db import init_db as _prov_init_db, record_session as _prov_record_session
from .provenance.capture import capture_tool, _prov_session_id
from .provenance import tools as _prov_tools
from .core.session import SessionManager
from .core.validators import (
    validate_fem_model_type,
    validate_flight_conditions,
    validate_flight_points,
    validate_mesh_params,
    validate_safe_name,
    validate_struct_props_present,
    validate_surface_names_exist,
    validate_wing_type,
)

mcp = FastMCP(
    "OpenAeroStruct",
    auth=build_auth_settings(),
    token_verifier=build_token_verifier(),
    # Pass host at construction time so FastMCP configures DNS rebinding protection
    # correctly.  When host="0.0.0.0" FastMCP skips the localhost-only allowlist,
    # which is required for ngrok (requests arrive with Host: <ngrok-url>).
    host=os.environ.get("OAS_HOST", "127.0.0.1"),
    instructions="""OpenAeroStruct aerostructural analysis and optimisation server.

REQUIRED WORKFLOW — always follow this order:
  1. create_surface  — define geometry (call once per surface; must precede any analysis)
  2. run_aero_analysis / run_aerostruct_analysis / compute_drag_polar / etc. — analyse
  3. run_optimization (optional) — optimise design variables
  4. reset (optional) — clear state between unrelated experiments

CRITICAL CONSTRAINTS:
  • num_y must be ODD (3, 5, 7, 9, …). Passing an even value raises an error.
  • Structural tools (run_aerostruct_analysis, and aerostruct optimization) require
    create_surface to have been called with fem_model_type="tube" or "wingbox" plus
    material properties (E, G, yield_stress, mrho). Aero-only surfaces will error.
  • Surface names are arbitrary strings but must match exactly between create_surface
    and analysis calls.

RESPONSE ENVELOPE (all analysis tools):
  Every analysis tool returns a versioned envelope (schema_version="1.0"):
    • results:     tool-specific payload (CL, CD, etc.)
    • validation:  physics and numerics checks — check "passed" before trusting results
    • telemetry:   timing and cache hit info
    • run_id:      use for get_run(), pin_run(), get_detailed_results(), visualize()
    • error:       present when the tool failed; check error.code for action to take
  Error codes: USER_INPUT_ERROR, SOLVER_CONVERGENCE_ERROR, CACHE_EVICTED_ERROR, INTERNAL_ERROR

VALIDATION:
  • Each response includes a "validation" block with physics/numerics checks.
  • Check validation.passed — if False, review validation.findings for error/warning details.
  • Each finding has: check_id, severity (error/warning/info), message, remediation hint.

OBSERVABILITY TOOLS:
  • get_run(run_id)                    — full manifest: inputs, outputs, validation, cache state
  • pin_run(run_id, surfaces, type)    — prevent cache eviction during multi-step workflows
  • unpin_run(run_id)                  — release pin when done
  • get_detailed_results(run_id, lvl)  — "standard" = sectional data; "full" = raw arrays
  • visualize(run_id, plot_type)       — ImageContent: lift_distribution, drag_polar,
                                         stress_distribution, convergence, planform,
                                         opt_history, opt_dv_evolution, opt_comparison
  • get_last_logs(run_id)              — server-side log records for debugging
  • configure_session(session_id, ...) — set per-session defaults (detail level, auto-plots, etc.)

VISUALIZATION OUTPUT MODES:
  visualize() supports three output modes, controlled per-call (output=) or per-session
  (configure_session(visualization_output=)):
  • "inline" (default) — returns [metadata, ImageContent] — best for claude.ai
  • "file"             — saves PNG to disk, returns [metadata] with file_path — no [image] noise in CLI
  • "url"              — returns [metadata] with dashboard_url + plot_url — clickable links for CLI
  The per-call output= parameter overrides the session default.
  In CLI environments (Claude Code), prefer "file" or "url" mode to avoid unhelpful [image] output.

PARAMETER TIPS:
  • Cruise conditions: velocity=248 m/s, Mach_number=0.84, density=0.38 kg/m³, re=1e6
  • Good starting mesh: num_x=2, num_y=7 (fast); use num_y=15 for higher fidelity
  • wing_type="CRM" produces a realistic transport wing with built-in twist;
    wing_type="rect" produces a flat untwisted planform — simpler but less realistic
  • failure > 1.0 means structural failure (utilisation ratio > 1); failure < 1.0 = OK
  • L_equals_W residual near 0 means the wing is sized to carry the aircraft weight

CONTROL-POINT ORDERING:
  • All *_cp arrays (twist_cp, chord_cp, thickness_cp, etc.) are ordered ROOT-to-TIP:
    cp[0] = root value, cp[-1] = tip value.
  • Example: twist_cp=[-7, 0] means root=-7° (washed in), tip=0° — correct washout.
  • Optimised DV arrays returned by run_optimization use the same root-to-tip ordering.

PERFORMANCE:
  • The first run_aero_analysis call builds and sets up the OpenMDAO problem (~0.1 s).
    Subsequent calls with the same surfaces reuse the cached problem — only the flight
    conditions change, so parameter sweeps are very fast.
  • Calling create_surface again with the same name invalidates the cache.
  • Use pin_run() to guarantee cache availability during multi-step workflows.

ARTIFACT STORAGE (every analysis is automatically saved):
  • Each analysis tool returns a run_id — use it to retrieve results later.
  • Storage hierarchy: {OAS_DATA_DIR}/{user}/{project}/{session_id}/{run_id}.json
  • OAS_USER env var sets user identity (default: OS login name)
  • OAS_PROJECT env var sets default project (default: "default")
  • Pass run_name="my label" to any analysis tool to tag a run
  • list_artifacts(session_id?, analysis_type?, user?, project?) — browse saved runs
  • get_artifact(run_id) — full metadata + results for a past run
  • get_artifact_summary(run_id) — metadata only (lightweight)
  • delete_artifact(run_id) — remove a saved artifact
  • oas://artifacts/{run_id} — resource access to any artifact by run_id

DESIGN VARIABLE NAMES FOR run_optimization:
  • All models:   'twist', 'chord', 'sweep', 'taper', 'alpha'
    Note: '_cp' suffix is accepted as an alias (e.g. 'twist_cp' → 'twist')
  • Tube only:    'thickness'   (maps to thickness_cp — does NOT exist on wingbox surfaces)
  • Wingbox only: 'spar_thickness', 'skin_thickness'  (do NOT use 'thickness' for wingbox)

CONSTRAINT NAMES FOR run_optimization:
  • All aerostruct: 'CL', 'CD', 'CM', 'failure', 'L_equals_W'
  • Tube only:      'thickness_intersects'  (NOT available for wingbox — raises an error)

Use the prompts (analyze_wing, aerostructural_design, optimize_wing, compare_designs) for guided
workflows, and the resources (oas://reference, oas://workflows) for quick lookup.

PROVENANCE & DECISION LOGGING:
  • start_session(notes)           — begin a named provenance session; call at workflow start
  • log_decision(type, reasoning, selected_action, prior_call_id?, confidence?) — record why
  • export_session_graph(session_id?, output_path?) — export DAG as JSON; load into viewer""",
)

_sessions = SessionManager()
_artifacts = ArtifactStore()

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
    # VPS / HTTP transport: RESOURCE_SERVER_URL is authoritative
    resource_url = os.environ.get("RESOURCE_SERVER_URL")
    if resource_url:
        return resource_url.rstrip("/")
    # stdio transport: check if the local daemon viewer is running
    prov_port = os.environ.get("OAS_PROV_PORT", "7654")
    if os.environ.get("OAS_PROV_VIEWER", "").lower() != "off":
        return f"http://localhost:{prov_port}"
    return None

# ---------------------------------------------------------------------------
# Provenance tools registration
# ---------------------------------------------------------------------------

mcp.tool()(_prov_tools.start_session)
mcp.tool()(_prov_tools.log_decision)
mcp.tool()(_prov_tools.export_session_graph)

# ---------------------------------------------------------------------------
# MCP Apps widget resource
# ---------------------------------------------------------------------------

_WIDGET_URI = "ui://oas/dashboard.html"


@mcp.resource(
    _WIDGET_URI,
    name="OAS Dashboard",
    description="Interactive OpenAeroStruct dashboard (MCP Apps widget)",
    mime_type="text/html;profile=mcp-app",
    meta={"ui": {"csp": {"resourceDomains": ["https://cdn.plot.ly", "https://unpkg.com"]}}},
)
def oas_dashboard_view() -> str:
    """Return the embedded dashboard HTML for MCP Apps hosts."""
    return DASHBOARD_HTML


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
    from .core.validation import ValidationFinding

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

    _artifacts.save(
        session_id=session_id,
        analysis_type=analysis_type,
        tool_name=tool_name,
        surfaces=surfaces,
        parameters=inputs,
        results=results_to_save,
        user=get_current_user(),
        project=session.project,
        name=run_name,
        validation=validation,
        telemetry=telem,
        run_id=run_id,
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


# ---------------------------------------------------------------------------
# Tool 1 — create_surface
# ---------------------------------------------------------------------------


@mcp.tool()
@capture_tool
async def create_surface(
    name: Annotated[str, "Unique surface name (e.g. 'wing', 'tail')"] = "wing",
    wing_type: Annotated[str, "Mesh type: 'rect', 'CRM', or 'uCRM_based'"] = "rect",
    span: Annotated[float, "Full wingspan in metres"] = 10.0,
    root_chord: Annotated[float, "Root chord length in metres"] = 1.0,
    taper: Annotated[float, "Taper ratio (tip_chord / root_chord), 1.0 = no taper"] = 1.0,
    sweep: Annotated[float, "Leading-edge sweep angle in degrees"] = 0.0,
    dihedral: Annotated[float, "Dihedral angle in degrees"] = 0.0,
    num_x: Annotated[int, "Number of chordwise mesh nodes (>= 2)"] = 2,
    num_y: Annotated[int, "Number of spanwise mesh nodes (must be odd, >= 3)"] = 7,
    symmetry: Annotated[bool, "If True, model only one half of the wing"] = True,
    twist_cp: Annotated[list[float] | None, "Twist control-point values in degrees, ordered root-to-tip (None = zero twist)"] = None,
    chord_cp: Annotated[list[float] | None, "Chord control-point scale factors, ordered root-to-tip (None = unit chord)"] = None,
    t_over_c_cp: Annotated[list[float] | None, "Thickness-to-chord ratio control points, ordered root-to-tip (None = [0.15])"] = None,
    CL0: Annotated[float, "Lift coefficient at alpha=0 (profile)"] = 0.0,
    CD0: Annotated[float, "Zero-lift drag coefficient (profile)"] = 0.015,
    with_viscous: Annotated[bool, "Include viscous (skin-friction) drag"] = True,
    with_wave: Annotated[bool, "Include wave drag"] = False,
    fem_model_type: Annotated[str | None, "Structural model: 'tube', 'wingbox', or None for aero-only"] = None,
    thickness_cp: Annotated[list[float] | None, "Tube wall thickness control points in metres, ordered root-to-tip (tube model only)"] = None,
    spar_thickness_cp: Annotated[list[float] | None, "Wingbox spar thickness control points in metres, ordered root-to-tip (wingbox model only)"] = None,
    skin_thickness_cp: Annotated[list[float] | None, "Wingbox skin thickness control points in metres, ordered root-to-tip (wingbox model only)"] = None,
    original_wingbox_airfoil_t_over_c: Annotated[float, "Thickness-to-chord ratio of the reference airfoil used for wingbox cross-section geometry (wingbox model only)"] = 0.12,
    E: Annotated[float, "Young's modulus in Pa (default: aluminium 7075, 70 GPa)"] = 70.0e9,
    G: Annotated[float, "Shear modulus in Pa (default: aluminium 7075, 30 GPa)"] = 30.0e9,
    yield_stress: Annotated[float, "Yield stress in Pa (default: 500 MPa)"] = 500.0e6,
    safety_factor: Annotated[float, "Safety factor applied to yield stress"] = 2.5,
    mrho: Annotated[float, "Material density in kg/m^3 (default: Al 7075, 3000 kg/m^3)"] = 3.0e3,
    offset: Annotated[list[float] | None, "3-element [x, y, z] offset of the surface origin in metres"] = None,
    S_ref_type: Annotated[str, "Reference area type: 'wetted' or 'projected'"] = "wetted",
    c_max_t: Annotated[float, "Chordwise location of maximum thickness (fraction of chord)"] = 0.303,
    wing_weight_ratio: Annotated[float, "Ratio of total wing weight to structural wing weight"] = 2.0,
    struct_weight_relief: Annotated[bool, "If True, include structural weight relief in the load distribution"] = False,
    distributed_fuel_weight: Annotated[bool, "If True, include distributed fuel weight in the load distribution"] = False,
    fuel_density: Annotated[float, "Fuel density in kg/m^3 (needed for fuel volume constraint)"] = 803.0,
    Wf_reserve: Annotated[float, "Reserve fuel mass in kg (subtracted from fuel volume constraint)"] = 15000.0,
    n_point_masses: Annotated[int, "Number of point masses (e.g. engines) attached to this surface"] = 0,
    num_twist_cp: Annotated[int | None, "Number of twist control points for CRM/uCRM_based mesh generation (None = auto)"] = None,
    session_id: Annotated[str, "Session identifier"] = "default",
) -> dict:
    """Define a lifting surface (wing, tail, canard) and store it in the session.

    Must be called before any analysis tools. The surface geometry (mesh, sweep,
    dihedral, taper) is computed immediately and cached for reuse.
    """
    # Validate inputs
    validate_wing_type(wing_type)
    validate_mesh_params(num_x, num_y)
    validate_fem_model_type(fem_model_type)
    if root_chord <= 0:
        raise ValueError(f"root_chord must be positive, got {root_chord}")
    if span <= 0:
        raise ValueError(f"span must be positive, got {span}")

    def _build():
        mesh, crm_twist = build_mesh(
            wing_type=wing_type,
            num_x=num_x,
            num_y=num_y,
            span=span,
            root_chord=root_chord,
            symmetry=symmetry,
            offset=offset,
            num_twist_cp=num_twist_cp,
        )

        # Apply geometric modifications
        if sweep != 0.0:
            mesh = apply_sweep(mesh, sweep)
        if dihedral != 0.0:
            mesh = apply_dihedral(mesh, dihedral)
        if taper != 1.0:
            mesh = apply_taper(mesh, taper)

        # Determine twist_cp
        # User-provided arrays are root-to-tip; reverse to OAS tip-to-root.
        # CRM twist from generate_mesh() is already in OAS order — do NOT reverse.
        if twist_cp is not None:
            tcp = _to_oas_order(np.array(twist_cp, dtype=float))
        elif crm_twist is not None:
            tcp = crm_twist
        else:
            tcp = np.zeros(2)

        # Build surface dict
        surface = {
            "name": name,
            "symmetry": symmetry,
            "S_ref_type": S_ref_type,
            "mesh": mesh,
            "twist_cp": tcp,
            "CL0": CL0,
            "CD0": CD0,
            "k_lam": 0.05,
            "t_over_c_cp": (
                _to_oas_order(np.array(t_over_c_cp, dtype=float))
                if t_over_c_cp is not None
                else np.array([0.15])
            ),
            "c_max_t": c_max_t,
            "with_viscous": with_viscous,
            "with_wave": with_wave,
        }

        if chord_cp is not None:
            surface["chord_cp"] = _to_oas_order(np.array(chord_cp, dtype=float))

        if fem_model_type and fem_model_type != "none":
            surface["fem_model_type"] = fem_model_type
            surface["E"] = E
            surface["G"] = G
            surface["yield"] = yield_stress
            surface["safety_factor"] = safety_factor
            surface["mrho"] = mrho
            surface["fem_origin"] = 0.35
            surface["wing_weight_ratio"] = wing_weight_ratio
            surface["struct_weight_relief"] = struct_weight_relief
            surface["distributed_fuel_weight"] = distributed_fuel_weight
            surface["exact_failure_constraint"] = False
            surface["fuel_density"] = fuel_density
            surface["Wf_reserve"] = Wf_reserve
            if n_point_masses > 0:
                surface["n_point_masses"] = n_point_masses

            if fem_model_type == "wingbox":
                # Wingbox-specific thickness control points
                ny2 = (num_y + 1) // 2
                n_cp = max(2, min(6, ny2 // 2))
                surface["spar_thickness_cp"] = (
                    _to_oas_order(np.array(spar_thickness_cp, dtype=float))
                    if spar_thickness_cp is not None
                    else np.linspace(0.004, 0.01, n_cp)
                )
                surface["skin_thickness_cp"] = (
                    _to_oas_order(np.array(skin_thickness_cp, dtype=float))
                    if skin_thickness_cp is not None
                    else np.linspace(0.005, 0.026, n_cp)
                )
                # Airfoil geometry for wingbox cross-section calculations
                surface["original_wingbox_airfoil_t_over_c"] = original_wingbox_airfoil_t_over_c
                surface["strength_factor_for_upper_skin"] = 1.0
                surface["data_x_upper"] = DEFAULT_WINGBOX_UPPER_X
                surface["data_y_upper"] = DEFAULT_WINGBOX_UPPER_Y
                surface["data_x_lower"] = DEFAULT_WINGBOX_LOWER_X
                surface["data_y_lower"] = DEFAULT_WINGBOX_LOWER_Y
            else:
                # Tube model thickness control points
                if thickness_cp is not None:
                    surface["thickness_cp"] = _to_oas_order(np.array(thickness_cp, dtype=float))
                else:
                    ny2 = (num_y + 1) // 2
                    n_cp = max(3, min(5, ny2 // 2))
                    surface["thickness_cp"] = np.ones(n_cp) * 0.1 * root_chord

        return surface

    session = _sessions.get(session_id)
    surface = await asyncio.to_thread(_suppress_output, _build)
    session.add_surface(surface)

    mesh = surface["mesh"]
    nx, ny, _ = mesh.shape
    # Estimate span and area from mesh
    y_coords = mesh[0, :, 1]
    actual_span = float(y_coords.max() - y_coords.min())
    if symmetry:
        actual_span *= 2.0
    # Rough panel area estimate
    chord_avg = float(np.mean(mesh[-1, :, 0] - mesh[0, :, 0]))

    return {
        "surface_name": name,
        "mesh_shape": [nx, ny, 3],
        "span_m": round(actual_span, 4),
        "mean_chord_m": round(chord_avg, 4),
        "estimated_area_m2": round(actual_span * chord_avg / (2.0 if symmetry else 1.0), 4),
        "twist_cp_shape": list(surface["twist_cp"].shape),
        "has_structure": fem_model_type is not None and fem_model_type != "none",
        "session_id": session_id,
        "status": "Surface created successfully",
    }


# ---------------------------------------------------------------------------
# Tool 2 — run_aero_analysis
# ---------------------------------------------------------------------------


@mcp.tool()
@capture_tool
async def run_aero_analysis(
    surfaces: Annotated[list[str], "Names of surfaces to include (must have been created via create_surface)"],
    velocity: Annotated[float, "Free-stream velocity in m/s"] = 248.136,
    alpha: Annotated[float, "Angle of attack in degrees"] = 5.0,
    Mach_number: Annotated[float, "Mach number"] = 0.84,
    reynolds_number: Annotated[float, "Reynolds number per unit length (1/m)"] = 1.0e6,
    density: Annotated[float, "Air density in kg/m^3"] = 0.38,
    cg: Annotated[list[float] | None, "Centre of gravity [x, y, z] in metres"] = None,
    session_id: Annotated[str, "Session identifier"] = "default",
    run_name: Annotated[str | None, "Optional label for this run (stored in artifact metadata)"] = None,
) -> dict:
    """Run a single-point VLM aerodynamic analysis.

    Computes CL, CD (inviscid + viscous + wave), CM, and L/D for the given
    flight conditions. Per-surface force breakdowns are also returned.
    """
    if cg is None:
        cg = [0.0, 0.0, 0.0]

    validate_flight_conditions(velocity, alpha, Mach_number, reynolds_number, density)
    session = _sessions.get(session_id)
    validate_surface_names_exist(surfaces, session)

    surface_dicts = session.get_surfaces(surfaces)
    run_id = _make_run_id()

    def _run():
        cached = session.get_cached_problem(surfaces, "aero")
        if cached is not None:
            prob = cached
            prob.set_val("v", velocity, units="m/s")
            prob.set_val("alpha", alpha, units="deg")
            prob.set_val("Mach_number", Mach_number)
            prob.set_val("re", reynolds_number, units="1/m")
            prob.set_val("rho", density, units="kg/m**3")
            prob.set_val("cg", np.array(cg), units="m")
        else:
            prob = build_aero_problem(
                surface_dicts,
                velocity=velocity,
                alpha=alpha,
                Mach_number=Mach_number,
                reynolds_number=reynolds_number,
                density=density,
                cg=cg,
            )
            session.store_problem(surfaces, "aero", prob)

        prob.run_model()
        aero_results = extract_aero_results(prob, surface_dicts, "aero")
        standard = extract_standard_detail(prob, surface_dicts, "aero", "aero")
        return aero_results, standard

    t0 = time.perf_counter()
    cache_hit = session.get_cached_problem(surfaces, "aero") is not None
    results, standard_detail = await asyncio.to_thread(_suppress_output, _run)

    session.store_mesh_snapshot(run_id, standard_detail.get("mesh_snapshot", {}))

    inputs = {
        "velocity": velocity, "alpha": alpha, "Mach_number": Mach_number,
        "reynolds_number": reynolds_number, "density": density,
    }
    findings = validate_aero(results, context={"alpha": alpha})
    return await _finalize_analysis(
        tool_name="run_aero_analysis", run_id=run_id,
        session=session, session_id=session_id, surfaces=surfaces,
        analysis_type="aero", inputs=inputs, results=results,
        standard_detail=standard_detail, findings=findings,
        t0=t0, cache_hit=cache_hit, run_name=run_name,
        surface_dicts=surface_dicts, auto_plots=True,
    )


# ---------------------------------------------------------------------------
# Tool 3 — run_aerostruct_analysis
# ---------------------------------------------------------------------------


@mcp.tool()
@capture_tool
async def run_aerostruct_analysis(
    surfaces: Annotated[list[str], "Names of surfaces (must have fem_model_type set)"],
    velocity: Annotated[float, "Free-stream velocity in m/s"] = 248.136,
    alpha: Annotated[float, "Angle of attack in degrees"] = 5.0,
    Mach_number: Annotated[float, "Mach number"] = 0.84,
    reynolds_number: Annotated[float, "Reynolds number per unit length (1/m)"] = 1.0e6,
    density: Annotated[float, "Air density in kg/m^3"] = 0.38,
    W0: Annotated[float, "Aircraft empty weight (excl. wing structure) in kg"] = 0.4 * 3e5,
    CT: Annotated[float | None, "Specific fuel consumption in 1/s (None = default cruise value)"] = None,
    R: Annotated[float, "Mission range in metres"] = 11.165e6,
    speed_of_sound: Annotated[float, "Speed of sound in m/s"] = 295.4,
    load_factor: Annotated[float, "Load factor (1.0 = 1-g cruise)"] = 1.0,
    empty_cg: Annotated[list[float] | None, "Empty CG location [x, y, z] in metres"] = None,
    session_id: Annotated[str, "Session identifier"] = "default",
    run_name: Annotated[str | None, "Optional label for this run (stored in artifact metadata)"] = None,
) -> dict:
    """Run a coupled aerostructural analysis (VLM + beam FEM).

    Returns aerodynamic coefficients plus structural mass, fuel burn, failure
    metric, and von Mises stress.  Surfaces must have been created with a
    fem_model_type of 'tube' or 'wingbox'.
    """
    if empty_cg is None:
        empty_cg = [0.0, 0.0, 0.0]

    validate_flight_conditions(velocity, alpha, Mach_number, reynolds_number, density)
    session = _sessions.get(session_id)
    validate_surface_names_exist(surfaces, session)
    surface_dicts = session.get_surfaces(surfaces)
    for s in surface_dicts:
        validate_struct_props_present(s)
    run_id = _make_run_id()

    from openaerostruct.utils.constants import grav_constant
    ct_val = CT if CT is not None else grav_constant * 17.0e-6

    def _run():
        cached = session.get_cached_problem(surfaces, "aerostruct")
        if cached is not None:
            prob = cached
            prob.set_val("v", velocity, units="m/s")
            prob.set_val("alpha", alpha, units="deg")
            prob.set_val("Mach_number", Mach_number)
            prob.set_val("re", reynolds_number, units="1/m")
            prob.set_val("rho", density, units="kg/m**3")
            prob.set_val("W0", W0, units="kg")
            prob.set_val("CT", ct_val, units="1/s")
            prob.set_val("R", R, units="m")
            prob.set_val("speed_of_sound", speed_of_sound, units="m/s")
            prob.set_val("load_factor", load_factor)
            prob.set_val("empty_cg", np.array(empty_cg), units="m")
        else:
            prob = build_aerostruct_problem(
                surface_dicts,
                velocity=velocity,
                alpha=alpha,
                Mach_number=Mach_number,
                reynolds_number=reynolds_number,
                density=density,
                CT=ct_val,
                R=R,
                W0=W0,
                speed_of_sound=speed_of_sound,
                load_factor=load_factor,
                empty_cg=empty_cg,
            )
            session.store_problem(surfaces, "aerostruct", prob)

        prob.run_model()
        as_results = extract_aerostruct_results(prob, surface_dicts, "AS_point_0")
        standard = extract_standard_detail(prob, surface_dicts, "aerostruct", "AS_point_0")
        return as_results, standard

    t0 = time.perf_counter()
    cache_hit = session.get_cached_problem(surfaces, "aerostruct") is not None
    results, standard_detail = await asyncio.to_thread(_suppress_output, _run)

    session.store_mesh_snapshot(run_id, standard_detail.get("mesh_snapshot", {}))

    inputs = {
        "velocity": velocity, "alpha": alpha, "Mach_number": Mach_number,
        "reynolds_number": reynolds_number, "density": density,
        "W0": W0, "R": R, "speed_of_sound": speed_of_sound, "load_factor": load_factor,
    }
    findings = validate_aerostruct(results, context={"alpha": alpha, "W0": W0})
    return await _finalize_analysis(
        tool_name="run_aerostruct_analysis", run_id=run_id,
        session=session, session_id=session_id, surfaces=surfaces,
        analysis_type="aerostruct", inputs=inputs, results=results,
        standard_detail=standard_detail, findings=findings,
        t0=t0, cache_hit=cache_hit, run_name=run_name,
        surface_dicts=surface_dicts, auto_plots=True,
    )


# ---------------------------------------------------------------------------
# Tool 4 — compute_drag_polar
# ---------------------------------------------------------------------------


@mcp.tool()
@capture_tool
async def compute_drag_polar(
    surfaces: Annotated[list[str], "Names of surfaces to include"],
    alpha_start: Annotated[float, "Starting angle of attack in degrees"] = -5.0,
    alpha_end: Annotated[float, "Ending angle of attack in degrees"] = 15.0,
    num_alpha: Annotated[int, "Number of alpha points to compute"] = 21,
    velocity: Annotated[float, "Free-stream velocity in m/s"] = 248.136,
    Mach_number: Annotated[float, "Mach number"] = 0.84,
    reynolds_number: Annotated[float, "Reynolds number per unit length (1/m)"] = 1.0e6,
    density: Annotated[float, "Air density in kg/m^3"] = 0.38,
    cg: Annotated[list[float] | None, "Centre of gravity [x, y, z] in metres"] = None,
    session_id: Annotated[str, "Session identifier"] = "default",
    run_name: Annotated[str | None, "Optional label for this run (stored in artifact metadata)"] = None,
) -> dict:
    """Compute a drag polar by sweeping angle of attack.

    Returns arrays of alpha, CL, CD, CM, and L/D.  The point of maximum L/D
    is highlighted.
    """
    if cg is None:
        cg = [0.0, 0.0, 0.0]
    if num_alpha < 2:
        raise ValueError("num_alpha must be >= 2")

    session = _sessions.get(session_id)
    validate_surface_names_exist(surfaces, session)
    surface_dicts = session.get_surfaces(surfaces)

    alphas = list(np.linspace(alpha_start, alpha_end, num_alpha))

    def _run():
        # Build problem once with first alpha value
        prob = build_aero_problem(
            surface_dicts,
            velocity=velocity,
            alpha=alphas[0],
            Mach_number=Mach_number,
            reynolds_number=reynolds_number,
            density=density,
            cg=cg,
        )

        CLs, CDs, CMs = [], [], []
        for a in alphas:
            prob.set_val("alpha", a, units="deg")
            prob.run_model()
            CLs.append(float(np.asarray(prob.get_val("aero.CL")).ravel()[0]))
            CDs.append(float(np.asarray(prob.get_val("aero.CD")).ravel()[0]))
            cm = np.asarray(prob.get_val("aero.CM")).ravel()
            CMs.append(float(cm[1]) if len(cm) > 1 else float(cm[0]))

        return CLs, CDs, CMs

    run_id = _make_run_id()
    t0 = time.perf_counter()
    CLs, CDs, CMs = await asyncio.to_thread(_suppress_output, _run)

    LoDs = [cl / cd if cd > 0 else None for cl, cd in zip(CLs, CDs)]
    valid_LoDs = [(i, v) for i, v in enumerate(LoDs) if v is not None]
    best_idx, best_LoD = max(valid_LoDs, key=lambda x: x[1]) if valid_LoDs else (0, None)

    polar_results = {
        "alpha_deg": [round(float(a), 4) for a in alphas],
        "CL": [round(v, 6) for v in CLs],
        "CD": [round(v, 6) for v in CDs],
        "CM": [round(v, 6) for v in CMs],
        "L_over_D": [round(v, 4) if v is not None else None for v in LoDs],
        "best_L_over_D": {
            "alpha_deg": round(float(alphas[best_idx]), 4),
            "CL": round(CLs[best_idx], 6),
            "CD": round(CDs[best_idx], 6),
            "L_over_D": round(best_LoD, 4) if best_LoD else None,
        },
    }
    inputs = {
        "alpha_start": alpha_start, "alpha_end": alpha_end, "num_alpha": num_alpha,
        "velocity": velocity, "Mach_number": Mach_number,
        "reynolds_number": reynolds_number, "density": density,
    }

    findings = validate_drag_polar(polar_results, context={"alpha_start": alpha_start})
    return await _finalize_analysis(
        tool_name="compute_drag_polar", run_id=run_id,
        session=session, session_id=session_id, surfaces=surfaces,
        analysis_type="drag_polar", inputs=inputs, results=polar_results,
        standard_detail=None, findings=findings,
        t0=t0, cache_hit=False, run_name=run_name,
        surface_dicts=None, auto_plots=True,
    )


# ---------------------------------------------------------------------------
# Tool 5 — compute_stability_derivatives
# ---------------------------------------------------------------------------


@mcp.tool()
@capture_tool
async def compute_stability_derivatives(
    surfaces: Annotated[list[str], "Names of surfaces to include"],
    alpha: Annotated[float, "Angle of attack in degrees"] = 5.0,
    velocity: Annotated[float, "Free-stream velocity in m/s"] = 248.136,
    Mach_number: Annotated[float, "Mach number"] = 0.84,
    reynolds_number: Annotated[float, "Reynolds number per unit length (1/m)"] = 1.0e6,
    density: Annotated[float, "Air density in kg/m^3"] = 0.38,
    cg: Annotated[list[float] | None, "Centre of gravity [x, y, z] in metres — affects CM and static margin"] = None,
    session_id: Annotated[str, "Session identifier"] = "default",
    run_name: Annotated[str | None, "Optional label for this run (stored in artifact metadata)"] = None,
) -> dict:
    """Compute stability derivatives: CL_alpha, CM_alpha, and static margin.

    Uses two AeroPoint instances and finite differencing in alpha (1e-4 deg step)
    to compute lift-curve slope and pitching-moment slope.  Static margin is
    -CM_alpha / CL_alpha.
    """
    if cg is None:
        cg = [0.0, 0.0, 0.0]

    validate_flight_conditions(velocity, alpha, Mach_number, reynolds_number, density)
    session = _sessions.get(session_id)
    validate_surface_names_exist(surfaces, session)
    surface_dicts = session.get_surfaces(surfaces)

    def _run():
        import openmdao.api as om
        from openaerostruct.geometry.geometry_group import Geometry
        from openaerostruct.aerodynamics.aero_groups import AeroPoint

        alpha_FD_stepsize = 1e-4  # deg

        prob = om.Problem(reports=False)

        indep = om.IndepVarComp()
        indep.add_output("v", val=velocity, units="m/s")
        indep.add_output("alpha", val=alpha, units="deg")
        indep.add_output("Mach_number", val=Mach_number)
        indep.add_output("re", val=reynolds_number, units="1/m")
        indep.add_output("rho", val=density, units="kg/m**3")
        indep.add_output("cg", val=np.array(cg), units="m")
        prob.model.add_subsystem("prob_vars", indep, promotes=["*"])

        # FD alpha offset
        alpha_perturb = om.ExecComp(
            "alpha_plus_delta = alpha + delta_alpha",
            units="deg",
            delta_alpha={"val": alpha_FD_stepsize, "constant": True},
        )
        prob.model.add_subsystem("alpha_for_FD", alpha_perturb, promotes=["*"])

        # Geometry groups
        for surface in surface_dicts:
            s_name = surface["name"]
            geom = Geometry(surface=surface)
            prob.model.add_subsystem(s_name + "_geom", geom)

        # Two AeroPoints
        point_names = ["aero_point", "aero_point_FD"]
        for i, pname in enumerate(point_names):
            ag = AeroPoint(surfaces=surface_dicts)
            prob.model.add_subsystem(pname, ag)
            prob.model.connect("v", pname + ".v")
            prob.model.connect("Mach_number", pname + ".Mach_number")
            prob.model.connect("re", pname + ".re")
            prob.model.connect("rho", pname + ".rho")
            prob.model.connect("cg", pname + ".cg")
            alpha_src = "alpha" if i == 0 else "alpha_plus_delta"
            prob.model.connect(alpha_src, pname + ".alpha")
            for surface in surface_dicts:
                s_name = surface["name"]
                prob.model.connect(s_name + "_geom.mesh", pname + "." + s_name + ".def_mesh")
                prob.model.connect(s_name + "_geom.mesh", pname + ".aero_states." + s_name + "_def_mesh")
                prob.model.connect(s_name + "_geom.t_over_c", pname + "." + s_name + "_perf.t_over_c")

        # Stability derivatives via ExecComp
        stab_comp = om.ExecComp(
            ["CL_alpha = (CL_FD - CL) / delta_alpha", "CM_alpha = (CM_FD - CM) / delta_alpha"],
            delta_alpha={"val": alpha_FD_stepsize, "constant": True},
            CL_alpha={"val": 0.0, "units": "1/deg"},
            CL_FD={"val": 0.0, "units": None},
            CL={"val": 0.0, "units": None},
            CM_alpha={"val": np.zeros(3), "units": "1/deg"},
            CM_FD={"val": np.zeros(3), "units": None},
            CM={"val": np.zeros(3), "units": None},
        )
        prob.model.add_subsystem("stability_derivs", stab_comp, promotes_outputs=["*"])
        prob.model.connect("aero_point.CL", "stability_derivs.CL")
        prob.model.connect("aero_point.CM", "stability_derivs.CM")
        prob.model.connect("aero_point_FD.CL", "stability_derivs.CL_FD")
        prob.model.connect("aero_point_FD.CM", "stability_derivs.CM_FD")

        # Static margin
        sm_comp = om.ExecComp(
            "static_margin = -CM_alpha / CL_alpha",
            CM_alpha={"val": 0.0, "units": "1/deg"},
            CL_alpha={"val": 0.0, "units": "1/deg"},
            static_margin={"val": 0.0, "units": None},
        )
        prob.model.add_subsystem("static_margin", sm_comp, promotes_outputs=["*"])
        prob.model.connect("CL_alpha", "static_margin.CL_alpha")
        prob.model.connect("CM_alpha", "static_margin.CM_alpha", src_indices=1)

        prob.setup(force_alloc_complex=False)
        prob.set_val("v", velocity, units="m/s")
        prob.set_val("alpha", alpha, units="deg")
        prob.set_val("Mach_number", Mach_number)
        prob.set_val("re", reynolds_number, units="1/m")
        prob.set_val("rho", density, units="kg/m**3")
        prob.set_val("cg", np.array(cg), units="m")

        prob.run_model()
        return extract_stability_results(prob)

    run_id = _make_run_id()
    t0 = time.perf_counter()
    results = await asyncio.to_thread(_suppress_output, _run)
    inputs = {
        "alpha": alpha, "velocity": velocity, "Mach_number": Mach_number,
        "reynolds_number": reynolds_number, "density": density,
    }

    findings = validate_stability(results, context={"alpha": alpha})
    return await _finalize_analysis(
        tool_name="compute_stability_derivatives", run_id=run_id,
        session=session, session_id=session_id, surfaces=surfaces,
        analysis_type="stability", inputs=inputs, results=results,
        standard_detail=None, findings=findings,
        t0=t0, cache_hit=False, run_name=run_name,
        surface_dicts=surface_dicts, auto_plots=False,
    )


# ---------------------------------------------------------------------------
# Tool 6 — run_optimization
# ---------------------------------------------------------------------------


@mcp.tool()
@capture_tool
async def run_optimization(
    surfaces: Annotated[list[str], "Names of surfaces to use"],
    analysis_type: Annotated[str, "Analysis type: 'aero' or 'aerostruct'"] = "aero",
    objective: Annotated[str, "Objective to minimise: 'CD', 'fuelburn', or 'structural_mass'"] = "CD",
    design_variables: Annotated[
        list[dict],
        "List of DV dicts, e.g. [{'name':'twist','lower':-10,'upper':10}, {'name':'alpha','lower':-5,'upper':10}]"
    ] = None,
    constraints: Annotated[
        list[dict],
        "List of constraint dicts, e.g. [{'name':'CL','equals':0.5,'point':0}, {'name':'failure','upper':0.0,'point':1}]"
    ] = None,
    velocity: Annotated[float, "Free-stream velocity in m/s (single-point only)"] = 248.136,
    alpha: Annotated[float, "Initial angle of attack in degrees"] = 5.0,
    Mach_number: Annotated[float, "Mach number (single-point only)"] = 0.84,
    reynolds_number: Annotated[float, "Reynolds number per unit length (1/m, single-point only)"] = 1.0e6,
    density: Annotated[float, "Air density in kg/m^3 (single-point only)"] = 0.38,
    W0: Annotated[float, "Aircraft empty weight in kg (single-point aerostruct only)"] = 0.4 * 3e5,
    speed_of_sound: Annotated[float, "Speed of sound in m/s (single-point aerostruct only)"] = 295.4,
    load_factor: Annotated[float, "Load factor (single-point aerostruct only)"] = 1.0,
    CT: Annotated[float | None, "Thrust specific fuel consumption in 1/s (None = OAS default ~1.67e-4)"] = None,
    R: Annotated[float, "Range in metres"] = 14.307e6,
    flight_points: Annotated[
        list[dict] | None,
        "Multipoint flight conditions. Each dict requires: velocity, Mach_number, density, "
        "reynolds_number, speed_of_sound, load_factor. Enables multipoint aerostruct optimization."
    ] = None,
    W0_without_point_masses: Annotated[float, "Empty weight + reserve fuel in kg (multipoint only)"] = 143000.0,
    point_masses: Annotated[list[list[float]] | None, "Point masses in kg, e.g. [[10000]] for one engine"] = None,
    point_mass_locations: Annotated[list[list[float]] | None, "Point mass [x,y,z] locations in m, e.g. [[25,-10,0]]"] = None,
    objective_scaler: Annotated[float, "Scaler applied to the objective in add_objective (e.g. 1e4 for CD, 1e-5 for fuelburn)"] = 1.0,
    tolerance: Annotated[float, "Optimiser convergence tolerance"] = 1e-6,
    max_iterations: Annotated[int, "Maximum optimiser iterations"] = 200,
    session_id: Annotated[str, "Session identifier"] = "default",
    run_name: Annotated[str | None, "Optional label for this run (stored in artifact metadata)"] = None,
) -> dict:
    """Run a design optimisation.

    Minimises the objective function subject to the given constraints by
    varying the specified design variables.  Supports aero-only (VLM),
    single-point aerostructural, and multipoint aerostructural problems.

    Design variable names (all models):     'twist', 'chord', 'sweep', 'taper', 'alpha', 't_over_c'
    Design variable names (tube only):      'thickness'
    Design variable names (wingbox only):   'spar_thickness', 'skin_thickness'
    Design variable names (multipoint):     'alpha_maneuver', 'fuel_mass'
    Constraint names (aero):                'CL', 'CD', 'CM'
    Constraint names (aerostruct):          all aero + 'failure', 'thickness_intersects', 'L_equals_W'
    Constraint names (multipoint):          all aerostruct + 'fuel_vol_delta', 'fuel_diff'
    Objective names (aero):                 'CD', 'CL'
    Objective names (aerostruct):           'fuelburn', 'structural_mass', 'CD'

    For multipoint optimization, pass flight_points as a list of dicts with keys:
    velocity, Mach_number, density, reynolds_number, speed_of_sound, load_factor.
    Point 0 = cruise, point 1 = maneuver.  Constraint dicts accept an optional
    'point' key (int index, default 0) to target a specific flight point.
    """
    if design_variables is None:
        design_variables = [{"name": "alpha", "lower": -10.0, "upper": 15.0}]
    if constraints is None:
        constraints = [{"name": "CL", "equals": 0.5}]

    session = _sessions.get(session_id)
    validate_surface_names_exist(surfaces, session)
    surface_dicts = session.get_surfaces(surfaces)

    if analysis_type not in ("aero", "aerostruct"):
        raise ValueError("analysis_type must be 'aero' or 'aerostruct'")

    if analysis_type == "aerostruct":
        for s in surface_dicts:
            validate_struct_props_present(s)

    if flight_points is not None:
        validate_flight_points(flight_points)

    from openaerostruct.utils.constants import grav_constant
    ct_default = grav_constant * 17.0e-6
    ct_val = CT if CT is not None else ct_default

    def _run():
        from .core.builders import (
            DV_NAME_MAP, OBJECTIVE_MAP_AERO, OBJECTIVE_MAP_AEROSTRUCT,
            _MP_SCALAR_DVS, resolve_path,
        )
        from .core.convergence import OptimizationTracker

        primary_name = surface_dicts[0]["name"] if surface_dicts else "wing"

        if flight_points is not None and analysis_type == "aerostruct":
            # ----------------------------------------------------------------
            # MULTIPOINT path
            # ----------------------------------------------------------------
            prob, point_names = build_multipoint_optimization_problem(
                surface_dicts,
                objective=objective,
                design_variables=design_variables,
                constraints=constraints,
                flight_points=flight_points,
                CT=ct_val,
                R=R,
                W0_without_point_masses=W0_without_point_masses,
                alpha=alpha,
                point_masses=point_masses,
                point_mass_locations=point_mass_locations,
                tolerance=tolerance,
                max_iterations=max_iterations,
            )

            dv_path_map: dict[str, str] = {}
            for dv in design_variables:
                dv_name = dv["name"]
                template = DV_NAME_MAP.get(dv_name)
                if template is None and dv_name.endswith("_cp"):
                    template = DV_NAME_MAP.get(dv_name[:-3])
                if template:
                    path = (template if dv_name in _MP_SCALAR_DVS
                            else resolve_path(template, primary_name, point_names[0]))
                    dv_path_map[dv_name] = path

            tracker = OptimizationTracker()
            initial_dvs = tracker.record_initial(prob, dv_path_map)
            tracker.attach(prob)

            obj_template = OBJECTIVE_MAP_AEROSTRUCT.get(objective)
            obj_path = resolve_path(obj_template, primary_name, point_names[0]) if obj_template else ""

            prob.run_driver()
            success = prob.driver.result.success if hasattr(prob.driver, "result") else True
            opt_history = tracker.extract(dv_path_map, obj_path)

            # Per-point roles
            if len(point_names) == 2:
                roles = ["cruise", "maneuver"]
            else:
                roles = ["cruise"] + [f"maneuver_{i}" for i in range(1, len(point_names))]
            final_results = extract_multipoint_results(prob, surface_dicts, point_names, roles)

            # Standard detail from cruise point for visualization
            standard = extract_standard_detail(prob, surface_dicts, "aerostruct", point_names[0])

        else:
            # ----------------------------------------------------------------
            # SINGLE-POINT path
            # ----------------------------------------------------------------
            if analysis_type == "aero":
                flight_conditions = {
                    "velocity": velocity,
                    "alpha": alpha,
                    "Mach_number": Mach_number,
                    "reynolds_number": reynolds_number,
                    "density": density,
                }
            else:
                flight_conditions = {
                    "velocity": velocity,
                    "alpha": alpha,
                    "Mach_number": Mach_number,
                    "reynolds_number": reynolds_number,
                    "density": density,
                    "CT": ct_val,
                    "R": R,
                    "W0": W0,
                    "speed_of_sound": speed_of_sound,
                    "load_factor": load_factor,
                }

            prob, point_name = build_optimization_problem(
                surface_dicts,
                analysis_type=analysis_type,
                objective=objective,
                design_variables=design_variables,
                constraints=constraints,
                flight_conditions=flight_conditions,
                objective_scaler=objective_scaler,
                tolerance=tolerance,
                max_iterations=max_iterations,
            )

            dv_path_map = {}
            for dv in design_variables:
                template = DV_NAME_MAP.get(dv["name"])
                if template:
                    dv_path_map[dv["name"]] = resolve_path(template, primary_name, point_name)

            tracker = OptimizationTracker()
            initial_dvs = tracker.record_initial(prob, dv_path_map)
            tracker.attach(prob)

            obj_map = OBJECTIVE_MAP_AERO if analysis_type == "aero" else OBJECTIVE_MAP_AEROSTRUCT
            obj_template = obj_map.get(objective)
            obj_path = resolve_path(obj_template, primary_name, point_name) if obj_template else ""

            prob.run_driver()
            success = prob.driver.result.success if hasattr(prob.driver, "result") else True
            opt_history = tracker.extract(dv_path_map, obj_path)

            if analysis_type == "aero":
                final_results = extract_aero_results(prob, surface_dicts, point_name)
            else:
                final_results = extract_aerostruct_results(prob, surface_dicts, point_name)

            standard = extract_standard_detail(prob, surface_dicts, analysis_type, point_name)

        # Extract optimised DV values
        dv_results: dict = {}
        for dv_name, path in dv_path_map.items():
            try:
                val = prob.get_val(path)
                dv_results[dv_name] = np.asarray(val).tolist()
            except Exception:
                pass

        # Convert cp arrays from OAS order (tip→root) to MCP order (root→tip)
        for dv_name in list(dv_results):
            if _is_cp_dv(dv_name):
                dv_results[dv_name] = _from_oas_order(np.array(dv_results[dv_name])).tolist()
        for dv_name in list(initial_dvs):
            if _is_cp_dv(dv_name):
                initial_dvs[dv_name] = _from_oas_order(np.array(initial_dvs[dv_name])).tolist()
        for dv_name, iters in opt_history.get("dv_history", {}).items():
            if _is_cp_dv(dv_name):
                opt_history["dv_history"][dv_name] = [
                    _from_oas_order(np.array(v)).tolist() for v in iters
                ]

        result = {
            "success": bool(success),
            "optimized_design_variables": dv_results,
            "final_results": final_results,
            "optimization_history": {"initial_dvs": initial_dvs, **opt_history},
        }
        return result, standard

    run_id = _make_run_id()
    t0 = time.perf_counter()
    result, standard_detail = await asyncio.to_thread(_suppress_output, _run)

    session.store_mesh_snapshot(run_id, standard_detail.get("mesh_snapshot", {}))

    inputs: dict = {
        "analysis_type": analysis_type, "objective": objective,
        "alpha": alpha,
    }
    if flight_points is not None:
        inputs["multipoint"] = True
        inputs["n_flight_points"] = len(flight_points)
        inputs["CT"] = ct_val
        inputs["R"] = R
    else:
        inputs["velocity"] = velocity
        inputs["Mach_number"] = Mach_number
        inputs["density"] = density

    findings = validate_optimization(result, context={
        "analysis_type": analysis_type,
        "max_iterations": max_iterations,
        "objective_scaler": objective_scaler,
        "design_variables": [dv if isinstance(dv, dict) else {"name": dv} for dv in design_variables],
    })
    return await _finalize_analysis(
        tool_name="run_optimization", run_id=run_id,
        session=session, session_id=session_id, surfaces=surfaces,
        analysis_type="optimization", inputs=inputs, results=result,
        standard_detail=standard_detail, findings=findings,
        t0=t0, cache_hit=False, run_name=run_name,
        surface_dicts=surface_dicts, auto_plots=False,
    )


# ---------------------------------------------------------------------------
# Tool 7 — reset
# ---------------------------------------------------------------------------


@mcp.tool()
@capture_tool
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


# ---------------------------------------------------------------------------
# Tools 8–11 — artifact management
# ---------------------------------------------------------------------------


@mcp.tool()
@capture_tool
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


@mcp.tool()
@capture_tool
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


@mcp.tool()
@capture_tool
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


@mcp.tool()
@capture_tool
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


# ---------------------------------------------------------------------------
# Tools 12–18 — observability, tracing, visualization, session defaults
# ---------------------------------------------------------------------------


@mcp.tool()
@capture_tool
async def get_run(
    run_id: Annotated[str, "Run ID to inspect"],
    session_id: Annotated[str | None, "Session hint for faster lookup"] = None,
) -> dict:
    """Return a full manifest for a run: inputs, outputs, validation, cache state.

    This is the primary 'what do I know about this run?' endpoint for agents.
    It answers: what inputs were used, what came out, did it pass validation,
    is the problem still cached, and what plot types are available.

    Scoped to the authenticated user.
    """
    run_id = await _resolve_run_id(run_id, session_id)
    user = get_current_user()
    artifact = await asyncio.to_thread(_artifacts.get, run_id, session_id, user)
    if artifact is None:
        raise ValueError(f"Run '{run_id}' not found in artifact store.")

    meta = artifact.get("metadata", {})
    results = artifact.get("results", {})
    sid = meta.get("session_id", session_id or "default")
    session = _sessions.get(sid)

    # Cache status
    surface_names = meta.get("surfaces", [])
    analysis_type = meta.get("analysis_type", "aero")
    cache_info = session.cache_status(surface_names, analysis_type)
    cache_info["pinned"] = session.is_pinned(run_id)

    # Determine which detail levels are available
    has_standard = bool(results.get("standard_detail"))
    has_mesh = bool(session.get_mesh_snapshot(run_id))

    # Plot types available given what's stored
    if analysis_type == "drag_polar":
        available_plots = ["drag_polar"]
    elif analysis_type == "optimization":
        available_plots = ["lift_distribution", "opt_history"]
        final_r = results.get("final_results", {})
        if "fuelburn" in final_r or "structural_mass" in final_r:
            available_plots.append("stress_distribution")
        opt_hist = results.get("optimization_history", {})
        if opt_hist.get("initial_dvs") or opt_hist.get("dv_history"):
            available_plots.extend(["opt_dv_evolution", "opt_comparison"])
        if has_mesh or has_standard:
            available_plots.append("planform")
    else:
        available_plots = ["lift_distribution"]
        if analysis_type == "aerostruct":
            available_plots.append("stress_distribution")
        if has_mesh or has_standard:
            available_plots.append("planform")
    conv = session.get_convergence(run_id)
    if not conv:
        conv = results.get("convergence") or artifact.get("convergence")
    if conv:
        available_plots.append("convergence")

    return {
        "run_id": run_id,
        "tool_name": meta.get("tool_name"),
        "analysis_type": analysis_type,
        "timestamp": meta.get("timestamp"),
        "user": meta.get("user"),
        "project": meta.get("project"),
        "name": meta.get("name"),
        "surfaces": surface_names,
        "inputs": meta.get("parameters", {}),
        "outputs_summary": {
            k: v for k, v in results.items()
            if k not in ("standard_detail", "convergence") and not isinstance(v, (list, dict))
        },
        "cache_state": cache_info,
        "detail_levels_available": {
            "summary": True,
            "standard": has_standard,
        },
        "available_plots": available_plots,
    }


@mcp.tool()
@capture_tool
async def pin_run(
    run_id: Annotated[str, "Run ID whose cached problem to pin"],
    surfaces: Annotated[list[str], "Surface names used in this run"],
    analysis_type: Annotated[str, "Analysis type: 'aero' or 'aerostruct'"] = "aero",
    session_id: Annotated[str, "Session identifier"] = "default",
) -> dict:
    """Pin a cached OpenMDAO problem so it won't be evicted during multi-step workflows.

    Use this after an analysis run when you plan to call get_detailed_results()
    or visualize() later — it guarantees the live problem stays in memory.
    Call unpin_run() when done to release memory.
    """
    session = _sessions.get(session_id)
    pinned = session.pin_run(run_id, surfaces, analysis_type)
    return {
        "run_id": run_id,
        "pinned": pinned,
        "message": (
            f"Run '{run_id}' pinned — cached problem will not be evicted."
            if pinned
            else f"No cached problem found for run '{run_id}' (surfaces={surfaces}, type={analysis_type})."
        ),
    }


@mcp.tool()
@capture_tool
async def unpin_run(
    run_id: Annotated[str, "Run ID to unpin"],
    session_id: Annotated[str, "Session identifier"] = "default",
) -> dict:
    """Release a pin on a cached OpenMDAO problem, allowing it to be evicted."""
    session = _sessions.get(session_id)
    released = session.unpin_run(run_id)
    return {
        "run_id": run_id,
        "released": released,
        "message": (
            f"Pin for run '{run_id}' released."
            if released
            else f"No pin found for run '{run_id}'."
        ),
    }


@mcp.tool()
@capture_tool
async def get_detailed_results(
    run_id: Annotated[str, "Run ID to retrieve details for"],
    detail_level: Annotated[
        str,
        "Detail level: 'standard' = sectional Cl, stress, mesh (persisted); "
        "'summary' = just the top-level results dict",
    ] = "standard",
    session_id: Annotated[str | None, "Session hint"] = None,
) -> dict:
    """Retrieve detailed results for a past run.

    'standard' detail includes spanwise sectional Cl, von Mises stress
    distributions, and mesh coordinates — captured at run time and
    persisted in the artifact so they survive cache eviction.

    'summary' returns only the top-level scalars (CL, CD, etc.).

    Scoped to the authenticated user.
    """
    run_id = await _resolve_run_id(run_id, session_id)
    user = get_current_user()
    artifact = await asyncio.to_thread(_artifacts.get, run_id, session_id, user)
    if artifact is None:
        raise ValueError(f"Run '{run_id}' not found.")

    results = artifact.get("results", {})

    if detail_level == "summary":
        return {
            "run_id": run_id,
            "detail_level": "summary",
            "results": {
                k: v for k, v in results.items()
                if not isinstance(v, (list, dict)) or k in ("surfaces",)
            },
        }
    elif detail_level == "standard":
        standard = results.get("standard_detail", {})
        return {
            "run_id": run_id,
            "detail_level": "standard",
            "sectional_data": standard.get("sectional_data", {}),
            "mesh_snapshot": standard.get("mesh_snapshot", {}),
        }
    else:
        raise ValueError(
            f"Unknown detail_level {detail_level!r}. Use 'summary' or 'standard'."
        )


@mcp.tool(meta={"ui": {"resourceUri": _WIDGET_URI}})
@capture_tool
async def visualize(
    run_id: Annotated[str, "Run ID to visualize"],
    plot_type: Annotated[
        str,
        "Plot type — one of: lift_distribution, drag_polar, stress_distribution, "
        "convergence, planform, opt_history, opt_dv_evolution, opt_comparison, n2",
    ],
    session_id: Annotated[str | None, "Session hint for faster artifact lookup"] = None,
    case_name: Annotated[str, "Human-readable label for the plot title"] = "",
    output: Annotated[
        str | None,
        "Override visualization output mode for this call: "
        "'inline' = PNG image (default for claude.ai), "
        "'file' = save PNG to disk only (no [image] noise in CLI), "
        "'url' = return dashboard URL (best for remote/VPS CLI). "
        "When None, uses session default (set via configure_session).",
    ] = None,
) -> list:
    """Generate a visualisation plot and return a base64-encoded PNG (or HTML for n2).

    Response includes:
      plot_type, run_id, format, width_px, height_px, image_hash, image_base64

    Use image_hash for client-side caching — if the hash matches a cached image,
    there is no need to re-render.

    Available plot types:
      lift_distribution   — spanwise Cl bar chart or per-surface CL
      drag_polar          — CL vs CD and L/D vs alpha (requires drag polar run)
      stress_distribution — spanwise von Mises stress and failure index
      convergence         — solver residual history (if captured)
      planform            — wing planform top view with optional deflection overlay
      opt_history         — optimizer objective convergence (optimization runs only)
      opt_dv_evolution    — design variable evolution over iterations (optimization only)
      opt_comparison      — before/after DV comparison: initial vs optimized values
      n2                  — interactive N2/DSM diagram (saves HTML to disk, returns metadata with file_path)

    Output modes (set per-call via 'output' param, or per-session via configure_session):
      inline  — returns [metadata, ImageContent] (default, best for claude.ai)
      file    — saves PNG to disk, returns [metadata] with file_path (no [image] noise in CLI)
      url     — returns [metadata] with dashboard_url and plot_url (clickable links for CLI)

    Scoped to the authenticated user.
    """
    if plot_type not in PLOT_TYPES:
        raise ValueError(
            f"Unknown plot_type {plot_type!r}. "
            f"Supported: {sorted(PLOT_TYPES)}"
        )

    if output is not None and output not in ("inline", "file", "url"):
        raise ValueError(
            f"Unknown output mode {output!r}. Use 'inline', 'file', or 'url'."
        )

    run_id = await _resolve_run_id(run_id, session_id)
    user = get_current_user()
    artifact = await asyncio.to_thread(_artifacts.get, run_id, session_id, user)
    if artifact is None:
        raise ValueError(f"Run '{run_id}' not found.")

    # Resolve effective output mode: per-call override > session default
    artifact_meta = artifact.get("metadata", {})
    sid = artifact_meta.get("session_id", session_id or "default")
    session = _sessions.get(sid)
    effective_output = output or session.defaults.visualization_output

    # Compute save_dir — always save when mode is "file" or "url", also for "inline"
    _user = artifact_meta.get("user", user)
    _project = artifact_meta.get("project", "default")
    save_dir = str(_artifacts._data_dir / _user / _project / sid)

    # N2 diagram — needs a live OpenMDAO Problem, not artifact data
    if plot_type == "n2":
        analysis_type = artifact_meta.get("analysis_type", "aero")
        surfaces = artifact_meta.get("surfaces", [])

        # Optimization runs wrap an underlying aero or aerostruct analysis
        if analysis_type == "optimization":
            analysis_type = artifact.get("results", {}).get("analysis_type", "aero")

        prob = session.get_cached_problem(surfaces, analysis_type) if session else None
        if prob is None:
            raise ValueError(
                f"No cached OpenMDAO Problem for run '{run_id}'. "
                "Re-run the analysis in the current session, then call visualize again."
            )
        output_dir = _artifacts._data_dir / _user / _project / sid
        n2_result = await asyncio.to_thread(generate_n2, prob, run_id, case_name, output_dir)
        return [n2_result.metadata]

    results = artifact.get("results", {})
    artifact_type = artifact_meta.get("analysis_type", "")

    # For optimization runs, the aerodynamic results live inside `final_results`.
    # Merge them into plot_results so lift_distribution / stress_distribution work.
    if artifact_type == "optimization":
        final_r = results.get("final_results", {})
        plot_results = dict(final_r)
    else:
        plot_results = dict(results)

    standard = results.get("standard_detail", {})

    # For planform, prefer session-stored mesh snapshot (faster), fall back to artifact
    mesh_snap = session.get_mesh_snapshot(run_id) or standard.get("mesh_snapshot", {})
    mesh_data = {"mesh_snapshot": mesh_snap} if mesh_snap else {}
    # Provide a mesh array for the planform plot from the first surface snapshot
    for surf_name, surf_mesh in mesh_snap.items():
        le = surf_mesh.get("leading_edge")
        te = surf_mesh.get("trailing_edge")
        if le and te:
            # Build a minimal [nx=2, ny, 3] mesh from LE and TE rows
            mesh_data["mesh"] = np.array([le, te]).tolist()
        break

    conv_data = session.get_convergence(run_id)
    if not conv_data:
        conv_data = results.get("convergence") or artifact.get("convergence") or {}

    # Inject sectional_data into results for lift_distribution / stress plots
    if standard.get("sectional_data"):
        # Merge sectional_data into per-surface dicts
        for surf_name, sect in standard["sectional_data"].items():
            if surf_name in plot_results.get("surfaces", {}):
                plot_results["surfaces"][surf_name]["sectional_data"] = sect
        plot_results["sectional_data"] = standard.get("sectional_data", {})

    # Build optimization_history for opt_* plot types
    opt_history: dict | None = None
    if artifact_type == "optimization" or plot_type.startswith("opt_"):
        raw_hist = results.get("optimization_history", {})
        opt_history = {
            **raw_hist,
            # Expose final DVs alongside initial for opt_comparison
            "final_dvs": results.get("optimized_design_variables", {}),
        }

    plot_result = await asyncio.to_thread(
        generate_plot,
        plot_type, run_id, plot_results, conv_data, mesh_data, case_name, opt_history,
        save_dir,
    )
    # Attach structured plot data so MCP Apps widget can render interactive Plotly charts.
    # Text/image clients (Claude) are unaffected — they use the PNG image as before.
    plot_result.metadata["plot_data"] = extract_plot_data(
        plot_type, plot_results, conv_data, mesh_data, opt_history
    )

    # Branch return based on output mode
    if effective_output == "file":
        # File mode: metadata only (PNG already saved to disk), no ImageContent noise
        return [plot_result.metadata]
    elif effective_output == "url":
        # URL mode: add dashboard and plot URLs for clickable access in CLI
        base_url = _get_viewer_base_url()
        if base_url:
            plot_result.metadata["dashboard_url"] = (
                f"{base_url}/dashboard?run_id={run_id}"
            )
            plot_result.metadata["plot_url"] = (
                f"{base_url}/plot?run_id={run_id}&plot_type={plot_type}"
            )
        return [plot_result.metadata]
    else:
        # Inline mode (default): metadata + ImageContent for claude.ai
        return [plot_result.metadata, plot_result.image]


@mcp.tool()
@capture_tool
async def get_n2_html(
    run_id: Annotated[str, "Run ID whose N2 diagram to fetch"],
    session_id: Annotated[str | None, "Session hint for faster artifact lookup"] = None,
) -> list:
    """Fetch the saved N2 HTML file for a run.

    Called on-demand (e.g. by the widget download button) after visualize() has
    already generated the file.  Returns the full HTML as TextContent so the
    caller can save or display it.

    Raises ValueError if the artifact is not found or the N2 file has not been
    generated yet (call visualize(run_id, 'n2') first).
    """
    from mcp.types import TextContent

    artifact = await asyncio.to_thread(_artifacts.get, run_id, session_id)
    if artifact is None:
        raise ValueError(f"Run '{run_id}' not found.")

    artifact_meta = artifact.get("metadata", {})
    user = artifact_meta.get("user", get_current_user())
    project = artifact_meta.get("project", "default")
    sid = artifact_meta.get("session_id", session_id or "default")

    n2_path = _artifacts._data_dir / user / project / sid / f"n2_{run_id}.html"
    if not n2_path.exists():
        raise ValueError(
            f"N2 HTML file not found at {n2_path}. "
            "Call visualize(run_id, 'n2') first to generate it."
        )
    html = n2_path.read_text(encoding="utf-8")
    return [TextContent(type="text", text=html)]


@mcp.tool()
@capture_tool
async def get_last_logs(
    run_id: Annotated[str, "Run ID to retrieve server-side logs for"],
) -> dict:
    """Retrieve server-side log records captured during a run.

    Agents cannot access server stderr, so this exposes recent log lines
    through MCP for debugging convergence issues, unexpected outputs, etc.

    Returns a list of log records with time, level, message, and logger name.
    Returns empty list if no logs were captured for this run_id.
    """
    run_id = await _resolve_run_id(run_id)
    logs = get_run_logs(run_id)
    if logs is None:
        logs = []
    return {
        "run_id": run_id,
        "log_count": len(logs),
        "logs": logs,
    }


@mcp.tool()
@capture_tool
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


@mcp.tool()
@capture_tool
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


# ---------------------------------------------------------------------------
# Resources — reference material the LLM can read on demand
# ---------------------------------------------------------------------------

from pathlib import Path as _Path
_REFERENCE = (_Path(__file__).parent / "reference.md").read_text()
_WORKFLOWS = (_Path(__file__).parent / "workflows.md").read_text()



@mcp.resource("oas://reference", description="Parameter reference for all OAS MCP tools")
def reference_guide() -> str:
    return _REFERENCE


@mcp.resource("oas://workflows", description="Step-by-step workflows for common analysis tasks")
def workflow_guide() -> str:
    return _WORKFLOWS


@mcp.resource("oas://artifacts/{run_id}", description="Retrieve a saved analysis artifact by run_id")
def artifact_by_run_id(run_id: str) -> str:
    """Return the full artifact JSON for the given run_id."""
    artifact = _artifacts.get(run_id)
    if artifact is None:
        return json.dumps({"error": f"Artifact '{run_id}' not found"})
    return json.dumps(artifact, indent=2)


# ---------------------------------------------------------------------------
# Prompts — workflow templates that seed the agent with goal + context
# ---------------------------------------------------------------------------


@mcp.prompt(
    name="analyze_wing",
    description="Set up and run a complete aerodynamic wing analysis (aero + drag polar + stability)",
)
def prompt_analyze_wing(
    wing_type: str = "CRM",
    span: str = "default",
    target_CL: str = "0.5",
    Mach: str = "0.84",
) -> str:
    span_note = "" if span == "default" else f" with a span of {span} m"
    span_param = "" if span == "default" else f"\n   Set span={span}."
    return f"""\
Analyse a {wing_type} wing{span_note} at Mach {Mach} and find the operating point \
that achieves CL ≈ {target_CL}.

Follow these steps using the OpenAeroStruct tools:

1. Call create_surface to define the wing geometry.
   Use wing_type="{wing_type}", num_x=2, num_y=7, symmetry=True, with_viscous=True, CD0=0.015.{span_param}
   Use wing_type="CRM" for a realistic transport wing; "rect" for a clean rectangular planform.

2. Call run_aero_analysis at alpha=5.0 (default cruise: velocity=248.136, Mach_number={Mach}, density=0.38).
   Read envelope.summary.narrative and check validation.passed.
   Note any flags in summary.flags (e.g. tip_loaded, induced_drag_dominant).

3. Call visualize(run_id, "lift_distribution") to see the spanwise Cl distribution.

4. Call compute_drag_polar with alpha_start=-5.0, alpha_end=15.0, num_alpha=21
   to map out the full polar and find the alpha that gives CL ≈ {target_CL}.
   Check results.best_L_over_D for the optimum operating point.

5. Call compute_stability_derivatives at the operating alpha.
   Set cg to approximately 25% of the mean chord ahead of the aerodynamic centre
   to check whether the configuration is statically stable.

6. Report results:
   - Operating point: alpha, CL, CD, L/D at the target CL
   - Best L/D point: alpha, CL, L/D from the drag polar
   - Lift distribution balance (from summary.derived_metrics)
   - Drag breakdown: CDi%, CDv%, CDw% (from summary.derived_metrics.drag_breakdown_pct)
   - Stability: CL_alpha, static margin, and whether the configuration is statically stable
   - Any validation warnings
"""


@mcp.prompt(
    name="aerostructural_design",
    description="Run a coupled aerostructural analysis and interpret structural health",
)
def prompt_aerostructural_design(
    W0_kg: str = "120000",
    load_factor: str = "2.5",
    material: str = "aluminum",
) -> str:
    material_props = {
        "aluminum":   "E=70e9, G=30e9, yield_stress=500e6, mrho=3000.0",
        "titanium":   "E=114e9, G=42e9, yield_stress=950e6, mrho=4430.0",
        "composite":  "E=70e9, G=30e9, yield_stress=900e6, mrho=1600.0",
    }.get(material, "E=70e9, G=30e9, yield_stress=500e6, mrho=3000.0")

    return f"""\
Size a wing structure for an aircraft with empty weight W0={W0_kg} kg using \
{material} material properties, at a load factor of {load_factor}.

Follow these steps:

1. Call create_surface with fem_model_type="tube" and material properties:
   {material_props}, safety_factor=2.5
   Use wing_type="CRM", num_x=2, num_y=7, symmetry=True, with_viscous=True, CD0=0.015.

2. Call run_aerostruct_analysis with:
   W0={W0_kg}, load_factor={load_factor}, Mach_number=0.84, density=0.38,
   velocity=248.136, R=11.165e6, speed_of_sound=295.4

3. Interpret the results:
   • failure < 0  →  structure is safe (report the margin)
   • failure > 0  →  structure has failed; the design needs thicker skins
   • L_equals_W residual: if |L_equals_W| > 0.1, note that alpha or W0 may need adjustment
   • Report structural_mass, fuelburn, and the failure metric.

4. If failure > 0, call run_optimization with objective="fuelburn",
   design_variables=[thickness (lower=0.003, upper=0.25), alpha, twist],
   constraints=[L_equals_W=0, failure<=0, thickness_intersects<=0]
   to find the minimum-weight feasible structure.
"""


@mcp.prompt(
    name="optimize_wing",
    description="Optimise wing twist and/or thickness for minimum drag or fuel burn",
)
def prompt_optimize_wing(
    objective: str = "CD",
    target_CL: str = "0.5",
    analysis_type: str = "aero",
) -> str:
    struct_note = ""
    dv_list = '[{"name":"twist","lower":-10,"upper":15}, {"name":"alpha","lower":-5,"upper":10}]'
    con_list = f'[{{"name":"CL","equals":{target_CL}}}]'

    if analysis_type == "aerostruct":
        struct_note = (
            "Use fem_model_type='tube', E=70e9, G=30e9, yield_stress=500e6, "
            "safety_factor=2.5, mrho=3000.0 in create_surface.\n   "
        )
        dv_list = (
            '[{"name":"twist","lower":-10,"upper":15},'
            '{"name":"thickness","lower":0.003,"upper":0.25,"scaler":100},'
            '{"name":"alpha","lower":-5,"upper":10}]'
        )
        con_list = (
            '[{"name":"L_equals_W","equals":0},'
            '{"name":"failure","upper":0},'
            '{"name":"thickness_intersects","upper":0}]'
        )

    final_metric = "fuelburn" if objective == "fuelburn" else "L/D"

    return f"""\
Optimise a wing for minimum {objective} subject to CL={target_CL} \
using a {analysis_type} analysis.

Follow these steps:

1. Call create_surface:
   {struct_note}Use wing_type="CRM", num_x=2, num_y=7, symmetry=True, \
with_viscous=True, CD0=0.015.

2. Call run_aero_analysis (or run_aerostruct_analysis) at alpha=5.0 to establish a baseline.
   Note baseline CL, CD, L/D from summary.narrative.
   Save the baseline run_id for later comparison.

3. Call run_optimization with:
   analysis_type="{analysis_type}"
   objective="{objective}"
   design_variables={dv_list}
   constraints={con_list}
   Mach_number=0.84, density=0.38, velocity=248.136

4. Call visualize(run_id, "opt_history") to see objective convergence.
   If design variables changed significantly, also call visualize(run_id, "opt_dv_evolution").
   Call visualize(run_id, "opt_comparison") for a side-by-side DV comparison.

5. Report results:
   - Convergence: success (True/False), number of iterations
   - Objective improvement: summary.derived_metrics.objective_improvement_pct
   - Optimised DV values: results.optimized_design_variables (root-to-tip ordering)
   - Final performance: CL, CD, {final_metric} from results.final_results
   - Constraint satisfaction: CL residual, failure margin
   - Any validation warnings

Decision guide:
- Minimize drag (aero-only): objective="CD", DVs=[twist, alpha], constraints=[CL=target]
- Minimize fuel burn (aerostruct): objective="fuelburn", DVs=[twist, thickness, alpha],
  constraints=[L_equals_W=0, failure<=0, thickness_intersects<=0]
- Minimize structural mass: objective="structural_mass", same DVs/constraints as fuelburn
"""


@mcp.prompt(
    name="compare_designs",
    description="Compare two OAS analysis runs side by side using run_ids",
)
def prompt_compare_designs(
    run_id_1: str = "",
    run_id_2: str = "",
) -> str:
    if run_id_1 and run_id_2:
        run_spec = f"Compare run_id_1={run_id_1!r} and run_id_2={run_id_2!r}."
    else:
        run_spec = (
            "No run_ids were specified. Call list_artifacts() and use the two most "
            "recent run_ids, or ask the user to provide them."
        )

    return f"""\
Compare two OAS analysis runs side by side. {run_spec}

Follow these steps:

1. Identify the two runs — accept any of:
   - Two explicit run_ids provided above
   - "last two runs" → call list_artifacts() and use the two most recent run_ids
   - "before and after" → use the run_id from before and after an optimization

2. Retrieve both artifacts in parallel — call get_artifact(run_id_1) and
   get_artifact(run_id_2) simultaneously.
   Extract metadata.analysis_type, results, and metadata.parameters from each.

3. Build a comparison table — create a markdown table with these metrics (where applicable):

   | Metric               | Run 1 | Run 2 | Change | Change % |
   |----------------------|-------|-------|--------|----------|
   | CL                   | ...   | ...   | ...    | ...      |
   | CD                   | ...   | ...   | ...    | ...      |
   | L/D                  | ...   | ...   | ...    | ...      |
   | CM                   | ...   | ...   | ...    | ...      |
   | fuelburn (kg)        | ...   | ...   | ...    | ...      |
   | structural_mass (kg) | ...   | ...   | ...    | ...      |
   | failure              | ...   | ...   | ...    | ...      |

   Highlight rows with >5% change in bold or with a ★ marker.

4. Compare design variables — if both runs have results.optimized_design_variables
   (optimization runs) or different input parameters, note what changed.

5. Spanwise distribution qualitative comparison — call get_detailed_results(run_id, "standard")
   for each run (in parallel), then describe:
   - Whether the lift distribution became more/less elliptical
   - Whether the stress distribution changed significantly

6. Summarize in 3-5 sentences: what changed, by how much, and what it means for the
   design. Reference the analysis_type context (aero vs aerostruct, cruise vs polar)
   and make a design recommendation.

Output format:
- Markdown table for quantitative metrics
- Bullet list for qualitative observations
- Final 3-5 sentence summary with design recommendation
"""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    """Console-script entry point for oas-mcp.

    Supports two transports:

    * ``stdio`` (default) — standard MCP stdio transport for Claude Desktop /
      local clients.
    * ``http`` — streamable HTTP transport for remote clients.  Requires the
      ``[http]`` extra (``pip install 'openaerostruct[http]'``).  Reads host
      and port from ``--host`` / ``--port`` CLI args or ``OAS_HOST`` /
      ``OAS_PORT`` env vars.

    Set the transport via ``--transport`` or the ``OAS_TRANSPORT`` env var.

    Environment variables are loaded from a ``.env`` file in the working
    directory (or any parent) via ``python-dotenv`` at module import time,
    before Keycloak settings and FastMCP are initialised.  Variables already
    set in the process environment take precedence over the file.
    """
    import argparse

    parser = argparse.ArgumentParser(description="OpenAeroStruct MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default=os.environ.get("OAS_TRANSPORT", "stdio"),
        help="Transport protocol (default: stdio)",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("OAS_HOST", "127.0.0.1"),
        help="Bind host for HTTP transport (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("OAS_PORT", "8000")),
        help="Bind port for HTTP transport (default: 8000)",
    )
    args = parser.parse_args()

    # --- Provenance setup ---
    _prov_init_db()
    import uuid as _uuid
    _auto_sid = f"auto-{_uuid.uuid4().hex[:8]}"
    _prov_session_id.set(_auto_sid)
    _prov_record_session(_auto_sid, notes="Auto-created on server startup")
    if args.transport == "stdio":
        # Legacy daemon thread viewer for local dev (localhost only, no auth)
        try:
            import sys as _sys
            from .provenance.viewer_server import start_viewer_server as _start_viewer
            _prov_port = _start_viewer()
            if _prov_port:
                _sep = "─" * 54
                print(f"\n{_sep}", file=_sys.stderr)
                print("  OAS Provenance Viewer", file=_sys.stderr)
                print(_sep, file=_sys.stderr)
                print(f"  Viewer    http://localhost:{_prov_port}/viewer", file=_sys.stderr)
                print(f"            Interactive DAG — load any session from the", file=_sys.stderr)
                print(f"            drop-down or drop an exported JSON file.", file=_sys.stderr)
                print(f"  Sessions  http://localhost:{_prov_port}/sessions", file=_sys.stderr)
                print(f"            JSON list of all recorded provenance sessions.", file=_sys.stderr)
                print(f"  Plot API  http://localhost:{_prov_port}/plot?run_id=<id>&plot_type=<type>", file=_sys.stderr)
                print(f"            Render a saved analysis run as a PNG image.", file=_sys.stderr)
                print(_sep + "\n", file=_sys.stderr)
        except Exception:
            pass
        mcp.run()
    else:
        # --- HTTP transport ---
        try:
            import uvicorn
        except ImportError as exc:
            raise ImportError(
                "uvicorn is required for HTTP transport. "
                "Install it with: pip install 'openaerostruct[http]'"
            ) from exc

        import sys as _sys
        from .core.viewer_routes import build_viewer_app

        _warn_if_unauthenticated(args.host, args.port)
        mcp_asgi = mcp.streamable_http_app()
        viewer_app = build_viewer_app()

        if viewer_app is not None:
            # Compose viewer + MCP: viewer handles its known paths,
            # everything else falls through to the MCP ASGI app.
            from .core.viewer_routes import make_fallback_app
            app = make_fallback_app(viewer_app, mcp_asgi)
            # Print viewer info
            _sep = "─" * 54
            print(f"\n{_sep}", file=_sys.stderr)
            print("  OAS Provenance Viewer (HTTP transport)", file=_sys.stderr)
            print(_sep, file=_sys.stderr)
            print(f"  Viewer    http://{args.host}:{args.port}/viewer", file=_sys.stderr)
            print(f"            Protected by Basic Auth (OAS_VIEWER_USER/PASSWORD)", file=_sys.stderr)
            print(_sep + "\n", file=_sys.stderr)
        else:
            app = mcp_asgi

        uvicorn.run(app, host=args.host, port=args.port)
    # --- End provenance setup ---


def _warn_if_unauthenticated(host: str, port: int) -> None:
    """Print a loud warning to stderr when HTTP transport runs without auth."""
    import sys

    issuer_url = _auth_env("OIDC_ISSUER_URL", "KEYCLOAK_ISSUER_URL")

    if issuer_url:
        print(
            f"\n  OAS MCP — HTTP transport  |  auth: OIDC ({issuer_url})\n",
            file=sys.stderr,
        )
        return

    url = f"http://{host}:{port}/mcp"
    print(
        "\n"
        "╔══════════════════════════════════════════════════════════════════╗\n"
        "║                  ⚠  NO AUTHENTICATION ENABLED  ⚠                ║\n"
        "╠══════════════════════════════════════════════════════════════════╣\n"
        "║  The server is accepting ALL requests on:                        ║\n"
        f"║    {url:<60}  ║\n"
        "║                                                                  ║\n"
        "║  Anyone who can reach this port can call every tool, run         ║\n"
        "║  optimizations, and read/delete all stored artifacts.            ║\n"
        "║                                                                  ║\n"
        "║  This is fine for local development.  For any deployment that    ║\n"
        "║  is reachable over a network, set:                               ║\n"
        "║                                                                  ║\n"
        "║    OIDC_ISSUER_URL=https://<provider>/...                        ║\n"
        "║    OIDC_CLIENT_ID=oas-mcp                                        ║\n"
        "║    OIDC_CLIENT_SECRET=<secret>                                    ║\n"
        "║                                                                  ║\n"
        "║  Works with any OIDC provider (Authentik, Keycloak, Auth0, …).  ║\n"
        "╚══════════════════════════════════════════════════════════════════╝\n",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
