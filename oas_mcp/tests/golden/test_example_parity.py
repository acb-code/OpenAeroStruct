"""
Tier 3 — OAS example workflow parity tests.

Verify that the MCP server produces results matching direct OAS example
workflow executions.  Each test class exercises an MCP tool with zero
coverage in the existing integration-test parity suite:

  TestStabilityDerivsParity   — compute_stability_derivatives
  TestCRMAeroOptParity        — run_optimization(analysis_type="aero")
  TestCRMAerostructOptParity  — run_optimization(analysis_type="aerostruct")

Run with pytest:
    pytest oas_mcp/tests/golden/test_example_parity.py -v

Run directly to generate a side-by-side workflow report and HTML file:
    python oas_mcp/tests/golden/test_example_parity.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

# Make sibling modules (workflow, generate_example_parity) importable
_GOLDEN_DIR = str(Path(__file__).parent)
if _GOLDEN_DIR not in sys.path:
    sys.path.insert(0, _GOLDEN_DIR)

pytestmark = [pytest.mark.slow, pytest.mark.example_parity]

_PARITY_PATH = Path(__file__).parent / "example_parity_values.json"

# OAS code summaries shown in the workflow report
_OAS_CODE_STABILITY = """\
mesh_dict = {"num_y": 15, "num_x": 3, "wing_type": "rect", "span_cos_spacing": 0.0}
mesh = generate_mesh(mesh_dict)
prob.set_val("wing_geom.sweep", 10.0, units="deg")
prob.run_model()
CL_alpha = (CL_FD - CL) / delta_alpha  # delta_alpha = 1e-4 deg
static_margin = -CM_alpha / CL_alpha"""

_OAS_CODE_CRM_AERO_OPT = """\
# aero_walkthrough parts 1-6 (CRM mesh, num_y=7)
prob.driver.options["tol"] = 1e-9
prob.model.add_design_var("wing.twist_cp", lower=-10.0, upper=15.0)
prob.model.add_constraint("aero_point_0.wing_perf.CL", equals=0.5)
prob.model.add_objective("aero_point_0.wing_perf.CD", scaler=1e4)
prob.setup(); prob.run_driver()
# Reference (part_7.py): CD=0.03339, CL=0.5, CM=-1.7886
# MCP equivalent: objective_scaler=1e4, tolerance=1e-9"""

_OAS_CODE_CRM_AEROSTRUCT_OPT = """\
# run_CRM.py (CRM mesh, num_y=5, tube FEM)
prob.model.add_design_var("wing.twist_cp", lower=-10, upper=15)
prob.model.add_design_var("wing.thickness_cp", lower=0.01, upper=0.5, scaler=1e2)
prob.model.add_design_var("alpha", lower=-10, upper=10)
prob.model.add_constraint("AS_point_0.wing_perf.failure", upper=0)
prob.model.add_constraint("AS_point_0.wing_perf.thickness_intersects", upper=0)
prob.model.add_constraint("AS_point_0.L_equals_W", equals=0)
prob.model.add_objective("AS_point_0.fuelburn", scaler=1e-5)
prob.setup(); prob.run_driver()"""


def _r(envelope: dict) -> dict:
    return envelope["results"]


@pytest.fixture(scope="module")
def parity() -> dict:
    with _PARITY_PATH.open() as f:
        return json.load(f)


# Module-level result cache — avoids re-running expensive optimizations per method.
_CACHE: dict = {}


async def _cached(key: str, runner) -> dict:
    """Return cached result or run runner() and cache."""
    if key not in _CACHE:
        result, _ = await runner()
        _CACHE[key] = result
    return _CACHE[key]


# ---------------------------------------------------------------------------
# MCP runner coroutines — return (results_dict, WorkflowRecorder)
# Used by both pytest (ignoring recorder) and __main__ (using recorder for report)
# ---------------------------------------------------------------------------


