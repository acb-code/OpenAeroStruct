"""Matplotlib-based plot generation for OAS MCP results.

All plots return an ``mcp.server.fastmcp.utilities.types.Image`` object that
FastMCP auto-converts to ``ImageContent`` in the MCP response.  A SHA-256
hash is embedded in the Image object's ``_hash`` attribute for client-side
caching (accessible via ``img._hash``).

Supported plot types (strict enum)
-----------------------------------
  "lift_distribution"   — spanwise sectional Cl distribution
  "drag_polar"          — CL vs CD and L/D vs alpha
  "stress_distribution" — spanwise von Mises stress
  "convergence"         — solver residual vs iteration (if trace available)
  "planform"            — wing planform + deflection overlay
  "opt_history"         — optimizer objective convergence history
  "opt_dv_evolution"    — design variable evolution over optimizer iterations
  "opt_comparison"      — before/after DV comparison (initial vs optimized)

All plots include:
  - Axes labels with units
  - Title including run_id and case name
  - Data ranges in subtitle

Standard pixel dimensions: 900 × 540 px at 150 dpi
"""

from __future__ import annotations

import hashlib
import io
from typing import Any

import numpy as np

from mcp.server.fastmcp.utilities.types import Image

# Lazy matplotlib import — avoid importing at module load to keep startup fast.
_MPL_AVAILABLE: bool | None = None


def _require_mpl():
    """Import matplotlib with non-interactive backend; raise if unavailable."""
    global _MPL_AVAILABLE
    if _MPL_AVAILABLE is False:
        raise ImportError(
            "matplotlib is required for visualisation. "
            "Install it with: pip install matplotlib"
        )
    try:
        import matplotlib
        matplotlib.use("Agg")  # non-interactive, safe for server-side use
        import matplotlib.pyplot as plt
        _MPL_AVAILABLE = True
        return matplotlib, plt
    except ImportError:
        _MPL_AVAILABLE = False
        raise ImportError(
            "matplotlib is required for visualisation. "
            "Install it with: pip install matplotlib"
        )


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PLOT_TYPES = frozenset({
    "lift_distribution",
    "drag_polar",
    "stress_distribution",
    "convergence",
    "planform",
    "opt_history",
    "opt_dv_evolution",
    "opt_comparison",
})

_FIG_WIDTH_IN = 6.0   # inches
_FIG_HEIGHT_IN = 3.6  # inches
_DPI = 150            # → 900 × 540 px


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fig_to_response(fig, run_id: str, plot_type: str) -> Image:
    """Convert a matplotlib Figure to an MCP Image object.

    FastMCP auto-converts ``Image`` returns to ``ImageContent``, so the plot
    is delivered as a proper image attachment rather than raw base64 text.
    The SHA-256 hash is stored on ``img._hash`` for client-side caching.
    """
    _, plt = _require_mpl()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=_DPI, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    png_bytes = buf.read()
    sha = "sha256-" + hashlib.sha256(png_bytes).hexdigest()[:16]
    img = Image(data=png_bytes, format="png")
    img._hash = sha  # type: ignore[attr-defined]
    return img


def _make_fig(run_id: str, title: str) -> tuple:
    """Create a standard-size figure with the given title."""
    _, plt = _require_mpl()
    fig, ax = plt.subplots(figsize=(_FIG_WIDTH_IN, _FIG_HEIGHT_IN))
    fig.suptitle(f"{title}\n(run_id: {run_id})", fontsize=9, y=0.98)
    return fig, ax


# ---------------------------------------------------------------------------
# Plot: lift_distribution
# ---------------------------------------------------------------------------


