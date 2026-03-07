#!/usr/bin/env python3
"""
Generate or update example_parity_values.json by running OAS example workflows.

Each runner executes the actual OAS example/walkthrough script (or assembles
the walkthrough parts) and extracts results from the returned prob object.
This ensures ground truth always comes from canonical OAS code — no duplication.

Sources (relative to repo root):
  stability_derivs   — openaerostruct/examples/stability_derivatives.py
  crm_aero_opt       — openaerostruct/docs/aero_walkthrough/part_1.py .. part_6.py
  crm_aerostruct_opt — openaerostruct/examples/run_CRM.py

Run:
    OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 \\
        python oas_mcp/tests/golden/generate_example_parity.py
"""
from __future__ import annotations

import contextlib
import json
import os
import platform
import sys
import tempfile
from pathlib import Path

import numpy as np

PARITY_PATH = Path(__file__).parent / "example_parity_values.json"
REPO_ROOT = Path(__file__).parents[3]  # golden/ → tests/ → oas_mcp/ → repo root

# Tolerances are policy decisions — not derived from runs.
_TOLERANCES: dict = {
    "stability_derivs": {
        "CL": {"rel": 1e-6},
        "CD": {"rel": 1e-6},
        "CM": {"rel": 1e-6},
        "CL_alpha": {"rel": 1e-4},
        "CM_alpha": {"rel": 1e-4},
        "static_margin": {"rel": 1e-4},
    },
    "crm_aero_opt": {
        "CD": {"rel": 1e-4},
        "CL": {"rel": 1e-4},
        "CM": {"rel": 1e-4},
    },
    "crm_aerostruct_opt": {
        "fuelburn": {"rel": 1e-4},
        "CL": {"rel": 1e-3},
        "CD": {"rel": 1e-3},
    },
}


@contextlib.contextmanager
def _quiet():
    """Suppress stdout/stderr from OAS/OpenMDAO during a run."""
    with open(os.devnull, "w") as devnull:
        with contextlib.redirect_stdout(devnull), contextlib.redirect_stderr(devnull):
            yield


@contextlib.contextmanager
def _in_tmpdir():
    """Change to a temp dir to isolate SqliteRecorder .db files, then restore."""
    orig_dir = os.getcwd()
    with tempfile.TemporaryDirectory() as tmpdir:
        os.chdir(tmpdir)
        try:
            yield tmpdir
        finally:
            os.chdir(orig_dir)


# ---------------------------------------------------------------------------
# Public runners — called by test_example_parity.py __main__ and main() below
# ---------------------------------------------------------------------------


def run_stability_derivatives() -> dict:
    """Run openaerostruct/examples/stability_derivatives.py and return results.

    Extracts CL, CD, CM (pitching), CL_alpha, CM_alpha (pitching), static_margin.
    """
    import runpy

    script = REPO_ROOT / "openaerostruct/examples/stability_derivatives.py"
    with _in_tmpdir():
        with _quiet():
            ns = runpy.run_path(str(script))
    prob = ns["prob"]
    return {
        "CL": float(prob.get_val("aero_point.CL")[0]),
        "CD": float(prob.get_val("aero_point.CD")[0]),
        "CM": float(prob.get_val("aero_point.CM")[1]),          # pitching (index 1)
        "CL_alpha": float(prob.get_val("CL_alpha", units="1/deg")[0]),
        "CM_alpha": float(prob.get_val("CM_alpha", units="1/deg")[1]),  # pitching
        "static_margin": float(prob.get_val("static_margin")[0]),
    }


def run_crm_aero_opt() -> dict:
    """Execute aero walkthrough parts 1-6 in sequence and return optimised results.

    Parts 1-6 are documentation code snippets that build up the problem
    incrementally; concatenating them produces a complete runnable script.

    Reference (part_7.py assert_near_equal):
      CD = 0.033389699871650073, CL = 0.5, CM[1] = -1.7885550372372376
    """
    parts_dir = REPO_ROOT / "openaerostruct/docs/aero_walkthrough"
    code = "\n".join(
        (parts_dir / f"part_{i}.py").read_text() for i in range(1, 7)
    )
    with _in_tmpdir():
        ns: dict = {"__name__": "__main__"}
        with _quiet():
            exec(compile(code, "<aero_walkthrough_parts1-6>", "exec"), ns)  # noqa: S102
    prob = ns["prob"]
    return {
        "CD": float(prob.get_val("aero_point_0.wing_perf.CD")[0]),
        "CL": float(prob.get_val("aero_point_0.wing_perf.CL")[0]),
        "CM": float(prob.get_val("aero_point_0.CM")[1]),
    }


