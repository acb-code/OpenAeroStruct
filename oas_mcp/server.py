"""
OAS MCP Server — FastMCP entry point.

All @mcp.tool() registrations live here.  Heavy OpenMDAO work is dispatched
to a thread pool via asyncio.to_thread() so the event loop stays responsive.
"""

from __future__ import annotations

# Load .env before any module-level env var reads.
# auth.py reads KEYCLOAK_ISSUER_URL at import time, and FastMCP() is
# constructed at module level — both must happen after dotenv runs.
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
import warnings
from typing import Annotated, Any

import numpy as np
from mcp.server.fastmcp import FastMCP

from .core.builders import (
    build_aero_problem,
    build_aerostruct_problem,
    build_optimization_problem,
)
from .core.defaults import (
    DEFAULT_AEROSTRUCT_CONDITIONS,
    DEFAULT_AERO_CONDITIONS,
    DEFAULT_WINGBOX_UPPER_X,
    DEFAULT_WINGBOX_UPPER_Y,
    DEFAULT_WINGBOX_LOWER_X,
    DEFAULT_WINGBOX_LOWER_Y,
)
from .core.mesh import apply_dihedral, apply_sweep, apply_taper, build_mesh
from .core.results import (
    extract_aero_results,
    extract_aerostruct_results,
    extract_stability_results,
)
from .core.artifacts import ArtifactStore
from .core.auth import build_auth_settings, build_token_verifier
from .core.session import SessionManager
from .core.validators import (
    validate_fem_model_type,
    validate_flight_conditions,
    validate_mesh_params,
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

PARAMETER TIPS:
  • Cruise conditions: velocity=248 m/s, Mach_number=0.84, density=0.38 kg/m³, re=1e6
  • Good starting mesh: num_x=2, num_y=7 (fast); use num_y=15 for higher fidelity
  • wing_type="CRM" produces a realistic transport wing with built-in twist;
    wing_type="rect" produces a flat untwisted planform — simpler but less realistic
  • failure < 0 means no structural failure; failure > 0 means the structure has failed
  • L_equals_W residual near 0 means the wing is sized to carry the aircraft weight

PERFORMANCE:
  • The first run_aero_analysis call builds and sets up the OpenMDAO problem (~0.1 s).
    Subsequent calls with the same surfaces reuse the cached problem — only the flight
    conditions change, so parameter sweeps are very fast.
  • Calling create_surface again with the same name invalidates the cache.

ARTIFACT STORAGE (every analysis is automatically saved):
  • Each analysis tool returns a run_id — use it to retrieve results later.
  • list_artifacts(session_id?, analysis_type?) — browse saved runs
  • get_artifact(run_id) — full metadata + results for a past run
  • get_artifact_summary(run_id) — metadata only (lightweight)
  • delete_artifact(run_id) — remove a saved artifact
  • oas://artifacts/{run_id} — resource access to any artifact by run_id

DESIGN VARIABLE NAMES FOR run_optimization:
  • All models:   'twist', 'chord', 'sweep', 'taper', 'alpha'
  • Tube only:    'thickness'   (maps to thickness_cp — does NOT exist on wingbox surfaces)
  • Wingbox only: 'spar_thickness', 'skin_thickness'  (do NOT use 'thickness' for wingbox)

Use the prompts (analyze_wing, aerostructural_design, optimize_wing) for guided
workflows, and the resources (oas://reference, oas://workflows) for quick lookup.""",
)

_sessions = SessionManager()
_artifacts = ArtifactStore()


def _suppress_output(func, *args, **kwargs):
    """Run func(*args, **kwargs) while suppressing stdout/stderr and OpenMDAO warnings."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return func(*args, **kwargs)


# ---------------------------------------------------------------------------
# Tool 1 — create_surface
# ---------------------------------------------------------------------------


@mcp.tool()
async def create_surface(
    name: Annotated[str, "Unique surface name (e.g. 'wing', 'tail')"] = "wing",
    wing_type: Annotated[str, "Mesh type: 'rect' for rectangular or 'CRM' for Common Research Model"] = "rect",
    span: Annotated[float, "Full wingspan in metres"] = 10.0,
    root_chord: Annotated[float, "Root chord length in metres"] = 1.0,
    taper: Annotated[float, "Taper ratio (tip_chord / root_chord), 1.0 = no taper"] = 1.0,
    sweep: Annotated[float, "Leading-edge sweep angle in degrees"] = 0.0,
    dihedral: Annotated[float, "Dihedral angle in degrees"] = 0.0,
    num_x: Annotated[int, "Number of chordwise mesh nodes (>= 2)"] = 2,
    num_y: Annotated[int, "Number of spanwise mesh nodes (must be odd, >= 3)"] = 7,
    symmetry: Annotated[bool, "If True, model only one half of the wing"] = True,
    twist_cp: Annotated[list[float] | None, "Twist control-point values in degrees (None = zero twist)"] = None,
    chord_cp: Annotated[list[float] | None, "Chord control-point scale factors (None = unit chord)"] = None,
    t_over_c_cp: Annotated[list[float] | None, "Thickness-to-chord ratio control points (None = [0.15])"] = None,
    CL0: Annotated[float, "Lift coefficient at alpha=0 (profile)"] = 0.0,
    CD0: Annotated[float, "Zero-lift drag coefficient (profile)"] = 0.015,
    with_viscous: Annotated[bool, "Include viscous (skin-friction) drag"] = True,
    with_wave: Annotated[bool, "Include wave drag"] = False,
    fem_model_type: Annotated[str | None, "Structural model: 'tube', 'wingbox', or None for aero-only"] = None,
    thickness_cp: Annotated[list[float] | None, "Tube wall thickness control points in metres (tube model only)"] = None,
    spar_thickness_cp: Annotated[list[float] | None, "Wingbox spar thickness control points in metres (wingbox model only)"] = None,
    skin_thickness_cp: Annotated[list[float] | None, "Wingbox skin thickness control points in metres (wingbox model only)"] = None,
    original_wingbox_airfoil_t_over_c: Annotated[float, "Thickness-to-chord ratio of the reference airfoil used for wingbox cross-section geometry (wingbox model only)"] = 0.12,
    E: Annotated[float, "Young's modulus in Pa (default: aluminium 7075, 70 GPa)"] = 70.0e9,
    G: Annotated[float, "Shear modulus in Pa (default: aluminium 7075, 30 GPa)"] = 30.0e9,
    yield_stress: Annotated[float, "Yield stress in Pa (default: 500 MPa)"] = 500.0e6,
    safety_factor: Annotated[float, "Safety factor applied to yield stress"] = 2.5,
    mrho: Annotated[float, "Material density in kg/m^3 (default: Al 7075, 3000 kg/m^3)"] = 3.0e3,
    offset: Annotated[list[float] | None, "3-element [x, y, z] offset of the surface origin in metres"] = None,
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
            sweep=sweep,
            dihedral=dihedral,
            taper=taper,
            offset=offset,
        )

        # Apply geometric modifications
        if sweep != 0.0:
            mesh = apply_sweep(mesh, sweep)
        if dihedral != 0.0:
            mesh = apply_dihedral(mesh, dihedral)
        if taper != 1.0:
            mesh = apply_taper(mesh, taper)

        # Determine twist_cp
        if twist_cp is not None:
            tcp = np.array(twist_cp, dtype=float)
        elif crm_twist is not None:
            tcp = crm_twist
        else:
            tcp = np.zeros(2)

        # Build surface dict
        surface = {
            "name": name,
            "symmetry": symmetry,
            "S_ref_type": "wetted",
            "mesh": mesh,
            "twist_cp": tcp,
            "CL0": CL0,
            "CD0": CD0,
            "k_lam": 0.05,
            "t_over_c_cp": np.array(t_over_c_cp if t_over_c_cp else [0.15]),
            "c_max_t": 0.303,
            "with_viscous": with_viscous,
            "with_wave": with_wave,
        }

        if chord_cp is not None:
            surface["chord_cp"] = np.array(chord_cp, dtype=float)

        if fem_model_type and fem_model_type != "none":
            surface["fem_model_type"] = fem_model_type
            surface["E"] = E
            surface["G"] = G
            surface["yield"] = yield_stress
            surface["safety_factor"] = safety_factor
            surface["mrho"] = mrho
            surface["fem_origin"] = 0.35
            surface["wing_weight_ratio"] = 2.0
            surface["struct_weight_relief"] = False
            surface["distributed_fuel_weight"] = False
            surface["exact_failure_constraint"] = False

            if fem_model_type == "wingbox":
                # Wingbox-specific thickness control points
                ny2 = (num_y + 1) // 2
                n_cp = max(2, min(6, ny2 // 2))
                surface["spar_thickness_cp"] = (
                    np.array(spar_thickness_cp, dtype=float)
                    if spar_thickness_cp is not None
                    else np.linspace(0.004, 0.01, n_cp)
                )
                surface["skin_thickness_cp"] = (
                    np.array(skin_thickness_cp, dtype=float)
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
                    surface["thickness_cp"] = np.array(thickness_cp, dtype=float)
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
async def run_aero_analysis(
    surfaces: Annotated[list[str], "Names of surfaces to include (must have been created via create_surface)"],
    velocity: Annotated[float, "Free-stream velocity in m/s"] = 248.136,
    alpha: Annotated[float, "Angle of attack in degrees"] = 5.0,
    Mach_number: Annotated[float, "Mach number"] = 0.84,
    reynolds_number: Annotated[float, "Reynolds number per unit length (1/m)"] = 1.0e6,
    density: Annotated[float, "Air density in kg/m^3"] = 0.38,
    cg: Annotated[list[float] | None, "Centre of gravity [x, y, z] in metres"] = None,
    session_id: Annotated[str, "Session identifier"] = "default",
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
        return extract_aero_results(prob, surface_dicts, "aero")

    results = await asyncio.to_thread(_suppress_output, _run)
    run_id = _artifacts.save(
        session_id=session_id,
        analysis_type="aero",
        tool_name="run_aero_analysis",
        surfaces=surfaces,
        parameters={
            "velocity": velocity, "alpha": alpha, "Mach_number": Mach_number,
            "reynolds_number": reynolds_number, "density": density,
        },
        results=results,
    )
    return {**results, "run_id": run_id}


# ---------------------------------------------------------------------------
# Tool 3 — run_aerostruct_analysis
# ---------------------------------------------------------------------------


@mcp.tool()
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
        return extract_aerostruct_results(prob, surface_dicts, "AS_point_0")

    results = await asyncio.to_thread(_suppress_output, _run)
    run_id = _artifacts.save(
        session_id=session_id,
        analysis_type="aerostruct",
        tool_name="run_aerostruct_analysis",
        surfaces=surfaces,
        parameters={
            "velocity": velocity, "alpha": alpha, "Mach_number": Mach_number,
            "reynolds_number": reynolds_number, "density": density,
            "W0": W0, "R": R, "speed_of_sound": speed_of_sound, "load_factor": load_factor,
        },
        results=results,
    )
    return {**results, "run_id": run_id}


# ---------------------------------------------------------------------------
# Tool 4 — compute_drag_polar
# ---------------------------------------------------------------------------


@mcp.tool()
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
    run_id = _artifacts.save(
        session_id=session_id,
        analysis_type="drag_polar",
        tool_name="compute_drag_polar",
        surfaces=surfaces,
        parameters={
            "alpha_start": alpha_start, "alpha_end": alpha_end, "num_alpha": num_alpha,
            "velocity": velocity, "Mach_number": Mach_number,
            "reynolds_number": reynolds_number, "density": density,
        },
        results=polar_results,
    )
    return {**polar_results, "run_id": run_id}


# ---------------------------------------------------------------------------
# Tool 5 — compute_stability_derivatives
# ---------------------------------------------------------------------------


@mcp.tool()
async def compute_stability_derivatives(
    surfaces: Annotated[list[str], "Names of surfaces to include"],
    alpha: Annotated[float, "Angle of attack in degrees"] = 5.0,
    velocity: Annotated[float, "Free-stream velocity in m/s"] = 248.136,
    Mach_number: Annotated[float, "Mach number"] = 0.84,
    reynolds_number: Annotated[float, "Reynolds number per unit length (1/m)"] = 1.0e6,
    density: Annotated[float, "Air density in kg/m^3"] = 0.38,
    cg: Annotated[list[float] | None, "Centre of gravity [x, y, z] in metres — affects CM and static margin"] = None,
    session_id: Annotated[str, "Session identifier"] = "default",
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

    results = await asyncio.to_thread(_suppress_output, _run)
    run_id = _artifacts.save(
        session_id=session_id,
        analysis_type="stability",
        tool_name="compute_stability_derivatives",
        surfaces=surfaces,
        parameters={
            "alpha": alpha, "velocity": velocity, "Mach_number": Mach_number,
            "reynolds_number": reynolds_number, "density": density,
        },
        results=results,
    )
    return {**results, "run_id": run_id}


# ---------------------------------------------------------------------------
# Tool 6 — run_optimization
# ---------------------------------------------------------------------------


@mcp.tool()
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
        "List of constraint dicts, e.g. [{'name':'CL','equals':0.5}, {'name':'failure','upper':0.0}]"
    ] = None,
    velocity: Annotated[float, "Free-stream velocity in m/s"] = 248.136,
    alpha: Annotated[float, "Initial angle of attack in degrees"] = 5.0,
    Mach_number: Annotated[float, "Mach number"] = 0.84,
    reynolds_number: Annotated[float, "Reynolds number per unit length (1/m)"] = 1.0e6,
    density: Annotated[float, "Air density in kg/m^3"] = 0.38,
    W0: Annotated[float, "Aircraft empty weight in kg (aerostruct only)"] = 0.4 * 3e5,
    speed_of_sound: Annotated[float, "Speed of sound in m/s (aerostruct only)"] = 295.4,
    load_factor: Annotated[float, "Load factor (aerostruct only)"] = 1.0,
    tolerance: Annotated[float, "Optimiser convergence tolerance"] = 1e-6,
    max_iterations: Annotated[int, "Maximum optimiser iterations"] = 200,
    session_id: Annotated[str, "Session identifier"] = "default",
) -> dict:
    """Run a design optimisation.

    Minimises the objective function subject to the given constraints by
    varying the specified design variables.  Supports aero-only (VLM) and
    coupled aerostructural problems.

    Design variable names (all models):   'twist', 'chord', 'sweep', 'taper', 'alpha'
    Design variable names (tube only):    'thickness'
    Design variable names (wingbox only): 'spar_thickness', 'skin_thickness'
    Constraint names (aero):              'CL', 'CD', 'CM'
    Constraint names (aerostruct):        all aero + 'failure', 'thickness_intersects', 'L_equals_W'
    Objective names (aero):               'CD', 'CL'
    Objective names (aerostruct):         'fuelburn', 'structural_mass', 'CD'
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

    from openaerostruct.utils.constants import grav_constant
    ct_val = grav_constant * 17.0e-6

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
            "W0": W0,
            "speed_of_sound": speed_of_sound,
            "load_factor": load_factor,
        }

    def _run():
        prob, point_name = build_optimization_problem(
            surface_dicts,
            analysis_type=analysis_type,
            objective=objective,
            design_variables=design_variables,
            constraints=constraints,
            flight_conditions=flight_conditions,
            tolerance=tolerance,
            max_iterations=max_iterations,
        )

        prob.run_driver()
        success = prob.driver.result.success if hasattr(prob.driver, "result") else True

        if analysis_type == "aero":
            final_results = extract_aero_results(prob, surface_dicts, point_name)
        else:
            final_results = extract_aerostruct_results(prob, surface_dicts, point_name)

        # Extract optimised DV values
        from .core.builders import DV_NAME_MAP, resolve_path
        dv_results = {}
        primary_name = surface_dicts[0]["name"] if surface_dicts else "wing"
        for dv in design_variables:
            template = DV_NAME_MAP.get(dv["name"])
            if template:
                path = resolve_path(template, primary_name, point_name)
                try:
                    val = prob.get_val(path)
                    dv_results[dv["name"]] = np.asarray(val).tolist()
                except Exception:
                    pass

        return {
            "success": bool(success),
            "optimized_design_variables": dv_results,
            "final_results": final_results,
        }

    result = await asyncio.to_thread(_suppress_output, _run)
    run_id = _artifacts.save(
        session_id=session_id,
        analysis_type="optimization",
        tool_name="run_optimization",
        surfaces=surfaces,
        parameters={
            "analysis_type": analysis_type, "objective": objective,
            "velocity": velocity, "alpha": alpha, "Mach_number": Mach_number,
            "density": density,
        },
        results=result,
    )
    return {**result, "run_id": run_id}


