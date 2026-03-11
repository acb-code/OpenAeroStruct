"""
Build OpenMDAO Problem instances for aero and aerostruct analyses.

The _assemble_* helpers add subsystems and connections but do NOT call setup().
The build_* helpers call the assembler then setup().
build_optimization_problem uses the assembler, adds DVs/constraints/objective, then setup().
"""

from __future__ import annotations

import numpy as np
import openmdao.api as om
from openaerostruct.geometry.geometry_group import Geometry
from openaerostruct.aerodynamics.aero_groups import AeroPoint
from openaerostruct.integration.aerostruct_groups import AerostructGeometry, AerostructPoint
from openaerostruct.utils.constants import grav_constant

from .connections import connect_aero_surface, connect_aerostruct_surface
from .validators import validate_design_variables_for_surfaces


# ---------------------------------------------------------------------------
# Internal assemblers (no setup() call)
# ---------------------------------------------------------------------------


def _assemble_aero_model(
    prob: om.Problem,
    surfaces: list[dict],
    velocity: float,
    alpha: float,
    Mach_number: float,
    reynolds_number: float,
    density: float,
    cg: list | None,
) -> str:
    """Add IndepVarComp, Geometry groups, AeroPoint, and connections to prob.model.
    Returns the point_name."""
    if cg is None:
        cg = [0.0, 0.0, 0.0]

    indep = om.IndepVarComp()
    indep.add_output("v", val=velocity, units="m/s")
    indep.add_output("alpha", val=alpha, units="deg")
    indep.add_output("Mach_number", val=Mach_number)
    indep.add_output("re", val=reynolds_number, units="1/m")
    indep.add_output("rho", val=density, units="kg/m**3")
    indep.add_output("cg", val=np.array(cg), units="m")
    prob.model.add_subsystem("prob_vars", indep, promotes=["*"])

    point_name = "aero"

    for surface in surfaces:
        name = surface["name"]
        geom_group = Geometry(surface=surface)
        prob.model.add_subsystem(name, geom_group)

    aero_group = AeroPoint(surfaces=surfaces)
    prob.model.add_subsystem(
        point_name,
        aero_group,
        promotes_inputs=["v", "alpha", "Mach_number", "re", "rho", "cg"],
    )

    for surface in surfaces:
        connect_aero_surface(prob.model, surface["name"], point_name)

    return point_name


def _assemble_aerostruct_model(
    prob: om.Problem,
    surfaces: list[dict],
    velocity: float,
    alpha: float,
    Mach_number: float,
    reynolds_number: float,
    density: float,
    CT: float,
    R: float,
    W0: float,
    speed_of_sound: float,
    load_factor: float,
    empty_cg: list | None,
) -> str:
    """Add IndepVarComp, AerostructGeometry, AerostructPoint, connections.
    Returns the point_name."""
    if empty_cg is None:
        empty_cg = [0.0, 0.0, 0.0]

    indep = om.IndepVarComp()
    indep.add_output("v", val=velocity, units="m/s")
    indep.add_output("alpha", val=alpha, units="deg")
    indep.add_output("Mach_number", val=Mach_number)
    indep.add_output("re", val=reynolds_number, units="1/m")
    indep.add_output("rho", val=density, units="kg/m**3")
    indep.add_output("CT", val=CT, units="1/s")
    indep.add_output("R", val=R, units="m")
    indep.add_output("W0", val=W0, units="kg")
    indep.add_output("speed_of_sound", val=speed_of_sound, units="m/s")
    indep.add_output("load_factor", val=load_factor)
    indep.add_output("empty_cg", val=np.array(empty_cg), units="m")
    prob.model.add_subsystem("prob_vars", indep, promotes=["*"])

    point_name = "AS_point_0"

    for surface in surfaces:
        name = surface["name"]
        as_geom = AerostructGeometry(surface=surface)
        prob.model.add_subsystem(name, as_geom)

    AS_point = AerostructPoint(surfaces=surfaces)
    prob.model.add_subsystem(
        point_name,
        AS_point,
        promotes_inputs=[
            "v", "alpha", "Mach_number", "re", "rho",
            "CT", "R", "W0", "speed_of_sound", "empty_cg", "load_factor",
        ],
    )

    for surface in surfaces:
        connect_aerostruct_surface(
            prob.model,
            surface["name"],
            point_name,
            fem_model_type=surface.get("fem_model_type", "tube"),
        )

    return point_name


