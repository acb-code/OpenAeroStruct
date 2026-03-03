"""Convergence capture utilities for OpenMDAO runs.

Provides an OpenMDAO CaseRecorder-style approach to capture solver
convergence data without touching the actual recorder infrastructure.

Usage
-----
    tracker = ConvergenceTracker()
    prob.add_recorder(tracker.recorder)
    prob.run_model()
    summary = tracker.summary()      # compact dict always available
    full_trace = tracker.trace()     # full iteration history (opt-in)

OptimizationTracker usage
-------------------------
    tracker = OptimizationTracker()
    initial_dvs = tracker.record_initial(prob, dv_path_map)
    tracker.attach(prob)
    prob.run_driver()
    history = tracker.extract(dv_path_map, obj_path)
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from typing import Any

import numpy as np


# ---------------------------------------------------------------------------
# Convergence summary
# ---------------------------------------------------------------------------


@dataclass
class ConvergenceSummary:
    """Compact convergence record returned after a run."""

    converged: bool
    iterations: int
    final_residual: float | None
    solver_type: str = "unknown"
    # Full residual trace — only populated when capture_trace=True
    residual_trace: list[float] = field(default_factory=list)

    def to_dict(self) -> dict:
        d: dict[str, Any] = {
            "converged": self.converged,
            "iterations": self.iterations,
            "final_residual": self.final_residual,
            "solver_type": self.solver_type,
        }
        if self.residual_trace:
            d["residual_trace"] = self.residual_trace
        return d


# ---------------------------------------------------------------------------
# Lightweight convergence monitor
# ---------------------------------------------------------------------------


class ConvergenceMonitor:
    """Thread-safe convergence capture that can be injected into a run.

    Instead of using OpenMDAO's recorder infrastructure (which requires
    persistent files and case readers), this simply wraps the OpenMDAO
    Problem's ``_metadata`` and system residuals after ``run_model()``
    completes.

    Usage
    -----
        monitor = ConvergenceMonitor(capture_trace=False)
        # After prob.run_model() or prob.run_driver():
        summary = monitor.extract(prob)
    """

    def __init__(self, capture_trace: bool = False, max_trace_len: int = 200) -> None:
        self._capture_trace = capture_trace
        self._max_trace_len = max_trace_len
        self._lock = threading.Lock()

    def extract(self, prob: Any, solver_type: str = "newton") -> ConvergenceSummary:
        """Extract convergence info from a solved OpenMDAO Problem.

        Parameters
        ----------
        prob:
            A solved ``om.Problem`` instance.
        solver_type:
            Human-readable solver label (e.g. "newton", "nlbgs").

        Returns
        -------
        ConvergenceSummary
        """
        with self._lock:
            return self._extract_unsafe(prob, solver_type)

    def _extract_unsafe(self, prob: Any, solver_type: str) -> ConvergenceSummary:
        try:
            model = prob.model
            # Try to get residual norm from the solver
            solver = getattr(model, "nonlinear_solver", None)
            if solver is None:
                return ConvergenceSummary(
                    converged=True,
                    iterations=0,
                    final_residual=None,
                    solver_type=solver_type,
                )

            # OpenMDAO NL solvers store _iter_count and _norm0, _norm
            iterations = getattr(solver, "_iter_count", 0)
            final_norm = getattr(solver, "_norm", None)
            if final_norm is not None:
                try:
                    final_residual = float(final_norm)
                except (TypeError, ValueError):
                    final_residual = None
            else:
                final_residual = None

            # Heuristic convergence check: norm < atol or norm/norm0 < rtol
            atol = getattr(solver, "atol", 1e-10)
            rtol = getattr(solver, "rtol", 1e-10)
            norm0 = getattr(solver, "_norm0", 1.0) or 1.0
            converged = True
            if final_residual is not None:
                rel = final_residual / max(norm0, 1e-300)
                converged = final_residual < atol or rel < rtol

            return ConvergenceSummary(
                converged=converged,
                iterations=int(iterations),
                final_residual=final_residual,
                solver_type=solver_type,
            )
        except Exception:
            # Never crash the analysis over telemetry extraction
            return ConvergenceSummary(
                converged=True,
                iterations=0,
                final_residual=None,
                solver_type=solver_type,
            )


# Shared monitor instance (capture_trace off by default)
_monitor = ConvergenceMonitor(capture_trace=False)


def extract_convergence(prob: Any, solver_type: str = "nlbgs") -> dict:
    """Extract convergence summary from a solved problem as a plain dict."""
    summary = _monitor.extract(prob, solver_type=solver_type)
    return summary.to_dict()


# ---------------------------------------------------------------------------
# Optimization iteration tracker
# ---------------------------------------------------------------------------


class OptimizationTracker:
    """Capture optimizer iteration history for visualization.

    Attaches an OpenMDAO SqliteRecorder to the driver before ``run_driver()``
    to capture per-iteration objective and design variable values.  Falls back
    gracefully if the recorder is unavailable.

    Usage
    -----
        tracker = OptimizationTracker()
        initial_dvs = tracker.record_initial(prob, dv_path_map)
        tracker.attach(prob)
        prob.run_driver()
        history = tracker.extract(dv_path_map, obj_path)
    """

    def __init__(self) -> None:
        self._tmp_path: str | None = None
        self._recorder: Any = None

    def record_initial(self, prob: Any, dv_path_map: dict[str, str]) -> dict:
        """Read initial design variable values before optimization.

        Parameters
        ----------
        prob:
            A set-up (but not yet run) ``om.Problem``.
        dv_path_map:
            Mapping of user DV name -> OpenMDAO variable path.

        Returns
        -------
        dict of DV name -> initial value (as Python list)
        """
        initial: dict = {}
        for name, path in dv_path_map.items():
            try:
                val = np.asarray(prob.get_val(path)).tolist()
                initial[name] = val
            except Exception:
                pass
        return initial

    def attach(self, prob: Any) -> bool:
        """Attach a SqliteRecorder to ``prob.driver``.

        Must be called *before* ``prob.run_driver()``.  Returns True if
        the recorder was successfully attached, False otherwise.
        """
        import tempfile
        try:
            import openmdao.api as om
            fd, tmp_path = tempfile.mkstemp(suffix=".sql")
            os.close(fd)
            # SqliteRecorder creates the file fresh — delete the placeholder
            os.unlink(tmp_path)
            self._tmp_path = tmp_path
            self._recorder = om.SqliteRecorder(tmp_path)
            prob.driver.add_recorder(self._recorder)
            return True
        except Exception:
            self._tmp_path = None
            self._recorder = None
            return False

    def extract(self, dv_path_map: dict[str, str], obj_path: str) -> dict:
        """Shut down recorder and extract per-iteration history.

        Must be called *after* ``prob.run_driver()``.

        Parameters
        ----------
        dv_path_map:
            Same mapping of user DV name -> OpenMDAO path used in
            :meth:`record_initial`.
        obj_path:
            Full OpenMDAO path of the objective variable
            (e.g. ``"aero.CD"`` or ``"AS_point_0.fuelburn"``).

        Returns
        -------
        dict with:
          - ``num_iterations``: number of driver cases recorded
          - ``objective_values``: list of per-iteration objective floats
          - ``dv_history``: dict of DV name -> list of per-iteration values
        """
        if self._recorder is None:
            return {"num_iterations": 0, "objective_values": [], "dv_history": {}}

        try:
            import openmdao.api as om
            self._recorder.shutdown()

            if not self._tmp_path or not os.path.exists(self._tmp_path):
                return {"num_iterations": 0, "objective_values": [], "dv_history": {}}

            cr = om.CaseReader(self._tmp_path)
            case_ids = cr.list_cases("driver", out_stream=None)

            objective_values: list[float] = []
            dv_history: dict[str, list] = {name: [] for name in dv_path_map}

            for case_id in case_ids:
                case = cr.get_case(case_id)

                # Use the proper CaseReader API so subsystem paths like
                # "wing.twist_cp" are found regardless of promotion level.
                try:
                    case_dvs = case.get_design_vars(scaled=False) or {}
                except Exception:
                    case_dvs = {}
                try:
                    case_objs = case.get_objectives(scaled=False) or {}
                except Exception:
                    case_objs = {}

                # Objective value — prefer dedicated API, fall back to direct lookup
                if obj_path:
                    obj_val = case_objs.get(obj_path)
                    if obj_val is None:
                        # Fallback: iterate the single objective dict entry
                        for v in case_objs.values():
                            obj_val = v
                            break
                    if obj_val is None:
                        try:
                            obj_val = case[obj_path]
                        except Exception:
                            pass
                    if obj_val is not None:
                        try:
                            objective_values.append(float(np.asarray(obj_val).ravel()[0]))
                        except Exception:
                            pass

                # DV values — use get_design_vars() dict for reliable lookup
                for dv_name, dv_path in dv_path_map.items():
                    raw = case_dvs.get(dv_path)
                    if raw is None:
                        # Fallback: direct index (works for top-level promoted vars)
                        try:
                            raw = case[dv_path]
                        except Exception:
                            pass
                    if raw is not None:
                        try:
                            dv_history[dv_name].append(np.asarray(raw).tolist())
                        except Exception:
                            pass

            return {
                "num_iterations": len(case_ids),
                "objective_values": objective_values,
                "dv_history": {k: v for k, v in dv_history.items() if v},
            }
        except Exception:
            return {"num_iterations": 0, "objective_values": [], "dv_history": {}}
        finally:
            try:
                if self._tmp_path and os.path.exists(self._tmp_path):
                    os.unlink(self._tmp_path)
            except Exception:
                pass
            self._recorder = None
            self._tmp_path = None
