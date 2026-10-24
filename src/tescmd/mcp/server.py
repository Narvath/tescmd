"""FastMCP server factory exposing tescmd commands as MCP tools.

Uses Click's CliRunner to invoke tescmd commands, guaranteeing behavioral
parity with the CLI (caching, wake, auth, error handling all work).

Each tool calls ``runner.invoke(cli, ["--format", "json", "--wake", *args])``
and returns the JSON output.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tescmd.mcp.oauth import OwnerOAuthProvider, RegistrationRateLimitMiddleware

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)


def _make_cli_runner() -> Any:
    """Create a CliRunner with stderr separation.

    Click 8.2 removed the ``mix_stderr`` parameter (stderr is always
    separate).  Click 8.1 defaults to ``mix_stderr=True``, so we pass
    ``False`` when supported.  Routed through ``Any`` to avoid a
    version-dependent ``type: ignore`` that fails strict mypy on one
    version or the other.
    """
    from click.testing import CliRunner as _Runner

    _ctor: Any = _Runner
    try:
        return _ctor(mix_stderr=False)
    except TypeError:
        return _Runner()


@dataclass(frozen=True)
class _CliToolDef:
    """Definition of a CLI-backed MCP tool."""

    args: list[str]
    description: str
    is_write: bool


@dataclass(frozen=True)
class _CustomToolDef:
    """Definition of a custom callable MCP tool."""

    handler: Callable[[dict[str, Any]], dict[str, Any]]
    description: str
    input_schema: dict[str, Any]
    is_write: bool


# ---------------------------------------------------------------------------
# Tool definitions — command name → (args_template, description, is_write)
# ---------------------------------------------------------------------------

_READ_TOOLS: dict[str, tuple[list[str], str]] = {
    "vehicle_list": (["vehicle", "list"], "List all vehicles on the account"),
    "vehicle_info": (["vehicle", "info"], "Get vehicle info summary"),
    "vehicle_data": (["vehicle", "data"], "Get full vehicle data"),
    "vehicle_data_cached": (
        ["vehicle", "data-cached"],
        "Get Tesla's last reported vehicle data without waking the vehicle",
    ),
    "vehicle_location": (["vehicle", "location"], "Get vehicle location"),
    "vehicle_alerts": (["vehicle", "alerts"], "Get vehicle alerts"),
    "vehicle_nearby_chargers": (
        ["vehicle", "nearby-chargers"],
        "Find nearby Superchargers and destination chargers",
    ),
    "vehicle_release_notes": (["vehicle", "release-notes"], "Get software release notes"),
    "vehicle_service": (["vehicle", "service"], "Get service status"),
    "vehicle_drivers": (["vehicle", "drivers"], "List authorized drivers"),
    "vehicle_specs": (["vehicle", "specs"], "Get vehicle specifications"),
    "vehicle_warranty": (["vehicle", "warranty"], "Get warranty information"),
    "vehicle_fleet_status": (["vehicle", "fleet-status"], "Get fleet telemetry status"),
    "vehicle_subscriptions": (["vehicle", "subscriptions"], "List subscriptions"),
    "charge_status": (["charge", "status"], "Get charge status"),
    "charge_schedules_list": (
        ["charge", "schedules", "list"],
        "List saved charge schedules",
    ),
    "climate_status": (["climate", "status"], "Get climate status"),
    "security_status": (["security", "status"], "Get security/lock status"),
    "software_status": (["software", "status"], "Get software update status"),
    "energy_list": (["energy", "list"], "List energy products (Powerwall)"),
    "energy_status": (["energy", "status"], "Get energy site status"),
    "energy_live": (["energy", "live"], "Get live power flow data"),
    "energy_history": (["energy", "history"], "Get energy history"),
    "billing_history": (["billing", "history"], "Get Supercharger billing history"),
    "billing_sessions": (["billing", "sessions"], "Get Supercharger charging sessions"),
    "user_me": (["user", "me"], "Get account info"),
    "user_region": (["user", "region"], "Get account region"),
    "user_orders": (["user", "orders"], "Get vehicle orders"),
    "user_features": (["user", "features"], "Get feature flags"),
    "cache_status": (["cache", "status"], "Get cache status"),
    "auth_status": (["auth", "status"], "Get auth/token status"),
}

_WRITE_TOOLS: dict[str, tuple[list[str], str]] = {
    "charge_start": (["charge", "start"], "Start charging"),
    "charge_stop": (["charge", "stop"], "Stop charging"),
    "charge_limit": (["charge", "limit"], "Set charge limit (percentage)"),
    "charge_limit_max": (["charge", "limit-max"], "Set charge limit to maximum"),
    "charge_limit_std": (["charge", "limit-std"], "Set charge limit to standard"),
    "charge_amps": (["charge", "amps"], "Set charge amperage"),
    "charge_port_open": (["charge", "port-open"], "Open charge port"),
    "charge_port_close": (["charge", "port-close"], "Close charge port"),
    "climate_on": (["climate", "on"], "Turn on climate control"),
    "climate_off": (["climate", "off"], "Turn off climate control"),
    "climate_set": (["climate", "set"], "Set climate temperature"),
    "climate_precondition": (["climate", "precondition"], "Precondition cabin"),
    "climate_seat": (["climate", "seat"], "Set seat heater level"),
    "climate_wheel_heater": (["climate", "wheel-heater"], "Toggle steering wheel heater"),
    "climate_bioweapon": (["climate", "bioweapon"], "Toggle bioweapon defense mode"),
    "security_lock": (["security", "lock"], "Lock the vehicle"),
    "security_unlock": (["security", "unlock"], "Unlock the vehicle"),
    "security_sentry": (["security", "sentry"], "Toggle sentry mode"),
    "security_flash": (["security", "flash"], "Flash the lights"),
    "security_honk": (["security", "honk"], "Honk the horn"),
    "security_remote_start": (["security", "remote-start"], "Enable remote start"),
    "security_parental_activate": (
        ["security", "parental-activate"],
        "Activate Parental Controls with a four-digit PIN",
    ),
    "security_parental_clear_pin_admin": (
        ["security", "parental-clear-pin-admin", "--confirm"],
        "Clear the stored Parental Controls PIN (fleet manager or owner only)",
    ),
    "security_parental_deactivate": (
        ["security", "parental-deactivate"],
        "Deactivate Parental Controls with the current PIN",
    ),
    "security_parental_enable_setting": (
        ["security", "parental-enable-setting"],
        "Enable or disable an individual Parental Controls setting",
    ),
    "security_parental_speed_limit": (
        ["security", "parental-speed-limit"],
        "Set the Parental Controls speed limit in miles per hour",
    ),
    "trunk_open": (["trunk", "open"], "Open the trunk"),
    "trunk_close": (["trunk", "close"], "Close the trunk"),
    "trunk_frunk": (["trunk", "frunk"], "Open the frunk"),
    "trunk_window": (["trunk", "window"], "Vent or close windows"),
    "media_play_pause": (["media", "play-pause"], "Toggle media play/pause"),
    "media_next_track": (["media", "next-track"], "Skip to next track"),
    "media_prev_track": (["media", "prev-track"], "Go to previous track"),
    "media_adjust_volume": (["media", "adjust-volume"], "Set media volume level"),
    "nav_send": (["nav", "send"], "Send a destination to the vehicle"),
    "nav_gps": (["nav", "gps"], "Navigate to GPS coordinates (lat,lon)"),
    "nav_supercharger": (["nav", "supercharger"], "Navigate to nearest Supercharger"),
    "nav_waypoints": (["nav", "waypoints"], "Send multi-stop waypoints via Google Place IDs"),
    "nav_homelink": (["nav", "homelink"], "Trigger HomeLink (garage door)"),
    "software_schedule": (["software", "schedule"], "Schedule software update"),
    "software_cancel": (["software", "cancel"], "Cancel pending software update"),
    "vehicle_wake": (["vehicle", "wake"], "Wake the vehicle"),
    "vehicle_rename": (["vehicle", "rename"], "Rename the vehicle"),
    "cache_clear": (["cache", "clear"], "Clear response cache"),
}

# Commands excluded from MCP (long-running, interactive, or infrastructure)
_EXCLUDED = {
    "vehicle telemetry stream",
    "openclaw bridge",
    "serve",
    "auth login",
    "auth logout",
    "auth register",
    "setup",
    "mcp serve",
    "key generate",
    "key deploy",
    "key enroll",
    "key unenroll",
}


class MCPServer:
    """MCP server that wraps tescmd CLI commands as tools."""

    def __init__(
        self,
        *,
        auth_username: str | None = None,
        auth_password: str | None = None,
        oauth_database_path: Path | None = None,
    ) -> None:
        self._auth_username = auth_username
        self._auth_password = auth_password
        self._oauth_database_path = oauth_database_path
        self._oauth_provider: OwnerOAuthProvider | None = None
        self._tools: dict[str, _CliToolDef] = {}
        self._custom_tools: dict[str, _CustomToolDef] = {}
        for name, (args, desc) in _READ_TOOLS.items():
            self._tools[name] = _CliToolDef(args=args, description=desc, is_write=False)
        for name, (args, desc) in _WRITE_TOOLS.items():
            self._tools[name] = _CliToolDef(args=args, description=desc, is_write=True)

    def register_custom_tool(
        self,
        name: str,
        handler: Any,
        description: str,
        input_schema: dict[str, Any],
        *,
        is_write: bool = False,
    ) -> None:
        """Register a tool backed by a direct callable.

        Unlike CLI-based tools which route through ``CliRunner``, custom
        tools call *handler* directly with the arguments dict and expect
        a dict return value (serialised to JSON by the MCP wrapper).
        """
        self._custom_tools[name] = _CustomToolDef(
            handler=handler,
            description=description,
            input_schema=input_schema,
            is_write=is_write,
        )

    def close(self) -> None:
        """Close persistent OAuth state held by this server."""
        if self._oauth_provider is not None:
            self._oauth_provider.close()
            self._oauth_provider = None

    def list_tools(self) -> list[dict[str, Any]]:
        """Return MCP tool descriptors."""
        tools = []
        for name, defn in sorted(self._tools.items()):
            tool: dict[str, Any] = {
                "name": name,
                "description": defn.description,
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "vin": {
                            "type": "string",
                            "description": "Vehicle VIN (optional if TESLA_VIN set)",
                        },
                        "args": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Additional CLI arguments",
                        },
                    },
                },
            }
            tool["annotations"] = {"readOnlyHint": not defn.is_write}
            tools.append(tool)
        for name, cdefn in sorted(self._custom_tools.items()):
            tools.append(
                {
                    "name": name,
                    "description": cdefn.description,
                    "inputSchema": cdefn.input_schema,
                    "annotations": {"readOnlyHint": not cdefn.is_write},
                }
            )
        return tools

    def invoke_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Invoke a tool by running the corresponding CLI command.

        Custom tools (registered via :meth:`register_custom_tool`) call the
        handler directly.  CLI tools route through ``CliRunner``.

        Returns the parsed JSON output or an error dict.
        """
        if name in self._custom_tools:
            cdefn = self._custom_tools[name]
            try:
                return cdefn.handler(arguments)
            except KeyError as exc:
                logger.info("Custom tool %s missing parameter: %s", name, exc)
                return {"error": f"Missing required parameter: {exc}"}
            except Exception as exc:
                logger.warning("Custom tool %s failed: %s", name, exc, exc_info=True)
                return {"error": str(exc)}

        if name not in self._tools:
            logger.info("Unknown tool: %s", name)
            return {"error": f"Unknown tool: {name}"}

        defn = self._tools[name]
        args_template = defn.args

        # Build CLI args
        cli_args = ["--format", "json", "--wake"]

        vin = arguments.get("vin")
        if vin:
            cli_args.extend(["--vin", vin])

        cli_args.extend(args_template)

        extra_args = arguments.get("args", [])
        if extra_args:
            cli_args.extend(extra_args)

        logger.info("Invoke: %s", name)

        import os

        from tescmd.cli.main import cli

        runner = _make_cli_runner()
        result = runner.invoke(cli, cli_args, env=os.environ.copy())

        output = result.output.strip()
        if result.exit_code != 0:
            logger.info("Tool %s failed (exit=%d)", name, result.exit_code)
            # CliRunner catches exceptions in result.exception rather than
            # routing them through main()'s error handler.  Prefer the
            # exception message over stderr (which may only contain httpx logs).
            exc_msg = str(result.exception) if result.exception else ""
            error_output = exc_msg or (result.stderr.strip() if result.stderr else output)
            return {
                "error": error_output or f"Command failed with exit code {result.exit_code}",
                "exit_code": result.exit_code,
            }

        # Parse JSON output
        try:
            return json.loads(output)  # type: ignore[no-any-return]
        except json.JSONDecodeError:
            return {"output": output}

    async def run_stdio(self) -> None:
        """Run the MCP server on stdio transport."""
        from mcp.server.mcpserver import MCPServer as SDKMCPServer

        mcp = SDKMCPServer("tescmd", instructions="Tesla vehicle management via Fleet API")

        for name, defn in self._tools.items():
            self._register_fastmcp_tool(mcp, name, defn.description)
        for name, cdefn in self._custom_tools.items():
            self._register_custom_fastmcp_tool(mcp, name, cdefn.description)

        await mcp.run_stdio_async()

    def create_http_app(
        self, *, host: str = "127.0.0.1", port: int = 8080, public_url: str | None = None
    ) -> Any:
        """Build and return the MCP Starlette app (without starting uvicorn).

        Returns the ``Starlette`` ASGI application with auth routes and
        MCP tools registered.  Callers can mount additional routes (e.g.
        a telemetry WebSocket handler) alongside it before running uvicorn.
        """
        from urllib.parse import urlparse

        from mcp.server.auth.settings import (
            AuthSettings,
            ClientRegistrationOptions,
            RevocationOptions,
        )
        from mcp.server.mcpserver import MCPServer as SDKMCPServer
        from mcp.server.transport_security import TransportSecuritySettings
        from pydantic import AnyHttpUrl

        base_url = public_url.rstrip("/") if public_url else f"http://127.0.0.1:{port}"
        parsed_public_url = urlparse(public_url) if public_url else None
        if parsed_public_url is not None and (
            parsed_public_url.scheme != "https"
            or not parsed_public_url.hostname
            or parsed_public_url.username is not None
            or parsed_public_url.password is not None
            or parsed_public_url.path not in ("", "/")
            or parsed_public_url.query
            or parsed_public_url.fragment
        ):
            raise ValueError(
                "MCP public URL must be an HTTPS origin without credentials, path, "
                "query, or fragment"
            )
        if not self._auth_username or not self._auth_password:
            raise ValueError(
                "HTTP MCP requires TESCMD_MCP_AUTH_USERNAME and TESCMD_MCP_AUTH_PASSWORD"
            )
        if self._oauth_provider is None:
            database_path = self._oauth_database_path
            if database_path is None:
                from tescmd.models.config import AppSettings

                config_dir = Path(AppSettings().config_dir).expanduser()
                database_path = config_dir / "mcp_oauth.sqlite3"
            self._oauth_provider = OwnerOAuthProvider(
                username=self._auth_username,
                password=self._auth_password,
                database_path=database_path,
            )
        provider = self._oauth_provider

        # Build allowed hosts: always include localhost, add public hostname
        # when exposed via Tailscale Funnel or similar reverse proxy.
        allowed_hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
        allowed_origins = [
            "http://127.0.0.1:*",
            "http://localhost:*",
            "http://[::1]:*",
        ]
        if parsed_public_url is not None:
            hostname = parsed_public_url.hostname
            assert hostname is not None
            allowed_hosts.append(hostname)
            allowed_hosts.append(f"{hostname}:*")
            allowed_origins.append(f"{parsed_public_url.scheme}://{hostname}")
            allowed_origins.append(f"{parsed_public_url.scheme}://{hostname}:*")

        mcp = SDKMCPServer(
            "tescmd",
            instructions="Tesla vehicle management via Fleet API",
            auth_server_provider=provider,
            auth=AuthSettings(
                issuer_url=AnyHttpUrl(base_url),
                resource_server_url=AnyHttpUrl(base_url),
                validate_token_resource=True,
                client_registration_options=ClientRegistrationOptions(enabled=True),
                revocation_options=RevocationOptions(enabled=True),
            ),
        )

        for name, defn in self._tools.items():
            self._register_fastmcp_tool(mcp, name, defn.description)
        for name, cdefn in self._custom_tools.items():
            self._register_custom_fastmcp_tool(mcp, name, cdefn.description)

        from starlette.routing import Route

        app = mcp.streamable_http_app(
            host=host,
            transport_security=TransportSecuritySettings(
                enable_dns_rebinding_protection=True,
                allowed_hosts=allowed_hosts,
                allowed_origins=allowed_origins,
            ),
        )
        app.add_middleware(RegistrationRateLimitMiddleware)
        app.routes.append(
            Route(
                "/auth/authorize",
                provider.handle_authorization_page,
                methods=["GET", "POST"],
            )
        )
        return app

    async def run_http(
        self, *, host: str = "127.0.0.1", port: int = 8080, public_url: str | None = None
    ) -> None:
        """Run the MCP server on streamable-http transport."""
        import uvicorn

        app = self.create_http_app(host=host, port=port, public_url=public_url)
        config = uvicorn.Config(app, host=host, port=port, log_level="warning")
        server = uvicorn.Server(config)
        try:
            await server.serve()
        except SystemExit as exc:
            if exc.code == 0:
                logger.debug("Uvicorn exited cleanly (code 0) on port %d", port)
                return
            raise OSError(f"MCP server failed to start on port {port}") from exc
        finally:
            self.close()

    def _register_fastmcp_tool(
        self,
        mcp: Any,
        tool_name: str,
        description: str,
    ) -> None:
        """Register a CLI-backed tool with the FastMCP server."""
        server = self

        @mcp.tool(name=tool_name, description=description)  # type: ignore
        async def _tool(vin: str = "", args: list[str] | None = None) -> str:
            result = await asyncio.to_thread(
                server.invoke_tool, tool_name, {"vin": vin, "args": args or []}
            )
            return json.dumps(result, default=str, indent=2)

    def _register_custom_fastmcp_tool(
        self,
        mcp: Any,
        tool_name: str,
        description: str,
    ) -> None:
        """Register a custom callable tool with the FastMCP server.

        Custom tools accept a JSON ``params`` string rather than the
        standard ``vin``/``args`` signature used by CLI tools.
        """
        server = self

        @mcp.tool(name=tool_name, description=description)  # type: ignore
        def _tool(params: str = "{}") -> str:
            try:
                arguments = json.loads(params) if params else {}
            except (json.JSONDecodeError, TypeError) as exc:
                return json.dumps({"error": f"Invalid params JSON: {exc}"}, indent=2)
            result = server.invoke_tool(tool_name, arguments)
            return json.dumps(result, default=str, indent=2)


def create_mcp_server(
    *,
    auth_username: str | None = None,
    auth_password: str | None = None,
    oauth_database_path: Path | None = None,
) -> MCPServer:
    """Factory function to create a configured MCP server."""
    return MCPServer(
        auth_username=auth_username,
        auth_password=auth_password,
        oauth_database_path=oauth_database_path,
    )