def _set_initial_values_aero(prob, velocity, alpha, Mach_number, reynolds_number, density, cg):
    prob.set_val("v", velocity, units="m/s")
    prob.set_val("alpha", alpha, units="deg")
    prob.set_val("Mach_number", Mach_number)
    prob.set_val("re", reynolds_number, units="1/m")
    prob.set_val("rho", density, units="kg/m**3")
    prob.set_val("cg", np.array(cg if cg else [0.0, 0.0, 0.0]), units="m")


def _set_initial_values_aerostruct(
    prob, velocity, alpha, Mach_number, reynolds_number, density,
    CT, R, W0, speed_of_sound, load_factor, empty_cg,
):
    prob.set_val("v", velocity, units="m/s")
    prob.set_val("alpha", alpha, units="deg")
    prob.set_val("Mach_number", Mach_number)
    prob.set_val("re", reynolds_number, units="1/m")
    prob.set_val("rho", density, units="kg/m**3")
    prob.set_val("CT", CT, units="1/s")
    prob.set_val("R", R, units="m")
    prob.set_val("W0", W0, units="kg")
    prob.set_val("speed_of_sound", speed_of_sound, units="m/s")
    prob.set_val("load_factor", load_factor)
    prob.set_val("empty_cg", np.array(empty_cg if empty_cg else [0.0, 0.0, 0.0]), units="m")


# ---------------------------------------------------------------------------
# Public builders
# ---------------------------------------------------------------------------


def build_aero_problem(
    surfaces: list[dict],
    velocity: float = 248.136,
    alpha: float = 5.0,
    Mach_number: float = 0.84,
    reynolds_number: float = 1.0e6,
    density: float = 0.38,
    cg: list | None = None,
) -> om.Problem:
    """Build and set up an aerodynamics-only OpenMDAO problem."""
    prob = om.Problem(reports=False)
    _assemble_aero_model(prob, surfaces, velocity, alpha, Mach_number, reynolds_number, density, cg)
    prob.setup(force_alloc_complex=False)
    _set_initial_values_aero(prob, velocity, alpha, Mach_number, reynolds_number, density, cg)
    return prob


def build_aerostruct_problem(
    surfaces: list[dict],
    velocity: float = 248.136,
    alpha: float = 5.0,
    Mach_number: float = 0.84,
    reynolds_number: float = 1.0e6,
    density: float = 0.38,
    CT: float | None = None,
    R: float = 11.165e6,
    W0: float = 0.4 * 3e5,
    speed_of_sound: float = 295.4,
    load_factor: float = 1.0,
    empty_cg: list | None = None,
) -> om.Problem:
    """Build and set up a coupled aerostructural OpenMDAO problem."""
    if CT is None:
        CT = grav_constant * 17.0e-6
    if empty_cg is None:
        empty_cg = [0.0, 0.0, 0.0]

    prob = om.Problem(reports=False)
    _assemble_aerostruct_model(
        prob, surfaces, velocity, alpha, Mach_number, reynolds_number,
        density, CT, R, W0, speed_of_sound, load_factor, empty_cg,
    )
    prob.setup(force_alloc_complex=False)
    _set_initial_values_aerostruct(
        prob, velocity, alpha, Mach_number, reynolds_number, density,
        CT, R, W0, speed_of_sound, load_factor, empty_cg,
    )
    return prob


