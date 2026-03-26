"""Tests for oas_mcp.core.viewer_auth — OIDC session auth for the viewer."""

from __future__ import annotations

import os
from unittest import mock

import pytest

from oas_mcp.core.viewer_auth import (
    ViewerOIDCConfig,
    build_viewer_oidc_config,
    get_viewer_user,
    is_viewer_admin,
    login_redirect,
    require_viewer_oidc,
)


# ---------------------------------------------------------------------------
# build_viewer_oidc_config()
# ---------------------------------------------------------------------------

def test_returns_none_without_env():
    """No OIDC env vars => None."""
    with mock.patch.dict(os.environ, {}, clear=True):
        os.environ.pop("OIDC_ISSUER_URL", None)
        os.environ.pop("OAS_VIEWER_OIDC_CLIENT_SECRET", None)
        assert build_viewer_oidc_config() is None


def test_returns_none_without_client_secret():
    """Issuer set but no client secret => None."""
    env = {"OIDC_ISSUER_URL": "https://auth.example.com/realms/oas"}
    with mock.patch.dict(os.environ, env, clear=True):
        os.environ.pop("OAS_VIEWER_OIDC_CLIENT_SECRET", None)
        assert build_viewer_oidc_config() is None


def test_returns_config_with_env():
    """Full env vars => valid config."""
    env = {
        "OIDC_ISSUER_URL": "https://auth.example.com/realms/oas",
        "OAS_VIEWER_OIDC_CLIENT_ID": "my-viewer",
        "OAS_VIEWER_OIDC_CLIENT_SECRET": "secret123",
        "OAS_VIEWER_SESSION_SECRET": "x" * 64,
        "OAS_VIEWER_ADMIN_ROLE": "superadmin",
        "RESOURCE_SERVER_URL": "https://mcp.example.com",
    }
    with mock.patch.dict(os.environ, env, clear=True):
        cfg = build_viewer_oidc_config()

    assert cfg is not None
    assert cfg.issuer_url == "https://auth.example.com/realms/oas"
    assert cfg.client_id == "my-viewer"
    assert cfg.client_secret == "secret123"
    assert cfg.redirect_uri == "https://mcp.example.com/viewer/callback"
    assert cfg.admin_role == "superadmin"
    assert cfg.session_secret == "x" * 64


def test_defaults():
    """Minimal env => defaults applied."""
    env = {
        "OIDC_ISSUER_URL": "https://auth.example.com/realms/oas",
        "OAS_VIEWER_OIDC_CLIENT_SECRET": "secret",
    }
    with mock.patch.dict(os.environ, env, clear=True):
        os.environ.pop("OAS_VIEWER_OIDC_CLIENT_ID", None)
        os.environ.pop("OAS_VIEWER_ADMIN_ROLE", None)
        os.environ.pop("RESOURCE_SERVER_URL", None)
        cfg = build_viewer_oidc_config()

    assert cfg.client_id == "oas-viewer"
    assert cfg.admin_role == "oas-admin"
    assert cfg.redirect_uri == "http://localhost:8000/viewer/callback"


def test_auto_generates_session_secret():
    """Missing OAS_VIEWER_SESSION_SECRET => auto-generated (with warning)."""
    env = {
        "OIDC_ISSUER_URL": "https://auth.example.com/realms/oas",
        "OAS_VIEWER_OIDC_CLIENT_SECRET": "secret",
    }
    with mock.patch.dict(os.environ, env, clear=True):
        os.environ.pop("OAS_VIEWER_SESSION_SECRET", None)
        cfg = build_viewer_oidc_config()
    assert len(cfg.session_secret) == 64  # token_hex(32) = 64 hex chars


def test_keycloak_legacy_issuer():
    """Legacy KEYCLOAK_ISSUER_URL is accepted."""
    env = {
        "KEYCLOAK_ISSUER_URL": "https://auth.example.com/realms/oas",
        "OAS_VIEWER_OIDC_CLIENT_SECRET": "secret",
    }
    with mock.patch.dict(os.environ, env, clear=True):
        os.environ.pop("OIDC_ISSUER_URL", None)
        cfg = build_viewer_oidc_config()
    assert cfg is not None
    assert "auth.example.com" in cfg.issuer_url


