"""Extract results from solved OpenMDAO problems.

Standard-level extraction
-------------------------
Call ``extract_standard_detail(prob, surfaces, analysis_type, point_name)``
to capture sectional data (spanwise Cl, von Mises distributions) and mesh
snapshots that survive cache eviction.  This is stored in the artifact at
run time and does not require the live om.Problem later.
"""

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


def extract_standard_detail(
    prob: om.Problem,
    surfaces: list[dict],
    analysis_type: str,
    point_name: str,
) -> dict:
    """Extract 'standard' detail level data at run time.

    This data is persisted in the artifact store and survives cache eviction,
    unlike 'full' detail which requires the live om.Problem.

    Returns a dict with:
      - ``sectional_data``: per-surface spanwise distributions
      - ``mesh_snapshot``: leading/trailing edge coordinates of undeformed mesh
    """
    standard: dict = {"sectional_data": {}, "mesh_snapshot": {}}

    for surface in surfaces:
        name = surface["name"]
        perf = f"{point_name}.{name}_perf"
        sect: dict = {}

        # Spanwise panel y-stations from mesh (normalised 0→1)
        mesh = surface.get("mesh")
        if mesh is not None:
            y_coords = np.asarray(mesh[0, :, 1]).ravel()
            y_min, y_max = float(y_coords.min()), float(y_coords.max())
            span_half = max(abs(y_max - y_min), 1e-12)
            y_norm = ((y_coords - y_min) / span_half).tolist()
            sect["y_span_norm"] = y_norm

            # Mesh snapshot: leading/trailing edge for planform plot
            le = np.asarray(mesh[0, :, :]).tolist()
            te = np.asarray(mesh[-1, :, :]).tolist()
            standard["mesh_snapshot"][name] = {
                "leading_edge": le,
                "trailing_edge": te,
                "nx": int(mesh.shape[0]),
                "ny": int(mesh.shape[1]),
            }

        # Sectional CL (panel-level) — path varies by OAS version
        for cl_path in [
            f"{point_name}.{name}_perf.Cl",
            f"{point_name}.aero_states.{name}_sec_forces",
        ]:
            cl_val = _try_get(prob, cl_path)
            if cl_val is not None:
                cl_arr = np.asarray(cl_val).ravel()
                if len(cl_arr) > 1:
                    sect["Cl"] = cl_arr.tolist()
                break

        # Spanwise von Mises stress (aerostruct only)
        if analysis_type == "aerostruct":
            vm_path = f"{perf}.vonmises"
            vm_val = _try_get(prob, vm_path)
            if vm_val is not None:
                vm_2d = np.asarray(vm_val)
                # vonmises shape: (ny-1, 2) for tube, (ny-1, 4) for wingbox.
                # Take max over the last dimension to get peak stress per element.
                if vm_2d.ndim >= 2:
                    vm_per_elem = vm_2d.max(axis=-1).ravel()
                else:
                    vm_per_elem = vm_2d.ravel()
                if len(vm_per_elem) > 1:
                    sect["vonmises_MPa"] = (vm_per_elem / 1e6).tolist()

            # Failure index distribution (per element)
            fi_path = f"{perf}.failure"
            fi_val = _try_get(prob, fi_path)
            if fi_val is not None:
                fi_arr = np.asarray(fi_val).ravel()
                if len(fi_arr) > 1:
                    sect["failure_index"] = fi_arr.tolist()
                elif "vonmises_MPa" in sect:
                    # FailureKS returns a scalar; derive per-element failure index
                    # from vonmises: failure_i = vm / sigma_allow - 1
                    yield_stress = surface.get("yield_stress", 500e6)
                    safety_factor = surface.get("safety_factor", 2.5)
                    sigma_allow = yield_stress / safety_factor
                    vm_pa = np.array(sect["vonmises_MPa"]) * 1e6
                    sect["failure_index"] = (vm_pa / sigma_allow - 1.0).tolist()

        standard["sectional_data"][name] = sect

    return standard


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
