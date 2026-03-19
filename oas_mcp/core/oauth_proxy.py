"""OAuth proxy provider that delegates authentication to an upstream OIDC provider.

Implements the MCP SDK's ``OAuthAuthorizationServerProvider`` protocol so that
the MCP server itself acts as the authorization server (with ``/authorize``,
``/token``, ``/register`` endpoints), while delegating user authentication to
an upstream OIDC provider (Authentik, Keycloak, etc.).

This enables Dynamic Client Registration (DCR) for clients like Claude Code
and claude.ai even when the upstream provider doesn't support RFC 7591.
"""

from __future__ import annotations

import json
import secrets
import time
import urllib.parse
import urllib.request
from typing import Any

from pydantic import AnyUrl

from mcp.server.auth.provider import (
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from oas_mcp.core.auth import OIDCTokenVerifier, _current_user_ctx
from oas_mcp.core.telemetry import logger


class _UpstreamAccessToken:
    """Thin wrapper holding the upstream token + decoded metadata."""

    def __init__(self, token: str, client_id: str, scopes: list[str], expires_at: int | None):
        self.token = token
        self.client_id = client_id
        self.scopes = scopes
        self.expires_at = expires_at


def _discover(issuer_url: str) -> dict[str, Any]:
    """Fetch OIDC discovery document from the upstream provider."""
    url = f"{issuer_url.rstrip('/')}/.well-known/openid-configuration"
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read())


