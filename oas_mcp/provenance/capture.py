"""@capture_tool decorator for automatic provenance recording.

Usage::

    @mcp.tool()
    @capture_tool        # inner decorator — @mcp.tool() sees the original signature
    async def some_tool(...):
        ...

Session ID resolution
---------------------
FastMCP dispatches each tool call as an independent asyncio task, so a
ContextVar set by ``start_session`` is not visible in subsequent tool calls.
We solve this with two layers:

1. ``_server_session_id`` — module-level string, shared across all tasks in
   the process.  Set by ``start_session`` and read by ``capture_tool``.
2. ``_prov_session_id`` — ContextVar used *only* by tests (via the
   ``isolate_provenance`` fixture) to override the module-level value without
   affecting other threads.  Takes priority when non-empty.

``_get_session_id()`` implements the precedence: ContextVar → module-level.
"""

from __future__ import annotations

import functools
import inspect
import time
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

from .db import _next_seq, record_tool_call, _dumps

# ---------------------------------------------------------------------------
# Session ID state
# ---------------------------------------------------------------------------

# Module-level: persists across asyncio task boundaries (server use)
_server_session_id: str = "default"

# ContextVar: overrides module-level only when explicitly set (test isolation)
_prov_session_id: ContextVar[str] = ContextVar("_prov_session_id", default="")


def _get_session_id() -> str:
    """Return the active provenance session ID.

    Priority: ContextVar (tests) > module-level (server).
    """
    ctx = _prov_session_id.get()
    return ctx if ctx else _server_session_id


def set_server_session_id(session_id: str) -> None:
    """Set the module-level session ID (called by start_session tool)."""
    global _server_session_id
    _server_session_id = session_id


# ---------------------------------------------------------------------------
# JSON serialiser
# ---------------------------------------------------------------------------


def _safe_json(kwargs: dict) -> str:
    """Serialise kwargs to JSON with str() fallback for un-serialisable objects."""
    return _dumps(kwargs)


# ---------------------------------------------------------------------------
# Decorator
# ---------------------------------------------------------------------------


def capture_tool(fn):
    """Wrap *fn* so every call is recorded in the provenance DB."""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        call_id = str(uuid.uuid4())
        session_id = _get_session_id()
        tool_name = fn.__name__
        started_at = datetime.now(timezone.utc).isoformat()
        t0 = time.perf_counter()

        inputs_json = _safe_json(kwargs)
        outputs_json: str | None = None
        status = "ok"
        error_msg: str | None = None
        result = None

        try:
            result = await fn(*args, **kwargs)
        except Exception as exc:
            status = "error"
            error_msg = str(exc)
            raise
        else:
            # Inject _provenance into the returned dict so Claude can pass
            # call_id to log_decision as prior_call_id.
            if isinstance(result, dict):
                result["_provenance"] = {
                    "call_id": call_id,
                    "session_id": session_id,
                }
            try:
                outputs_json = _safe_json(
                    result if isinstance(result, dict) else {"result": str(result)}
                )
            except Exception:
                outputs_json = None
            return result
        finally:
            duration_s = time.perf_counter() - t0
            try:
                from .db import _db_path
                if _db_path is not None:
                    seq = _next_seq(session_id)
                    record_tool_call(
                        call_id,
                        session_id,
                        seq,
                        tool_name,
                        inputs_json,
                        outputs_json,
                        status,
                        error_msg,
                        started_at,
                        duration_s,
                    )
            except Exception:
                pass  # Never swallow the original exception

    # Preserve the original function's signature for FastMCP introspection
    wrapper.__signature__ = inspect.signature(fn, eval_str=True)
    return wrapper