def run_crm_aerostruct_opt() -> dict:
    """Run openaerostruct/examples/run_CRM.py and return optimised results.

    The script writes aerostruct.db; running in a tmpdir keeps the workspace clean.
    """
    import runpy

    script = REPO_ROOT / "openaerostruct/examples/run_CRM.py"
    with _in_tmpdir():
        with _quiet():
            ns = runpy.run_path(str(script))
    prob = ns["prob"]
    return {
        "fuelburn": float(prob.get_val("AS_point_0.fuelburn")[0]),
        "CL": float(prob.get_val("AS_point_0.wing_perf.CL")[0]),
        "CD": float(prob.get_val("AS_point_0.wing_perf.CD")[0]),
    }


# ---------------------------------------------------------------------------
# Collect + diff + write
# ---------------------------------------------------------------------------


def _collect_example_values() -> dict:
    """Run all three example workflows and return the full parity_values dict."""
    import openaerostruct
    import openmdao

    cases: dict = {}

    print("  [1/3] Stability derivatives  (stability_derivatives.py)...", end="", flush=True)
    r1 = run_stability_derivatives()
    cases["stability_derivs"] = {
        "source": "openaerostruct/examples/stability_derivatives.py",
        "surface_config": {
            "name": "wing", "wing_type": "rect", "num_x": 3, "num_y": 15,
            "symmetry": True, "S_ref_type": "wetted", "sweep": 10.0,
            "t_over_c_cp": [0.15], "c_max_t": 0.303,
            "CD0": 0.015, "with_viscous": True, "with_wave": False,
            "num_twist_cp": 2, "twist_cp": [0.0, 0.0],
        },
        "flight_config": {
            "surfaces": ["wing"], "velocity": 248.136, "alpha": 5.0,
            "Mach_number": 0.84, "reynolds_number": 1000000.0, "density": 0.38,
            "cg": [0.5, 0.0, 0.0],
        },
        "expected": r1,
        "tolerances": _TOLERANCES["stability_derivs"],
    }
    print(f"  CL={r1['CL']:.8g}  CL_alpha={r1['CL_alpha']:.8g}  SM={r1['static_margin']:.6g}")

    print("  [2/3] CRM aero twist opt  (aero_walkthrough parts 1-6)...", end="", flush=True)
    r2 = run_crm_aero_opt()
    cases["crm_aero_opt"] = {
        "source": "openaerostruct/docs/aero_walkthrough/part_1-7.py",
        "reference_assertions": (
            "part_7.py: CD=0.033389699871650073, CL=0.5, CM=-1.7885550372372376"
        ),
        "surface_config": {
            "name": "wing", "wing_type": "CRM", "num_x": 2, "num_y": 7,
            "num_twist_cp": 5, "symmetry": True, "S_ref_type": "wetted",
            "t_over_c_cp": [0.15], "c_max_t": 0.303,
            "CD0": 0.015, "with_viscous": True, "with_wave": False,
        },
        "flight_config": {
            "surfaces": ["wing"], "velocity": 248.136, "alpha": 5.0,
            "Mach_number": 0.84, "reynolds_number": 1000000.0, "density": 0.38,
        },
        "optimization": {
            "design_variables": [{"name": "twist", "lower": -10.0, "upper": 15.0}],
            "constraints": [{"name": "CL", "equals": 0.5}],
            "objective": "CD",
            "objective_scaler": 1e4,   # matches part_5.py scaler=1e4
            "tolerance": 1e-9,         # matches part_5.py tol=1e-9
        },
        "expected": r2,
        "tolerances": _TOLERANCES["crm_aero_opt"],
    }
    print(f"  CD={r2['CD']:.10g}  CL={r2['CL']:.8g}  CM={r2['CM']:.8g}")

    print("  [3/3] CRM aerostruct opt  (run_CRM.py)...", end="", flush=True)
    r3 = run_crm_aerostruct_opt()
    cases["crm_aerostruct_opt"] = {
        "source": "openaerostruct/examples/run_CRM.py",
        "surface_config": {
            "name": "wing", "wing_type": "CRM", "num_x": 2, "num_y": 5,
            "num_twist_cp": 5, "symmetry": True, "S_ref_type": "wetted",
            "fem_model_type": "tube",
            "thickness_cp": [0.3, 0.2, 0.1],  # MCP root-to-tip (OAS is tip-to-root)
            "t_over_c_cp": [0.15], "c_max_t": 0.303,
            "CD0": 0.015, "with_viscous": True, "with_wave": False,
            "E": 70e9, "G": 30e9, "yield_stress": 500e6,
            "safety_factor": 2.5, "mrho": 3e3, "wing_weight_ratio": 2.0,
            "struct_weight_relief": False, "distributed_fuel_weight": False,
        },
        "flight_config": {
            "surfaces": ["wing"], "velocity": 248.136, "alpha": 5.0,
            "Mach_number": 0.84, "reynolds_number": 1000000.0, "density": 0.38,
            "W0": 120000.0, "R": 11165000.0, "speed_of_sound": 295.4,
        },
        "optimization": {
            "design_variables": [
                {"name": "twist", "lower": -10.0, "upper": 15.0},
                {"name": "thickness", "lower": 0.01, "upper": 0.5, "scaler": 1e2},  # matches run_CRM.py
                {"name": "alpha", "lower": -10.0, "upper": 10.0},
            ],
            "constraints": [
                {"name": "failure", "upper": 0.0},
                {"name": "thickness_intersects", "upper": 0.0},
                {"name": "L_equals_W", "equals": 0.0},
            ],
            "objective": "fuelburn",
            "objective_scaler": 1e-5,  # matches run_CRM.py scaler=1e-5
            "tolerance": 1e-9,         # matches run_CRM.py tol=1e-9
        },
        "expected": r3,
        "tolerances": _TOLERANCES["crm_aerostruct_opt"],
    }
    print(f"  fuelburn={r3['fuelburn']:.8g}  CL={r3['CL']:.6g}  CD={r3['CD']:.6g}")

    return {
        "schema_version": "1.0",
        "description": "OAS example workflow reference values for MCP example parity tests",
        "reproducibility_header": {
            "python_version": sys.version,
            "platform": platform.platform(),
            "openmdao_version": openmdao.__version__,
            "oas_version": openaerostruct.__version__,
            "numpy_version": np.__version__,
            "note": "Run with OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1",
        },
        "cases": cases,
    }