def plot_lift_distribution(run_id: str, results: dict, case_name: str = "") -> dict:
    """Plot spanwise sectional Cl distribution.

    Looks for ``sectional_data.Cl`` (list of floats) and
    ``sectional_data.y_span`` (list of floats, normalised span stations).

    Falls back to a bar chart of per-surface CL if sectional data is absent.
    """
    _require_mpl()
    import matplotlib.pyplot as plt

    title = f"Lift Distribution — {case_name}" if case_name else "Lift Distribution"
    fig, ax = _make_fig(run_id, title)

    sectional = results.get("sectional_data", {})
    Cl = sectional.get("Cl")
    y = sectional.get("y_span_norm")

    if Cl and y and len(Cl) == len(y):
        ax.plot(y, Cl, "b-o", markersize=3, linewidth=1.5)
        ax.set_xlabel("Normalised spanwise station η = 2y/b  [—]")
        ax.set_ylabel("Sectional lift coefficient  Cl  [—]")
        ax.set_xlim(0, 1)
        cl_min, cl_max = min(Cl), max(Cl)
        ax.set_title(
            f"Cl ∈ [{cl_min:.3f}, {cl_max:.3f}]", fontsize=8
        )
    else:
        # Fallback: per-surface bar chart
        surfaces = results.get("surfaces", {})
        names = list(surfaces.keys())
        cls = [surfaces[n].get("CL", 0.0) for n in names]
        ax.bar(names, cls, color="steelblue", edgecolor="navy", linewidth=0.8)
        ax.set_xlabel("Surface")
        ax.set_ylabel("CL  [—]")
        ax.set_title("Per-surface CL (sectional data not available)", fontsize=8)

    ax.grid(True, alpha=0.3)
    fig.tight_layout(rect=[0, 0, 1, 0.93])
    return _fig_to_response(fig, run_id, "lift_distribution")


# ---------------------------------------------------------------------------
# Plot: drag_polar
# ---------------------------------------------------------------------------


def plot_drag_polar(run_id: str, results: dict, case_name: str = "") -> dict:
    """Plot CL vs CD and L/D vs alpha side-by-side."""
    _require_mpl()
    import matplotlib.pyplot as plt

    title = f"Drag Polar — {case_name}" if case_name else "Drag Polar"
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(_FIG_WIDTH_IN, _FIG_HEIGHT_IN))
    fig.suptitle(f"{title}\n(run_id: {run_id})", fontsize=9, y=0.98)

    alphas = results.get("alpha_deg", [])
    CLs = results.get("CL", [])
    CDs = results.get("CD", [])
    LoDs = results.get("L_over_D", [])

    # Panel 1: CL vs CD (drag polar)
    ax1.plot(CDs, CLs, "b-o", markersize=3, linewidth=1.5)
    ax1.set_xlabel("CD  [—]")
    ax1.set_ylabel("CL  [—]")
    ax1.set_title("CL vs CD", fontsize=8)
    if CDs and CLs:
        ax1.set_title(
            f"CL ∈ [{min(CLs):.3f}, {max(CLs):.3f}], CD ∈ [{min(CDs):.4f}, {max(CDs):.4f}]",
            fontsize=7,
        )
    ax1.grid(True, alpha=0.3)

    # Highlight best L/D
    best = results.get("best_L_over_D", {})
    if best and best.get("CL") is not None and best.get("CD") is not None:
        ax1.plot(
            best["CD"], best["CL"], "r*", markersize=10,
            label=f"Best L/D = {best.get('L_over_D', '?'):.2f}",
            zorder=5,
        )
        ax1.legend(fontsize=7)

    # Panel 2: L/D vs alpha
    valid = [(a, ld) for a, ld in zip(alphas, LoDs) if ld is not None]
    if valid:
        a_vals, ld_vals = zip(*valid)
        ax2.plot(a_vals, ld_vals, "g-o", markersize=3, linewidth=1.5)
    ax2.set_xlabel("α  [deg]")
    ax2.set_ylabel("L/D  [—]")
    ax2.set_title("L/D vs α", fontsize=8)
    ax2.axhline(0, color="k", linewidth=0.5, linestyle="--")
    ax2.grid(True, alpha=0.3)

    fig.tight_layout(rect=[0, 0, 1, 0.93])
    return _fig_to_response(fig, run_id, "drag_polar")


# ---------------------------------------------------------------------------
# Plot: stress_distribution
# ---------------------------------------------------------------------------


