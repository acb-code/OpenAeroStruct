"""Provider-based telemetry for the OAS MCP Server.

Features
--------
- Structured logging with correlation IDs (run_id, session_id, tool_name)
- Per-run log capture so agents can call ``get_last_logs(run_id)``
- Redaction: arrays are replaced with shape/hash summaries; no raw geometry
- Optional OpenTelemetry integration (off by default; enable via env var)
- Semantic attribute conventions:
    mcp.tool.name, oas.surface.count, oas.mesh.nx, oas.mesh.ny,
    oas.solver.converged, oas.cache.hit

Environment variables
---------------------
OAS_TELEMETRY_MODE: "off" (default) | "logging" | "otel"
OAS_LOG_LEVEL:      "DEBUG" | "INFO" (default) | "WARNING" | "ERROR"
OAS_LOG_MAX_RUNS:   max number of run log buffers to retain (default: 100)
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import Lock
from typing import Any

import numpy as np

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_TELEMETRY_MODE = os.environ.get("OAS_TELEMETRY_MODE", "logging").lower()
_LOG_LEVEL = getattr(logging, os.environ.get("OAS_LOG_LEVEL", "INFO").upper(), logging.INFO)
_MAX_RUNS = int(os.environ.get("OAS_LOG_MAX_RUNS", "100"))

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

logger = logging.getLogger("oas_mcp")
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s [%(name)s] %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )
    logger.addHandler(_handler)
    logger.setLevel(_LOG_LEVEL)
    logger.propagate = False


# ---------------------------------------------------------------------------
# Per-run log capture
# ---------------------------------------------------------------------------


@dataclass
class _RunLogBuffer:
    run_id: str
    session_id: str
    tool_name: str
    records: list[dict] = field(default_factory=list)


class _RunLogHandler(logging.Handler):
    """In-memory handler that routes records to the active run buffer."""

    def __init__(self) -> None:
        super().__init__()
        self._lock = Lock()
        self._active: _RunLogBuffer | None = None
        self._store: deque[_RunLogBuffer] = deque(maxlen=_MAX_RUNS)
        self._by_run_id: dict[str, _RunLogBuffer] = {}

    def set_active(self, buf: _RunLogBuffer) -> None:
        with self._lock:
            self._active = buf

    def clear_active(self, buf: _RunLogBuffer) -> None:
        with self._lock:
            if self._active is buf:
                self._active = None
            # Evict oldest if needed (deque handles maxlen)
            self._store.append(buf)
            self._by_run_id[buf.run_id] = buf

    def emit(self, record: logging.LogRecord) -> None:
        with self._lock:
            if self._active is not None:
                from datetime import datetime, timezone
                ts = datetime.fromtimestamp(record.created, tz=timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%S"
                )
                self._active.records.append({
                    "time": ts,
                    "level": record.levelname,
                    "message": record.getMessage(),
                    "logger": record.name,
                })

    def get_logs(self, run_id: str) -> list[dict] | None:
        with self._lock:
            buf = self._by_run_id.get(run_id)
            if buf is None and self._active and self._active.run_id == run_id:
                buf = self._active
            return list(buf.records) if buf is not None else None


_run_log_handler = _RunLogHandler()
logger.addHandler(_run_log_handler)


# ---------------------------------------------------------------------------
# Redaction helpers
# ---------------------------------------------------------------------------


def _redact_array(arr: Any) -> dict:
    """Replace a numpy array with a shape/stats summary (never raw values)."""
    a = np.asarray(arr)
    h = hashlib.sha256(a.tobytes()).hexdigest()[:8]
    summary: dict[str, Any] = {"shape": list(a.shape), "hash": h}
    if a.size > 0:
        summary["min"] = float(a.min())
        summary["max"] = float(a.max())
        summary["mean"] = float(a.mean())
    return summary


def redact(obj: Any, max_depth: int = 4) -> Any:
    """Recursively redact numpy arrays from a nested object.

    Arrays → shape/hash/stats summary.  Other values pass through.
    """
    if max_depth <= 0:
        return "..."
    if isinstance(obj, np.ndarray):
        return _redact_array(obj)
    if isinstance(obj, dict):
        return {k: redact(v, max_depth - 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        if len(obj) > 20:
            return f"[list of {len(obj)} items]"
        return [redact(v, max_depth - 1) for v in obj]
    return obj


# ---------------------------------------------------------------------------
# Span context manager
# ---------------------------------------------------------------------------


@contextmanager
def span(
    tool_name: str,
    run_id: str,
    session_id: str = "default",
    attributes: dict | None = None,
):
    """Context manager that brackets an OAS tool call with timing + logs.

    Emits structured log records on entry and exit.  If OAS_TELEMETRY_MODE
    includes "otel", also creates an OpenTelemetry span (when available).

    Yields
    ------
    dict  — mutable attributes dict; callers can add keys during the span.
    """
    attrs: dict[str, Any] = {
        "mcp.tool.name": tool_name,
        "oas.run_id": run_id,
        "oas.session_id": session_id,
        **(attributes or {}),
    }

    buf = _RunLogBuffer(run_id=run_id, session_id=session_id, tool_name=tool_name)
    _run_log_handler.set_active(buf)

    t0 = time.perf_counter()
    logger.info("START %s run_id=%s session=%s", tool_name, run_id, session_id)

    otel_span = None
    if _TELEMETRY_MODE == "otel":
        try:
            from opentelemetry import trace as _trace
            tracer = _trace.get_tracer("oas_mcp")
            otel_span = tracer.start_span(tool_name)
            for k, v in attrs.items():
                otel_span.set_attribute(k, str(v))
        except ImportError:
            pass

    try:
        yield attrs
        elapsed = time.perf_counter() - t0
        attrs["elapsed_s"] = round(elapsed, 4)
        logger.info(
            "END   %s run_id=%s elapsed=%.3fs",
            tool_name, run_id, elapsed,
        )
    except Exception as exc:
        elapsed = time.perf_counter() - t0
        attrs["elapsed_s"] = round(elapsed, 4)
        attrs["error"] = str(exc)
        logger.error(
            "ERROR %s run_id=%s elapsed=%.3fs error=%r",
            tool_name, run_id, elapsed, str(exc),
        )
        if otel_span:
            otel_span.record_exception(exc)
        raise
    finally:
        _run_log_handler.clear_active(buf)
        if otel_span:
            otel_span.end()


def get_run_logs(run_id: str) -> list[dict] | None:
    """Return captured log records for *run_id*, or None if not found."""
    return _run_log_handler.get_logs(run_id)


# ---------------------------------------------------------------------------
# Telemetry summary builder
# ---------------------------------------------------------------------------


def make_telemetry(
    elapsed_s: float,
    cache_hit: bool,
    surface_count: int = 0,
    mesh_shape: tuple[int, int, int] | None = None,
    extra: dict | None = None,
) -> dict:
    """Build a telemetry block for the response envelope.

    Parameters
    ----------
    elapsed_s:
        Wall-clock seconds for the analysis.
    cache_hit:
        Whether the OpenMDAO problem was retrieved from cache.
    surface_count:
        Number of surfaces analysed.
    mesh_shape:
        (nx, ny, 3) shape of the first surface mesh, if available.
    extra:
        Additional key/value pairs to include.
    """
    telem: dict[str, Any] = {
        "elapsed_s": round(elapsed_s, 4),
        "oas.cache.hit": cache_hit,
        "oas.surface.count": surface_count,
    }
    if mesh_shape is not None:
        telem["oas.mesh.nx"] = mesh_shape[0]
        telem["oas.mesh.ny"] = mesh_shape[1]
    if extra:
        telem.update(extra)
    return telem