# ---------------------------------------------------------------------------
# DV / constraint / objective name mappings
# ---------------------------------------------------------------------------

DV_NAME_MAP = {
    "twist": "{name}.twist_cp",
    "thickness": "{name}.thickness_cp",
    "chord": "{name}.chord_cp",
    "sweep": "{name}.sweep",
    "taper": "{name}.taper",
    "alpha": "alpha",
    "spar_thickness": "{name}.spar_thickness_cp",
    "skin_thickness": "{name}.skin_thickness_cp",
    "t_over_c": "{name}.geometry.t_over_c_cp",
    "alpha_maneuver": "alpha_maneuver",
    "fuel_mass": "fuel_mass",
}

CONSTRAINT_NAME_MAP_AERO = {
    "CL": "{point}.{name}_perf.CL",
    "CD": "{point}.{name}_perf.CD",
    "CM": "{point}.CM",
}

CONSTRAINT_NAME_MAP_AEROSTRUCT = {
    "CL": "{point}.{name}_perf.CL",
    "CD": "{point}.{name}_perf.CD",
    "CM": "{point}.CM",
    "failure": "{point}.{name}_perf.failure",
    # thickness_intersects is tube-only — raises ValueError for wingbox surfaces
    "thickness_intersects": "{point}.{name}_perf.thickness_intersects",
    "L_equals_W": "{point}.L_equals_W",
    # Multipoint-only top-level constraints (no point/name substitution)
    "fuel_vol_delta": "fuel_vol_delta.fuel_vol_delta",
    "fuel_diff": "fuel_diff",
}

OBJECTIVE_MAP_AERO = {
    "CD": "{point}.CD",
    "CL": "{point}.CL",
}

OBJECTIVE_MAP_AEROSTRUCT = {
    "fuelburn": "{point}.fuelburn",
    "structural_mass": "{name}.structural_mass",
    "CD": "{point}.{name}_perf.CD",
}


def resolve_path(template: str, name: str, point: str) -> str:
    return template.format(name=name, point=point)


# ---------------------------------------------------------------------------
# Optimisation problem builder
# ---------------------------------------------------------------------------


