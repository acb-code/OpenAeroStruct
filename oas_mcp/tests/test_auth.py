"""Unit tests for Keycloak JWT auth — no network calls, no OAS computation."""

from unittest.mock import MagicMock, patch

import pytest


class TestVerifyToken:
    """Tests for KeycloakTokenVerifier.verify_token → AccessToken | None."""

    def _make_verifier(self):
        from oas_mcp.core.auth import KeycloakTokenVerifier

        return KeycloakTokenVerifier(
            issuer_url="https://keycloak.example.com/realms/oas",
            client_id="oas-mcp",
        )

    @pytest.mark.asyncio
    async def test_returns_access_token_with_azp_and_scope(self):
        verifier = self._make_verifier()
        claims = {
            "azp": "my-client",
            "scope": "mcp:tools openid",
            "exp": 9999999999,
            "sub": "user-1",
        }
        with patch.object(verifier, "verify", return_value=claims):
            result = await verifier.verify_token("fake.token.here")

        assert result is not None
        assert result.token == "fake.token.here"
        assert result.client_id == "my-client"
        assert result.scopes == ["mcp:tools", "openid"]
        assert result.expires_at == 9999999999

    @pytest.mark.asyncio
    async def test_falls_back_to_client_id_claim(self):
        verifier = self._make_verifier()
        claims = {"client_id": "fallback-client", "scope": "read", "exp": 1234}
        with patch.object(verifier, "verify", return_value=claims):
            result = await verifier.verify_token("fake.token.here")

        assert result is not None
        assert result.client_id == "fallback-client"

    @pytest.mark.asyncio
    async def test_falls_back_to_constructor_client_id_when_no_claim(self):
        verifier = self._make_verifier()
        claims = {"scope": "read", "exp": 1234}
        with patch.object(verifier, "verify", return_value=claims):
            result = await verifier.verify_token("fake.token.here")

        assert result is not None
        assert result.client_id == "oas-mcp"

    @pytest.mark.asyncio
    async def test_returns_none_on_invalid_token_error(self):
        import jwt

        verifier = self._make_verifier()
        with patch.object(verifier, "verify", side_effect=jwt.InvalidTokenError("bad")):
            result = await verifier.verify_token("invalid.token")

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_any_exception(self):
        verifier = self._make_verifier()
        with patch.object(verifier, "verify", side_effect=RuntimeError("network error")):
            result = await verifier.verify_token("some.token")

        assert result is None

    @pytest.mark.asyncio
    async def test_empty_scope_produces_empty_list(self):
        verifier = self._make_verifier()
        claims = {"azp": "c", "scope": "", "exp": 1}
        with patch.object(verifier, "verify", return_value=claims):
            result = await verifier.verify_token("t")

        assert result is not None
        assert result.scopes == []


class TestBuilderFunctions:
    """Tests for build_auth_settings and build_token_verifier."""

    def test_build_auth_settings_returns_none_without_env(self, monkeypatch):
        monkeypatch.delenv("KEYCLOAK_ISSUER_URL", raising=False)
        # Re-import so the module-level constant is re-evaluated via the function
        from importlib import reload
        import oas_mcp.core.auth as auth_mod

        # Patch the module-level constant directly
        with patch.object(auth_mod, "KEYCLOAK_ISSUER_URL", ""):
            result = auth_mod.build_auth_settings()
        assert result is None

    def test_build_token_verifier_returns_none_without_env(self):
        from importlib import reload
        import oas_mcp.core.auth as auth_mod

        with patch.object(auth_mod, "KEYCLOAK_ISSUER_URL", ""):
            result = auth_mod.build_token_verifier()
        assert result is None

    def test_build_token_verifier_returns_verifier_with_env(self):
        import oas_mcp.core.auth as auth_mod
        from oas_mcp.core.auth import KeycloakTokenVerifier

        with patch.object(auth_mod, "KEYCLOAK_ISSUER_URL", "https://kc.example.com/realms/r"):
            with patch.object(auth_mod, "KEYCLOAK_CLIENT_ID", "test-client"):
                result = auth_mod.build_token_verifier()

        assert isinstance(result, KeycloakTokenVerifier)
        assert result._issuer_url == "https://kc.example.com/realms/r"
        assert result._client_id == "test-client"
