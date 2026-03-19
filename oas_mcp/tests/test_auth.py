"""Unit tests for OIDC JWT auth — no network calls, no OAS computation."""

from unittest.mock import MagicMock, patch

import pytest


class TestVerifyToken:
    """Tests for OIDCTokenVerifier.verify_token → AccessToken | None."""

    def _make_verifier(self):
        from oas_mcp.core.auth import OIDCTokenVerifier

        return OIDCTokenVerifier(
            issuer_url="https://auth.example.com/application/o/oas-mcp",
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
        claims = {"client_id": "fallback-client", "scope": "mcp:tools read", "exp": 1234}
        with patch.object(verifier, "verify", return_value=claims):
            result = await verifier.verify_token("fake.token.here")

        assert result is not None
        assert result.client_id == "fallback-client"

    @pytest.mark.asyncio
    async def test_falls_back_to_constructor_client_id_when_no_claim(self):
        verifier = self._make_verifier()
        claims = {"scope": "mcp:tools read", "exp": 1234}
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
        """Token with empty scope is rejected (missing mcp:tools)."""
        verifier = self._make_verifier()
        claims = {"azp": "c", "scope": "", "exp": 1}
        with patch.object(verifier, "verify", return_value=claims):
            result = await verifier.verify_token("t")

        # Empty scope means mcp:tools is absent → rejected
        assert result is None

    # ------------------------------------------------------------------
    # Fix #1: logging on exception
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_logs_warning_on_verification_exception(self):
        """verify_token() must emit a warning when verify() raises."""
        verifier = self._make_verifier()
        with patch.object(verifier, "verify", side_effect=RuntimeError("network error")):
            with patch("oas_mcp.core.auth.logger") as mock_logger:
                result = await verifier.verify_token("bad.token")

        assert result is None
        mock_logger.warning.assert_called_once()
        call_kwargs = mock_logger.warning.call_args
        assert call_kwargs.kwargs.get("exc_info") is True or (
            len(call_kwargs.args) >= 1 and "verification" in call_kwargs.args[0].lower()
        )

    # ------------------------------------------------------------------
    # Fix #4: contextvar reset
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_contextvar_reset_before_failed_verification(self):
        """A failing request must not inherit a previous request's identity."""
        from oas_mcp.core.auth import _current_user_ctx, get_current_user

        verifier = self._make_verifier()
        # First request succeeds and sets the contextvar
        good_claims = {"preferred_username": "alice", "scope": "mcp:tools", "exp": 9999999999}
        with patch.object(verifier, "verify", return_value=good_claims):
            good_result = await verifier.verify_token("good.token")
        assert good_result is not None
        assert _current_user_ctx.get() == "alice"

        # Second request fails — contextvar must be cleared, not left as "alice"
        with patch.object(verifier, "verify", side_effect=RuntimeError("expired")):
            bad_result = await verifier.verify_token("bad.token")
        assert bad_result is None
        assert _current_user_ctx.get() == ""

    # ------------------------------------------------------------------
    # Fix #5: scope validation
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_returns_none_when_mcp_tools_scope_missing(self):
        """Valid JWT but without mcp:tools scope must be rejected."""
        verifier = self._make_verifier()
        claims = {"azp": "c", "scope": "openid profile", "exp": 9999999999, "sub": "u"}
        with patch.object(verifier, "verify", return_value=claims):
            result = await verifier.verify_token("token.without.scope")

        assert result is None

    @pytest.mark.asyncio
    async def test_logs_warning_when_mcp_tools_scope_missing(self):
        """A warning must be emitted when mcp:tools scope is absent."""
        verifier = self._make_verifier()
        claims = {"azp": "c", "scope": "openid", "exp": 9999999999}
        with patch.object(verifier, "verify", return_value=claims):
            with patch("oas_mcp.core.auth.logger") as mock_logger:
                result = await verifier.verify_token("t")

        assert result is None
        mock_logger.warning.assert_called_once()

    @pytest.mark.asyncio
    async def test_accepts_token_with_mcp_tools_among_other_scopes(self):
        """mcp:tools present alongside other scopes must be accepted."""
        verifier = self._make_verifier()
        claims = {
            "azp": "c",
            "scope": "openid profile mcp:tools email",
            "exp": 9999999999,
            "sub": "u",
        }
        with patch.object(verifier, "verify", return_value=claims):
            result = await verifier.verify_token("t")

        assert result is not None
        assert "mcp:tools" in result.scopes


class TestBuilderFunctions:
    """Tests for build_auth_settings and build_token_verifier."""

    def test_build_auth_settings_returns_none_without_env(self, monkeypatch):
        monkeypatch.delenv("OIDC_ISSUER_URL", raising=False)
        monkeypatch.delenv("KEYCLOAK_ISSUER_URL", raising=False)
        import oas_mcp.core.auth as auth_mod

        result = auth_mod.build_auth_settings()
        assert result is None

    def test_build_token_verifier_returns_none_without_env(self, monkeypatch):
        monkeypatch.delenv("OIDC_ISSUER_URL", raising=False)
        monkeypatch.delenv("KEYCLOAK_ISSUER_URL", raising=False)
        import oas_mcp.core.auth as auth_mod

        result = auth_mod.build_token_verifier()
        assert result is None

    def test_build_token_verifier_returns_verifier_with_env(self, monkeypatch):
        monkeypatch.setenv("OIDC_ISSUER_URL", "https://auth.example.com/application/o/app")
        monkeypatch.setenv("OIDC_CLIENT_ID", "test-client")
        monkeypatch.delenv("KEYCLOAK_ISSUER_URL", raising=False)
        import oas_mcp.core.auth as auth_mod
        from oas_mcp.core.auth import OIDCTokenVerifier

        result = auth_mod.build_token_verifier()

        assert isinstance(result, OIDCTokenVerifier)
        assert result._issuer_url == "https://auth.example.com/application/o/app"
        assert result._client_id == "test-client"

    def test_build_token_verifier_falls_back_to_legacy_keycloak_env(self, monkeypatch):
        monkeypatch.delenv("OIDC_ISSUER_URL", raising=False)
        monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://kc.example.com/realms/r")
        monkeypatch.setenv("KEYCLOAK_CLIENT_ID", "legacy-client")
        import oas_mcp.core.auth as auth_mod
        from oas_mcp.core.auth import OIDCTokenVerifier

        result = auth_mod.build_token_verifier()

        assert isinstance(result, OIDCTokenVerifier)
        assert result._issuer_url == "https://kc.example.com/realms/r"
        assert result._client_id == "legacy-client"
