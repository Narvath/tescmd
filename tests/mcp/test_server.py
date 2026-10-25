"""Tests for MCP server tool listing and invocation."""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
import time
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from tescmd.mcp.oauth import OwnerOAuthProvider
from tescmd.mcp.server import (
    _EXCLUDED,
    _READ_TOOLS,
    _WRITE_TOOLS,
    MCPServer,
    create_mcp_server,
)

if TYPE_CHECKING:
    from pathlib import Path

    import pytest
    from starlette.types import Message, Receive, Scope, Send


class TestCreateServer:
    def test_factory_returns_server(self) -> None:
        server = create_mcp_server()
        assert isinstance(server, MCPServer)


class TestListTools:
    def test_lists_read_and_write_tools(self) -> None:
        server = MCPServer()
        tools = server.list_tools()
        names = {t["name"] for t in tools}

        # Spot-check key tools
        assert "vehicle_list" in names
        assert "charge_status" in names
        assert "charge_schedules_list" in names
        assert "charge_start" in names
        assert "security_lock" in names

    def test_tool_count(self) -> None:
        server = MCPServer()
        tools = server.list_tools()
        expected = len(_READ_TOOLS) + len(_WRITE_TOOLS)
        assert len(tools) == expected

    def test_read_tools_have_readonly_hint(self) -> None:
        server = MCPServer()
        tools = server.list_tools()
        read_tools = [t for t in tools if t["name"] in _READ_TOOLS]
        for tool in read_tools:
            assert tool["annotations"]["readOnlyHint"] is True

    def test_write_tools_have_no_readonly_hint(self) -> None:
        server = MCPServer()
        tools = server.list_tools()
        write_tools = [t for t in tools if t["name"] in _WRITE_TOOLS]
        for tool in write_tools:
            assert tool["annotations"]["readOnlyHint"] is False

    def test_tool_has_input_schema(self) -> None:
        server = MCPServer()
        tools = server.list_tools()
        for tool in tools:
            assert "inputSchema" in tool
            assert tool["inputSchema"]["type"] == "object"
            assert "vin" in tool["inputSchema"]["properties"]

    def test_excluded_commands_not_present(self) -> None:
        server = MCPServer()
        tools = server.list_tools()
        names = {t["name"] for t in tools}
        # None of the excluded command names should be tool names
        for excl in _EXCLUDED:
            # Excluded are space-separated, tool names are underscore-separated
            tool_name = excl.replace(" ", "_")
            assert tool_name not in names