class OIDCProxyProvider(OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, Any]):
    """Proxies OAuth flows to an upstream OIDC provider (e.g. Authentik).

    Parameters
    ----------
    upstream_issuer_url:
        The upstream OIDC issuer URL (e.g.
        ``https://auth.lakesideai.dev/application/o/oas-mcp/``).
    upstream_client_id:
        Client ID registered with the upstream provider.
    upstream_client_secret:
        Client secret for the upstream provider.
    server_url:
        Public URL of this MCP server (e.g. ``https://mcp.lakesideai.dev``).
    """

    def __init__(
        self,
        upstream_issuer_url: str,
        upstream_client_id: str,
        upstream_client_secret: str,
        server_url: str,
    ) -> None:
        self._upstream_issuer = upstream_issuer_url.rstrip("/")
        self._upstream_client_id = upstream_client_id
        self._upstream_client_secret = upstream_client_secret
        self._server_url = server_url.rstrip("/")

        # Discover upstream endpoints
        disc = _discover(self._upstream_issuer)
        self._upstream_authorize_url: str = disc["authorization_endpoint"]
        self._upstream_token_url: str = disc["token_endpoint"]
        self._upstream_jwks_uri: str = disc["jwks_uri"]
        logger.info(
            "OIDC proxy: upstream authorize=%s token=%s",
            self._upstream_authorize_url,
            self._upstream_token_url,
        )

        # JWT verifier for validating upstream access tokens
        self._jwt_verifier = OIDCTokenVerifier(
            issuer_url=upstream_issuer_url,
            client_id=upstream_client_id,
        )

        # ---- In-memory stores ----
        # DCR client registrations: client_id → OAuthClientInformationFull
        self._clients: dict[str, OAuthClientInformationFull] = {}
        # Pending authorization flows: state → {client, params}
        self._auth_states: dict[str, dict[str, Any]] = {}
        # Issued authorization codes: code → dict (auth code metadata + upstream tokens)
        self._auth_codes: dict[str, dict[str, Any]] = {}
        # Refresh tokens: our_token → {upstream_refresh_token, client_id, scopes}
        self._refresh_tokens: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # OAuthAuthorizationServerProvider — client registration
    # ------------------------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self._clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self._clients[client_info.client_id] = client_info
        logger.info("DCR: registered client %s (%s)", client_info.client_id, client_info.client_name)

    # ------------------------------------------------------------------
    # OAuthAuthorizationServerProvider — authorization
    # ------------------------------------------------------------------

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        """Redirect the user to the upstream provider for authentication."""
        state = secrets.token_urlsafe(32)
        self._auth_states[state] = {
            "client": client,
            "params": params,
        }

        # Build upstream authorize URL
        query = urllib.parse.urlencode({
            "response_type": "code",
            "client_id": self._upstream_client_id,
            "redirect_uri": f"{self._server_url}/oauth/callback",
            "scope": "openid profile email mcp:tools",
            "state": state,
        })
        url = f"{self._upstream_authorize_url}?{query}"
        logger.info("authorize: redirecting to upstream provider (state=%s…)", state[:8])
        return url

    # ------------------------------------------------------------------
    # Callback handler (called by the custom Starlette route, not the SDK)
    # ------------------------------------------------------------------

    async def handle_upstream_callback(self, code: str, state: str) -> str:
        """Handle the redirect back from the upstream provider.

        Exchanges the upstream authorization code for tokens, generates our
        own authorization code, and returns a redirect URL to the client.
        """
        auth_state = self._auth_states.pop(state, None)
        if not auth_state:
            raise ValueError("Unknown or expired authorization state")

        params: AuthorizationParams = auth_state["params"]
        client: OAuthClientInformationFull = auth_state["client"]

        # Exchange upstream code for tokens
        upstream_tokens = self._exchange_upstream_code(code)

        # Generate our own authorization code
        our_code = secrets.token_urlsafe(32)
        self._auth_codes[our_code] = {
            "code": our_code,
            "client_id": client.client_id,
            "scopes": params.scopes or ["mcp:tools"],
            "code_challenge": params.code_challenge,
            "redirect_uri": params.redirect_uri,
            "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
            "resource": params.resource,
            "upstream_tokens": upstream_tokens,
            "expires_at": time.time() + 300,
        }

        # Build redirect back to the MCP client
        redirect_params: dict[str, str] = {"code": our_code}
        if params.state:
            redirect_params["state"] = params.state
        sep = "&" if "?" in str(params.redirect_uri) else "?"
        redirect_url = f"{params.redirect_uri}{sep}{urllib.parse.urlencode(redirect_params)}"

        logger.info("callback: issued auth code, redirecting client (code=%s…)", our_code[:8])
        return redirect_url

    def _exchange_upstream_code(self, code: str) -> dict[str, Any]:
        """Exchange an upstream authorization code for tokens (synchronous)."""
        data = urllib.parse.urlencode({
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": f"{self._server_url}/oauth/callback",
            "client_id": self._upstream_client_id,
            "client_secret": self._upstream_client_secret,
        }).encode()

        req = urllib.request.Request(
            self._upstream_token_url,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())

    # ------------------------------------------------------------------
    # OAuthAuthorizationServerProvider — token exchange
    # ------------------------------------------------------------------

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        stored = self._auth_codes.get(authorization_code)
        if not stored:
            return None
        if stored["client_id"] != client.client_id:
            return None
        if time.time() > stored["expires_at"]:
            self._auth_codes.pop(authorization_code, None)
            return None
        return AuthorizationCode(
            code=stored["code"],
            scopes=stored["scopes"],
            expires_at=stored["expires_at"],
            client_id=stored["client_id"],
            code_challenge=stored["code_challenge"],
            redirect_uri=stored["redirect_uri"],
            redirect_uri_provided_explicitly=stored["redirect_uri_provided_explicitly"],
            resource=stored.get("resource"),
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        stored = self._auth_codes.pop(authorization_code.code, None)
        if not stored:
            raise ValueError("Authorization code not found or already used")

        upstream = stored["upstream_tokens"]
        access_token = upstream["access_token"]

        # If upstream gave us a refresh token, store it mapped to our own token
        our_refresh_token = None
        if upstream.get("refresh_token"):
            our_refresh_token = secrets.token_urlsafe(32)
            self._refresh_tokens[our_refresh_token] = {
                "upstream_refresh_token": upstream["refresh_token"],
                "client_id": client.client_id,
                "scopes": authorization_code.scopes,
            }

        return OAuthToken(
            access_token=access_token,
            token_type="bearer",
            expires_in=upstream.get("expires_in"),
            refresh_token=our_refresh_token,
            scope=" ".join(authorization_code.scopes),
        )

    # ------------------------------------------------------------------
    # OAuthAuthorizationServerProvider — refresh tokens
    # ------------------------------------------------------------------

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        stored = self._refresh_tokens.get(refresh_token)
        if not stored or stored["client_id"] != client.client_id:
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=stored["client_id"],
            scopes=stored["scopes"],
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        stored = self._refresh_tokens.pop(refresh_token.token, None)
        if not stored:
            raise ValueError("Refresh token not found")

        # Exchange with upstream
        data = urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "refresh_token": stored["upstream_refresh_token"],
            "client_id": self._upstream_client_id,
            "client_secret": self._upstream_client_secret,
        }).encode()

        req = urllib.request.Request(
            self._upstream_token_url,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            upstream = json.loads(resp.read())

        # Store the new refresh token
        our_new_refresh = None
        if upstream.get("refresh_token"):
            our_new_refresh = secrets.token_urlsafe(32)
            self._refresh_tokens[our_new_refresh] = {
                "upstream_refresh_token": upstream["refresh_token"],
                "client_id": client.client_id,
                "scopes": scopes or refresh_token.scopes,
            }

        return OAuthToken(
            access_token=upstream["access_token"],
            token_type="bearer",
            expires_in=upstream.get("expires_in"),
            refresh_token=our_new_refresh,
            scope=" ".join(scopes or refresh_token.scopes),
        )

    # ------------------------------------------------------------------
    # OAuthAuthorizationServerProvider — access token validation
    # ------------------------------------------------------------------

    async def load_access_token(self, token: str) -> Any | None:
        """Validate an upstream access token by verifying its JWT signature."""
        from mcp.server.auth.provider import AccessToken as MCPAccessToken

        # Reset contextvar
        _current_user_ctx.set("")

        try:
            claims = self._jwt_verifier.verify(token)
        except Exception:
            logger.warning("OAuth proxy: token verification failed", exc_info=True)
            return None

        token_scopes = claims.get("scope", "").split()
        if "mcp:tools" not in token_scopes:
            logger.warning("OAuth proxy: token missing mcp:tools scope")
            return None

        # Store username in contextvar for get_current_user()
        username = claims.get("preferred_username") or claims.get("sub", "")
        _current_user_ctx.set(username)

        return MCPAccessToken(
            token=token,
            client_id=claims.get("azp") or claims.get("client_id", self._upstream_client_id),
            scopes=token_scopes,
            expires_at=claims.get("exp"),
        )

    # ------------------------------------------------------------------
    # OAuthAuthorizationServerProvider — revocation (no-op)
    # ------------------------------------------------------------------

    async def revoke_token(self, token: Any) -> None:
        """Revoke a token. Best-effort removal from local stores."""
        if hasattr(token, "token"):
            self._refresh_tokens.pop(token.token, None)