# ---------------------------------------------------------------------------
# Tool 7 — reset
# ---------------------------------------------------------------------------


@mcp.tool()
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
async def list_artifacts(
    session_id: Annotated[str | None, "Filter by session ID, or None to list all sessions"] = None,
    analysis_type: Annotated[
        str | None,
        "Filter by type: 'aero', 'aerostruct', 'drag_polar', 'stability', 'optimization'",
    ] = None,
) -> dict:
    """List saved analysis artifacts with optional filters.

    Returns a count and a list of index entries (run_id, session_id,
    analysis_type, timestamp, surfaces, tool_name).  Does not load the
    full results payload — use get_artifact for that.
    """
    entries = await asyncio.to_thread(_artifacts.list, session_id, analysis_type)
    return {"count": len(entries), "artifacts": entries}


@mcp.tool()
async def get_artifact(
    run_id: Annotated[str, "Run ID returned by an analysis tool"],
    session_id: Annotated[
        str | None, "Session that owns this artifact — speeds up lookup when provided"
    ] = None,
) -> dict:
    """Retrieve a saved artifact (metadata + full results) by run_id."""
    artifact = await asyncio.to_thread(_artifacts.get, run_id, session_id)
    if artifact is None:
        raise ValueError(f"Artifact '{run_id}' not found")
    return artifact