def build_optimization_problem(
    surfaces: list[dict],
    analysis_type: str,
    objective: str,
    design_variables: list[dict],
    constraints: list[dict],
    flight_conditions: dict,
    objective_scaler: float = 1.0,
    tolerance: float = 1e-6,
    max_iterations: int = 200,
) -> tuple[om.Problem, str]:
    """
    Build and set up an optimisation problem.

    Returns (prob, point_name).  The caller should call prob.run_driver().
    """
    prob = om.Problem(reports=False)

    # Configure driver before setup
    prob.driver = om.ScipyOptimizeDriver()
    prob.driver.options["tol"] = tolerance
    prob.driver.options["maxiter"] = max_iterations

    if analysis_type == "aero":
        point_name = _assemble_aero_model(
            prob, surfaces,
            velocity=flight_conditions.get("velocity", 248.136),
            alpha=flight_conditions.get("alpha", 5.0),
            Mach_number=flight_conditions.get("Mach_number", 0.84),
            reynolds_number=flight_conditions.get("reynolds_number", 1.0e6),
            density=flight_conditions.get("density", 0.38),
            cg=flight_conditions.get("cg"),
        )
        obj_map = OBJECTIVE_MAP_AERO
        con_map = CONSTRAINT_NAME_MAP_AERO
    else:
        CT = flight_conditions.get("CT", grav_constant * 17.0e-6)
        point_name = _assemble_aerostruct_model(
            prob, surfaces,
            velocity=flight_conditions.get("velocity", 248.136),
            alpha=flight_conditions.get("alpha", 5.0),
            Mach_number=flight_conditions.get("Mach_number", 0.84),
            reynolds_number=flight_conditions.get("reynolds_number", 1.0e6),
            density=flight_conditions.get("density", 0.38),
            CT=CT,
            R=flight_conditions.get("R", 11.165e6),
            W0=flight_conditions.get("W0", 0.4 * 3e5),
            speed_of_sound=flight_conditions.get("speed_of_sound", 295.4),
            load_factor=flight_conditions.get("load_factor", 1.0),
            empty_cg=flight_conditions.get("empty_cg"),
        )
        obj_map = OBJECTIVE_MAP_AEROSTRUCT
        con_map = CONSTRAINT_NAME_MAP_AEROSTRUCT

    validate_design_variables_for_surfaces(design_variables, surfaces)

    primary_name = surfaces[0]["name"] if surfaces else "wing"

    # Add design variables
    for dv in design_variables:
        dv_name = dv["name"]
        template = DV_NAME_MAP.get(dv_name)
        if template is None:
            # Accept _cp-suffixed names as aliases (e.g. twist_cp → twist)
            if dv_name.endswith("_cp"):
                template = DV_NAME_MAP.get(dv_name[:-3])
            if template is None:
                raise ValueError(
                    f"Unknown design variable {dv_name!r}. Options: {list(DV_NAME_MAP)}"
                )
        path = resolve_path(template, primary_name, point_name)
        kwargs = {}
        if "lower" in dv:
            kwargs["lower"] = dv["lower"]
        if "upper" in dv:
            kwargs["upper"] = dv["upper"]
        if "scaler" in dv:
            kwargs["scaler"] = dv["scaler"]
        prob.model.add_design_var(path, **kwargs)

    # Add constraints
    for con in constraints:
        con_name = con["name"]
        template = con_map.get(con_name)
        if template is None:
            raise ValueError(f"Unknown constraint {con_name!r}. Options: {list(con_map)}")
        if con_name == "thickness_intersects":
            wingbox_surfs = [s["name"] for s in surfaces if s.get("fem_model_type") == "wingbox"]
            if wingbox_surfs:
                raise ValueError(
                    f"Constraint 'thickness_intersects' is only available for tube "
                    f"fem_model_type surfaces. Surface(s) {wingbox_surfs} use 'wingbox'. "
                    f"Remove 'thickness_intersects' from constraints for wingbox optimizations."
                )
        path = resolve_path(template, primary_name, point_name)
        kwargs = {}
        if "equals" in con:
            kwargs["equals"] = con["equals"]
        if "lower" in con:
            kwargs["lower"] = con["lower"]
        if "upper" in con:
            kwargs["upper"] = con["upper"]
        prob.model.add_constraint(path, **kwargs)

    # Add objective
    obj_template = obj_map.get(objective)
    if obj_template is None:
        raise ValueError(f"Unknown objective {objective!r}. Options: {list(obj_map)}")
    obj_path = resolve_path(obj_template, primary_name, point_name)
    obj_kwargs = {"scaler": objective_scaler} if objective_scaler != 1.0 else {}
    prob.model.add_objective(obj_path, **obj_kwargs)

    prob.setup(force_alloc_complex=False)

    # Explicitly set surface-level array initial values so that any early
    # prob.get_val() calls (e.g. in OptimizationTracker.record_initial) do not
    # trigger OpenMDAO finalization with default values (1.0) instead of the
    # values from the surface dict, which would shift the optimizer's starting
    # point and lead to a different local optimum.
    _cp_keys = ("twist_cp", "thickness_cp", "t_over_c_cp",
                 "spar_thickness_cp", "skin_thickness_cp")
    for surface in surfaces:
        sname = surface["name"]
        for key in _cp_keys:
            if key in surface:
                try:
                    prob.set_val(f"{sname}.{key}", surface[key])
                except Exception:
                    pass

    # Set initial values
    if analysis_type == "aero":
        _set_initial_values_aero(
            prob,
            flight_conditions.get("velocity", 248.136),
            flight_conditions.get("alpha", 5.0),
            flight_conditions.get("Mach_number", 0.84),
            flight_conditions.get("reynolds_number", 1.0e6),
            flight_conditions.get("density", 0.38),
            flight_conditions.get("cg"),
        )
    else:
        _set_initial_values_aerostruct(
            prob,
            flight_conditions.get("velocity", 248.136),
            flight_conditions.get("alpha", 5.0),
            flight_conditions.get("Mach_number", 0.84),
            flight_conditions.get("reynolds_number", 1.0e6),
            flight_conditions.get("density", 0.38),
            flight_conditions.get("CT", grav_constant * 17.0e-6),
            flight_conditions.get("R", 11.165e6),
            flight_conditions.get("W0", 0.4 * 3e5),
            flight_conditions.get("speed_of_sound", 295.4),
            flight_conditions.get("load_factor", 1.0),
            flight_conditions.get("empty_cg"),
        )

    return prob, point_name


