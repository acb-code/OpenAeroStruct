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
    "thickness_intersects": "{point}.{name}_perf.thickness_intersects",
    "L_equals_W": "{point}.L_equals_W",
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
            raise ValueError(f"Unknown design variable {dv_name!r}. Options: {list(DV_NAME_MAP)}")
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
    prob.model.add_objective(obj_path)

    prob.setup(force_alloc_complex=False)

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