@mcp.tool()
async def get_artifact_summary(
    run_id: Annotated[str, "Run ID returned by an analysis tool"],
    session_id: Annotated[str | None, "Session that owns this artifact"] = None,
) -> dict:
    """Retrieve artifact metadata only (no results payload) — much smaller response.

    Returns: run_id, session_id, analysis_type, timestamp, surfaces,
    tool_name, parameters.
    """
    summary = await asyncio.to_thread(_artifacts.get_summary, run_id, session_id)
    if summary is None:
        raise ValueError(f"Artifact '{run_id}' not found")
    return summary


@mcp.tool()
async def delete_artifact(
    run_id: Annotated[str, "Run ID to delete"],
    session_id: Annotated[str | None, "Session that owns this artifact"] = None,
) -> dict:
    """Permanently delete a saved artifact from disk."""
    deleted = await asyncio.to_thread(_artifacts.delete, run_id, session_id)
    if not deleted:
        raise ValueError(f"Artifact '{run_id}' not found")
    return {"status": "deleted", "run_id": run_id}


# ---------------------------------------------------------------------------
# Resources — reference material the LLM can read on demand
# ---------------------------------------------------------------------------

_REFERENCE = """\
# OpenAeroStruct MCP — quick reference

## Workflow order (mandatory)
  create_surface → run_aero_analysis | run_aerostruct_analysis
                 → compute_drag_polar | compute_stability_derivatives
                 → run_optimization
  reset  (clears all state)

## create_surface — key parameters
  name          str      Surface identifier used in all other tools
  wing_type     str      "rect" (flat, no twist) | "CRM" (transport planform with twist)
  span          float    Full wingspan in metres (default 10.0)
  root_chord    float    Root chord in metres (default 1.0)
  num_x         int      Chordwise nodes, >= 2 (default 2)
  num_y         int      Spanwise nodes, ODD, >= 3 (default 7)
  symmetry      bool     True = model half-span (recommended, default True)
  sweep         float    Leading-edge sweep, degrees (default 0)
  dihedral      float    Dihedral angle, degrees (default 0)
  taper         float    Taper ratio tip/root chord (default 1.0 = no taper)
  twist_cp      float[]  Twist control points, degrees (None = zero twist)
  t_over_c_cp   float[]  Thickness/chord control points (default [0.15])
  with_viscous  bool     Include viscous drag (default True)
  with_wave     bool     Include wave drag (default False)
  CD0           float    Zero-lift profile drag added to total (default 0.015)
  fem_model_type str|None "tube" | "wingbox" | None  — enables structural analysis
  E             float    Young's modulus, Pa (default 70e9 = Al 7075)
  G             float    Shear modulus, Pa (default 30e9 = Al 7075)
  yield_stress  float    Yield stress, Pa (default 500e6)
  safety_factor float    Safety factor on yield (default 2.5)
  mrho          float    Material density, kg/m³ (default 3000 = Al 7075)
  thickness_cp  float[]  Tube thickness control points, m (default 0.1*root_chord)
  offset        float[3] [x,y,z] origin offset in metres (e.g. tail: [50,0,0])

## Typical flight conditions (cruise, ~FL350)
  velocity=248.136 m/s  Mach_number=0.84  density=0.38 kg/m³
  reynolds_number=1e6   speed_of_sound=295.4 m/s

## run_aero_analysis — returns
  CL, CD, CM, L_over_D
  surfaces.{name}.{CL, CD, CDi, CDv, CDw}

## run_aerostruct_analysis — returns (all of aero plus)
  fuelburn kg, structural_mass kg, L_equals_W (residual, 0=trimmed)
  surfaces.{name}.{failure, max_vonmises_Pa, structural_mass_kg}
  failure < 0  →  safe;  failure > 0  →  structural failure

## compute_drag_polar — returns
  alpha_deg[], CL[], CD[], CM[], L_over_D[]
  best_L_over_D.{alpha_deg, CL, CD, L_over_D}

## compute_stability_derivatives — returns
  CL_alpha (1/deg), CM_alpha (1/deg), static_margin, stability (string)
  static_margin = -CM_alpha/CL_alpha;  positive = statically stable

## run_optimization — design variable names
  twist, thickness, chord, sweep, taper, alpha
  spar_thickness, skin_thickness  (wingbox only)

## run_optimization — constraint names
  aero:        CL, CD, CM
  aerostruct:  CL, CD, CM, failure, thickness_intersects, L_equals_W

## run_optimization — objective names
  aero:        CD, CL
  aerostruct:  fuelburn, structural_mass, CD

## Artifact storage (automatic)
  Every analysis tool saves a run_id.  Use it to retrieve results later.
  list_artifacts(session_id?, analysis_type?)   list saved runs (index only)
  get_artifact(run_id, session_id?)             full metadata + results
  get_artifact_summary(run_id, session_id?)     metadata only (no payload)
  delete_artifact(run_id, session_id?)          remove permanently
  oas://artifacts/{run_id}                      resource access by run_id
  OAS_DATA_DIR env var controls storage root    (default: ./oas_data/artifacts/)

## Common errors and fixes
  "num_y must be odd"           → change num_y to nearest odd number
  "missing structural props"    → re-create surface with fem_model_type="tube"
  "Surface not found"           → call create_surface first with that exact name
  "Unknown design variable"     → check spelling against the DV list above
"""