# ---------------------------------------------------------------------------
# Multipoint aerostructural assembler and optimisation problem builder
# ---------------------------------------------------------------------------


def _assemble_multipoint_aerostruct_model(
    prob: om.Problem,
    surfaces: list[dict],
    flight_points: list[dict],
    CT: float,
    R: float,
    W0_without_point_masses: float,
    alpha: float = 0.0,
    alpha_maneuver: float = 0.0,
    empty_cg: list | None = None,
    fuel_mass: float = 10000.0,
    point_masses: list | None = None,
    point_mass_locations: list | None = None,
) -> list[str]:
    """Assemble a multipoint aerostructural model following the OAS tutorial.

    Returns list of point names ["AS_point_0", "AS_point_1", ...].
    """
    from openaerostruct.structures.wingbox_fuel_vol_delta import WingboxFuelVolDelta

    if empty_cg is None:
        empty_cg = [0.0, 0.0, 0.0]

    N = len(flight_points)
    v_arr = np.array([fp["velocity"] for fp in flight_points])
    mach_arr = np.array([fp["Mach_number"] for fp in flight_points])
    re_arr = np.array([fp["reynolds_number"] for fp in flight_points])
    rho_arr = np.array([fp["density"] for fp in flight_points])
    sos_arr = np.array([fp["speed_of_sound"] for fp in flight_points])
    lf_arr = np.array([fp.get("load_factor", 1.0) for fp in flight_points])

    has_point_masses = point_masses is not None and len(point_masses) > 0
    pm_arr = np.array(point_masses) if has_point_masses else np.zeros((1, 1))
    pml_arr = np.array(point_mass_locations) if has_point_masses else np.zeros((1, 3))

    indep = om.IndepVarComp()
    indep.add_output("v", val=v_arr, units="m/s")
    indep.add_output("Mach_number", val=mach_arr)
    indep.add_output("re", val=re_arr, units="1/m")
    indep.add_output("rho", val=rho_arr, units="kg/m**3")
    indep.add_output("speed_of_sound", val=sos_arr, units="m/s")
    indep.add_output("load_factor", val=lf_arr)
    indep.add_output("CT", val=CT, units="1/s")
    indep.add_output("R", val=R, units="m")
    indep.add_output("W0_without_point_masses", val=W0_without_point_masses, units="kg")
    indep.add_output("alpha", val=alpha, units="deg")
    indep.add_output("alpha_maneuver", val=alpha_maneuver, units="deg")
    indep.add_output("empty_cg", val=np.array(empty_cg), units="m")
    indep.add_output("fuel_mass", val=fuel_mass, units="kg")
    indep.add_output("point_masses", val=pm_arr, units="kg")
    indep.add_output("point_mass_locations", val=pml_arr, units="m")
    prob.model.add_subsystem("prob_vars", indep, promotes=["*"])

    prob.model.add_subsystem(
        "W0_comp",
        om.ExecComp("W0 = W0_without_point_masses + 2 * sum(point_masses)", units="kg"),
        promotes=["*"],
    )

    for surface in surfaces:
        prob.model.add_subsystem(surface["name"], AerostructGeometry(surface=surface))

    point_names = []
    for i in range(N):
        pt = f"AS_point_{i}"
        point_names.append(pt)

        AS_point = AerostructPoint(surfaces=surfaces, internally_connect_fuelburn=False)
        prob.model.add_subsystem(pt, AS_point)

        prob.model.connect("v", pt + ".v", src_indices=[i])
        prob.model.connect("Mach_number", pt + ".Mach_number", src_indices=[i])
        prob.model.connect("re", pt + ".re", src_indices=[i])
        prob.model.connect("rho", pt + ".rho", src_indices=[i])
        prob.model.connect("speed_of_sound", pt + ".speed_of_sound", src_indices=[i])
        prob.model.connect("load_factor", pt + ".load_factor", src_indices=[i])
        prob.model.connect("CT", pt + ".CT")
        prob.model.connect("R", pt + ".R")
        prob.model.connect("W0", pt + ".W0")
        prob.model.connect("empty_cg", pt + ".empty_cg")
        prob.model.connect("fuel_mass", pt + ".total_perf.L_equals_W.fuelburn")
        prob.model.connect("fuel_mass", pt + ".total_perf.CG.fuelburn")

        for surface in surfaces:
            name = surface["name"]
            fem_type = surface.get("fem_model_type", "tube")
            struct_weight_relief = surface.get("struct_weight_relief", False)
            distributed_fuel_weight = surface.get("distributed_fuel_weight", False)

            if distributed_fuel_weight:
                prob.model.connect("load_factor", pt + ".coupled.load_factor", src_indices=[i])

            connect_aerostruct_surface(prob.model, name, pt, fem_model_type=fem_type)

            if struct_weight_relief:
                prob.model.connect(name + ".element_mass", pt + ".coupled." + name + ".element_mass")

            if has_point_masses:
                coupled_name = pt + ".coupled." + name
                prob.model.connect("point_masses", coupled_name + ".point_masses")
                prob.model.connect("point_mass_locations", coupled_name + ".point_mass_locations")

            if distributed_fuel_weight:
                prob.model.connect(
                    name + ".struct_setup.fuel_vols",
                    pt + ".coupled." + name + ".struct_states.fuel_vols",
                )
                prob.model.connect("fuel_mass", pt + ".coupled." + name + ".struct_states.fuel_mass")

    prob.model.connect("alpha", "AS_point_0.alpha")
    if N > 1:
        prob.model.connect("alpha_maneuver", "AS_point_1.alpha")

    # Fuel volume constraint and diff components (wingbox only)
    wingbox_surfaces = [s for s in surfaces if s.get("fem_model_type") == "wingbox"]
    if wingbox_surfaces:
        wb_surf = wingbox_surfaces[0]
        wb_name = wb_surf["name"]
        prob.model.add_subsystem("fuel_vol_delta", WingboxFuelVolDelta(surface=wb_surf))
        prob.model.connect(wb_name + ".struct_setup.fuel_vols", "fuel_vol_delta.fuel_vols")
        prob.model.connect("AS_point_0.fuelburn", "fuel_vol_delta.fuelburn")

        comp = om.ExecComp("fuel_diff = (fuel_mass - fuelburn) / fuelburn", units="kg")
        prob.model.add_subsystem("fuel_diff", comp, promotes_inputs=["fuel_mass"], promotes_outputs=["fuel_diff"])
        prob.model.connect("AS_point_0.fuelburn", "fuel_diff.fuelburn")

    return point_names