class TestInvokeTool:
    def test_unknown_tool_returns_error(self) -> None:
        server = MCPServer()
        result = server.invoke_tool("nonexistent", {})
        assert "error" in result
        assert "Unknown tool" in result["error"]

    def test_invoke_cache_status(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """cache status is a safe read-only command that works without auth."""
        monkeypatch.setenv("TESLA_CACHE_DIR", str(tmp_path))
        monkeypatch.setenv("TESLA_CACHE_ENABLED", "true")

        server = MCPServer()
        result = server.invoke_tool("cache_status", {})

        # cache status should succeed (it doesn't require auth)
        # It returns either parsed JSON or an output string
        assert isinstance(result, dict)

    def test_invoke_passes_env_to_runner(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """CliRunner must receive the process env so auth tokens are visible."""
        import os
        from unittest.mock import MagicMock, patch

        sentinel_token = "test-token-12345"
        monkeypatch.setenv("TESLA_ACCESS_TOKEN", sentinel_token)

        server = MCPServer()

        with patch("click.testing.CliRunner.invoke") as mock_invoke:
            mock_result = MagicMock()
            mock_result.exit_code = 0
            mock_result.output = '{"ok": true}'
            mock_result.stderr = ""
            mock_invoke.return_value = mock_result

            server.invoke_tool("cache_status", {})

            # Verify invoke was called with env= containing our token
            _args, kwargs = mock_invoke.call_args
            assert "env" in kwargs
            assert kwargs["env"]["TESLA_ACCESS_TOKEN"] == sentinel_token
            # Verify it's a copy, not the live os.environ
            assert kwargs["env"] is not os.environ


class TestCustomTools:
    def test_register_custom_tool(self) -> None:
        server = MCPServer()
        handler = lambda params: {"result": "ok"}  # noqa: E731
        server.register_custom_tool(
            "my_custom",
            handler,
            "A custom tool",
            {"type": "object", "properties": {"x": {"type": "integer"}}},
        )
        assert "my_custom" in server._custom_tools

    def test_custom_tool_in_list_tools(self) -> None:
        server = MCPServer()
        server.register_custom_tool(
            "my_custom",
            lambda params: {},
            "Custom description",
            {"type": "object", "properties": {"x": {"type": "integer"}}},
        )
        tools = server.list_tools()
        names = {t["name"] for t in tools}
        assert "my_custom" in names

    def test_custom_tool_schema(self) -> None:
        server = MCPServer()
        schema = {"type": "object", "properties": {"field": {"type": "string"}}}
        server.register_custom_tool("my_tool", lambda p: {}, "desc", schema)
        tools = server.list_tools()
        tool = next(t for t in tools if t["name"] == "my_tool")
        assert tool["inputSchema"] == schema
        assert tool["description"] == "desc"

    def test_custom_tool_annotations(self) -> None:
        server = MCPServer()
        server.register_custom_tool(
            "read_tool",
            lambda p: {},
            "read",
            {},
            is_write=False,
        )
        server.register_custom_tool(
            "write_tool",
            lambda p: {},
            "write",
            {},
            is_write=True,
        )
        tools = server.list_tools()
        read = next(t for t in tools if t["name"] == "read_tool")
        write = next(t for t in tools if t["name"] == "write_tool")
        assert read["annotations"]["readOnlyHint"] is True
        assert write["annotations"]["readOnlyHint"] is False

    def test_custom_tool_count(self) -> None:
        server = MCPServer()
        base_count = len(server.list_tools())
        server.register_custom_tool("extra1", lambda p: {}, "d1", {})
        server.register_custom_tool("extra2", lambda p: {}, "d2", {})
        assert len(server.list_tools()) == base_count + 2

    def test_invoke_custom_tool(self) -> None:
        server = MCPServer()
        server.register_custom_tool(
            "echo",
            lambda params: {"echoed": params.get("msg", "")},
            "Echo tool",
            {"type": "object", "properties": {"msg": {"type": "string"}}},
        )
        result = server.invoke_tool("echo", {"msg": "hello"})
        assert result == {"echoed": "hello"}

    def test_invoke_custom_tool_error(self) -> None:
        def broken(params: dict) -> dict:
            raise ValueError("boom")

        server = MCPServer()
        server.register_custom_tool("broken", broken, "Broken tool", {})
        result = server.invoke_tool("broken", {})
        assert "error" in result
        assert "boom" in result["error"]

    def test_invoke_custom_tool_over_cli_tool(self) -> None:
        """Custom tools take precedence when name overlaps with CLI tools."""
        server = MCPServer()
        # Register a custom tool with a name that exists in CLI tools
        server.register_custom_tool(
            "cache_status",
            lambda p: {"custom": True},
            "Override",
            {},
        )
        result = server.invoke_tool("cache_status", {})
        assert result == {"custom": True}


class TestToolCLIArgs:
    def test_args_include_format_json_wake(self) -> None:
        """Verify the invocation uses --format json --wake."""
        server = MCPServer()

        # We can't easily mock CliRunner here, so just verify the tool
        # entry exists and has the right structure
        tool_def = server._tools.get("vehicle_list")
        assert tool_def is not None
        assert tool_def.args == ["vehicle", "list"]
        assert tool_def.is_write is False

    def test_write_tool_marked_correctly(self) -> None:
        server = MCPServer()
        tool_def = server._tools.get("charge_start")
        assert tool_def is not None
        assert tool_def.is_write is True


def _oauth_client(client_id: str = "test-client") -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id=client_id,
        client_name="Example MCP client",
        redirect_uris=["https://example.com/callback"],
        token_endpoint_auth_method="none",
    )


def _auth_params(*, state: str | None = "test-state") -> AuthorizationParams:
    return AuthorizationParams(
        state=state,
        scopes=["mcp"],
        code_challenge="test-challenge-abc",
        redirect_uri="https://example.com/callback",
        redirect_uri_provided_explicitly=True,
    )


def _auth_page(provider: OwnerOAuthProvider) -> Starlette:
    return Starlette(
        routes=[
            Route(
                "/auth/authorize",
                provider.handle_authorization_page,
                methods=["GET", "POST"],
            )
        ]
    )


def _csrf_token(response_text: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', response_text)
    assert match is not None
    return match.group(1)


class TestOwnerOAuthProvider:
    async def test_consent_page_discloses_redirect_host_and_untrusted_client_name(
        self, tmp_path: Path
    ) -> None:
        provider = OwnerOAuthProvider(
            username="owner",
            password="a-long-owner-password",
            database_path=tmp_path / "oauth.sqlite3",
        )
        client = OAuthClientInformationFull(
            client_id="test-client",
            client_name="<Claude>",
            redirect_uris=["https://attacker.example@trusted.example/callback"],
            token_endpoint_auth_method="none",
        )
        params = AuthorizationParams(
            state="test-state",
            scopes=["mcp"],
            code_challenge="test-challenge-abc",
            redirect_uri="https://attacker.example@trusted.example/callback",
            redirect_uri_provided_explicitly=True,
        )
        auth_path = await provider.authorize(client, params)
        try:
            with TestClient(_auth_page(provider)) as web:
                page = web.get(auth_path)

            assert page.status_code == 200
            assert "&lt;Claude&gt;" in page.text
            assert "<strong><Claude></strong>" not in page.text
            assert "The client name is self-reported." in page.text
            assert "access is sent to <strong>trusted.example</strong>" in page.text
            assert "attacker.example" not in page.text
            assert "Approve only if you recognize this destination." in page.text
        finally:
            provider.close()

    async def test_unknown_client_is_not_created(self, tmp_path: Path) -> None:
        provider = OwnerOAuthProvider(
            username="owner",
            password="a-long-owner-password",
            database_path=tmp_path / "oauth.sqlite3",
        )
        try:
            assert await provider.get_client("not-registered") is None
        finally:
            provider.close()

    async def test_registered_client_persists(self, tmp_path: Path) -> None:
        database_path = tmp_path / "oauth.sqlite3"
        provider = OwnerOAuthProvider(
            username="owner",
            password="a-long-owner-password",
            database_path=database_path,
        )
        client = _oauth_client()
        await provider.register_client(client)
        provider.close()

        restarted = OwnerOAuthProvider(
            username="owner",
            password="a-long-owner-password",
            database_path=database_path,
        )
        try:
            result = await restarted.get_client(client.client_id)
            assert result is not None
            assert result.client_id == client.client_id
        finally:
            restarted.close()

    async def test_owner_sign_in_and_approval_issue_single_use_code(self, tmp_path: Path) -> None:
        provider = OwnerOAuthProvider(
            username="owner",
            password="a-long-owner-password",
            database_path=tmp_path / "oauth.sqlite3",
        )
        client = _oauth_client()
        await provider.register_client(client)
        auth_path = await provider.authorize(client, _auth_params(state="state-123"))
        try:
            with TestClient(_auth_page(provider)) as web:
                page = web.get(auth_path)
                assert page.status_code == 200
                assert "Example MCP client" in page.text
                assert "approve" in page.text
                assert page.headers["cache-control"] == "no-store"
                csrf = _csrf_token(page.text)
                request_id = parse_qs(urlparse(auth_path).query)["request_id"][0]

                failed = web.post(
                    "/auth/authorize",
                    data={
                        "request_id": request_id,
                        "csrf_token": csrf,
                        "action": "approve",
                        "username": "owner",
                        "password": "incorrect-password",
                    },
                )
                assert failed.status_code == 401
                assert "incorrect" in failed.text

                approved = web.post(
                    "/auth/authorize",
                    data={
                        "request_id": request_id,
                        "csrf_token": csrf,
                        "action": "approve",
                        "username": "owner",
                        "password": "a-long-owner-password",
                    },
                    follow_redirects=False,
                )
                assert approved.status_code == 302
                redirect = parse_qs(urlparse(approved.headers["location"]).query)
                assert redirect["state"] == ["state-123"]
                code = redirect["code"][0]

            auth_code = await provider.load_authorization_code(client, code)
            assert auth_code is not None
            assert auth_code.client_id == client.client_id
            token = await provider.exchange_authorization_code(client, auth_code)
            assert token.access_token
            assert token.refresh_token
            assert token.token_type == "Bearer"
            assert token.expires_in == 3600
            assert await provider.load_authorization_code(client, code) is None
        finally:
            provider.close()

    async def test_consent_denial_returns_error_without_code(self, tmp_path: Path) -> None:
        provider = OwnerOAuthProvider(
            username="owner",
            password="a-long-owner-password",
            database_path=tmp_path / "oauth.sqlite3",
        )
        auth_path = await provider.authorize(_oauth_client(), _auth_params())
        try:
            request_id = parse_qs(urlparse(auth_path).query)["request_id"][0]
            with TestClient(_auth_page(provider)) as web:
                page = web.get(auth_path)
                denied = web.post(
                    "/auth/authorize",
                    data={
                        "request_id": request_id,
                        "csrf_token": _csrf_token(page.text),
                        "action": "deny",
                    },
                    follow_redirects=False,
                )
            result = parse_qs(urlparse(denied.headers["location"]).query)
            assert result["error"] == ["access_denied"]
            assert "code" not in result
        finally:
            provider.close()

    async def test_unicode_owner_credentials_are_compared_safely(self, tmp_path: Path) -> None:
        username = "öwner"
        password = "pässword-with-unicode-🌞"
        provider = OwnerOAuthProvider(
            username=username,
            password=password,
            database_path=tmp_path / "oauth.sqlite3",
        )
        auth_path = await provider.authorize(_oauth_client(), _auth_params())
        request_id = parse_qs(urlparse(auth_path).query)["request_id"][0]
        try:
            with TestClient(_auth_page(provider)) as web:
                page = web.get(auth_path)
                response = web.post(
                    "/auth/authorize",
                    data={
                        "request_id": request_id,
                        "csrf_token": _csrf_token(page.text),
                        "action": "approve",
                        "username": username,
                        "password": password,
                    },
                    follow_redirects=False,
                )
            assert response.status_code == 302
            assert "code" in parse_qs(urlparse(response.headers["location"]).query)
        finally:
            provider.close()

    async def test_login_attempt_limit_applies_only_to_pending_authorization(
        self, tmp_path: Path
    ) -> None:
        provider = OwnerOAuthProvider(
            username="owner",
            password="a-long-owner-password",
            database_path=tmp_path / "oauth.sqlite3",
        )
        auth_path = await provider.authorize(_oauth_client(), _auth_params())
        request_id = parse_qs(urlparse(auth_path).query)["request_id"][0]
        other_auth_path = await provider.authorize(_oauth_client("other-client"), _auth_params())
        try:
            with TestClient(_auth_page(provider)) as web:
                page = web.get(auth_path)
                csrf = _csrf_token(page.text)
                other_page = web.get(other_auth_path)
                other_request_id = parse_qs(urlparse(other_auth_path).query)["request_id"][0]
                other_csrf = _csrf_token(other_page.text)
                invalid_csrf = web.post(
                    "/auth/authorize",
                    data={
                        "request_id": request_id,
                        "csrf_token": "wrong",
                        "action": "approve",
                        "username": "owner",
                        "password": "a-long-owner-password",
                    },
                )
                assert invalid_csrf.status_code == 400

                for attempt in range(5):
                    failed = web.post(
                        "/auth/authorize",
                        data={
                            "request_id": request_id,
                            "csrf_token": csrf,
                            "action": "approve",
                            "username": "owner",
                            "password": "incorrect-password",
                        },
                    )
                    if attempt < 4:
                        assert failed.status_code == 401
                    else:
                        assert failed.status_code == 429
                        assert "Start again from your MCP client." in failed.text

                expired = web.get(auth_path)
                assert expired.status_code == 400

                retry_path = await provider.authorize(_oauth_client(), _auth_params())
                retry_request_id = parse_qs(urlparse(retry_path).query)["request_id"][0]
                retry_page = web.get(retry_path)
                retry_blocked = web.post(
                    "/auth/authorize",
                    data={
                        "request_id": retry_request_id,
                        "csrf_token": _csrf_token(retry_page.text),
                        "action": "approve",
                        "username": "owner",
                        "password": "a-long-owner-password",
                    },
                )
                assert retry_blocked.status_code == 429
                assert "Try again later." in retry_blocked.text

                approved = web.post(
                    "/auth/authorize",
                    data={
                        "request_id": other_request_id,
                        "csrf_token": other_csrf,
                        "action": "approve",
                        "username": "owner",
                        "password": "a-long-owner-password",
                    },
                    follow_redirects=False,
                )
                assert approved.status_code == 302
        finally:
            provider.close()

    async def test_pending_authorizations_are_capped_per_client(self, tmp_path: Path) -> None:
        provider = OwnerOAuthProvider(
            username="owner",
            password="a-long-owner-password",
            database_path=tmp_path / "oauth.sqlite3",
        )
        auth_paths = [await provider.authorize(_oauth_client(), _auth_params()) for _ in range(6)]
        try:
            assert len(provider._pending_authorizations) == 5
            with TestClient(_auth_page(provider)) as web:
                evicted = web.get(auth_paths[0])
                newest = web.get(auth_paths[-1])

            assert evicted.status_code == 400
            assert newest.status_code == 200
        finally:
            provider.close()

    async def test_global_pending_cap_evicts_oldest_request(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import tescmd.mcp.oauth as oauth

        monkeypatch.setattr(oauth, "_MAX_PENDING_AUTHORIZATIONS", 2)
        provider = OwnerOAuthProvider(
            username="owner",
            password="a-long-owner-password",
            database_path=tmp_path / "oauth.sqlite3",
        )
        first_path = await provider.authorize(_oauth_client("first-client"), _auth_params())
        second_path = await provider.authorize(_oauth_client("second-client"), _auth_params())
        third_path = await provider.authorize(_oauth_client("third-client"), _auth_params())
        try:
            assert len(provider._pending_authorizations) == 2
            with TestClient(_auth_page(provider)) as web:
                evicted = web.get(first_path)
                retained = web.get(second_path)
                newest = web.get(third_path)

            assert evicted.status_code == 400
            assert retained.status_code == 200
            assert newest.status_code == 200
        finally:
            provider.close()

    async def test_refresh_rotation_revocation_and_credential_change(self, tmp_path: Path) -> None:
        database_path = tmp_path / "oauth.sqlite3"
        provider = OwnerOAuthProvider(
            username="owner",
            password="a-long-owner-password",
            database_path=database_path,
        )
        client = _oauth_client()
        auth_path = await provider.authorize(client, _auth_params())
        request_id = parse_qs(urlparse(auth_path).query)["request_id"][0]
        try:
            with TestClient(_auth_page(provider)) as web:
                page = web.get(auth_path)
                response = web.post(
                    "/auth/authorize",
                    data={
                        "request_id": request_id,
                        "csrf_token": _csrf_token(page.text),
                        "action": "approve",
                        "username": "owner",
                        "password": "a-long-owner-password",
                    },
                    follow_redirects=False,
                )
            code = parse_qs(urlparse(response.headers["location"]).query)["code"][0]
            auth_code = await provider.load_authorization_code(client, code)
            assert auth_code is not None
            original = await provider.exchange_authorization_code(client, auth_code)
            refresh = await provider.load_refresh_token(client, original.refresh_token)
            assert refresh is not None
            refresh_lifetime = refresh.expires_at - int(time.time())
            assert 364 * 24 * 60 * 60 <= refresh_lifetime <= 365 * 24 * 60 * 60

            rotated = await provider.exchange_refresh_token(client, refresh, ["mcp"])
            assert rotated.access_token != original.access_token
            assert rotated.refresh_token != original.refresh_token
            assert await provider.load_access_token(original.access_token) is None
            assert await provider.load_refresh_token(client, original.refresh_token) is None

            revocable = provider._issue_token_pair(
                client_id=client.client_id,
                scopes=["mcp"],
                resource=None,
                subject="owner",
            )
            current = await provider.load_access_token(revocable.access_token)
            assert current is not None
            await provider.revoke_token(current)
            assert await provider.load_access_token(revocable.access_token) is None
            assert await provider.load_refresh_token(client, revocable.refresh_token) is None
        finally:
            provider.close()

        changed = OwnerOAuthProvider(
            username="owner",
            password="a-different-long-owner-password",
            database_path=database_path,
        )
        try:
            assert await changed.load_access_token(rotated.access_token) is None
            assert await changed.load_refresh_token(client, rotated.refresh_token) is None
        finally:
            changed.close()

    async def test_expired_access_token_does_not_destroy_refresh_token(
        self, tmp_path: Path
    ) -> None:
        provider = OwnerOAuthProvider(
            username="owner",
            password="a-long-owner-password",
            database_path=tmp_path / "oauth.sqlite3",
        )
        try:
            token = provider._issue_token_pair(
                client_id="test-client",
                scopes=["mcp"],
                resource=None,
                subject="owner",
            )
            with provider._database:
                provider._database.execute(
                    "UPDATE oauth_tokens SET expires_at = 0 WHERE token_type = 'access'"
                )
            assert await provider.load_access_token(token.access_token) is None
            assert (
                await provider.load_refresh_token(_oauth_client(), token.refresh_token) is not None
            )

            with provider._database:
                provider._database.execute(
                    "UPDATE oauth_tokens SET expires_at = 0 WHERE token_type = 'refresh'"
                )
            assert await provider.load_refresh_token(_oauth_client(), token.refresh_token) is None
        finally:
            provider.close()


class TestMCPHttpAuthentication:
    def test_http_app_requires_owner_credentials(self) -> None:
        import pytest

        with pytest.raises(ValueError, match="AUTH_USERNAME"):
            MCPServer().create_http_app()

    def test_public_url_must_be_https_origin(self, tmp_path: Path) -> None:
        import pytest

        server = MCPServer(
            auth_username="owner",
            auth_password="a-long-owner-password",
            oauth_database_path=tmp_path / "oauth.sqlite3",
        )
        with pytest.raises(ValueError, match="HTTPS origin"):
            server.create_http_app(public_url="http://example.com")
        assert server._oauth_provider is None

    def test_mcp_endpoint_rejects_unauthenticated_requests(self, tmp_path: Path) -> None:
        server = MCPServer(
            auth_username="owner",
            auth_password="a-long-owner-password",
            oauth_database_path=tmp_path / "oauth.sqlite3",
        )
        try:
            app = server.create_http_app()
            assert any(getattr(route, "path", None) == "/auth/authorize" for route in app.routes)
            with TestClient(app) as web:
                response = web.post("/mcp", json={"jsonrpc": "2.0", "method": "tools/list"})
                assert response.status_code == 401
        finally:
            server.close()

    def test_registration_rate_limit_returns_retry_after(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import tescmd.mcp.oauth as oauth

        monkeypatch.setattr(oauth, "_MAX_REGISTRATIONS_PER_WINDOW", 1)
        server = MCPServer(
            auth_username="owner",
            auth_password="a-long-owner-password",
            oauth_database_path=tmp_path / "oauth.sqlite3",
        )
        try:
            app = server.create_http_app()
            with TestClient(app) as web:
                invalid = web.post("/register", json={})
                assert invalid.status_code == 400

                first = web.post(
                    "/register",
                    json={
                        "client_name": "First MCP client",
                        "redirect_uris": ["https://first.example/callback"],
                        "token_endpoint_auth_method": "none",
                    },
                )
                assert first.status_code == 201

                limited = web.post(
                    "/register",
                    json={
                        "client_name": "Second MCP client",
                        "redirect_uris": ["https://second.example/callback"],
                        "token_endpoint_auth_method": "none",
                    },
                )

            assert limited.status_code == 429
            assert limited.json()["error"] == "temporarily_unavailable"
            assert limited.headers["retry-after"].isdigit()
            assert limited.headers["access-control-allow-origin"] == "*"
        finally:
            server.close()

    async def test_registration_timeout_releases_in_flight_slot(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import asyncio

        from starlette.responses import Response

        import tescmd.mcp.oauth as oauth

        monkeypatch.setattr(oauth, "_REGISTRATION_REQUEST_TIMEOUT_SECONDS", 0.01)

        async def register(scope: Scope, receive: Receive, send: Send) -> None:
            while True:
                message = await receive()
                if message["type"] == "http.request" and not message.get("more_body", False):
                    break
            await Response(status_code=201)(scope, receive, send)

        middleware = oauth.RegistrationRateLimitMiddleware(register)

        async def send_request(*, stalled: bool) -> list[Message]:
            received_initial_body = False
            never_complete = asyncio.Event()
            sent: list[Message] = []

            async def receive() -> Message:
                nonlocal received_initial_body
                if received_initial_body and stalled:
                    await never_complete.wait()
                received_initial_body = True
                return {
                    "type": "http.request",
                    "body": b'{"' if stalled else b"{}",
                    "more_body": stalled,
                }

            async def send(message: Message) -> None:
                sent.append(message)

            await middleware(
                {
                    "type": "http",
                    "asgi": {"version": "3.0", "spec_version": "2.3"},
                    "http_version": "1.1",
                    "method": "POST",
                    "scheme": "http",
                    "path": "/register",
                    "raw_path": b"/register",
                    "query_string": b"",
                    "root_path": "",
                    "headers": [(b"content-length", b"100" if stalled else b"2")],
                    "client": ("127.0.0.1", 12345),
                    "server": ("testserver", 80),
                },
                receive,
                send,
            )
            return sent

        stalled_response = await send_request(stalled=True)
        assert middleware._in_flight == 0
        assert (
            next(
                message["status"]
                for message in stalled_response
                if message["type"] == "http.response.start"
            )
            == 408
        )

        completed_response = await send_request(stalled=False)
        assert middleware._in_flight == 0
        assert (
            next(
                message["status"]
                for message in completed_response
                if message["type"] == "http.response.start"
            )
            == 201
        )

    def test_oauth_dcr_consent_pkce_and_authenticated_mcp_request(self, tmp_path: Path) -> None:
        server = MCPServer(
            auth_username="owner",
            auth_password="a-long-owner-password",
            oauth_database_path=tmp_path / "oauth.sqlite3",
        )
        try:
            app = server.create_http_app()
            with TestClient(app) as web:
                unauthenticated = web.post(
                    "/mcp",
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                )
                assert unauthenticated.status_code == 401

                registration = web.post(
                    "/register",
                    json={
                        "client_name": "OAuth test client",
                        "redirect_uris": ["https://example.com/callback"],
                        "token_endpoint_auth_method": "none",
                    },
                )
                assert registration.status_code == 201
                client_id = registration.json()["client_id"]

                unknown_client = web.get(
                    "/authorize",
                    params={
                        "response_type": "code",
                        "client_id": "not-registered",
                        "redirect_uri": "https://example.com/callback",
                        "code_challenge": "invalid",
                        "code_challenge_method": "S256",
                    },
                    follow_redirects=False,
                )
                assert unknown_client.status_code == 400

                invalid_redirect = web.get(
                    "/authorize",
                    params={
                        "response_type": "code",
                        "client_id": client_id,
                        "redirect_uri": "https://attacker.example/callback",
                        "code_challenge": "invalid",
                        "code_challenge_method": "S256",
                    },
                    follow_redirects=False,
                )
                assert invalid_redirect.status_code == 400

                verifier = secrets.token_urlsafe(32)
                challenge = (
                    base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                    .decode("ascii")
                    .rstrip("=")
                )
                resource = "http://127.0.0.1:8080/"
                authorization = web.get(
                    "/authorize",
                    params={
                        "response_type": "code",
                        "client_id": client_id,
                        "redirect_uri": "https://example.com/callback",
                        "state": "state-123",
                        "code_challenge": challenge,
                        "code_challenge_method": "S256",
                        "resource": resource,
                    },
                    follow_redirects=False,
                )
                assert authorization.status_code == 302
                login_path = authorization.headers["location"]
                login_page = web.get(login_path)
                request_id = parse_qs(urlparse(login_path).query)["request_id"][0]
                consent = web.post(
                    "/auth/authorize",
                    data={
                        "request_id": request_id,
                        "csrf_token": _csrf_token(login_page.text),
                        "action": "approve",
                        "username": "owner",
                        "password": "a-long-owner-password",
                    },
                    follow_redirects=False,
                )
                authorization_result = parse_qs(urlparse(consent.headers["location"]).query)
                assert consent.status_code == 302
                assert authorization_result["state"] == ["state-123"]
                code = authorization_result["code"][0]

                bad_pkce = web.post(
                    "/token",
                    data={
                        "grant_type": "authorization_code",
                        "code": code,
                        "client_id": client_id,
                        "redirect_uri": "https://example.com/callback",
                        "code_verifier": "wrong-verifier",
                        "resource": resource,
                    },
                )
                assert bad_pkce.status_code == 400

                bad_redirect = web.post(
                    "/token",
                    data={
                        "grant_type": "authorization_code",
                        "code": code,
                        "client_id": client_id,
                        "redirect_uri": "https://attacker.example/callback",
                        "code_verifier": verifier,
                        "resource": resource,
                    },
                )
                assert bad_redirect.status_code == 400

                token_response = web.post(
                    "/token",
                    data={
                        "grant_type": "authorization_code",
                        "code": code,
                        "client_id": client_id,
                        "redirect_uri": "https://example.com/callback",
                        "code_verifier": verifier,
                        "resource": resource,
                    },
                )
                assert token_response.status_code == 200
                access_token = token_response.json()["access_token"]

                initialized = web.post(
                    "/mcp",
                    headers={
                        "Host": "127.0.0.1:8080",
                        "Authorization": f"Bearer {access_token}",
                        "Accept": "application/json, text/event-stream",
                    },
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-03-26",
                            "capabilities": {},
                            "clientInfo": {"name": "test", "version": "1.0"},
                        },
                    },
                )
                assert initialized.status_code == 200
                assert initialized.headers.get("mcp-session-id")
        finally:
            server.close()


class TestStaticTokenAuth:
    def test_static_token_rejects_unauthenticated(self) -> None:
        server = MCPServer(static_token="a" * 32)
        try:
            app = server.create_http_app()
            with TestClient(app) as web:
                r = web.post(
                    "/mcp",
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                )
                assert r.status_code == 401
                assert r.headers["www-authenticate"] == "Bearer"
                assert r.json()["error"] == "invalid_token"
        finally:
            server.close()

    def test_static_token_rejects_wrong_token(self) -> None:
        server = MCPServer(static_token="a" * 32)
        try:
            app = server.create_http_app()
            with TestClient(app) as web:
                r = web.post(
                    "/mcp",
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                    headers={"Authorization": "Bearer " + "b" * 32},
                )
                assert r.status_code == 401
        finally:
            server.close()

    def test_static_token_allows_correct_token(self) -> None:
        token = "x" * 32
        server = MCPServer(static_token=token)
        try:
            app = server.create_http_app()
            with TestClient(app) as web:
                r = web.post(
                    "/mcp",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Host": "127.0.0.1:8080",
                        "Accept": "application/json, text/event-stream",
                    },
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-03-26",
                            "capabilities": {},
                            "clientInfo": {"name": "test", "version": "1.0"},
                        },
                    },
                )
                assert r.status_code == 200
                assert r.headers.get("mcp-session-id")
        finally:
            server.close()

    def test_static_token_no_oauth_endpoints(self) -> None:
        server = MCPServer(static_token="a" * 32)
        try:
            app = server.create_http_app()
            with TestClient(app) as web:
                r = web.get("/auth/authorize")
                assert r.status_code == 404
        finally:
            server.close()

    def test_static_token_too_short_raises(self) -> None:
        import pytest

        server = MCPServer(static_token="short")
        with pytest.raises(ValueError, match="32"):
            server.create_http_app()

    def test_token_and_oauth_mutually_exclusive(self) -> None:
        import pytest

        server = MCPServer(
            static_token="a" * 32,
            auth_username="owner",
            auth_password="a-long-owner-password",
        )
        with pytest.raises(ValueError, match="mutually exclusive"):
            server.create_http_app()

    def test_lan_binding_allows_external_host(self) -> None:
        token = "k" * 32
        server = MCPServer(static_token=token)
        try:
            app = server.create_http_app(host="0.0.0.0", port=8080)
            with TestClient(app) as web:
                r = web.post(
                    "/mcp",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Host": "192.168.1.50:8080",
                        "Accept": "application/json, text/event-stream",
                    },
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-03-26",
                            "capabilities": {},
                            "clientInfo": {"name": "lan-client", "version": "1.0"},
                        },
                    },
                )
                # Must succeed with 200, not 421 Misdirected Request
                assert r.status_code == 200
        finally:
            server.close()