def plot_stress_distribution(run_id: str, results: dict, case_name: str = "") -> dict:
    """Plot spanwise von Mises stress and failure index distribution.

    Looks for per-surface ``sectional_data.vonmises_MPa`` and
    ``sectional_data.failure_index``.  Falls back to scalar values if arrays
    are unavailable.
    """
    _require_mpl()
    import matplotlib.pyplot as plt

    title = f"Stress Distribution — {case_name}" if case_name else "Stress Distribution"
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(_FIG_WIDTH_IN, _FIG_HEIGHT_IN))
    fig.suptitle(f"{title}\n(run_id: {run_id})", fontsize=9, y=0.98)

    plotted = False
    for surf_name, surf_res in results.get("surfaces", {}).items():
        sectional = surf_res.get("sectional_data", {})
        y = sectional.get("y_span_norm")
        vm = sectional.get("vonmises_MPa")
        fi = sectional.get("failure_index")

        if y and vm and len(y) == len(vm):
            ax1.plot(y, vm, label=surf_name, linewidth=1.5)
            plotted = True
        else:
            max_vm = surf_res.get("max_vonmises_Pa")
            if max_vm is not None:
                ax1.axhline(
                    max_vm / 1e6, linestyle="--",
                    label=f"{surf_name} max={max_vm/1e6:.1f} MPa",
                    linewidth=1.5,
                )
                plotted = True

        if y and fi and len(y) == len(fi):
            ax2.plot(y, fi, label=surf_name, linewidth=1.5)
        else:
            failure = surf_res.get("failure")
            if failure is not None:
                ax2.axhline(
                    failure, linestyle="--",
                    label=f"{surf_name} failure={failure:.3f}",
                    linewidth=1.5,
                )

    ax1.set_xlabel("Normalised spanwise station η  [—]")
    ax1.set_ylabel("von Mises stress  [MPa]")
    ax1.set_title("Von Mises Stress", fontsize=8)
    ax1.legend(fontsize=7)
    ax1.grid(True, alpha=0.3)

    ax2.axhline(1.0, color="red", linewidth=1.0, linestyle="--", label="Failure threshold")
    ax2.set_xlabel("Normalised spanwise station η  [—]")
    ax2.set_ylabel("Failure index  [—]")
    ax2.set_title("Structural Failure Index", fontsize=8)
    ax2.legend(fontsize=7)
    ax2.grid(True, alpha=0.3)

    if not plotted:
        ax1.text(0.5, 0.5, "No stress data available", transform=ax1.transAxes,
                 ha="center", va="center", fontsize=10, color="gray")

    fig.tight_layout(rect=[0, 0, 1, 0.93])
    return _fig_to_response(fig, run_id, "stress_distribution")


# ---------------------------------------------------------------------------
# Plot: convergence
# ---------------------------------------------------------------------------


def plot_convergence(run_id: str, convergence_data: dict, case_name: str = "") -> dict:
    """Plot solver residual history.

    Parameters
    ----------
    convergence_data:
        Dict with keys ``residual_trace`` (list of floats) and optionally
        ``converged`` (bool), ``iterations`` (int), ``final_residual`` (float).
    """
    _require_mpl()
    import matplotlib.pyplot as plt

    title = f"Convergence — {case_name}" if case_name else "Convergence History"
    fig, ax = _make_fig(run_id, title)

    trace = convergence_data.get("residual_trace", [])
    converged = convergence_data.get("converged", None)
    final = convergence_data.get("final_residual")

    if trace:
        iters = list(range(len(trace)))
        ax.semilogy(iters, trace, "b-o", markersize=3, linewidth=1.5)
        ax.set_xlabel("Iteration  [—]")
        ax.set_ylabel("Residual norm  [—]")
        status = "converged" if converged else ("not converged" if converged is False else "")
        ax.set_title(f"Final residual: {final:.3e}  {status}" if final else "", fontsize=8)
    else:
        # No trace available — show summary only
        msg = (
            f"Solver: {convergence_data.get('solver_type', 'unknown')}\n"
            f"Iterations: {convergence_data.get('iterations', '?')}\n"
            f"Converged: {converged}\n"
            f"Final residual: {final}"
        )
        ax.text(0.5, 0.5, msg, transform=ax.transAxes,
                ha="center", va="center", fontsize=10,
                bbox={"facecolor": "lightyellow", "alpha": 0.8, "edgecolor": "gray"})
        ax.set_title("Residual trace not captured (opt-in: set capture_trace=True)", fontsize=8)
        ax.axis("off")

    ax.grid(True, alpha=0.3)
    fig.tight_layout(rect=[0, 0, 1, 0.93])
    return _fig_to_response(fig, run_id, "convergence")