# Top-level constraint templates that need no point/name substitution
_MP_TOPLEVEL_CONSTRAINTS = {"fuel_vol_delta", "fuel_diff"}
# DV names that are scalar top-level variables (not surface-path-formatted)
_MP_SCALAR_DVS = {"alpha_maneuver", "fuel_mass"}


def build_multipoint_optimization_problem(
    surfaces: list[dict],
    objective: str,
    design_variables: list[dict],
    constraints: list[dict],
    flight_points: list[dict],
    CT: float,
    R: float,
    W0_without_point_masses: float,
    alpha: float = 0.0,
    alpha_maneuver: float = 0.0,
    empty_cg: list | None = None,
    fuel_mass: float = 10000.0,
    point_masses: list | None = None,
    point_mass_locations: list | None = None,
    tolerance: float = 1e-2,
    max_iterations: int = 200,
) -> tuple[om.Problem, list[str]]:
    """Build and set up a multipoint aerostructural optimisation problem.

    Returns (prob, point_names).  The caller should call prob.run_driver().
    """
    prob = om.Problem(reports=False)
    prob.driver = om.ScipyOptimizeDriver()
    prob.driver.options["optimizer"] = "SLSQP"
    prob.driver.options["tol"] = tolerance
    prob.driver.options["maxiter"] = max_iterations

    point_names = _assemble_multipoint_aerostruct_model(
        prob, surfaces, flight_points, CT, R, W0_without_point_masses,
        alpha, alpha_maneuver, empty_cg, fuel_mass, point_masses, point_mass_locations,
    )

    validate_design_variables_for_surfaces(design_variables, surfaces)

    primary_name = surfaces[0]["name"] if surfaces else "wing"

    for dv in design_variables:
        dv_name = dv.get("name", "")
        template = DV_NAME_MAP.get(dv_name)
        if template is None and dv_name.endswith("_cp"):
            template = DV_NAME_MAP.get(dv_name[:-3])
        if template is None:
            raise ValueError(f"Unknown design variable {dv_name!r}. Options: {list(DV_NAME_MAP)}")
        # Scalar DVs have literal paths; surface DVs need name/point substitution
        path = template if dv_name in _MP_SCALAR_DVS else resolve_path(template, primary_name, point_names[0])
        kwargs = {}
        if "lower" in dv:
            kwargs["lower"] = dv["lower"]
        if "upper" in dv:
            kwargs["upper"] = dv["upper"]
        if "scaler" in dv:
            kwargs["scaler"] = dv["scaler"]
        prob.model.add_design_var(path, **kwargs)

    for con in constraints:
        con_name = con.get("name", "")
        template = CONSTRAINT_NAME_MAP_AEROSTRUCT.get(con_name)
        if template is None:
            raise ValueError(f"Unknown constraint {con_name!r}. Options: {list(CONSTRAINT_NAME_MAP_AEROSTRUCT)}")
        if con_name in _MP_TOPLEVEL_CONSTRAINTS:
            path = template  # literal path, no substitution
        else:
            pt_idx = con.get("point", 0)
            pt_name = point_names[pt_idx] if pt_idx < len(point_names) else point_names[0]
            path = resolve_path(template, primary_name, pt_name)
        kwargs = {}
        if "equals" in con:
            kwargs["equals"] = con["equals"]
        if "lower" in con:
            kwargs["lower"] = con["lower"]
        if "upper" in con:
            kwargs["upper"] = con["upper"]
        prob.model.add_constraint(path, **kwargs)

    obj_template = OBJECTIVE_MAP_AEROSTRUCT.get(objective)
    if obj_template is None:
        raise ValueError(f"Unknown objective {objective!r}. Options: {list(OBJECTIVE_MAP_AEROSTRUCT)}")
    obj_path = resolve_path(obj_template, primary_name, point_names[0])
    prob.model.add_objective(obj_path)

    prob.setup(force_alloc_complex=False)

    # Set Aitken-accelerated linear solver on each coupled group
    for pt in point_names:
        pt_group = getattr(prob.model, pt, None)
        if pt_group is not None:
            coupled = getattr(pt_group, "coupled", None)
            if coupled is not None:
                coupled.linear_solver = om.LinearBlockGS(iprint=0, maxiter=30, use_aitken=True)

    return prob, point_names
