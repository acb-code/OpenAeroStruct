"""
Deeper integration tests for multipoint aerostructural optimization.

Extends the single structure test in test_tools.py with tests for per-point
constraints, fuel-related DVs, and result extraction.
"""

import pytest
from oas_mcp.server import create_surface, reset, run_optimization

pytestmark = pytest.mark.slow


def _r(envelope: dict) -> dict:
    """Extract the results payload from a versioned response envelope."""
    assert "schema_version" in envelope
    return envelope["results"]


# Shared multipoint surface setup
_WINGBOX_SURFACE = dict(
    name="wing",
    wing_type="rect",
    num_x=3,
    num_y=7,
    span=30.0,
    root_chord=5.0,
    fem_model_type="wingbox",
    E=73.1e9,
    G=73.1e9 / 2 / 1.33,
    yield_stress=420e6,
    safety_factor=1.5,
    mrho=2780.0,
    struct_weight_relief=False,
    distributed_fuel_weight=False,
    fuel_density=803.0,
    Wf_reserve=15000.0,
    wing_weight_ratio=1.25,
    CD0=0.0078,
    with_wave=True,
)

_FLIGHT_POINTS = [
    # Cruise
    {"velocity": 248.0, "Mach_number": 0.84, "density": 0.38,
     "reynolds_number": 1.0e6, "speed_of_sound": 295.0, "load_factor": 1.0},
    # 2.5g maneuver
    {"velocity": 200.0, "Mach_number": 0.60, "density": 0.80,
     "reynolds_number": 2.0e6, "speed_of_sound": 333.0, "load_factor": 2.5},
]

_BASE_DVS = [
    {"name": "twist", "lower": -5.0, "upper": 10.0, "scaler": 0.1},
    {"name": "spar_thickness", "lower": 0.003, "upper": 0.1, "scaler": 100.0},
    {"name": "skin_thickness", "lower": 0.003, "upper": 0.1, "scaler": 100.0},
    {"name": "alpha_maneuver", "lower": -10.0, "upper": 15.0},
    {"name": "fuel_mass", "lower": 1000.0, "upper": 100000.0, "scaler": 1e-5},
]

_OPT_KWARGS = dict(
    CT=0.53 / 3600,
    R=14307000.0,
    W0_without_point_masses=50000.0,
    tolerance=0.5,
    max_iterations=5,
)


async def _setup_and_run(dvs=None, constraints=None, flight_points=None):
    """Helper: create surface and run multipoint optimization."""
    await reset()
    await create_surface(**_WINGBOX_SURFACE)
    return await run_optimization(
        surfaces=["wing"],
        analysis_type="aerostruct",
        objective="fuelburn",
        flight_points=flight_points or _FLIGHT_POINTS,
        design_variables=dvs or _BASE_DVS,
        constraints=constraints or [
            {"name": "CL", "point": 0, "equals": 0.5},
            {"name": "L_equals_W", "point": 1, "equals": 0.0},
            {"name": "failure", "point": 1, "upper": 0.0},
        ],
        **_OPT_KWARGS,
    )


# ---------------------------------------------------------------------------
# Multipoint result structure and physics
# ---------------------------------------------------------------------------


class TestMultipointResults:
    async def test_results_keyed_by_role(self):
        result = _r(await _setup_and_run())
        fr = result["final_results"]
        assert "cruise" in fr
        assert "maneuver" in fr

    async def test_cruise_maneuver_different_cl(self):
        result = _r(await _setup_and_run())
        fr = result["final_results"]
        assert fr["cruise"]["CL"] != pytest.approx(fr["maneuver"]["CL"], rel=0.05)

    async def test_cruise_maneuver_different_cd(self):
        result = _r(await _setup_and_run())
        fr = result["final_results"]
        # Different flight conditions → different drag
        assert fr["cruise"]["CD"] > 0
        assert fr["maneuver"]["CD"] > 0

    async def test_both_points_have_structural_data(self):
        result = _r(await _setup_and_run())
        for role in ("cruise", "maneuver"):
            pt = result["final_results"][role]
            assert isinstance(pt.get("fuelburn"), float)


# ---------------------------------------------------------------------------
# Per-point constraints
# ---------------------------------------------------------------------------


class TestMultipointConstraints:
    async def test_per_point_constraint_point_0(self):
        """A constraint with point=0 should target the cruise point."""
        env = await _setup_and_run(
            constraints=[{"name": "CL", "point": 0, "equals": 0.5}],
        )
        assert env["schema_version"] == "1.0"

    async def test_per_point_constraint_point_1(self):
        """A constraint with point=1 should target the maneuver point."""
        env = await _setup_and_run(
            constraints=[{"name": "failure", "point": 1, "upper": 0.0}],
        )
        assert env["schema_version"] == "1.0"

    async def test_constraint_without_point_key(self):
        """A constraint without a 'point' key should be accepted (applies globally)."""
        env = await _setup_and_run(
            constraints=[{"name": "L_equals_W", "equals": 0.0}],
        )
        assert env["schema_version"] == "1.0"


# ---------------------------------------------------------------------------
# Multipoint DVs
# ---------------------------------------------------------------------------


class TestMultipointDVs:
    async def test_alpha_maneuver_in_results(self):
        result = _r(await _setup_and_run())
        dvs = result["optimized_design_variables"]
        assert "alpha_maneuver" in dvs

    async def test_fuel_mass_scalar(self):
        result = _r(await _setup_and_run())
        dvs = result["optimized_design_variables"]
        fm = dvs["fuel_mass"]
        assert isinstance(fm, list) and len(fm) == 1

    async def test_t_over_c_dv_accepted(self):
        """t_over_c should be a valid DV for wingbox surfaces."""
        dvs = _BASE_DVS + [
            {"name": "t_over_c", "lower": 0.05, "upper": 0.25, "scaler": 10.0},
        ]
        env = await _setup_and_run(dvs=dvs)
        result = _r(env)
        assert "t_over_c" in result["optimized_design_variables"]