async def _mcp_stability_derivs() -> tuple[dict, object]:
    """MCP: rect wing stability derivatives (stability_derivatives.py)."""
    from oas_mcp.server import compute_stability_derivatives, create_surface, reset
    from workflow import WorkflowRecorder

    rec = WorkflowRecorder(
        "Stability Derivatives",
        "openaerostruct/examples/stability_derivatives.py",
    )
    await rec.call(reset)
    await rec.call(
        create_surface,
        name="wing", wing_type="rect",
        num_x=3, num_y=15, symmetry=True,
        S_ref_type="wetted", sweep=10.0,
        t_over_c_cp=[0.15], c_max_t=0.303,
        CD0=0.015, with_viscous=True, with_wave=False,
        num_twist_cp=2, twist_cp=[0.0, 0.0],
    )
    envelope = await rec.call(
        compute_stability_derivatives,
        surfaces=["wing"],
        alpha=5.0, velocity=248.136,
        Mach_number=0.84, reynolds_number=1e6, density=0.38,
        cg=[0.5, 0.0, 0.0],
    )
    return _r(envelope), rec


async def _mcp_crm_aero_opt() -> tuple[dict, object]:
    """MCP: CRM aero twist optimization (aero_walkthrough parts 1-7)."""
    from oas_mcp.server import create_surface, reset, run_optimization
    from workflow import WorkflowRecorder

    rec = WorkflowRecorder(
        "CRM Aero Twist Optimization",
        "openaerostruct/docs/aero_walkthrough/part_1-7.py",
    )
    await rec.call(reset)
    await rec.call(
        create_surface,
        name="wing", wing_type="CRM",
        num_x=2, num_y=7, num_twist_cp=5, symmetry=True,
        S_ref_type="wetted",
        t_over_c_cp=[0.15], c_max_t=0.303,
        CD0=0.015, with_viscous=True, with_wave=False,
    )
    envelope = await rec.call(
        run_optimization,
        surfaces=["wing"],
        analysis_type="aero",
        objective="CD",
        design_variables=[{"name": "twist", "lower": -10.0, "upper": 15.0}],
        constraints=[{"name": "CL", "equals": 0.5}],
        velocity=248.136, alpha=5.0,
        Mach_number=0.84, reynolds_number=1e6, density=0.38,
        objective_scaler=1e4,   # matches part_5.py scaler=1e4
        tolerance=1e-9,         # matches prob.driver.options["tol"] = 1e-9
    )
    return _r(envelope)["final_results"], rec


async def _mcp_crm_aerostruct_opt() -> tuple[dict, object]:
    """MCP: CRM aerostruct tube optimization (run_CRM.py).

    OAS thickness_cp=[0.1,0.2,0.3] is tip-to-root; MCP root-to-tip → [0.3,0.2,0.1].
    """
    from oas_mcp.server import create_surface, reset, run_optimization
    from workflow import WorkflowRecorder

    rec = WorkflowRecorder(
        "CRM Aerostruct Optimization",
        "openaerostruct/examples/run_CRM.py",
    )
    await rec.call(reset)
    await rec.call(
        create_surface,
        name="wing", wing_type="CRM",
        num_x=2, num_y=5, num_twist_cp=5, symmetry=True,
        S_ref_type="wetted",
        t_over_c_cp=[0.15], c_max_t=0.303,
        CD0=0.015, with_viscous=True, with_wave=False,
        fem_model_type="tube",
        thickness_cp=[0.3, 0.2, 0.1],   # root-to-tip (MCP convention)
        E=70e9, G=30e9, yield_stress=500e6, safety_factor=2.5, mrho=3e3,
        wing_weight_ratio=2.0, struct_weight_relief=False, distributed_fuel_weight=False,
    )
    envelope = await rec.call(
        run_optimization,
        surfaces=["wing"],
        analysis_type="aerostruct",
        objective="fuelburn",
        design_variables=[
            {"name": "twist", "lower": -10.0, "upper": 15.0},
            {"name": "thickness", "lower": 0.01, "upper": 0.5, "scaler": 1e2},  # matches run_CRM.py
            {"name": "alpha", "lower": -10.0, "upper": 10.0},
        ],
        constraints=[
            {"name": "failure", "upper": 0.0},
            {"name": "thickness_intersects", "upper": 0.0},
            {"name": "L_equals_W", "equals": 0.0},
        ],
        velocity=248.136, alpha=5.0,
        Mach_number=0.84, reynolds_number=1e6, density=0.38,
        W0=120000.0, R=11.165e6, speed_of_sound=295.4,
        objective_scaler=1e-5,  # matches run_CRM.py scaler=1e-5
        tolerance=1e-9,         # matches prob.driver.options["tol"] = 1e-9
    )
    return _r(envelope)["final_results"], rec