_WORKFLOWS = """\
# OpenAeroStruct MCP — step-by-step workflows

---
## Workflow A — aerodynamic analysis of a new wing

Goal: characterise CL, CD, L/D of a wing at cruise.

Step 1 — define the geometry:
  create_surface(
      name="wing", wing_type="CRM",
      num_x=2, num_y=7, symmetry=True,
      with_viscous=True, CD0=0.015
  )

Step 2 — single-point cruise analysis:
  run_aero_analysis(
      surfaces=["wing"],
      velocity=248.136, alpha=5.0,
      Mach_number=0.84, density=0.38
  )

Step 3 — drag polar to find best L/D:
  compute_drag_polar(
      surfaces=["wing"],
      alpha_start=0.0, alpha_end=12.0, num_alpha=13,
      Mach_number=0.84, density=0.38
  )
  → inspect best_L_over_D to find operating point

Step 4 (optional) — stability check:
  compute_stability_derivatives(
      surfaces=["wing"],
      alpha=5.0, Mach_number=0.84, density=0.38,
      cg=[<x_cg>, 0, 0]   # x_cg in metres from leading edge
  )

---
## Workflow B — aerostructural sizing

Goal: check whether a wing structure can carry the aerodynamic loads at cruise,
and compute mission fuel burn.

Step 1 — define wing with structural properties:
  create_surface(
      name="wing", wing_type="CRM",
      num_x=2, num_y=7, symmetry=True,
      with_viscous=True, CD0=0.015,
      fem_model_type="tube",
      E=70e9, G=30e9, yield_stress=500e6, safety_factor=2.5, mrho=3000.0
  )

Step 2 — coupled aerostructural analysis:
  run_aerostruct_analysis(
      surfaces=["wing"],
      velocity=248.136, alpha=5.0,
      Mach_number=0.84, density=0.38,
      W0=120000,         # aircraft empty weight excl. wing, kg
      R=11.165e6,        # mission range, m
      speed_of_sound=295.4, load_factor=1.0
  )

Step 3 — interpret results:
  • failure < 0  →  structure is safe at this load
  • failure > 0  →  increase thickness_cp values or reduce load_factor
  • L_equals_W ≈ 0  →  wing is sized for aircraft weight; large residual means
    alpha or W0 needs adjustment
  • fuelburn / structural_mass are the primary sizing metrics

---
## Workflow C — aerodynamic optimisation

Goal: minimise drag at a fixed lift coefficient by varying twist and alpha.

Step 1 — define geometry (aero-only surface is fine):
  create_surface(
      name="wing", wing_type="CRM",
      num_x=2, num_y=7, symmetry=True,
      with_viscous=True, CD0=0.015
  )

Step 2 — run optimisation:
  run_optimization(
      surfaces=["wing"],
      analysis_type="aero",
      objective="CD",
      design_variables=[
          {"name": "twist", "lower": -10.0, "upper": 15.0},
          {"name": "alpha", "lower": -5.0,  "upper": 15.0}
      ],
      constraints=[{"name": "CL", "equals": 0.5}],
      Mach_number=0.84, density=0.38
  )

Step 3 — check result:
  • success=True and final_results.CL ≈ 0.5  →  converged
  • success=False  →  try wider DV bounds or a different starting alpha

---
## Workflow D — aerostructural optimisation (minimum fuel burn)

Step 1 — define wing with structural properties (see Workflow B, Step 1)

Step 2:
  run_optimization(
      surfaces=["wing"],
      analysis_type="aerostruct",
      objective="fuelburn",
      design_variables=[
          {"name": "twist",     "lower": -10.0, "upper": 15.0},
          {"name": "thickness", "lower":  0.003, "upper": 0.25,  "scaler": 1e2},
          {"name": "alpha",     "lower":  -5.0,  "upper": 10.0}
      ],
      constraints=[
          {"name": "L_equals_W",  "equals": 0.0},
          {"name": "failure",     "upper":  0.0},
          {"name": "thickness_intersects", "upper": 0.0}
      ],
      W0=120000, R=11.165e6, Mach_number=0.84, density=0.38
  )

---
## Workflow E — multi-surface (wing + tail)

Step 1 — create both surfaces:
  create_surface(name="wing", wing_type="CRM", num_x=2, num_y=7, ...)
  create_surface(name="tail", wing_type="rect", span=6.0, root_chord=1.5,
                 num_x=2, num_y=5, offset=[20.0, 0.0, 0.0],
                 CD0=0.0, CL0=0.0)

Step 2 — analyse both together:
  run_aero_analysis(surfaces=["wing", "tail"], ...)
  compute_drag_polar(surfaces=["wing", "tail"], ...)

  Trimmed stability (CM=0) requires adjusting the tail incidence angle
  (twist_cp on the tail) until CM ≈ 0 at the desired operating CL.
"""


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
    return f"""\
Analyse a {wing_type} wing{span_note} at Mach {Mach} and find the operating point \
that achieves CL ≈ {target_CL}.

Follow these steps using the OpenAeroStruct tools:

1. Call create_surface to define the wing geometry.
   Use wing_type="{wing_type}", num_x=2, num_y=7, symmetry=True, with_viscous=True.
   {"" if span == "default" else f"Set span={span}."}

2. Call run_aero_analysis at a starting alpha (try alpha=5.0) to get a baseline CL/CD/L_D.

3. Call compute_drag_polar with alpha_start=-2.0, alpha_end=12.0, num_alpha=15 \
to map out the full polar and find the alpha that gives CL≈{target_CL}.

4. Call compute_stability_derivatives at the operating alpha. \
Set cg to approximately 25% of the mean chord ahead of the aerodynamic centre \
to check whether the configuration is statically stable.

5. Report: operating alpha, CL, CD, L/D, CL_alpha, static margin, \
and whether the configuration is statically stable.
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
    dv_list = '[{"name":"twist","lower":-10,"upper":15}, {"name":"alpha","lower":-5,"upper":15}]'
    con_list = f'[{{"name":"CL","equals":{target_CL}}}]'

    if analysis_type == "aerostruct":
        struct_note = (
            "Use fem_model_type='tube', E=70e9, G=30e9, yield_stress=500e6, "
            "safety_factor=2.5, mrho=3000.0 in create_surface.\n   "
        )
        dv_list = (
            '[{"name":"twist","lower":-10,"upper":15},'
            '{"name":"thickness","lower":0.003,"upper":0.25,"scaler":100},'
            '{"name":"alpha","lower":-5,"upper":15}]'
        )
        con_list = (
            '[{"name":"L_equals_W","equals":0},'
            '{"name":"failure","upper":0},'
            '{"name":"thickness_intersects","upper":0}]'
        )

    return f"""\