# ---------------------------------------------------------------------------
# Plot: planform
# ---------------------------------------------------------------------------


def plot_planform(run_id: str, mesh_data: dict, case_name: str = "") -> dict:
    """Plot wing planform (top view) with optional deflection overlay.

    Parameters
    ----------
    mesh_data:
        Dict with ``mesh`` (list of shape [nx, ny, 3]) and optionally
        ``def_mesh`` (deformed mesh list of same shape) for deflection overlay.
    """
    _require_mpl()
    import matplotlib.pyplot as plt

    title = f"Planform — {case_name}" if case_name else "Wing Planform"
    fig, ax = _make_fig(run_id, title)

    mesh_list = mesh_data.get("mesh")
    def_mesh_list = mesh_data.get("def_mesh")

    if mesh_list is None:
        ax.text(0.5, 0.5, "Mesh data not available in artifact.\n"
                "Call get_detailed_results(run_id, 'standard') first.",
                transform=ax.transAxes, ha="center", va="center",
                fontsize=9, color="gray")
        ax.axis("off")
        fig.tight_layout(rect=[0, 0, 1, 0.93])
        return _fig_to_response(fig, run_id, "planform")

    mesh = np.array(mesh_list)
    nx, ny, _ = mesh.shape

    # Draw leading and trailing edges
    le = mesh[0, :, :]   # leading edge nodes
    te = mesh[-1, :, :]  # trailing edge nodes

    ax.plot(le[:, 1], le[:, 0], "b-", linewidth=1.5, label="LE (undeformed)")
    ax.plot(te[:, 1], te[:, 0], "b--", linewidth=1.0, label="TE (undeformed)")
    ax.plot([le[0, 1], te[0, 1]], [le[0, 0], te[0, 0]], "b-", linewidth=0.8)   # root
    ax.plot([le[-1, 1], te[-1, 1]], [le[-1, 0], te[-1, 0]], "b-", linewidth=0.8)  # tip

    if def_mesh_list is not None:
        def_mesh = np.array(def_mesh_list)
        def_le = def_mesh[0, :, :]
        def_te = def_mesh[-1, :, :]
        ax.plot(def_le[:, 1], def_le[:, 0], "r-", linewidth=1.5, label="LE (deformed)", alpha=0.7)
        ax.plot(def_te[:, 1], def_te[:, 0], "r--", linewidth=1.0, alpha=0.7)

    ax.set_xlabel("Spanwise y  [m]")
    ax.set_ylabel("Chordwise x  [m]")
    ax.set_title(f"Mesh: {nx}×{ny} nodes", fontsize=8)
    ax.set_aspect("equal")
    ax.legend(fontsize=7, loc="upper left")
    ax.grid(True, alpha=0.3)
    fig.tight_layout(rect=[0, 0, 1, 0.93])
    return _fig_to_response(fig, run_id, "planform")


# ---------------------------------------------------------------------------
# Plot: opt_history
# ---------------------------------------------------------------------------


def plot_opt_history(run_id: str, optimization_history: dict, case_name: str = "") -> Image:
    """Plot optimizer objective convergence history.

    Shows the objective value per optimizer iteration.  If only initial and
    final values are available (no per-iteration trace), displays a two-point
    comparison.

    Parameters
    ----------
    optimization_history:
        Dict from ``results.optimization_history`` with keys:
        ``objective_values`` (list[float]), ``num_iterations`` (int),
        ``initial_dvs`` (dict).
    """
    _require_mpl()
    import matplotlib.pyplot as plt

    title = f"Objective Convergence — {case_name}" if case_name else "Objective Convergence"
    fig, ax = _make_fig(run_id, title)

    obj_vals = optimization_history.get("objective_values", [])
    n_iter = optimization_history.get("num_iterations", 0)

    if obj_vals and len(obj_vals) > 1:
        iters = list(range(len(obj_vals)))
        ax.plot(iters, obj_vals, "b-o", markersize=4, linewidth=1.5)
        ax.set_xlabel("Optimizer iteration  [—]")
        ax.set_ylabel("Objective value  [—]")
        pct = 100.0 * (obj_vals[-1] - obj_vals[0]) / max(abs(obj_vals[0]), 1e-300)
        ax.set_title(
            f"Initial: {obj_vals[0]:.4g}   Final: {obj_vals[-1]:.4g}   "
            f"Change: {pct:+.1f}%",
            fontsize=8,
        )
    elif obj_vals:
        # Only one point recorded — show as a single marker with annotation
        ax.plot([0], obj_vals[:1], "bo", markersize=8)
        ax.set_xlabel("Optimizer iteration  [—]")
        ax.set_ylabel("Objective value  [—]")
        ax.set_title(f"Recorded: {obj_vals[0]:.4g}  (n_iter={n_iter})", fontsize=8)
    else:
        # No per-iteration data — show summary text
        msg = (
            f"No per-iteration objective trace captured.\n"
            f"Optimizer iterations: {n_iter}\n\n"
            "Run with a SqliteRecorder-enabled build to capture full history."
        )
        ax.text(0.5, 0.5, msg, transform=ax.transAxes,
                ha="center", va="center", fontsize=9, color="gray",
                bbox={"facecolor": "lightyellow", "alpha": 0.8, "edgecolor": "gray"})
        ax.axis("off")

    ax.grid(True, alpha=0.3)
    fig.tight_layout(rect=[0, 0, 1, 0.93])
    return _fig_to_response(fig, run_id, "opt_history")