# ---------------------------------------------------------------------------
# Case 1: Stability Derivatives
# Source: openaerostruct/examples/stability_derivatives.py
# ---------------------------------------------------------------------------


class TestStabilityDerivsParity:
    """Parity: openaerostruct/examples/stability_derivatives.py"""

    @pytest.mark.asyncio
    async def test_cl_matches_oas(self, parity):
        r = await _cached("stability", _mcp_stability_derivs)
        case = parity["cases"]["stability_derivs"]
        expected = case["expected"]["CL"]
        tol = case["tolerances"]["CL"]["rel"]
        assert r["CL"] == pytest.approx(expected, rel=tol), (
            f"CL mismatch: got {r['CL']:.10f}, expected {expected:.10f}"
        )

    @pytest.mark.asyncio
    async def test_cd_matches_oas(self, parity):
        r = await _cached("stability", _mcp_stability_derivs)
        case = parity["cases"]["stability_derivs"]
        expected = case["expected"]["CD"]
        tol = case["tolerances"]["CD"]["rel"]
        assert r["CD"] == pytest.approx(expected, rel=tol), (
            f"CD mismatch: got {r['CD']:.10f}, expected {expected:.10f}"
        )

    @pytest.mark.asyncio
    async def test_cm_matches_oas(self, parity):
        r = await _cached("stability", _mcp_stability_derivs)
        case = parity["cases"]["stability_derivs"]
        expected = case["expected"]["CM"]
        tol = case["tolerances"]["CM"]["rel"]
        assert r["CM"] == pytest.approx(expected, rel=tol), (
            f"CM mismatch: got {r['CM']:.10f}, expected {expected:.10f}"
        )

    @pytest.mark.asyncio
    async def test_cl_alpha_matches_oas(self, parity):
        r = await _cached("stability", _mcp_stability_derivs)
        case = parity["cases"]["stability_derivs"]
        expected = case["expected"]["CL_alpha"]
        tol = case["tolerances"]["CL_alpha"]["rel"]
        assert r["CL_alpha"] == pytest.approx(expected, rel=tol), (
            f"CL_alpha mismatch: got {r['CL_alpha']:.10f}, expected {expected:.10f}"
        )

    @pytest.mark.asyncio
    async def test_cm_alpha_matches_oas(self, parity):
        r = await _cached("stability", _mcp_stability_derivs)
        case = parity["cases"]["stability_derivs"]
        expected = case["expected"]["CM_alpha"]
        tol = case["tolerances"]["CM_alpha"]["rel"]
        assert r["CM_alpha"] == pytest.approx(expected, rel=tol), (
            f"CM_alpha mismatch: got {r['CM_alpha']:.10f}, expected {expected:.10f}"
        )

    @pytest.mark.asyncio
    async def test_static_margin_matches_oas(self, parity):
        r = await _cached("stability", _mcp_stability_derivs)
        case = parity["cases"]["stability_derivs"]
        expected = case["expected"]["static_margin"]
        tol = case["tolerances"]["static_margin"]["rel"]
        assert r["static_margin"] == pytest.approx(expected, rel=tol), (
            f"static_margin mismatch: got {r['static_margin']:.10f}, expected {expected:.10f}"
        )


