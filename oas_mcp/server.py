"""
OAS MCP Server — FastMCP entry point.

All @mcp.tool() registrations live here.  Heavy OpenMDAO work is dispatched
to a thread pool via asyncio.to_thread() so the event loop stays responsive.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import warnings
from typing import Annotated, Any

import numpy as np
from mcp.server.fastmcp import FastMCP

from .core.builders import (
    build_aero_problem,
    build_aerostruct_problem,
    build_optimization_problem,
)
from .core.defaults import DEFAULT_AEROSTRUCT_CONDITIONS, DEFAULT_AERO_CONDITIONS
from .core.mesh import apply_dihedral, apply_sweep, apply_taper, build_mesh
from .core.results import (
    extract_aero_results,
    extract_aerostruct_results,
    extract_stability_results,
)
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
    instructions=(
        "Aerostructural analysis and optimization of aircraft wings using OpenAeroStruct. "
        "Start by calling create_surface to define one or more lifting surfaces, then call "
        "run_aero_analysis or run_aerostruct_analysis to compute performance metrics."
    ),
)

_sessions = SessionManager()


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
    thickness_cp: Annotated[list[float] | None, "Tube wall thickness control points in metres"] = None,
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

            if thickness_cp is not None:
                surface["thickness_cp"] = np.array(thickness_cp, dtype=float)
            else:
                # Default: 3 control points, 10% chord thickness
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
    return results


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
    return results


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

    return {
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
    return results


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

    Design variable names: 'twist', 'thickness', 'chord', 'sweep', 'taper', 'alpha'
    Constraint names (aero): 'CL', 'CD', 'CM'
    Constraint names (aerostruct): all aero + 'failure', 'thickness_intersects', 'L_equals_W'
    Objective names (aero): 'CD', 'CL'
    Objective names (aerostruct): 'fuelburn', 'structural_mass', 'CD'
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
    return result


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
# Entry point
# ---------------------------------------------------------------------------


def main():
    """Console-script entry point for oas-mcp."""
    mcp.run()


if __name__ == "__main__":
    main()