# ---------------------------------------------------------------------------
# Plot: opt_dv_evolution
# ---------------------------------------------------------------------------


def plot_opt_dv_evolution(run_id: str, optimization_history: dict, case_name: str = "") -> Image:
    """Plot design variable evolution over optimizer iterations.

    For vector DVs (e.g. twist_cp), plots the mean of the DV vector per
    iteration.  For scalar DVs, plots the scalar value directly.

    Parameters
    ----------
    optimization_history:
        Dict from ``results.optimization_history`` with key
        ``dv_history`` (dict of DV name -> list of per-iteration values).
    """
    _require_mpl()
    import matplotlib.pyplot as plt

    title = f"DV Evolution — {case_name}" if case_name else "Design Variable Evolution"
    fig, ax = _make_fig(run_id, title)

    dv_history = optimization_history.get("dv_history", {})

    if not dv_history:
        # Fall back to showing initial vs final values
        initial = optimization_history.get("initial_dvs", {})
        # Try to get final from dv_history or signal absence
        ax.text(0.5, 0.5, "No per-iteration DV history captured.\n"
                "Use opt_comparison to see initial vs final values.",
                transform=ax.transAxes, ha="center", va="center",
                fontsize=9, color="gray")
        ax.axis("off")
        fig.tight_layout(rect=[0, 0, 1, 0.93])
        return _fig_to_response(fig, run_id, "opt_dv_evolution")

    colors = plt.rcParams["axes.prop_cycle"].by_key()["color"]
    for idx, (dv_name, history) in enumerate(dv_history.items()):
        if not history:
            continue
        color = colors[idx % len(colors)]
        iters = list(range(len(history)))
        # history[i] is either a scalar or a list (vector DV)
        try:
            means = [float(np.asarray(v).mean()) for v in history]
        except Exception:
            continue
        label = dv_name
        if isinstance(history[0], list) and len(history[0]) > 1:
            label = f"{dv_name} (mean)"
        ax.plot(iters, means, "-o", markersize=3, linewidth=1.5, label=label, color=color)

    ax.set_xlabel("Optimizer iteration  [—]")
    ax.set_ylabel("DV value  [various units]")
    ax.set_title(f"{len(dv_history)} design variable(s)", fontsize=8)
    ax.legend(fontsize=7, loc="best")
    ax.grid(True, alpha=0.3)
    fig.tight_layout(rect=[0, 0, 1, 0.93])
    return _fig_to_response(fig, run_id, "opt_dv_evolution")


# ---------------------------------------------------------------------------
# Plot: opt_comparison
# ---------------------------------------------------------------------------