# ---------------------------------------------------------------------------
# Case 2: CRM Aero Twist Optimization
# Source: openaerostruct/docs/aero_walkthrough/part_1.py – part_7.py
# ---------------------------------------------------------------------------


class TestCRMAeroOptParity:
    """Parity: openaerostruct/docs/aero_walkthrough/part_1-7.py"""

    @pytest.mark.asyncio
    async def test_cd_matches_oas(self, parity):
        r = await _cached("crm_aero_opt", _mcp_crm_aero_opt)
        case = parity["cases"]["crm_aero_opt"]
        expected = case["expected"]["CD"]
        tol = case["tolerances"]["CD"]["rel"]
        assert r["CD"] == pytest.approx(expected, rel=tol), (
            f"CRM Aero Opt CD mismatch: got {r['CD']:.10f}, expected {expected:.10f}"
        )

    @pytest.mark.asyncio
    async def test_cl_matches_oas(self, parity):
        r = await _cached("crm_aero_opt", _mcp_crm_aero_opt)
        case = parity["cases"]["crm_aero_opt"]
        expected = case["expected"]["CL"]
        tol = case["tolerances"]["CL"]["rel"]
        assert r["CL"] == pytest.approx(expected, rel=tol), (
            f"CRM Aero Opt CL mismatch: got {r['CL']:.10f}, expected {expected:.10f}"
        )

    @pytest.mark.asyncio
    async def test_cm_matches_oas(self, parity):
        r = await _cached("crm_aero_opt", _mcp_crm_aero_opt)
        case = parity["cases"]["crm_aero_opt"]
        expected = case["expected"]["CM"]
        tol = case["tolerances"]["CM"]["rel"]
        assert r["CM"] == pytest.approx(expected, rel=tol), (
            f"CRM Aero Opt CM mismatch: got {r['CM']:.10f}, expected {expected:.10f}"
        )


# ---------------------------------------------------------------------------
# Case 3: CRM Aerostruct Optimization
# Source: openaerostruct/examples/run_CRM.py
# ---------------------------------------------------------------------------


class TestCRMAerostructOptParity:
    """Parity: openaerostruct/examples/run_CRM.py"""

    @pytest.mark.asyncio
    async def test_fuelburn_matches_oas(self, parity):
        r = await _cached("crm_aerostruct_opt", _mcp_crm_aerostruct_opt)
        case = parity["cases"]["crm_aerostruct_opt"]
        expected = case["expected"]["fuelburn"]
        tol = case["tolerances"]["fuelburn"]["rel"]
        assert r["fuelburn"] == pytest.approx(expected, rel=tol), (
            f"CRM Aerostruct fuelburn mismatch: got {r['fuelburn']:.4f}, expected {expected:.4f}"
        )

    @pytest.mark.asyncio
    async def test_cl_matches_oas(self, parity):
        r = await _cached("crm_aerostruct_opt", _mcp_crm_aerostruct_opt)
        case = parity["cases"]["crm_aerostruct_opt"]
        expected = case["expected"]["CL"]
        tol = case["tolerances"]["CL"]["rel"]
        assert r["CL"] == pytest.approx(expected, rel=tol), (
            f"CRM Aerostruct CL mismatch: got {r['CL']:.10f}, expected {expected:.10f}"
        )

    @pytest.mark.asyncio
    async def test_cd_matches_oas(self, parity):
        r = await _cached("crm_aerostruct_opt", _mcp_crm_aerostruct_opt)
        case = parity["cases"]["crm_aerostruct_opt"]
        expected = case["expected"]["CD"]
        tol = case["tolerances"]["CD"]["rel"]
        assert r["CD"] == pytest.approx(expected, rel=tol), (
            f"CRM Aerostruct CD mismatch: got {r['CD']:.10f}, expected {expected:.10f}"
        )


# ---------------------------------------------------------------------------
# Direct execution: side-by-side workflow comparison + HTML report
# ---------------------------------------------------------------------------