# ---------------------------------------------------------------------------
# ViewerOIDCConfig.get_verifier()
# ---------------------------------------------------------------------------

def test_get_verifier_caches():
    """Verifier is lazily created and cached."""
    cfg = ViewerOIDCConfig(
        issuer_url="https://auth.example.com/realms/oas",
        client_id="oas-viewer",
        client_secret="secret",
        redirect_uri="http://localhost:8000/viewer/callback",
        session_secret="x" * 64,
    )
    v1 = cfg.get_verifier()
    v2 = cfg.get_verifier()
    assert v1 is v2


# ---------------------------------------------------------------------------
# require_viewer_oidc decorator
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_require_oidc_redirects_unauthenticated():
    """No session username => redirect to login."""
    cfg = ViewerOIDCConfig(
        issuer_url="https://auth.example.com/realms/oas",
        client_id="oas-viewer",
        client_secret="secret",
        redirect_uri="http://localhost:8000/viewer/callback",
        session_secret="x" * 64,
        authorization_endpoint="https://auth.example.com/realms/oas/protocol/openid-connect/auth",
    )
    decorator = require_viewer_oidc(cfg)

    called = False

    @decorator
    async def handler(request):
        nonlocal called
        called = True

    # Build a fake request with an empty session
    from starlette.testclient import TestClient
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.middleware import Middleware
    from starlette.middleware.sessions import SessionMiddleware

    app = Starlette(
        routes=[Route("/test", handler)],
        middleware=[Middleware(SessionMiddleware, secret_key="x" * 64)],
    )
    client = TestClient(app, follow_redirects=False)
    resp = client.get("/test")
    assert resp.status_code == 302
    assert "auth.example.com" in resp.headers["location"]
    assert not called


@pytest.mark.anyio
async def test_require_oidc_passes_authenticated():
    """Session with username => handler is called, state is populated."""
    import base64
    import json

    from itsdangerous import TimestampSigner

    cfg = ViewerOIDCConfig(
        issuer_url="https://auth.example.com/realms/oas",
        client_id="oas-viewer",
        client_secret="secret",
        redirect_uri="http://localhost:8000/viewer/callback",
        session_secret="x" * 64,
    )
    decorator = require_viewer_oidc(cfg)

    captured_user = None
    captured_admin = None

    @decorator
    async def handler(request):
        nonlocal captured_user, captured_admin
        captured_user = get_viewer_user(request)
        captured_admin = is_viewer_admin(request)
        from starlette.responses import PlainTextResponse
        return PlainTextResponse("ok")

    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.middleware import Middleware
    from starlette.middleware.sessions import SessionMiddleware

    app = Starlette(
        routes=[Route("/test", handler)],
        middleware=[Middleware(SessionMiddleware, secret_key="x" * 64, session_cookie="sess")],
    )

    # Build a signed session cookie
    signer = TimestampSigner("x" * 64)
    session_data = {"username": "bob", "is_admin": True}
    payload = base64.b64encode(json.dumps(session_data).encode()).decode()
    signed = signer.sign(payload).decode()

    from starlette.testclient import TestClient
    client = TestClient(app)
    resp = client.get("/test", cookies={"sess": signed})
    assert resp.status_code == 200
    assert captured_user == "bob"
    assert captured_admin is True


# ---------------------------------------------------------------------------
# get_viewer_user / is_viewer_admin
# ---------------------------------------------------------------------------

def test_get_viewer_user_returns_empty_without_state():
    """Without request.state.viewer_user, returns empty string."""
    from starlette.requests import Request
    from starlette.datastructures import Headers

    scope = {"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b""}
    request = Request(scope)
    assert get_viewer_user(request) == ""


def test_is_viewer_admin_returns_false_without_state():
    """Without request.state.viewer_is_admin, returns False."""
    from starlette.requests import Request

    scope = {"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b""}
    request = Request(scope)
    assert is_viewer_admin(request) is False
