"""Tests for the Starlette-based viewer routes (HTTP transport)."""

from __future__ import annotations

import base64
import os
import uuid
from unittest import mock

import pytest
from starlette.testclient import TestClient

from oas_mcp.core.viewer_routes import build_viewer_app
from oas_mcp.provenance.db import init_db, record_session


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate_prov_db(tmp_path):
    """Redirect provenance DB to a per-test temp file."""
    init_db(tmp_path / "prov.db")


def _env_with_auth(user="admin", password="secret"):
    return {"OAS_VIEWER_USER": user, "OAS_VIEWER_PASSWORD": password}


def _basic_header(user: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture
def authed_app():
    """Build viewer app with auth env vars set."""
    with mock.patch.dict(os.environ, _env_with_auth()):
        app = build_viewer_app()
    assert app is not None
    return app


@pytest.fixture
def client(authed_app):
    """TestClient with valid credentials injected into every request."""
    return TestClient(authed_app, headers=_basic_header("admin", "secret"))


@pytest.fixture
def unauthed_client(authed_app):
    """TestClient with no auth headers."""
    return TestClient(authed_app)


# ---------------------------------------------------------------------------
# build_viewer_app() behaviour
# ---------------------------------------------------------------------------

def test_build_returns_none_when_env_unset():
    with mock.patch.dict(os.environ, {}, clear=True):
        # Remove viewer vars if present
        os.environ.pop("OAS_VIEWER_USER", None)
        os.environ.pop("OAS_VIEWER_PASSWORD", None)
        assert build_viewer_app() is None


def test_build_returns_none_when_password_unset():
    with mock.patch.dict(os.environ, {"OAS_VIEWER_USER": "admin"}, clear=True):
        os.environ.pop("OAS_VIEWER_PASSWORD", None)
        assert build_viewer_app() is None


def test_build_returns_app_when_both_set():
    with mock.patch.dict(os.environ, _env_with_auth()):
        app = build_viewer_app()
    assert app is not None


# ---------------------------------------------------------------------------
# Auth enforcement
# ---------------------------------------------------------------------------

def test_auth_rejects_no_credentials(unauthed_client):
    resp = unauthed_client.get("/viewer")
    assert resp.status_code == 401
    assert "Basic" in resp.headers.get("WWW-Authenticate", "")


def test_auth_rejects_wrong_password(authed_app):
    client = TestClient(authed_app, headers=_basic_header("admin", "wrong"))
    resp = client.get("/viewer")
    assert resp.status_code == 401


def test_auth_accepts_valid_credentials(client):
    resp = client.get("/viewer")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Viewer HTML
# ---------------------------------------------------------------------------

def test_viewer_html_served(client):
    resp = client.get("/viewer")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


def test_viewer_html_trailing_slash(client):
    resp = client.get("/viewer/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


# ---------------------------------------------------------------------------
# Sessions endpoint
# ---------------------------------------------------------------------------

def test_sessions_endpoint(client):
    resp = client.get("/sessions")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)


def test_sessions_includes_seeded(client):
    sid = f"test-{uuid.uuid4().hex[:8]}"
    record_session(sid, notes="seeded session")
    resp = client.get("/sessions")
    assert resp.status_code == 200
    ids = [s["session_id"] for s in resp.json()]
    assert sid in ids


# ---------------------------------------------------------------------------
# Graph endpoint
# ---------------------------------------------------------------------------

def test_graph_requires_session_id(client):
    resp = client.get("/graph")
    assert resp.status_code == 400
    assert "session_id" in resp.json()["error"]


def test_graph_with_valid_session(client):
    sid = f"test-{uuid.uuid4().hex[:8]}"
    record_session(sid, notes="graph test")
    resp = client.get(f"/graph?session_id={sid}")
    assert resp.status_code == 200
    data = resp.json()
    assert "session" in data
    assert "nodes" in data
    assert "edges" in data


# ---------------------------------------------------------------------------
# Plot types endpoint
# ---------------------------------------------------------------------------

def test_plot_types_requires_run_id(client):
    resp = client.get("/plot_types")
    assert resp.status_code == 400


def test_plot_types_not_found(client):
    resp = client.get("/plot_types?run_id=nonexistent")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Plot endpoint
# ---------------------------------------------------------------------------

def test_plot_requires_params(client):
    resp = client.get("/plot")
    assert resp.status_code == 400


def test_plot_not_found(client):
    resp = client.get("/plot?run_id=nonexistent&plot_type=planform")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------

def test_cors_headers(client):
    resp = client.options(
        "/sessions",
        headers={
            "Origin": "http://example.com",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "Authorization",
        },
    )
    assert resp.headers.get("access-control-allow-origin") == "*"