def _rel_diff(a: float, b: float) -> float:
    denom = max(abs(a), abs(b), 1e-300)
    return abs(a - b) / denom


def _status(diff: float, tol: float) -> str:
    return "PASS" if diff <= tol else "FAIL"


async def _run_example_parity_report() -> int:
    """Run all 3 cases and print a side-by-side OAS vs MCP comparison."""
    import generate_example_parity
    from workflow import WorkflowManifest, build_html_report

    with _PARITY_PATH.open() as f:
        ref = json.load(f)

    cases = [
        {
            "label": "Stability Derivatives",
            "source": "openaerostruct/examples/stability_derivatives.py",
            "oas_fn": generate_example_parity.run_stability_derivatives,
            "mcp_fn": _mcp_stability_derivs,
            "ref_key": "stability_derivs",
            "quantities": ["CL", "CD", "CM", "CL_alpha", "CM_alpha", "static_margin"],
            "oas_code": _OAS_CODE_STABILITY,
        },
        {
            "label": "CRM Aero Twist Optimization",
            "source": "openaerostruct/docs/aero_walkthrough/part_1-7.py",
            "oas_fn": generate_example_parity.run_crm_aero_opt,
            "mcp_fn": _mcp_crm_aero_opt,
            "ref_key": "crm_aero_opt",
            "quantities": ["CD", "CL", "CM"],
            "oas_code": _OAS_CODE_CRM_AERO_OPT,
        },
        {
            "label": "CRM Aerostruct Optimization",
            "source": "openaerostruct/examples/run_CRM.py",
            "oas_fn": generate_example_parity.run_crm_aerostruct_opt,
            "mcp_fn": _mcp_crm_aerostruct_opt,
            "ref_key": "crm_aerostruct_opt",
            "quantities": ["fuelburn", "CL", "CD"],
            "oas_code": _OAS_CODE_CRM_AEROSTRUCT_OPT,
        },
    ]

    total = 0
    passed = 0
    manifests: list[WorkflowManifest] = []
    sep = "=" * 70

    print(sep)
    print("Example Workflow Parity Report — OAS Examples vs MCP Server")
    print(sep)

    for i, case in enumerate(cases, 1):
        ref_case = ref["cases"][case["ref_key"]]
        quantities = case["quantities"]
        tolerances = {q: ref_case["tolerances"][q]["rel"] for q in quantities}

        print(f"\n[{i}/{len(cases)}] {case['label']}  ({case['source']})")

        print("  Running OAS direct...", end="", flush=True)
        oas_results = case["oas_fn"]()
        print(" done")

        print("  Running MCP server...", end="", flush=True)
        mcp_results, recorder = await case["mcp_fn"]()
        print(" done")

        manifest = recorder.finalize(oas_results, mcp_results, tolerances, case["oas_code"])
        manifests.append(manifest)
        manifest.print_report()

        for qty in quantities:
            oas_val = oas_results.get(qty)
            mcp_val = mcp_results.get(qty)
            if oas_val is None or mcp_val is None:
                continue
            diff = _rel_diff(oas_val, mcp_val)
            status = _status(diff, tolerances[qty])
            total += 1
            if status == "PASS":
                passed += 1

    print()
    print(sep)
    print(f"Summary: {passed}/{total} passed")
    print(sep)

    # Save per-case JSON manifests
    manifest_dir = Path(__file__).parent / "workflow_manifests"
    manifest_dir.mkdir(exist_ok=True)
    for m in manifests:
        safe_name = m.name.lower().replace(" ", "_")
        with (manifest_dir / f"{safe_name}.json").open("w") as f:
            json.dump(m.to_dict(), f, indent=2)

    # Build combined HTML report
    html_path = Path(__file__).parent / "workflow_report.html"
    html_path.write_text(build_html_report(manifests))
    print(f"\nHTML report written to: {html_path}")
    print(f"JSON manifests written to: {manifest_dir}/")

    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_run_example_parity_report()))
