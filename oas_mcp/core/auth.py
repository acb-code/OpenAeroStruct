"""Keycloak JWT token verifier for OAS MCP Server HTTP transport.

Validates RS256 JWTs by fetching JWKS from the Keycloak realm's
``/protocol/openid-connect/certs`` endpoint.

Configuration is via environment variables:

    KEYCLOAK_ISSUER_URL    — full realm URL, e.g.
                             https://keycloak.example.com/realms/oas
    KEYCLOAK_CLIENT_ID     — OAuth2 client ID (default: "oas-mcp")
    KEYCLOAK_CLIENT_SECRET — client secret (used for introspection fallback)
    RESOURCE_SERVER_URL    — public URL of this server (default: http://localhost:8000)

Usage in server.py (HTTP mode only)::

    from oas_mcp.core.auth import KeycloakTokenVerifier, build_auth_settings
    verifier = KeycloakTokenVerifier(issuer_url=..., client_id=...)
    mcp = FastMCP("OpenAeroStruct", token_verifier=verifier, auth=build_auth_settings())
"""

from __future__ import annotations

import os
from typing import Any

KEYCLOAK_ISSUER_URL: str = os.environ.get("KEYCLOAK_ISSUER_URL", "")
KEYCLOAK_CLIENT_ID: str = os.environ.get("KEYCLOAK_CLIENT_ID", "oas-mcp")
KEYCLOAK_CLIENT_SECRET: str = os.environ.get("KEYCLOAK_CLIENT_SECRET", "")
RESOURCE_SERVER_URL: str = os.environ.get("RESOURCE_SERVER_URL", "http://localhost:8000")


class KeycloakTokenVerifier:
    """Validates RS256 JWTs issued by a Keycloak realm.

    Fetches the JWKS on first use and caches the signing key.  The
    ``PyJWT[crypto]`` and ``httpx`` packages must be installed
    (``pip install 'openaerostruct[http]'``).

    Parameters
    ----------
    issuer_url:
        Full Keycloak realm URL, e.g.
        ``https://keycloak.example.com/realms/my-realm``.
    client_id:
        OAuth2 audience / client ID that tokens must be issued for.
    client_secret:
        Optional client secret for token-introspection fallback.
    """

    def __init__(
        self,
        issuer_url: str,
        client_id: str,
        client_secret: str = "",
    ) -> None:
        self._issuer_url = issuer_url.rstrip("/")
        self._client_id = client_id
        self._client_secret = client_secret
        self._jwks_client: Any = None  # jwt.PyJWKClient, imported lazily

    def _get_jwks_client(self) -> Any:
        if self._jwks_client is None:
            try:
                import jwt  # PyJWT
            except ImportError as exc:
                raise ImportError(
                    "PyJWT[crypto] is required for Keycloak auth. "
                    "Install it with: pip install 'openaerostruct[http]'"
                ) from exc
            jwks_uri = f"{self._issuer_url}/protocol/openid-connect/certs"
            self._jwks_client = jwt.PyJWKClient(jwks_uri)
        return self._jwks_client

    def verify(self, token: str) -> dict[str, Any]:
        """Validate *token* and return the decoded JWT claims.

        Raises ``jwt.exceptions.InvalidTokenError`` (or a subclass) on
        any validation failure.
        """
        import jwt  # PyJWT

        client = self._get_jwks_client()
        signing_key = client.get_signing_key_from_jwt(token)
        payload: dict[str, Any] = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=self._client_id,
            issuer=self._issuer_url,
        )
        return payload

    # ------------------------------------------------------------------
    # MCP TokenVerifier protocol
    # ------------------------------------------------------------------

    async def verify_token(self, token: str) -> dict[str, Any]:
        """Async wrapper around :meth:`verify` for FastMCP integration."""
        import asyncio

        return await asyncio.to_thread(self.verify, token)


def build_auth_settings() -> Any:
    """Return a FastMCP ``AuthSettings`` object configured from env vars.

    Returns ``None`` if ``KEYCLOAK_ISSUER_URL`` is not set (auth disabled).
    """
    if not KEYCLOAK_ISSUER_URL:
        return None
    try:
        from mcp.server.auth.settings import AuthSettings  # type: ignore[import]
    except ImportError:
        return None
    return AuthSettings(
        issuer_url=KEYCLOAK_ISSUER_URL,
        required_scopes=["mcp:tools"],
        resource_server_url=RESOURCE_SERVER_URL,
    )


def build_token_verifier() -> KeycloakTokenVerifier | None:
    """Return a :class:`KeycloakTokenVerifier` if ``KEYCLOAK_ISSUER_URL`` is set."""
    if not KEYCLOAK_ISSUER_URL:
        return None
    return KeycloakTokenVerifier(
        issuer_url=KEYCLOAK_ISSUER_URL,
        client_id=KEYCLOAK_CLIENT_ID,
        client_secret=KEYCLOAK_CLIENT_SECRET,
    )
