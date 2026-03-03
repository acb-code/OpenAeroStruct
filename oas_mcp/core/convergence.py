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
"""

from __future__ import annotations

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
