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
    """Build viewer app with Basic Auth env vars set."""
    with mock.patch.dict(os.environ, _env_with_auth()):
        app, mode = build_viewer_app()
    assert app is not None
    assert mode == "basic"
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
        os.environ.pop("OAS_VIEWER_OIDC_CLIENT_SECRET", None)
        app, mode = build_viewer_app()
        assert app is None
        assert mode == ""


def test_build_returns_none_when_password_unset():
    with mock.patch.dict(os.environ, {"OAS_VIEWER_USER": "admin"}, clear=True):
        os.environ.pop("OAS_VIEWER_PASSWORD", None)
        os.environ.pop("OAS_VIEWER_OIDC_CLIENT_SECRET", None)
        app, mode = build_viewer_app()
        assert app is None
        assert mode == ""


def test_build_returns_app_when_both_set():
    with mock.patch.dict(os.environ, _env_with_auth()):
        app, mode = build_viewer_app()
    assert app is not None
    assert mode == "basic"


# ---------------------------------------------------------------------------
# Auth enforcement (Basic Auth mode)
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
# CORS (Basic Auth mode)
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


# ---------------------------------------------------------------------------
# OIDC mode — build_viewer_app
# ---------------------------------------------------------------------------

def _oidc_env():
    """Env vars that activate OIDC viewer mode."""
    return {
        "OIDC_ISSUER_URL": "https://auth.example.com/realms/oas",
        "OAS_VIEWER_OIDC_CLIENT_ID": "oas-viewer",
        "OAS_VIEWER_OIDC_CLIENT_SECRET": "test-secret",
        "OAS_VIEWER_SESSION_SECRET": "a" * 64,
        "RESOURCE_SERVER_URL": "https://mcp.example.com",
    }


def test_build_oidc_mode():
    """OIDC mode is chosen when OIDC vars are set, even without Basic Auth vars."""
    with mock.patch.dict(os.environ, _oidc_env(), clear=True):
        app, mode = build_viewer_app()
    assert app is not None
    assert mode == "oidc"


def test_build_oidc_takes_priority_over_basic():
    """OIDC mode wins when both OIDC and Basic Auth vars are set."""
    env = {**_oidc_env(), **_env_with_auth()}
    with mock.patch.dict(os.environ, env, clear=True):
        app, mode = build_viewer_app()
    assert mode == "oidc"


# ---------------------------------------------------------------------------
# OIDC mode — auth enforcement
# ---------------------------------------------------------------------------

@pytest.fixture
def oidc_app():
    """Build viewer app in OIDC mode."""
    with mock.patch.dict(os.environ, _oidc_env(), clear=True):
        app, mode = build_viewer_app()
    assert mode == "oidc"
    return app


@pytest.fixture
def oidc_client(oidc_app):
    """TestClient for OIDC app — no session, triggers redirect."""
    return TestClient(oidc_app, follow_redirects=False)


def test_oidc_redirects_unauthenticated(oidc_client):
    """Unauthenticated request should redirect to OIDC authorization endpoint."""
    resp = oidc_client.get("/viewer")
    assert resp.status_code == 302
    location = resp.headers["location"]
    # The redirect should contain the OIDC authorization code flow params
    assert "response_type=code" in location
    assert "client_id=oas-viewer" in location


def test_oidc_sessions_redirects(oidc_client):
    """All protected endpoints redirect when unauthenticated."""
    for path in ["/sessions", "/graph?session_id=x", "/plot_types?run_id=x"]:
        resp = oidc_client.get(path)
        assert resp.status_code == 302, f"{path} should redirect"


def test_oidc_allows_authenticated_session(oidc_app):
    """Requests with a valid session cookie should succeed."""
    client = TestClient(oidc_app)
    # Simulate an authenticated session by setting session data directly.
    # Starlette's SessionMiddleware reads from signed cookies, so we inject
    # the session via the middleware's internal signer.
    from itsdangerous import TimestampSigner
    import json
    import base64 as b64

    signer = TimestampSigner("a" * 64)
    session_data = {"username": "testuser", "is_admin": False}
    payload = b64.b64encode(json.dumps(session_data).encode()).decode()
    signed = signer.sign(payload).decode()

    resp = client.get("/viewer", cookies={"oas_viewer_session": signed})
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


def test_oidc_admin_session(oidc_app):
    """Admin session should also succeed."""
    from itsdangerous import TimestampSigner
    import json
    import base64 as b64

    signer = TimestampSigner("a" * 64)
    session_data = {"username": "admin", "is_admin": True}
    payload = b64.b64encode(json.dumps(session_data).encode()).decode()
    signed = signer.sign(payload).decode()

    resp = TestClient(oidc_app).get("/viewer", cookies={"oas_viewer_session": signed})
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# OIDC mode — login/callback/logout routes exist
# ---------------------------------------------------------------------------

def test_oidc_login_route_exists(oidc_client):
    """The /viewer/login route should exist and redirect to OIDC provider."""
    resp = oidc_client.get("/viewer/login")
    assert resp.status_code == 302


def test_oidc_callback_rejects_without_state(oidc_client):
    """Callback without state param should return 403."""
    resp = oidc_client.get("/viewer/callback?code=abc")
    assert resp.status_code == 403


def test_oidc_logout_clears_session(oidc_app):
    """Logout should redirect (to Keycloak or /viewer)."""
    from itsdangerous import TimestampSigner
    import json
    import base64 as b64

    signer = TimestampSigner("a" * 64)
    session_data = {"username": "testuser", "is_admin": False}
    payload = b64.b64encode(json.dumps(session_data).encode()).decode()
    signed = signer.sign(payload).decode()

    client = TestClient(oidc_app, follow_redirects=False)
    resp = client.get("/viewer/logout", cookies={"oas_viewer_session": signed})
    assert resp.status_code == 302


# ---------------------------------------------------------------------------
# OIDC mode — user-scoped artifact access
# ---------------------------------------------------------------------------

def test_oidc_plot_scoped_to_user(oidc_app):
    """Plot endpoint should pass user to generate_plot_png in OIDC mode."""
    from itsdangerous import TimestampSigner
    import json
    import base64 as b64

    signer = TimestampSigner("a" * 64)
    session_data = {"username": "alice", "is_admin": False}
    payload = b64.b64encode(json.dumps(session_data).encode()).decode()
    signed = signer.sign(payload).decode()

    with mock.patch(
        "oas_mcp.provenance.viewer_server.generate_plot_png", return_value=None
    ) as mock_gen:
        client = TestClient(oidc_app)
        resp = client.get(
            "/plot?run_id=test123&plot_type=planform",
            cookies={"oas_viewer_session": signed},
        )
        assert resp.status_code == 404  # artifact not found (mocked)
        mock_gen.assert_called_once_with("test123", "planform", user="alice")


def test_oidc_plot_admin_sees_all(oidc_app):
    """Admin should pass user=None (search all) to generate_plot_png."""
    from itsdangerous import TimestampSigner
    import json
    import base64 as b64

    signer = TimestampSigner("a" * 64)
    session_data = {"username": "admin", "is_admin": True}
    payload = b64.b64encode(json.dumps(session_data).encode()).decode()
    signed = signer.sign(payload).decode()

    with mock.patch(
        "oas_mcp.provenance.viewer_server.generate_plot_png", return_value=None
    ) as mock_gen:
        client = TestClient(oidc_app)
        resp = client.get(
            "/plot?run_id=test123&plot_type=planform",
            cookies={"oas_viewer_session": signed},
        )
        assert resp.status_code == 404
        mock_gen.assert_called_once_with("test123", "planform", user=None)