def _diff_summary(old: dict, new: dict) -> list[str]:
    lines: list[str] = []
    old_cases = old.get("cases", {})
    new_cases = new.get("cases", {})
    for case_name in sorted(set(old_cases) | set(new_cases)):
        if case_name not in old_cases:
            lines.append(f"  + NEW case: {case_name}")
            continue
        if case_name not in new_cases:
            lines.append(f"  - REMOVED case: {case_name}")
            continue
        old_exp = old_cases[case_name].get("expected", {})
        new_exp = new_cases[case_name].get("expected", {})
        for key in sorted(set(old_exp) | set(new_exp)):
            if key not in old_exp:
                lines.append(f"  {case_name}.{key}: NEW = {new_exp[key]}")
            elif key not in new_exp:
                lines.append(f"  {case_name}.{key}: REMOVED")
            else:
                ov, nv = old_exp[key], new_exp[key]
                if abs(nv - ov) > 1e-12:
                    pct = (nv - ov) / max(abs(ov), 1e-300) * 100
                    lines.append(
                        f"  {case_name}.{key}: {ov:.10g} → {nv:.10g}  ({pct:+.4f}%)"
                    )
    return lines


def main() -> None:
    print("Generating example parity reference values by running OAS example workflows...")
    print()
    new_values = _collect_example_values()

    old_values: dict = {}
    if PARITY_PATH.exists():
        with PARITY_PATH.open() as f:
            old_values = json.load(f)

    diff = _diff_summary(old_values, new_values)
    if diff:
        print("\nChanged values (review before committing):")
        for line in diff:
            print(line)
    else:
        print("\nNo numeric changes detected.")

    with PARITY_PATH.open("w") as f:
        json.dump(new_values, f, indent=2)
    print(f"\nWritten to {PARITY_PATH}")
    print("\nIMPORTANT: Review the diff above before committing updated baselines.")


if __name__ == "__main__":
    main()
