"""Tests for the public MCP URL option's environment-variable behavior."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from click.testing import CliRunner

from tescmd.cli.main import AppContext


def _app_context() -> AppContext:
    return AppContext(
        vin=None,
        profile="default",
        output_format="json",
        quiet=False,
        region=None,
        verbose=False,
    )


def _close_coroutine(coroutine: Any) -> None:
    coroutine.close()


def _run_coroutine(coroutine: Any) -> Any:
    return asyncio.run(coroutine)


@pytest.mark.parametrize(
    "args",
    [
        ["--transport", "stdio"],
        ["VIN", "--no-mcp"],
        ["--tailscale"],
    ],
)
def test_serve_ignores_public_url_environment_in_unused_modes(
    monkeypatch: pytest.MonkeyPatch, args: list[str]
) -> None:
    from tescmd.cli import serve

    monkeypatch.setattr(serve, "run_async", _close_coroutine)
    result = CliRunner().invoke(
        serve.serve_cmd,
        args,
        env={
            "TESCMD_MCP_PUBLIC_URL": "https://mcp.example.com",
            "TESCMD_MCP_AUTH_USERNAME": "owner",
            "TESCMD_MCP_AUTH_PASSWORD": "a-long-owner-password",
        },
        obj=_app_context(),
    )

    assert result.exit_code == 0, result.output


def test_serve_uses_public_url_environment_for_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tescmd.cli import serve

    public_urls: list[str | None] = []

    async def capture_public_url(*args: Any, **kwargs: Any) -> None:
        public_urls.append(kwargs["public_url"])

    monkeypatch.setattr(serve, "_cmd_serve", capture_public_url)
    monkeypatch.setattr(serve, "run_async", _run_coroutine)
    result = CliRunner().invoke(
        serve.serve_cmd,
        [],
        env={
            "TESCMD_MCP_PUBLIC_URL": "https://mcp.example.com",
            "TESCMD_MCP_AUTH_USERNAME": "owner",
            "TESCMD_MCP_AUTH_PASSWORD": "a-long-owner-password",
        },
        obj=_app_context(),
    )

    assert result.exit_code == 0, result.output
    assert public_urls == ["https://mcp.example.com"]


@pytest.mark.parametrize(
    "args",
    [
        ["--transport", "stdio"],
        ["--tailscale"],
    ],
)
def test_mcp_serve_ignores_public_url_environment_in_unused_modes(
    monkeypatch: pytest.MonkeyPatch, args: list[str]
) -> None:
    from tescmd._internal import async_utils
    from tescmd.cli.mcp_cmd import serve_cmd

    monkeypatch.setattr(async_utils, "run_async", _close_coroutine)
    result = CliRunner().invoke(
        serve_cmd,
        args,
        env={
            "TESCMD_MCP_PUBLIC_URL": "https://mcp.example.com",
            "TESCMD_MCP_AUTH_USERNAME": "owner",
            "TESCMD_MCP_AUTH_PASSWORD": "a-long-owner-password",
        },
        obj=_app_context(),
    )

    assert result.exit_code == 0, result.output


def test_mcp_serve_uses_public_url_environment_for_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tescmd._internal import async_utils
    from tescmd.cli import mcp_cmd
    from tescmd.cli.mcp_cmd import serve_cmd

    public_urls: list[str | None] = []

    async def capture_public_url(*args: Any) -> None:
        public_urls.append(args[-1])

    monkeypatch.setattr(mcp_cmd, "_cmd_serve", capture_public_url)
    monkeypatch.setattr(async_utils, "run_async", _run_coroutine)
    result = CliRunner().invoke(
        serve_cmd,
        [],
        env={
            "TESCMD_MCP_PUBLIC_URL": "https://mcp.example.com",
            "TESCMD_MCP_AUTH_USERNAME": "owner",
            "TESCMD_MCP_AUTH_PASSWORD": "a-long-owner-password",
        },
        obj=_app_context(),
    )

    assert result.exit_code == 0, result.output
    assert public_urls == ["https://mcp.example.com"]


@pytest.mark.parametrize(
    ("command", "args"),
    [
        ("serve", ["--transport", "stdio", "--public-url", "https://mcp.example.com"]),
        ("serve", ["VIN", "--no-mcp", "--public-url", "https://mcp.example.com"]),
        ("serve", ["--tailscale", "--public-url", "https://mcp.example.com"]),
        (
            "mcp",
            ["--transport", "stdio", "--public-url", "https://mcp.example.com"],
        ),
        ("mcp", ["--tailscale", "--public-url", "https://mcp.example.com"]),
    ],
)
def test_public_url_flag_still_rejects_incompatible_modes(
    monkeypatch: pytest.MonkeyPatch, command: str, args: list[str]
) -> None:
    from tescmd._internal import async_utils
    from tescmd.cli import serve
    from tescmd.cli.mcp_cmd import serve_cmd as mcp_serve_cmd

    monkeypatch.setattr(serve, "run_async", _close_coroutine)
    monkeypatch.setattr(async_utils, "run_async", _close_coroutine)
    selected_command = serve.serve_cmd if command == "serve" else mcp_serve_cmd
    result = CliRunner().invoke(
        selected_command,
        args,
        env={
            "TESCMD_MCP_AUTH_USERNAME": "owner",
            "TESCMD_MCP_AUTH_PASSWORD": "a-long-owner-password",
        },
        obj=_app_context(),
    )

    assert result.exit_code != 0
    assert "--public-url" in result.output