Optimise a wing for minimum {objective} subject to CL={target_CL} \
using a {analysis_type} analysis.

Follow these steps:

1. Call create_surface:
   {struct_note}Use wing_type="CRM", num_x=2, num_y=7, symmetry=True, \
with_viscous=True, CD0=0.015.

2. (Optional but recommended) Call run_aero_analysis at alpha=5.0 to get a \
baseline CL/CD before optimisation.

3. Call run_optimization with:
   analysis_type="{analysis_type}"
   objective="{objective}"
   design_variables={dv_list}
   constraints={con_list}
   Mach_number=0.84, density=0.38, velocity=248.136

4. Report:
   • success (True/False)
   • optimised DV values (twist distribution and alpha)
   • final CL, CD, and {"fuelburn" if objective == "fuelburn" else "L/D"}
   • improvement vs. the baseline from step 2
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

    if args.transport == "http":
        try:
            import uvicorn
        except ImportError as exc:
            raise ImportError(
                "uvicorn is required for HTTP transport. "
                "Install it with: pip install 'openaerostruct[http]'"
            ) from exc

        _warn_if_unauthenticated(args.host, args.port)
        app = mcp.streamable_http_app()
        uvicorn.run(app, host=args.host, port=args.port)
    else:
        mcp.run()


def _warn_if_unauthenticated(host: str, port: int) -> None:
    """Print a loud warning to stderr when HTTP transport runs without auth."""
    import sys

    from .core.auth import KEYCLOAK_ISSUER_URL

    if KEYCLOAK_ISSUER_URL:
        print(
            f"\n  OAS MCP — HTTP transport  |  auth: Keycloak ({KEYCLOAK_ISSUER_URL})\n",
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
        "║    KEYCLOAK_ISSUER_URL=https://<your-kc>/realms/<realm>          ║\n"
        "║    KEYCLOAK_CLIENT_ID=oas-mcp                                    ║\n"
        "║    KEYCLOAK_CLIENT_SECRET=<secret>                               ║\n"
        "║                                                                  ║\n"
        "║  See oas_mcp/keycloak_auth_setup.md for the full setup guide.    ║\n"
        "╚══════════════════════════════════════════════════════════════════╝\n",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