def plot_opt_comparison(run_id: str, optimization_history: dict, case_name: str = "") -> Image:
    """Plot before/after comparison of design variable values.

    Generates a grouped bar chart with one group per DV, showing the initial
    value (or mean for vector DVs) alongside the final optimized value.

    Parameters
    ----------
    optimization_history:
        Dict from ``results.optimization_history`` with keys:
        ``initial_dvs`` (dict) and ``final_dvs`` (dict).
    """
    _require_mpl()
    import matplotlib.pyplot as plt

    title = f"Before/After DV Comparison — {case_name}" if case_name else "Before/After DV Comparison"
    fig, ax = _make_fig(run_id, title)

    initial = optimization_history.get("initial_dvs", {})
    final = optimization_history.get("final_dvs", {})

    # Merge keys from both dicts
    all_dvs = list({**initial, **final}.keys())

    if not all_dvs:
        ax.text(0.5, 0.5, "No initial/final DV data available.",
                transform=ax.transAxes, ha="center", va="center",
                fontsize=10, color="gray")
        ax.axis("off")
        fig.tight_layout(rect=[0, 0, 1, 0.93])
        return _fig_to_response(fig, run_id, "opt_comparison")

    def _scalar_mean(v) -> float:
        """Reduce a DV value (scalar or vector) to a representative float."""
        arr = np.asarray(v).ravel()
        return float(arr.mean())

    init_vals = [_scalar_mean(initial[k]) if k in initial else float("nan") for k in all_dvs]
    final_vals = [_scalar_mean(final[k]) if k in final else float("nan") for k in all_dvs]

    x = np.arange(len(all_dvs))
    width = 0.35
    bars_i = ax.bar(x - width / 2, init_vals, width, label="Initial", color="steelblue",
                    edgecolor="navy", linewidth=0.8, alpha=0.85)
    bars_f = ax.bar(x + width / 2, final_vals, width, label="Optimized", color="darkorange",
                    edgecolor="saddlebrown", linewidth=0.8, alpha=0.85)

    ax.set_xticks(x)
    ax.set_xticklabels(all_dvs, rotation=15, ha="right", fontsize=8)
    ax.set_ylabel("DV value (mean)  [various units]")
    ax.set_title("Mean DV value: initial vs optimized", fontsize=8)
    ax.legend(fontsize=7)
    ax.grid(True, alpha=0.3, axis="y")
    fig.tight_layout(rect=[0, 0, 1, 0.93])
    return _fig_to_response(fig, run_id, "opt_comparison")


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def generate_plot(
    plot_type: str,
    run_id: str,
    results: dict,
    convergence_data: dict | None = None,
    mesh_data: dict | None = None,
    case_name: str = "",
    optimization_history: dict | None = None,
) -> Image:
    """Generate a plot and return an MCP Image object.

    Parameters
    ----------
    plot_type:
        One of the values in ``PLOT_TYPES``.
    run_id:
        Artifact run ID — included in the plot title.
    results:
        Analysis results dict (from extract_*_results).
    convergence_data:
        Convergence dict — required for "convergence" plot type.
    mesh_data:
        Mesh dict — required for "planform" plot type.
    case_name:
        Human-readable label for the plot title.
    optimization_history:
        Optimization history dict — required for opt_history, opt_dv_evolution,
        and opt_comparison plot types.

    Returns
    -------
    ``mcp.server.fastmcp.utilities.types.Image`` — FastMCP auto-converts this
    to ``ImageContent`` so the plot is delivered as a proper image attachment.
    The SHA-256 hash is on ``img._hash`` for client-side caching.
    """
    if plot_type not in PLOT_TYPES:
        raise ValueError(
            f"Unknown plot_type {plot_type!r}. "
            f"Supported types: {sorted(PLOT_TYPES)}"
        )

    if plot_type == "lift_distribution":
        return plot_lift_distribution(run_id, results, case_name)
    elif plot_type == "drag_polar":
        return plot_drag_polar(run_id, results, case_name)
    elif plot_type == "stress_distribution":
        return plot_stress_distribution(run_id, results, case_name)
    elif plot_type == "convergence":
        return plot_convergence(run_id, convergence_data or {}, case_name)
    elif plot_type == "planform":
        return plot_planform(run_id, mesh_data or {}, case_name)
    elif plot_type == "opt_history":
        return plot_opt_history(run_id, optimization_history or {}, case_name)
    elif plot_type == "opt_dv_evolution":
        return plot_opt_dv_evolution(run_id, optimization_history or {}, case_name)
    elif plot_type == "opt_comparison":
        return plot_opt_comparison(run_id, optimization_history or {}, case_name)
    else:
        raise ValueError(f"Unhandled plot_type: {plot_type!r}")
