"""CLI commands for the MCP (Model Context Protocol) server."""

from __future__ import annotations

import click

from tescmd.cli._options import global_options

mcp_group = click.Group("mcp", help="MCP (Model Context Protocol) server.")


@mcp_group.command("serve")
@click.option(
    "--transport",
    type=click.Choice(["stdio", "streamable-http"]),
    default="streamable-http",
    help="MCP transport (default: streamable-http)",
)
@click.option(
    "--port",
    type=int,
    default=8080,
    envvar="TESCMD_MCP_PORT",
    help="HTTP port (streamable-http only)",
)
@click.option(
    "--host",
    default="127.0.0.1",
    envvar="TESCMD_HOST",
    help="Bind address (default: 127.0.0.1)",
)
@click.option("--tailscale", is_flag=True, default=False, help="Expose via Tailscale Funnel")
@click.option(
    "--auth-username",
    envvar="TESCMD_MCP_AUTH_USERNAME",
    default=None,
    help="Owner username for HTTP MCP authorization",
)
@click.option(
    "--auth-password",
    envvar="TESCMD_MCP_AUTH_PASSWORD",
    default=None,
    help="Owner password for HTTP MCP authorization",
)
@click.option(
    "--public-url",
    envvar="TESCMD_MCP_PUBLIC_URL",
    default=None,
    help="Canonical HTTPS origin when served behind a reverse proxy",
)
@global_options
def serve_cmd(
    app_ctx: object,
    transport: str,
    port: int,
    host: str,
    tailscale: bool,
    auth_username: str | None,
    auth_password: str | None,
    public_url: str | None,
) -> None:
    """Start an MCP server exposing tescmd commands as tools.

    Agents (Claude Desktop, Claude Code, etc.) connect to this server
    and invoke tescmd commands as MCP tools with JSON output.

    \b
    Transports:
      streamable-http  HTTP server on --port (default)
      stdio            Read/write JSON-RPC on stdin/stdout

    \b
    HTTP authentication:
      The owner signs in and approves every OAuth client using
      TESCMD_MCP_AUTH_USERNAME and TESCMD_MCP_AUTH_PASSWORD.

    \b
    Examples:
      tescmd mcp serve                          # HTTP on :8080, owner auth required
      tescmd mcp serve --transport stdio        # stdio for Claude Desktop
      tescmd mcp serve --tailscale              # expose via Tailscale Funnel
    """
    from tescmd._internal.async_utils import run_async
    from tescmd.cli.main import AppContext

    assert isinstance(app_ctx, AppContext)

    if tailscale and public_url:
        raise click.UsageError("--tailscale and --public-url cannot be used together.")

    if transport != "stdio" and (
        not auth_username or not auth_username.strip() or not auth_password
    ):
        raise click.UsageError(
            "MCP owner credentials required for HTTP transport.\n"
            "Set TESCMD_MCP_AUTH_USERNAME and TESCMD_MCP_AUTH_PASSWORD "
            "in your .env file or environment, or pass --auth-username and --auth-password."
        )

    if tailscale and transport == "stdio":
        raise click.UsageError("--tailscale cannot be used with --transport stdio")

    if public_url and transport == "stdio":
        raise click.UsageError("--public-url cannot be used with --transport stdio")

    if transport != "stdio" and auth_password and len(auth_password) < 16:
        raise click.UsageError("TESCMD_MCP_AUTH_PASSWORD must be at least 16 characters.")

    run_async(
        _cmd_serve(
            app_ctx,
            transport,
            port,
            host,
            tailscale,
            auth_username,
            auth_password,
            public_url,
        )
    )


async def _cmd_serve(
    app_ctx: object,
    transport: str,
    port: int,
    host: str,
    tailscale: bool,
    auth_username: str | None,
    auth_password: str | None,
    public_url: str | None,
) -> None:
    import sys

    from tescmd.cli.main import AppContext
    from tescmd.mcp.server import create_mcp_server

    assert isinstance(app_ctx, AppContext)
    formatter = app_ctx.formatter
    server = create_mcp_server(
        auth_username=auth_username,
        auth_password=auth_password,
    )
    tool_count = len(server.list_tools())

    if transport == "stdio":
        # Log to stderr so stdout stays clean for JSON-RPC
        print(f"tescmd MCP server starting (stdio, {tool_count} tools)", file=sys.stderr)
        await server.run_stdio()
        return

    if not tailscale:
        if formatter.format != "json":
            formatter.rich.info(
                f"MCP server starting on http://{host}:{port}/mcp ({tool_count} tools)"
            )
            formatter.rich.info("Press Ctrl+C to stop.")
        await server.run_http(host=host, port=port, public_url=public_url)
        return

    # Tailscale Funnel mode
    from tescmd.telemetry.tailscale import TailscaleManager

    ts = TailscaleManager()
    await ts.check_available()
    await ts.check_running()

    if formatter.format != "json":
        formatter.rich.info(f"MCP server starting on port {port} ({tool_count} tools)")

    url = await ts.start_funnel(port)
    public_url = f"{url}/mcp"

    if formatter.format != "json":
        formatter.rich.info(f"Tailscale Funnel active: {public_url}")
        formatter.rich.info("Press Ctrl+C to stop.")
    else:
        print(f'{{"url": "{public_url}"}}', file=sys.stderr)

    try:
        await server.run_http(host=host, port=port, public_url=url)
    finally:
        await ts.stop_funnel()
        if formatter.format != "json":
            formatter.rich.info("Tailscale Funnel stopped.")
