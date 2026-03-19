"""Starlette sub-application serving the provenance DAG viewer.

Mounted on the main HTTP app (port 8000) when ``--transport http`` is active
and ``OAS_VIEWER_USER`` + ``OAS_VIEWER_PASSWORD`` env vars are both set.

Routes mirror those of the legacy ``viewer_server.py`` daemon thread so the
viewer HTML/JS works unchanged.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import secrets
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Basic Auth helpers
# ---------------------------------------------------------------------------

def _check_basic_auth(request: Request, username: str, password: str) -> bool:
    """Return True if the request carries a valid Basic Authorization header."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(auth[6:]).decode("utf-8")
    except Exception:
        return False
    parts = decoded.split(":", 1)
    if len(parts) != 2:
        return False
    return secrets.compare_digest(parts[0], username) and secrets.compare_digest(
        parts[1], password
    )


def _require_auth(handler):
    """Decorator that enforces Basic Auth on a Starlette endpoint."""

    async def wrapper(request: Request) -> Response:
        username = request.app.state.viewer_user
        password = request.app.state.viewer_password
        if not _check_basic_auth(request, username, password):
            return Response(
                content="Unauthorized",
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="OAS Viewer"'},
                media_type="text/plain",
            )
        return await handler(request)

    return wrapper


# ---------------------------------------------------------------------------
# Endpoint handlers
# ---------------------------------------------------------------------------

@_require_auth
async def viewer_html(request: Request) -> Response:
    """Serve the viewer/index.html page."""
    from oas_mcp.provenance.viewer_server import VIEWER_HTML

    if not VIEWER_HTML.exists():
        return Response("Viewer HTML not found", status_code=404, media_type="text/plain")
    content = await asyncio.to_thread(VIEWER_HTML.read_text, "utf-8")
    return HTMLResponse(content)


@_require_auth
async def sessions_endpoint(request: Request) -> Response:
    """Return JSON list of all provenance sessions."""
    from oas_mcp.provenance.db import _dumps, list_sessions

    sessions = await asyncio.to_thread(list_sessions)
    return Response(
        content=_dumps(sessions),
        status_code=200,
        media_type="application/json",
    )


@_require_auth
async def graph_endpoint(request: Request) -> Response:
    """Return JSON DAG for a given session_id."""
    from oas_mcp.provenance.db import _dumps, get_session_graph

    session_id = request.query_params.get("session_id")
    if not session_id:
        return JSONResponse({"error": "Missing session_id query parameter"}, status_code=400)
    try:
        graph = await asyncio.to_thread(get_session_graph, session_id)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
    return Response(
        content=_dumps(graph),
        status_code=200,
        media_type="application/json",
    )


@_require_auth
async def plot_endpoint(request: Request) -> Response:
    """Render a saved analysis run as a PNG image."""
    from oas_mcp.provenance.viewer_server import generate_plot_png

    run_id = request.query_params.get("run_id")
    plot_type = request.query_params.get("plot_type")
    if not run_id or not plot_type:
        return JSONResponse(
            {"error": "Missing run_id or plot_type query parameters"}, status_code=400
        )
    try:
        png_bytes = await asyncio.to_thread(generate_plot_png, run_id, plot_type)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)

    if png_bytes is None:
        return JSONResponse(
            {"error": f"Artifact not found: run_id={run_id!r}"}, status_code=404
        )
    return Response(content=png_bytes, status_code=200, media_type="image/png")


@_require_auth
async def plot_types_endpoint(request: Request) -> Response:
    """Return JSON list of applicable plot types for a run."""
    from oas_mcp.provenance.viewer_server import get_plot_types_for_run

    run_id = request.query_params.get("run_id")
    if not run_id:
        return JSONResponse({"error": "Missing run_id query parameter"}, status_code=400)
    try:
        types = await asyncio.to_thread(get_plot_types_for_run, run_id)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)

    if types is None:
        return JSONResponse(
            {"error": f"Artifact not found: run_id={run_id!r}"}, status_code=404
        )
    return JSONResponse(types)


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def build_viewer_app() -> Starlette | None:
    """Build and return the viewer Starlette app, or None if auth is unconfigured.

    When either ``OAS_VIEWER_USER`` or ``OAS_VIEWER_PASSWORD`` is unset the
    viewer is intentionally **not** mounted to avoid accidental public exposure.
    """
    viewer_user = os.environ.get("OAS_VIEWER_USER", "")
    viewer_password = os.environ.get("OAS_VIEWER_PASSWORD", "")

    if not viewer_user or not viewer_password:
        logger.warning(
            "Set OAS_VIEWER_USER and OAS_VIEWER_PASSWORD to enable the "
            "provenance viewer on the HTTP transport."
        )
        return None

    routes = [
        Route("/viewer", viewer_html),
        Route("/viewer/", viewer_html),
        Route("/sessions", sessions_endpoint),
        Route("/graph", graph_endpoint),
        Route("/plot", plot_endpoint),
        Route("/plot_types", plot_types_endpoint),
    ]

    app = Starlette(
        routes=routes,
        middleware=[
            Middleware(
                CORSMiddleware,
                allow_origins=["*"],
                allow_methods=["GET"],
                allow_headers=["Authorization"],
            ),
        ],
    )
    app.state.viewer_user = viewer_user
    app.state.viewer_password = viewer_password
    return app


# Paths the viewer app handles — used by the fallback dispatcher.
_VIEWER_PATHS = frozenset({"/viewer", "/viewer/", "/sessions", "/graph", "/plot", "/plot_types"})


def make_fallback_app(viewer_app: Starlette, fallback_app) -> Starlette:
    """Compose *viewer_app* with a fallback ASGI app (typically the MCP app).

    Requests whose path matches a viewer route go to *viewer_app*; everything
    else is forwarded to *fallback_app*.  CORS preflight (OPTIONS) for viewer
    paths is also handled by the viewer app.
    """

    async def dispatcher(scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            path = scope.get("path", "")
            if path in _VIEWER_PATHS:
                await viewer_app(scope, receive, send)
                return
        await fallback_app(scope, receive, send)

    # Return a thin wrapper that looks like an ASGI app
    return dispatcher  # type: ignore[return-value]
