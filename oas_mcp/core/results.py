"""Extract results from solved OpenMDAO problems."""

from __future__ import annotations

import numpy as np
import openmdao.api as om


def _scalar(val) -> float:
    """Convert numpy scalar / array to plain Python float."""
    arr = np.asarray(val).ravel()
    return float(arr[0])


def _try_get(prob: om.Problem, path: str, units: str | None = None):
    """Return value or None if path doesn't exist."""
    try:
        if units:
            return prob.get_val(path, units=units)
        return prob.get_val(path)
    except Exception:
        return None


def extract_aero_results(prob: om.Problem, surfaces: list[dict], point_name: str = "aero") -> dict:
    """Extract aerodynamic results from a solved AeroPoint problem."""
    CL = _scalar(prob.get_val(f"{point_name}.CL"))
    CD = _scalar(prob.get_val(f"{point_name}.CD"))
    CM_vec = np.asarray(prob.get_val(f"{point_name}.CM")).ravel()
    CM = float(CM_vec[1]) if len(CM_vec) > 1 else float(CM_vec[0])

    results = {
        "CL": CL,
        "CD": CD,
        "CM": CM,
        "L_over_D": CL / CD if CD != 0 else None,
        "surfaces": {},
    }

    for surface in surfaces:
        name = surface["name"]
        perf = f"{point_name}.{name}_perf"
        surf_res = {}
        for key, path in [
            ("CL", f"{perf}.CL"),
            ("CD", f"{perf}.CD"),
            ("CDi", f"{perf}.CDi"),
            ("CDv", f"{perf}.CDv"),
            ("CDw", f"{perf}.CDw"),
        ]:
            v = _try_get(prob, path)
            if v is not None:
                surf_res[key] = float(np.asarray(v).ravel()[0])
        results["surfaces"][name] = surf_res

    return results


def extract_aerostruct_results(
    prob: om.Problem, surfaces: list[dict], point_name: str = "AS_point_0"
) -> dict:
    """Extract coupled aerostructural results from a solved AerostructPoint problem."""
    CL = _scalar(prob.get_val(f"{point_name}.CL"))
    CD = _scalar(prob.get_val(f"{point_name}.CD"))
    CM_vec = np.asarray(prob.get_val(f"{point_name}.CM")).ravel()
    CM = float(CM_vec[1]) if len(CM_vec) > 1 else float(CM_vec[0])

    results = {
        "CL": CL,
        "CD": CD,
        "CM": CM,
        "L_over_D": CL / CD if CD != 0 else None,
        "surfaces": {},
    }

    # Mission / fuel burn
    fuelburn = _try_get(prob, f"{point_name}.fuelburn")
    if fuelburn is not None:
        results["fuelburn"] = _scalar(fuelburn)

    # L=W residual
    lew = _try_get(prob, f"{point_name}.L_equals_W")
    if lew is not None:
        results["L_equals_W"] = _scalar(lew)

    # Per-surface structural and aero outputs
    total_struct_mass = 0.0
    for surface in surfaces:
        name = surface["name"]
        perf = f"{point_name}.{name}_perf"
        surf_res = {}

        # Aero coefficients
        for key, path in [
            ("CL", f"{perf}.CL"),
            ("CD", f"{perf}.CD"),
            ("CDi", f"{perf}.CDi"),
            ("CDv", f"{perf}.CDv"),
            ("CDw", f"{perf}.CDw"),
        ]:
            v = _try_get(prob, path)
            if v is not None:
                surf_res[key] = float(np.asarray(v).ravel()[0])

        # Structural failure metric
        failure = _try_get(prob, f"{perf}.failure")
        if failure is not None:
            surf_res["failure"] = _scalar(failure)

        # Von Mises stress
        vonmises = _try_get(prob, f"{perf}.vonmises")
        if vonmises is not None:
            vm_arr = np.asarray(vonmises).ravel()
            surf_res["max_vonmises_Pa"] = float(vm_arr.max())

        # Structural mass from geometry group
        sm = _try_get(prob, f"{name}.structural_mass")
        if sm is not None:
            sm_val = _scalar(sm)
            surf_res["structural_mass_kg"] = sm_val
            total_struct_mass += sm_val

        results["surfaces"][name] = surf_res

    if total_struct_mass > 0:
        results["structural_mass"] = total_struct_mass

    return results


def extract_stability_results(prob: om.Problem) -> dict:
    """Extract stability derivative results."""
    results = {}

    for key, path, units in [
        ("CL", "aero_point.CL", None),
        ("CD", "aero_point.CD", None),
        ("CL_alpha", "CL_alpha", "1/deg"),
        ("static_margin", "static_margin", None),
    ]:
        v = _try_get(prob, path, units)
        if v is not None:
            results[key] = float(np.asarray(v).ravel()[0])

    # CM — pitching moment (index 1)
    cm = _try_get(prob, "aero_point.CM")
    if cm is not None:
        cm_arr = np.asarray(cm).ravel()
        results["CM"] = float(cm_arr[1]) if len(cm_arr) > 1 else float(cm_arr[0])

    # CM_alpha — pitching (index 1 of array output)
    cm_alpha = _try_get(prob, "CM_alpha", "1/deg")
    if cm_alpha is not None:
        cm_alpha_arr = np.asarray(cm_alpha).ravel()
        results["CM_alpha"] = float(cm_alpha_arr[1]) if len(cm_alpha_arr) > 1 else float(cm_alpha_arr[0])

    # Stability interpretation
    sm = results.get("static_margin")
    if sm is not None:
        if sm > 0.05:
            results["stability"] = "statically stable (positive static margin)"
        elif sm > 0.0:
            results["stability"] = "marginally stable"
        else:
            results["stability"] = "statically unstable (negative static margin)"

    return results
