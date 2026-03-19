"""Optional lightweight HTTP viewer server for provenance graphs.

Runs as a background daemon thread on port 7654 (override via OAS_PROV_PORT).
Endpoints:
  GET /viewer                          — serves viewer/index.html
  GET /graph?session_id=               — JSON from get_session_graph()
  GET /sessions                        — JSON from list_sessions()
  GET /plot?run_id=&plot_type=         — PNG image for a saved run
  GET /plot_types?run_id=              — JSON list of applicable plot types

The server fails silently if the port is already in use.
"""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import numpy as np

from .db import _dumps, get_session_graph, list_sessions

_VIEWER_HTML = Path(__file__).parent / "viewer" / "index.html"
_DEFAULT_PORT = 7654

# Maps analysis_type to applicable plot types (for /plot_types endpoint)
_ANALYSIS_PLOT_TYPES: dict[str, list[str]] = {
    "aero":         ["lift_distribution", "planform"],
    "aerostruct":   ["lift_distribution", "stress_distribution", "planform"],
    "drag_polar":   ["drag_polar"],
    "stability":    ["lift_distribution"],
    "optimization": ["opt_history", "opt_dv_evolution", "opt_comparison", "planform"],
}


def _generate_plot_png(run_id: str, plot_type: str) -> bytes | None:
    """Load an artifact by run_id and return a rendered PNG as bytes.

    Returns None if the artifact is not found.
    Raises ValueError for invalid plot_type or if matplotlib is unavailable.
    """
    from oas_mcp.core.artifacts import ArtifactStore
    from oas_mcp.core.plotting import PLOT_TYPES, generate_plot

    if plot_type not in PLOT_TYPES or plot_type == "n2":
        raise ValueError(
            f"Unsupported plot_type {plot_type!r}. "
            f"Supported: {sorted(PLOT_TYPES - {'n2'})}"
        )

    store = ArtifactStore()
    artifact = store.get(run_id)
    if artifact is None:
        return None

    results = artifact.get("results", {})
    artifact_type = artifact.get("metadata", {}).get("analysis_type", "aero")

    # For optimization runs, aero results live inside final_results
    if artifact_type == "optimization":
        plot_results = dict(results.get("final_results", {}))
    else:
        plot_results = dict(results)

    standard = results.get("standard_detail", {})

    # Inject sectional_data into per-surface dicts for lift/stress plots
    if standard.get("sectional_data"):
        for surf_name, sect in standard["sectional_data"].items():
            if surf_name in plot_results.get("surfaces", {}):
                plot_results["surfaces"][surf_name]["sectional_data"] = sect
        plot_results["sectional_data"] = standard.get("sectional_data", {})

    # Build mesh_data for planform plot
    mesh_data: dict = {}
    mesh_snap = standard.get("mesh_snapshot", {})
    if mesh_snap:
        mesh_data["mesh_snapshot"] = mesh_snap
        for _surf_name, surf_mesh in mesh_snap.items():
            le = surf_mesh.get("leading_edge")
            te = surf_mesh.get("trailing_edge")
            if le and te:
                mesh_data["mesh"] = np.array([le, te]).tolist()
            break

    # Convergence data
    conv_data = results.get("convergence") or artifact.get("convergence") or {}

    # Optimization history for opt_* plots
    opt_history: dict | None = None
    if artifact_type == "optimization" or plot_type.startswith("opt_"):
        raw_hist = results.get("optimization_history", {})
        opt_history = {
            **raw_hist,
            "final_dvs": results.get("optimized_design_variables", {}),
        }

    plot_result = generate_plot(
        plot_type, run_id, plot_results, conv_data, mesh_data, "", opt_history
    )
    return plot_result.image.data


def _get_plot_types_for_run(run_id: str) -> list[str] | None:
    """Return applicable plot types for a run, or None if not found."""
    from oas_mcp.core.artifacts import ArtifactStore

    store = ArtifactStore()
    summary = store.get_summary(run_id)
    if summary is None:
        return None
    analysis_type = summary.get("analysis_type", "aero")
    return _ANALYSIS_PLOT_TYPES.get(analysis_type, ["lift_distribution", "planform"])


class _ProvHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002
        pass  # Suppress request logging to avoid noise in MCP stdio output

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path in ("/viewer", "/viewer/"):
            self._serve_file(_VIEWER_HTML, "text/html; charset=utf-8")
        elif path == "/graph":
            session_id = qs.get("session_id", [None])[0]
            if session_id is None:
                self._error(400, "Missing session_id query parameter")
                return
            try:
                graph = get_session_graph(session_id)
                self._json(graph)
            except Exception as exc:
                self._error(500, str(exc))
        elif path == "/sessions":
            try:
                sessions = list_sessions()
                self._json(sessions)
            except Exception as exc:
                self._error(500, str(exc))
        elif path == "/plot":
            run_id = qs.get("run_id", [None])[0]
            plot_type = qs.get("plot_type", [None])[0]
            if not run_id or not plot_type:
                self._error(400, "Missing run_id or plot_type query parameters")
                return
            try:
                png_bytes = _generate_plot_png(run_id, plot_type)
                if png_bytes is None:
                    self._error(404, f"Artifact not found: run_id={run_id!r}")
                else:
                    self._png(png_bytes)
            except ValueError as exc:
                self._error(400, str(exc))
            except Exception as exc:
                self._error(500, str(exc))
        elif path == "/plot_types":
            run_id = qs.get("run_id", [None])[0]
            if not run_id:
                self._error(400, "Missing run_id query parameter")
                return
            try:
                types = _get_plot_types_for_run(run_id)
                if types is None:
                    self._error(404, f"Artifact not found: run_id={run_id!r}")
                else:
                    self._json(types)
            except Exception as exc:
                self._error(500, str(exc))
        else:
            self._error(404, f"Not found: {path}")

    def _serve_file(self, path: Path, content_type: str) -> None:
        if not path.exists():
            self._error(404, f"File not found: {path}")
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj) -> None:
        data = _dumps(obj).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _png(self, data: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _error(self, code: int, message: str) -> None:
        data = json.dumps({"error": message}).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def start_viewer_server() -> int | None:
    """Start the viewer HTTP server in a background daemon thread.

    Returns the port number on success, or None if the port was busy.
    Disabled when ``OAS_PROV_VIEWER=off`` (recommended for production).
    """
    if os.environ.get("OAS_PROV_VIEWER", "").lower() == "off":
        return None
    port = int(os.environ.get("OAS_PROV_PORT", str(_DEFAULT_PORT)))
    # Default to localhost in production; set OAS_PROV_HOST=0.0.0.0 explicitly
    # if Docker port mapping is needed in dev.
    bind_host = os.environ.get("OAS_PROV_HOST", "127.0.0.1")
    try:
        server = HTTPServer((bind_host, port), _ProvHandler)
    except OSError:
        return None

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return port
