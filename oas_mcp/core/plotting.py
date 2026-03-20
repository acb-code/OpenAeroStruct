"""Matplotlib-based plot generation for OAS MCP results.

All plots return a ``PlotResult`` containing an MCP ``Image`` object and a
metadata dict.  FastMCP recursively converts a ``[dict, Image]`` list return
to ``[TextContent, ImageContent]``, so text-only clients (e.g. ChatGPT MCP
connector) receive the metadata while image-capable clients (Claude) also get
the rendered PNG.

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

import base64
import hashlib
import io
import json
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from mcp.server.fastmcp.utilities.types import Image


@dataclass
class N2Result:
    """Container for a generated N2 diagram saved to disk.

    ``metadata`` is a plain dict with file path, size, hash, and compressed
    viewer data — small enough to return as a single TextContent.
    ``file_path`` is the absolute path to the saved HTML file.
    """
    metadata: dict  # plot_type, format, file_path, size_bytes, image_hash, viewer_data_compressed
    file_path: str  # absolute path to the saved HTML file


@dataclass
class PlotResult:
    """Container for a generated plot and its metadata.

    ``image`` is an MCP Image object (FastMCP converts it to ImageContent).
    ``metadata`` is a plain dict suitable for TextContent serialisation so
    text-only MCP clients still receive structured plot information.
    """
    image: Image
    metadata: dict  # plot_type, run_id, format, width_px, height_px, image_hash, note

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
    "n2",
    "wing_viewer",
})

_FIG_WIDTH_IN = 6.0   # inches
_FIG_HEIGHT_IN = 3.6  # inches
_DPI = 150            # → 900 × 540 px


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fig_to_response(
    fig, run_id: str, plot_type: str, save_dir: str | Path | None = None,
) -> PlotResult:
    """Convert a matplotlib Figure to a PlotResult (Image + metadata dict).

    Pixel dimensions are captured before closing the figure so they reflect
    the actual rendered size (bbox_inches="tight" can adjust the canvas).
    The SHA-256 hash in the metadata is used for client-side caching.

    If *save_dir* is given, the PNG is also persisted to
    ``{save_dir}/plots/{run_id}_{plot_type}.png`` and ``file_path`` is added
    to the metadata dict.
    """
    _, plt = _require_mpl()
    # Capture dimensions *before* savefig/close — tight bbox may change them
    width_px = round(fig.get_size_inches()[0] * _DPI)
    height_px = round(fig.get_size_inches()[1] * _DPI)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=_DPI, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    png_bytes = buf.read()
    sha = "sha256-" + hashlib.sha256(png_bytes).hexdigest()[:16]
    img = Image(data=png_bytes, format="png")
    metadata = {
        "plot_type": plot_type,
        "run_id": run_id,
        "format": "png",
        "width_px": width_px,
        "height_px": height_px,
        "image_hash": sha,
        "note": (
            "Image attached as ImageContent. "
            "If not visible, use get_detailed_results() for the underlying data."
        ),
    }

    # Persist PNG to disk when save_dir is provided
    if save_dir is not None:
        plots_dir = Path(save_dir) / "plots"
        plots_dir.mkdir(parents=True, exist_ok=True)
        file_path = plots_dir / f"{run_id}_{plot_type}.png"
        file_path.write_bytes(png_bytes)
        metadata["file_path"] = str(file_path.resolve())

    return PlotResult(image=img, metadata=metadata)


def _make_fig(run_id: str, title: str) -> tuple:
    """Create a standard-size figure with the given title."""
    _, plt = _require_mpl()
    fig, ax = plt.subplots(figsize=(_FIG_WIDTH_IN, _FIG_HEIGHT_IN))
    fig.suptitle(f"{title}\n(run_id: {run_id})", fontsize=9, y=0.98)
    return fig, ax


# ---------------------------------------------------------------------------
# Plot: lift_distribution
# ---------------------------------------------------------------------------


def plot_lift_distribution(run_id: str, results: dict, case_name: str = "", *, save_dir: str | Path | None = None) -> PlotResult:
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
    chords = sectional.get("chords")

    # sectional_data is stored keyed by surface name: {"wing": {"Cl": [...], ...}}.
    # Fall through the nested dict to find the first surface with both arrays.
    if (Cl is None or y is None) and sectional:
        for surf_data in sectional.values():
            if isinstance(surf_data, dict):
                Cl = surf_data.get("Cl")
                y = surf_data.get("y_span_norm")
                if chords is None:
                    chords = surf_data.get("chords")
                if Cl and y:
                    break

    if Cl and y and (len(Cl) == len(y) or len(Cl) == len(y) - 1):
        if len(Cl) == len(y) - 1:
            # Cl has ny-1 panel values, y has ny node values → use panel midpoints
            y_plot = [(y[i] + y[i + 1]) / 2.0 for i in range(len(Cl))]
        else:
            y_plot = y

        if chords is not None and len(chords) >= len(Cl) + 1:
            # Compute lift loading: Cl·c (proportional to circulation)
            chord_panel = [(chords[i] + chords[i + 1]) / 2.0 for i in range(len(Cl))]
            loading = [cl * c for cl, c in zip(Cl, chord_panel)]

            # Sort by ascending η for correct integration and plotting
            eta = np.array(y_plot)
            loading_arr = np.array(loading)
            sort_idx = np.argsort(eta)
            eta_sorted = eta[sort_idx]
            loading_sorted = loading_arr[sort_idx]

            # Elliptical overlay: loading_ell = (4·area / π) · sqrt(1 - η²)
            _trapz = getattr(np, "trapezoid", None) or np.trapz
            area = abs(float(_trapz(loading_sorted, eta_sorted)))
            loading_ell = (4.0 * area / np.pi) * np.sqrt(np.maximum(1.0 - eta_sorted**2, 0.0))

            ax.plot(eta_sorted, loading_sorted, "b-o", markersize=3, linewidth=1.5, label="Actual loading")
            ax.plot(eta_sorted, loading_ell, "g--", linewidth=1.5, label="Elliptical (ideal)")
            ax.legend(fontsize=7)
            ax.set_xlabel("Normalised spanwise station η = 2y/b  [—]   (0 = root, 1 = tip)")
            ax.set_ylabel("Lift loading  Cl·c  [m]")
            ax.set_xlim(0, 1)
            ld_min, ld_max = float(loading_sorted.min()), float(loading_sorted.max())
            ax.set_title(
                f"Cl·c ∈ [{ld_min:.4f}, {ld_max:.4f}]", fontsize=8
            )
        else:
            # Fallback: Cl-only plot (backward compat with old artifacts)
            ax.plot(y_plot, Cl, "b-o", markersize=3, linewidth=1.5)
            ax.set_xlabel("Normalised spanwise station η = 2y/b  [—]   (0 = root, 1 = tip)")
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
    return _fig_to_response(fig, run_id, "lift_distribution", save_dir=save_dir)


# ---------------------------------------------------------------------------
# Plot: drag_polar
# ---------------------------------------------------------------------------


def plot_drag_polar(run_id: str, results: dict, case_name: str = "", *, save_dir: str | Path | None = None) -> PlotResult:
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
    return _fig_to_response(fig, run_id, "drag_polar", save_dir=save_dir)


# ---------------------------------------------------------------------------
# Plot: stress_distribution
# ---------------------------------------------------------------------------


def plot_stress_distribution(run_id: str, results: dict, case_name: str = "", *, save_dir: str | Path | None = None) -> PlotResult:
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

    def _elem_y(y_nodes: list, n_elem: int) -> list | None:
        """Map nodal y_span_norm to element midpoints.

        OAS stores stress/failure per beam element (ny-1 values) while
        y_span_norm comes from the ny mesh nodes.  Average adjacent nodes
        to get the element-centre η coordinate.
        """
        if len(y_nodes) == n_elem:
            return y_nodes
        if len(y_nodes) == n_elem + 1:
            return [(y_nodes[i] + y_nodes[i + 1]) / 2.0 for i in range(n_elem)]
        return None

    plotted = False
    for surf_name, surf_res in results.get("surfaces", {}).items():
        sectional = surf_res.get("sectional_data", {})
        y_nodes = sectional.get("y_span_norm")
        vm = sectional.get("vonmises_MPa")
        fi = sectional.get("failure_index")
        yield_stress_pa = sectional.get("yield_stress_Pa")

        if y_nodes and vm:
            y_vm = _elem_y(y_nodes, len(vm))
            if y_vm is not None:
                ax1.plot(y_vm, vm, label=surf_name, linewidth=1.5)
                plotted = True
                # Yield stress reference line
                if yield_stress_pa is not None:
                    ax1.axhline(
                        yield_stress_pa / 1e6, color="red", linewidth=1.0,
                        linestyle="--", label="Yield stress",
                    )
            else:
                max_vm = surf_res.get("max_vonmises_Pa")
                if max_vm is not None:
                    ax1.axhline(
                        max_vm / 1e6, linestyle="--",
                        label=f"{surf_name} max={max_vm/1e6:.1f} MPa",
                        linewidth=1.5,
                    )
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

        if y_nodes and fi:
            y_fi = _elem_y(y_nodes, len(fi))
            if y_fi is not None:
                ax2.plot(y_fi, fi, label=surf_name, linewidth=1.5)
            else:
                failure = surf_res.get("failure")
                if failure is not None:
                    ax2.axhline(
                        failure, linestyle="--",
                        label=f"{surf_name} failure={failure:.3f}",
                        linewidth=1.5,
                    )
        else:
            failure = surf_res.get("failure")
            if failure is not None:
                ax2.axhline(
                    failure, linestyle="--",
                    label=f"{surf_name} failure={failure:.3f}",
                    linewidth=1.5,
                )

    ax1.set_xlabel("Normalised spanwise station η  [—]   (0 = root, 1 = tip)")
    ax1.set_ylabel("von Mises stress  [MPa]")
    ax1.set_title("Von Mises Stress", fontsize=8)
    ax1.legend(fontsize=7)
    ax1.grid(True, alpha=0.3)

    ax2.axhline(1.0, color="red", linewidth=1.0, linestyle="--", label="Failure threshold")
    ax2.set_xlabel("Normalised spanwise station η  [—]   (0 = root, 1 = tip)")
    ax2.set_ylabel("Failure index  [—]")
    ax2.set_title("Structural Failure Index", fontsize=8)
    ax2.legend(fontsize=7)
    ax2.grid(True, alpha=0.3)

    if not plotted:
        ax1.text(0.5, 0.5, "No stress data available", transform=ax1.transAxes,
                 ha="center", va="center", fontsize=10, color="gray")

    fig.tight_layout(rect=[0, 0, 1, 0.93])
    return _fig_to_response(fig, run_id, "stress_distribution", save_dir=save_dir)


# ---------------------------------------------------------------------------
# Plot: convergence
# ---------------------------------------------------------------------------


def plot_convergence(run_id: str, convergence_data: dict, case_name: str = "", *, save_dir: str | Path | None = None) -> PlotResult:
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
    return _fig_to_response(fig, run_id, "convergence", save_dir=save_dir)


# ---------------------------------------------------------------------------
# Plot: planform
# ---------------------------------------------------------------------------


def plot_planform(run_id: str, mesh_data: dict, case_name: str = "", *, save_dir: str | Path | None = None) -> PlotResult:
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
    # Get original mesh dimensions from snapshot (the mesh array is 2×ny for LE/TE only)
    snap_nx, snap_ny = nx, ny
    for surf_snap in mesh_data.get("mesh_snapshot", {}).values():
        snap_nx = surf_snap.get("nx", nx)
        snap_ny = surf_snap.get("ny", ny)
        break
    ax.set_title(f"Mesh: {snap_nx}×{snap_ny} nodes", fontsize=8)
    # Standard math convention: LE (smaller x) at bottom, TE (larger x) at top.
    ax.text(0.02, 0.02, "Half-span shown (symmetry)", transform=ax.transAxes,
            fontsize=7, color="gray", va="bottom")
    ax.legend(fontsize=7, loc="upper left")
    ax.grid(True, alpha=0.3)
    fig.tight_layout(rect=[0, 0, 1, 0.93])
    return _fig_to_response(fig, run_id, "planform", save_dir=save_dir)


# ---------------------------------------------------------------------------
# Plot: opt_history
# ---------------------------------------------------------------------------


def plot_opt_history(run_id: str, optimization_history: dict, case_name: str = "", *, save_dir: str | Path | None = None) -> PlotResult:
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
    return _fig_to_response(fig, run_id, "opt_history", save_dir=save_dir)


# ---------------------------------------------------------------------------
# Plot: opt_dv_evolution
# ---------------------------------------------------------------------------


def plot_opt_dv_evolution(run_id: str, optimization_history: dict, case_name: str = "", *, save_dir: str | Path | None = None) -> PlotResult:
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
        # Normalize to initial value so mixed-unit DVs share one axis
        initial_val = means[0] if means else 0.0
        if abs(initial_val) > 1e-12:
            means_norm = [m / initial_val for m in means]
        else:
            means_norm = [1.0] * len(means)
        label = dv_name
        if isinstance(history[0], list) and len(history[0]) > 1:
            label = f"{dv_name} (mean)"
        ax.plot(iters, means_norm, "-o", markersize=3, linewidth=1.5, label=label, color=color)

    ax.axhline(1.0, color="gray", linewidth=0.8, linestyle="--", alpha=0.7)
    ax.set_xlabel("Optimizer iteration  [—]")
    ax.set_ylabel("DV / DV_initial  [—]")
    ax.set_title(f"{len(dv_history)} design variable(s)", fontsize=8)
    ax.legend(fontsize=7, loc="best")
    ax.grid(True, alpha=0.3)
    fig.tight_layout(rect=[0, 0, 1, 0.93])
    return _fig_to_response(fig, run_id, "opt_dv_evolution", save_dir=save_dir)


# ---------------------------------------------------------------------------
# Plot: opt_comparison
# ---------------------------------------------------------------------------


def plot_opt_comparison(run_id: str, optimization_history: dict, case_name: str = "", *, save_dir: str | Path | None = None) -> PlotResult:
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

    dv_history = optimization_history.get("dv_history", {})

    init_ratios = []
    final_ratios = []
    for k in all_dvs:
        # Prefer dv_history for physical values when available
        if k in dv_history and dv_history[k]:
            hist = dv_history[k]
            init_val = float(np.asarray(hist[0]).mean())
            final_val = float(np.asarray(hist[-1]).mean())
        else:
            init_val = _scalar_mean(initial[k]) if k in initial else float("nan")
            final_val = _scalar_mean(final[k]) if k in final else float("nan")
        # Normalize: initial is always 1.0; final is ratio to initial
        if abs(init_val) > 1e-12:
            init_ratios.append(1.0)
            final_ratios.append(final_val / init_val)
        else:
            init_ratios.append(1.0)
            final_ratios.append(float("nan"))

    x = np.arange(len(all_dvs))
    width = 0.35
    bars_i = ax.bar(x - width / 2, init_ratios, width, label="Initial", color="steelblue",
                    edgecolor="navy", linewidth=0.8, alpha=0.85)
    bars_f = ax.bar(x + width / 2, final_ratios, width, label="Optimized", color="darkorange",
                    edgecolor="saddlebrown", linewidth=0.8, alpha=0.85)

    ax.axhline(1.0, color="gray", linewidth=0.8, linestyle="--", alpha=0.7)
    ax.set_xticks(x)
    ax.set_xticklabels(all_dvs, rotation=15, ha="right", fontsize=8)
    ax.set_ylabel("DV / DV_initial  [—]")
    ax.set_title("Mean DV ratio: initial vs optimized", fontsize=8)
    ax.legend(fontsize=7)
    ax.grid(True, alpha=0.3, axis="y")
    fig.tight_layout(rect=[0, 0, 1, 0.93])
    return _fig_to_response(fig, run_id, "opt_comparison", save_dir=save_dir)


# ---------------------------------------------------------------------------
# Plot: wing_viewer (classic multi-panel view)
# ---------------------------------------------------------------------------


def plot_wing_viewer(run_id: str, results: dict, case_name: str = "", *, save_dir: str | Path | None = None) -> PlotResult:
    """Plot a classic multi-panel wing view (3D wireframe + distributions).

    Mimics the layout of ``plot_wing.py``: 3D mesh on the left, distribution
    panels on the right. Layout adapts based on available data (aero-only gets
    2 right panels; aerostruct gets 4).
    """
    _require_mpl()
    import matplotlib.pyplot as plt
    from mpl_toolkits.mplot3d import Axes3D  # noqa: F401 — registers projection

    # Gather data from sectional_data (first surface)
    sectional_all = results.get("sectional_data", {})
    if not sectional_all:
        # Try nested under surfaces
        for surf_res in results.get("surfaces", {}).values():
            sectional_all = surf_res.get("sectional_data", {})
            if sectional_all:
                break

    sect = {}
    for surf_data in (sectional_all.values() if sectional_all else []):
        if isinstance(surf_data, dict) and surf_data.get("mesh"):
            sect = surf_data
            break
    if not sect:
        # Fall back to first surface with any data
        for surf_data in (sectional_all.values() if sectional_all else []):
            if isinstance(surf_data, dict):
                sect = surf_data
                break

    mesh_list = sect.get("mesh")
    has_struct = bool(sect.get("vonmises_MPa"))
    n_right = 4 if has_struct else 2

    fig = plt.figure(figsize=(10, 6))
    fig.suptitle(
        f"{'Wing Viewer — ' + case_name if case_name else 'Wing Viewer'}\n(run_id: {run_id})",
        fontsize=9, y=0.99,
    )

    # Left half: 3D wireframe (spans rows 0..3, cols 0..3)
    ax3d = fig.add_subplot(1, 2, 1, projection="3d")

    if mesh_list is not None:
        mesh = np.array(mesh_list)
        # Mirror for symmetric display (matching plot_wing.py convention)
        mirror = mesh.copy()
        mirror[:, :, 1] *= -1.0
        mirror = mirror[:, ::-1, :][:, 1:, :]
        full_mesh = np.concatenate([mesh, mirror], axis=1)

        x, y, z = full_mesh[:, :, 0], full_mesh[:, :, 1], full_mesh[:, :, 2]
        ax3d.plot_wireframe(x, y, z, rstride=1, cstride=1, color="k", alpha=0.3)

        def_mesh_list = sect.get("def_mesh")
        if def_mesh_list is not None:
            def_mesh = np.array(def_mesh_list)
            mirror_d = def_mesh.copy()
            mirror_d[:, :, 1] *= -1.0
            mirror_d = mirror_d[:, ::-1, :][:, 1:, :]
            full_def = np.concatenate([def_mesh, mirror_d], axis=1)
            ax3d.plot_wireframe(
                full_def[:, :, 0], full_def[:, :, 1], full_def[:, :, 2],
                rstride=1, cstride=1, color="k", linewidth=0.8,
            )

        ax3d.set_axis_off()
    else:
        ax3d.text2D(0.5, 0.5, "No mesh data", transform=ax3d.transAxes,
                     ha="center", va="center", fontsize=9, color="gray")

    ax3d.set_title("3D Mesh", fontsize=8)

    # Right panels — data from sectional_data
    y_norm = np.array(sect.get("y_span_norm", []))
    Cl = np.array(sect.get("Cl", []))
    chords = np.array(sect.get("chords", []))
    twist = np.array(sect.get("twist_deg", []))
    thickness = np.array(sect.get("thickness", []))
    vm = np.array(sect.get("vonmises_MPa", []))
    fi = np.array(sect.get("failure_index", []))
    yield_stress_pa = sect.get("yield_stress_Pa")

    # Sort nodal data by ascending η (OAS stores tip→root, we want root→tip)
    if len(y_norm) > 0:
        node_sort = np.argsort(y_norm)
        y_sorted = y_norm[node_sort]
    else:
        node_sort = None
        y_sorted = y_norm

    # Element midpoint η (sorted ascending)
    if len(y_sorted) > 1:
        y_elem = (y_sorted[:-1] + y_sorted[1:]) / 2.0
    else:
        y_elem = np.array([])

    if has_struct:
        # 4-panel layout on the right
        ax_twist = fig.add_subplot(4, 2, 2)
        ax_lift = fig.add_subplot(4, 2, 4)
        ax_thick = fig.add_subplot(4, 2, 6)
        ax_stress = fig.add_subplot(4, 2, 8)
    else:
        # 2-panel layout
        ax_twist = fig.add_subplot(2, 2, 2)
        ax_lift = fig.add_subplot(2, 2, 4)
        ax_thick = None
        ax_stress = None

    # Twist panel (nodal values, ny)
    if len(twist) > 0 and len(twist) == len(y_norm) and node_sort is not None:
        ax_twist.plot(y_sorted, twist[node_sort], "b-o", markersize=2, linewidth=1.2)
    elif len(twist) > 0:
        ax_twist.plot(range(len(twist)), twist, "b-o", markersize=2, linewidth=1.2)
        ax_twist.set_xlabel("Station index", fontsize=7)
    ax_twist.set_ylabel("Twist [deg]", fontsize=7)
    ax_twist.set_title("Twist Distribution", fontsize=8)
    ax_twist.grid(True, alpha=0.3)
    ax_twist.tick_params(labelsize=6)

    # Lift loading panel (panel values, ny-1)
    if len(Cl) > 0 and len(y_norm) > 0 and len(Cl) == len(y_norm) - 1:
        if len(chords) >= len(Cl) + 1:
            # Sort chords by ascending η, then compute panel midpoints
            chords_sorted = chords[node_sort]
            chord_panel = (chords_sorted[:-1] + chords_sorted[1:]) / 2.0
            # Panel values: reverse if nodes were tip→root (descending η)
            is_reversed = node_sort[0] > node_sort[-1]
            Cl_sorted = Cl[::-1] if is_reversed else Cl
            loading = Cl_sorted * chord_panel
            _trapz = getattr(np, "trapezoid", None) or np.trapz
            area = abs(float(_trapz(loading, y_elem)))
            loading_ell = (4.0 * area / np.pi) * np.sqrt(np.maximum(1.0 - y_elem**2, 0.0))
            ax_lift.plot(y_elem, loading, "b-o", markersize=2, linewidth=1.2, label="Actual")
            ax_lift.plot(y_elem, loading_ell, "g--", linewidth=1.2, label="Elliptical")
            ax_lift.legend(fontsize=6)
            ax_lift.set_ylabel("Cl·c [m]", fontsize=7)
        else:
            ax_lift.plot(y_elem if len(y_elem) == len(Cl) else range(len(Cl)),
                         Cl, "b-o", markersize=2, linewidth=1.2)
            ax_lift.set_ylabel("Cl", fontsize=7)
    ax_lift.set_title("Lift Loading", fontsize=8)
    ax_lift.grid(True, alpha=0.3)
    ax_lift.tick_params(labelsize=6)

    # Thickness panel (element values, ny-1) — aerostruct only
    if ax_thick is not None:
        if len(thickness) > 0 and len(thickness) == len(y_elem):
            # Reverse if y_norm was tip→root
            thick_plot = thickness[::-1] if (node_sort is not None and node_sort[0] > node_sort[-1]) else thickness
            ax_thick.plot(y_elem, thick_plot, "b-o", markersize=2, linewidth=1.2)
        elif len(thickness) > 0:
            ax_thick.plot(range(len(thickness)), thickness, "b-o", markersize=2, linewidth=1.2)
        ax_thick.set_ylabel("Thickness [m]", fontsize=7)
        ax_thick.set_title("Tube Thickness", fontsize=8)
        ax_thick.grid(True, alpha=0.3)
        ax_thick.tick_params(labelsize=6)

    # Stress panel (element values, ny-1) — aerostruct only
    if ax_stress is not None:
        # Filter NaN values from vonmises
        vm_valid = vm[~np.isnan(vm)] if len(vm) > 0 else vm
        if len(vm_valid) > 0 and len(vm) == len(y_elem):
            vm_plot = vm[::-1] if (node_sort is not None and node_sort[0] > node_sort[-1]) else vm
            # Only plot non-NaN data
            mask = ~np.isnan(vm_plot)
            if np.any(mask):
                ax_stress.plot(y_elem[mask], vm_plot[mask], "b-o", markersize=2, linewidth=1.2, label="von Mises")
            if yield_stress_pa is not None:
                ax_stress.axhline(
                    yield_stress_pa / 1e6, color="red", linewidth=1.0,
                    linestyle="--", label="Yield stress",
                )
            ax_stress.set_ylim(bottom=0)
            if yield_stress_pa is not None:
                ax_stress.set_ylim(top=yield_stress_pa / 1e6 * 1.1)
            ax_stress.legend(fontsize=6)
        elif len(vm) > 0:
            ax_stress.plot(range(len(vm)), vm, "b-o", markersize=2, linewidth=1.2)
        ax_stress.set_ylabel("Stress [MPa]", fontsize=7)
        ax_stress.set_xlabel("η (0=root, 1=tip)", fontsize=7)
        ax_stress.set_title("Von Mises Stress", fontsize=8)
        ax_stress.grid(True, alpha=0.3)
        ax_stress.tick_params(labelsize=6)

    fig.tight_layout(rect=[0, 0, 1, 0.94])
    return _fig_to_response(fig, run_id, "wing_viewer", save_dir=save_dir)


# ---------------------------------------------------------------------------
# N2 / DSM diagram (HTML saved to disk)
# ---------------------------------------------------------------------------


from .artifacts import _NumpyEncoder as _ArtifactsEncoder


class _NumpyEncoder(_ArtifactsEncoder):
    """JSON encoder for numpy types; extends artifacts encoder with OTel/complex support."""

    def default(self, obj: Any) -> Any:
        if isinstance(obj, np.ndarray):
            if np.issubdtype(obj.dtype, np.complexfloating):
                return obj.real.tolist()
            return obj.tolist()
        if isinstance(obj, np.complexfloating):
            return float(obj.real)
        if isinstance(obj, complex):
            return obj.real
        # Catch-all: type objects, enums, or other unserializable values from
        # OpenMDAO viewer data — convert to string rather than crash.
        try:
            return super().default(obj)
        except TypeError:
            return str(obj)


def generate_n2(
    prob,
    run_id: str,
    case_name: str = "",
    output_dir: str | Path | None = None,
) -> N2Result:
    """Generate an interactive N2 (Design Structure Matrix) diagram saved to disk.

    Calls ``openmdao.api.n2()`` to write a self-contained HTML file and
    extracts compressed viewer data for lightweight metadata delivery.

    Parameters
    ----------
    prob:
        A set-up (and ideally run) ``openmdao.api.Problem`` instance.
    run_id:
        Artifact run ID — used to name the file and included in metadata.
    case_name:
        Optional human-readable label used as the diagram title.
    output_dir:
        Directory to write the HTML file.  Falls back to ``./oas_data/n2/``.

    Returns
    -------
    N2Result
        ``metadata`` dict (small, ~15 KB) with ``file_path``, ``size_bytes``,
        ``image_hash``, and ``viewer_data_compressed`` (base64 zlib ~11 KB).
        ``file_path`` is the absolute path to the saved HTML file.
    """
    import openmdao.api as om
    from openmdao.visualization.n2_viewer.n2_viewer import _get_viewer_data

    out_dir = Path(output_dir) if output_dir is not None else Path("./oas_data/n2")
    out_dir.mkdir(parents=True, exist_ok=True)
    output_path = out_dir / f"n2_{run_id}.html"

    title = case_name or run_id
    om.n2(prob, outfile=str(output_path), show_browser=False, embeddable=False, title=title)

    html_bytes = output_path.read_bytes()
    sha = "sha256-" + hashlib.sha256(html_bytes).hexdigest()[:16]

    # Extract model data dict and compress it for lightweight delivery
    viewer_data = _get_viewer_data(prob, values=True)
    compressed = base64.b64encode(
        zlib.compress(json.dumps(viewer_data, cls=_NumpyEncoder).encode())
    ).decode()

    metadata = {
        "plot_type": "n2",
        "run_id": run_id,
        "format": "html_file",
        "file_path": str(output_path.resolve()),
        "size_bytes": len(html_bytes),
        "image_hash": sha,
        "viewer_data_compressed": compressed,
        "note": (
            f"Interactive N2 diagram saved to {output_path.resolve()}. "
            "Open in a browser to explore."
        ),
    }
    return N2Result(metadata=metadata, file_path=str(output_path.resolve()))


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
    save_dir: str | Path | None = None,
) -> PlotResult:
    """Generate a plot and return a PlotResult (Image + metadata).

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
    save_dir:
        If provided, the PNG is also saved to
        ``{save_dir}/plots/{run_id}_{plot_type}.png`` and ``file_path`` is
        added to the metadata.

    Returns
    -------
    PlotResult — contains ``image`` (MCP Image, converts to ImageContent) and
    ``metadata`` (plain dict for TextContent / text-only clients).
    """
    if plot_type not in PLOT_TYPES:
        raise ValueError(
            f"Unknown plot_type {plot_type!r}. "
            f"Supported types: {sorted(PLOT_TYPES)}"
        )

    if plot_type == "n2":
        raise ValueError(
            "plot_type='n2' must be handled in server.py via generate_n2(), "
            "not through generate_plot()."
        )

    if plot_type == "lift_distribution":
        return plot_lift_distribution(run_id, results, case_name, save_dir=save_dir)
    elif plot_type == "drag_polar":
        return plot_drag_polar(run_id, results, case_name, save_dir=save_dir)
    elif plot_type == "stress_distribution":
        return plot_stress_distribution(run_id, results, case_name, save_dir=save_dir)
    elif plot_type == "convergence":
        return plot_convergence(run_id, convergence_data or {}, case_name, save_dir=save_dir)
    elif plot_type == "planform":
        return plot_planform(run_id, mesh_data or {}, case_name, save_dir=save_dir)
    elif plot_type == "opt_history":
        return plot_opt_history(run_id, optimization_history or {}, case_name, save_dir=save_dir)
    elif plot_type == "opt_dv_evolution":
        return plot_opt_dv_evolution(run_id, optimization_history or {}, case_name, save_dir=save_dir)
    elif plot_type == "opt_comparison":
        return plot_opt_comparison(run_id, optimization_history or {}, case_name, save_dir=save_dir)
    elif plot_type == "wing_viewer":
        return plot_wing_viewer(run_id, results, case_name, save_dir=save_dir)
    else:
        raise ValueError(f"Unhandled plot_type: {plot_type!r}")
