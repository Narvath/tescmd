# MCP Server

tescmd includes a built-in [Model Context Protocol](https://modelcontextprotocol.io/) (MCP) server that exposes all CLI commands as tools. AI agents like Claude Desktop and Claude Code can connect to this server and query or control Tesla vehicles programmatically.

> **Recommended:** Use `tescmd serve` instead of `tescmd mcp serve` for most use cases. `tescmd serve` combines the MCP server with telemetry-driven cache warming, so agent reads are free while telemetry is active. See [When to Use Which](#when-to-use-which) below.

HTTP transport requires `TESCMD_MCP_AUTH_USERNAME` and a unique
`TESCMD_MCP_AUTH_PASSWORD` of at least 16 characters. Set them before running
the HTTP examples below. The stdio example does not need these credentials.

## Quick Start

```bash
# Recommended: MCP + telemetry cache warming
tescmd serve 5YJ3...

# MCP-only (same as tescmd mcp serve)
tescmd serve --no-telemetry

# stdio transport for Claude Desktop
tescmd serve --transport stdio

# Legacy: standalone MCP server
tescmd mcp serve
```

## When to Use Which

| Command | MCP | Telemetry | Cache Warming | OpenClaw | Best For |
|---|---|---|---|---|---|
| `tescmd serve VIN` | yes | yes | yes | optional | Production agent use |
| `tescmd serve --no-telemetry` | yes | - | - | - | Simple MCP-only setup |
| `tescmd serve --transport stdio` | yes (stdio) | - | - | - | Claude Desktop/Code subprocess |
| `tescmd serve VIN --no-mcp` | - | yes | - | optional | Telemetry dashboard/monitoring |
| `tescmd mcp serve` | yes | - | - | - | Lightweight MCP (legacy alias) |
| `tescmd openclaw bridge` | - | yes | - | yes | Dedicated OpenClaw bridge |
| `tescmd vehicle telemetry stream` | - | yes | - | - | Interactive telemetry dashboard |

## CLI Options

```
tescmd mcp serve [OPTIONS]
```

| Option | Description | Default |
|--------|-------------|---------|
| `--transport` | `streamable-http` or `stdio` | `streamable-http` |
| `--port PORT` | HTTP port (streamable-http only) | `8080` |
| `--tailscale` | Expose via Tailscale Funnel | off |
| `--auth-username` | Owner login name (env: `TESCMD_MCP_AUTH_USERNAME`) | required for HTTP |
| `--auth-password` | Owner login password (env: `TESCMD_MCP_AUTH_PASSWORD`) | 16+ chars, required for HTTP |
| `--public-url` | Canonical HTTPS origin behind a reverse proxy | optional |

## Authentication

HTTP transport requires owner credentials. Set them in the environment or pass them via CLI flags:

```bash
export TESCMD_MCP_AUTH_USERNAME="owner"
export TESCMD_MCP_AUTH_PASSWORD="use-a-unique-password-of-at-least-16-characters"
```

Or pass them directly (prefer environment variables so the password is not saved in shell history):

```bash
tescmd mcp serve --auth-username owner --auth-password "use-a-unique-password-of-at-least-16-characters"
```

**HTTP transport (streamable-http):** MCP clients use OAuth authorization code flow with PKCE. Dynamic Client Registration is supported, but a newly registered client is not authorized until the owner signs in and explicitly approves it:

1. Client connects to `/mcp` — receives `401 Unauthorized`
2. Client discovers OAuth endpoints via `/.well-known/oauth-protected-resource` and `/.well-known/oauth-authorization-server`
3. Client registers through `/register` (dynamic client registration)
4. Client opens the authorization page; the owner signs in and approves or denies that client
5. After approval, the client exchanges the code at `/token` for an access token
6. The client uses its bearer token for subsequent `/mcp` requests

To limit anonymous client-registration abuse, each server process allows up to 10 successful
registrations per 15-minute window. Further attempts receive HTTP 429 with a `Retry-After`
header; malformed registration requests do not consume the allowance. Registration requests
that do not complete within 10 seconds receive HTTP 408 and release their in-flight slot.

OAuth registrations and token state are stored in `mcp_oauth.sqlite3` inside the tescmd config directory. Keep that directory persistent and private. Changing the owner username or password invalidates existing codes and tokens. Access tokens expire after one hour; refresh tokens expire after 365 days and rotate when refreshed, so clients can normally renew access without asking you to authorize them again during that period.

When the server is behind a TLS-terminating reverse proxy, set `TESCMD_MCP_PUBLIC_URL` to the canonical HTTPS origin (for example, `https://mcp.example.com`; do not include a path). This URL is also available as `--public-url`.

The owner login protects client approval, but it does not replace network controls or TLS. **Tailscale Funnel makes the service reachable from the public internet.** Only expose it after configuring a strong unique password, HTTPS, persistent private storage, and verifying the login/approval flow. For private access, bind locally or use a tailnet-only route instead.

**stdio transport:** HTTP owner credentials are not needed for stdio, which is a local subprocess pipe and does not expose an HTTP login endpoint.

## Agent Setup

### Claude Desktop

Add to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "tescmd": {
      "command": "tescmd",
      "args": ["serve", "--transport", "stdio"]
    }
  }
}
```

### Claude Code

Add to `.mcp.json` in your project root:

```json
{
  "mcpServers": {
    "tescmd": {
      "command": "tescmd",
      "args": ["serve", "--transport", "stdio"]
    }
  }
}
```

For a remote HTTP server, use the `url` form. The owner must sign in and approve Claude Code during its OAuth flow. Tailscale Funnel is publicly reachable; use it only after configuring a strong owner password and testing the approval flow, or prefer a tailnet-only route:

```json
{
  "mcpServers": {
    "tescmd": {
      "type": "streamable-http",
      "url": "https://your-machine.tailnet.ts.net/mcp"
    }
  }
}
```

### Claude.ai (Web)

Claude.ai connects to remote MCP servers via the web UI. Add the server in your Claude.ai settings:

1. Go to **Settings → MCP Servers → Add Server**
2. Enter the URL of the HTTPS MCP endpoint: `https://mcp.example.net/mcp`
3. Complete the owner sign-in and approve prompt when Claude.ai starts OAuth

### Remote / Multi-Client

For scenarios where multiple agents connect to a single tescmd instance, use HTTP transport:

```bash
tescmd mcp serve --port 8080
```

Set `TESCMD_MCP_AUTH_USERNAME` and a unique `TESCMD_MCP_AUTH_PASSWORD` of at least 16 characters before starting the server. Agents connect to `http://127.0.0.1:8080/mcp` using the streamable-http MCP transport and must complete owner sign-in and approval.

## How It Works

Each MCP tool invokes the corresponding tescmd CLI command internally via Click's `CliRunner`. Every invocation runs with `--format json --wake`, which means:

- All responses are structured JSON (the standard tescmd JSON envelope)
- Vehicles are auto-woken if asleep (billable API call)
- Response caching is active (cached reads are instant and free)
- Authentication, error handling, and all CLI behavior work identically to running tescmd directly

This design guarantees behavioral parity -- there is no separate API client or code path for MCP. If a command works via `tescmd` on the command line, it works identically as an MCP tool.

## Available Tools

### Read Tools (28)

Read tools are annotated with `readOnlyHint: true`. They query vehicle and account state without side effects.

| Tool | Command | Description |
|------|---------|-------------|
| `vehicle_list` | `vehicle list` | List all vehicles on the account |
| `vehicle_info` | `vehicle info` | Get vehicle info summary |
| `vehicle_data` | `vehicle data` | Get full vehicle data |
| `vehicle_location` | `vehicle location` | Get vehicle location |
| `vehicle_alerts` | `vehicle alerts` | Get vehicle alerts |
| `vehicle_nearby_chargers` | `vehicle nearby-chargers` | Find nearby chargers |
| `vehicle_release_notes` | `vehicle release-notes` | Get software release notes |
| `vehicle_service` | `vehicle service` | Get service status |
| `vehicle_drivers` | `vehicle drivers` | List authorized drivers |
| `vehicle_specs` | `vehicle specs` | Get vehicle specifications |
| `vehicle_warranty` | `vehicle warranty` | Get warranty information |
| `vehicle_fleet_status` | `vehicle fleet-status` | Get fleet telemetry status |
| `vehicle_subscriptions` | `vehicle subscriptions` | List subscriptions |
| `charge_status` | `charge status` | Get charge status |
| `charge_schedules_list` | `charge schedules list` | List saved charge schedules |
| `climate_status` | `climate status` | Get climate status |
| `security_status` | `security status` | Get security/lock status |
| `software_status` | `software status` | Get software update status |
| `energy_list` | `energy list` | List energy products (Powerwall) |
| `energy_status` | `energy status` | Get energy site status |
| `energy_live` | `energy live` | Get live power flow data |
| `energy_history` | `energy history` | Get energy history |
| `billing_history` | `billing history` | Get Supercharger billing history |
| `billing_sessions` | `billing sessions` | Get charging sessions |
| `user_me` | `user me` | Get account info |
| `user_region` | `user region` | Get account region |
| `user_orders` | `user orders` | Get vehicle orders |
| `user_features` | `user features` | Get feature flags |
| `cache_status` | `cache status` | Get cache status |
| `auth_status` | `auth status` | Get auth/token status |

### Write Tools (37)

Write tools send commands to the vehicle. They are annotated with `readOnlyHint: false`.

| Tool | Command | Description |
|------|---------|-------------|
| `vehicle_wake` | `vehicle wake` | Wake the vehicle |
| `vehicle_rename` | `vehicle rename` | Rename the vehicle |
| `charge_start` | `charge start` | Start charging |
| `charge_stop` | `charge stop` | Stop charging |
| `charge_limit` | `charge limit` | Set charge limit (percentage) |
| `charge_limit_max` | `charge limit-max` | Set charge limit to maximum |
| `charge_limit_std` | `charge limit-std` | Set charge limit to standard |
| `charge_amps` | `charge amps` | Set charge amperage |
| `charge_port_open` | `charge port-open` | Open charge port |
| `charge_port_close` | `charge port-close` | Close charge port |
| `climate_on` | `climate on` | Turn on climate control |
| `climate_off` | `climate off` | Turn off climate control |
| `climate_set` | `climate set` | Set climate temperature |
| `climate_precondition` | `climate precondition` | Precondition cabin |
| `climate_seat` | `climate seat` | Set seat heater level |
| `climate_wheel_heater` | `climate wheel-heater` | Toggle steering wheel heater |
| `climate_bioweapon` | `climate bioweapon` | Toggle bioweapon defense mode |
| `security_lock` | `security lock` | Lock the vehicle |
| `security_unlock` | `security unlock` | Unlock the vehicle |
| `security_sentry` | `security sentry` | Toggle sentry mode |
| `security_flash` | `security flash` | Flash the lights |
| `security_honk` | `security honk` | Honk the horn |
| `security_remote_start` | `security remote-start` | Enable remote start |
| `trunk_open` | `trunk open` | Open the trunk |
| `trunk_close` | `trunk close` | Close the trunk |
| `trunk_frunk` | `trunk frunk` | Open the frunk |
| `trunk_window` | `trunk window` | Vent or close windows |
| `media_play_pause` | `media play-pause` | Toggle media play/pause |
| `media_next_track` | `media next-track` | Skip to next track |
| `media_prev_track` | `media prev-track` | Go to previous track |
| `media_adjust_volume` | `media adjust-volume` | Set media volume level |
| `nav_send` | `nav send` | Send destination to vehicle |
| `nav_supercharger` | `nav supercharger` | Navigate to nearest Supercharger |
| `software_schedule` | `software schedule` | Schedule software update |
| `software_cancel` | `software cancel` | Cancel pending software update |
| `cache_clear` | `cache clear` | Clear response cache |

### Trigger Tools (telemetry mode only)

When the MCP server is running with telemetry active (`tescmd serve VIN`), four additional tools are registered for managing trigger subscriptions. These are custom tools backed by in-memory state, not CLI commands.

| Tool | Description | Parameters |
|------|-------------|------------|
| `trigger_create` | Create a telemetry trigger | `field`, `operator`, `value?`, `once?`, `cooldown_seconds?` |
| `trigger_delete` | Delete a trigger by ID | `id` |
| `trigger_list` | List all active triggers | -- |
| `trigger_poll` | Poll for fired notifications | -- |

These tools are not available in MCP-only mode (`--no-telemetry`) since there is no telemetry data to evaluate triggers against.

See [openclaw.md](openclaw.md#trigger-subscription-system) for full trigger documentation including operators, geofencing, and firing modes.

**Example: Create a low-battery trigger**

```json
{
  "name": "trigger_create",
  "arguments": {
    "field": "BatteryLevel",
    "operator": "lt",
    "value": 20,
    "once": true
  }
}
```

> **Note:** Trigger tools use flat parameters (not the `vin`/`args` format of CLI-based tools). Each parameter is a top-level key in `arguments`.

**Example: Poll for notifications**

```json
{
  "name": "trigger_poll",
  "arguments": {}
}
```

Returns:

```json
{
  "notifications": [
    {
      "trigger_id": "a1b2c3d4e5f6",
      "field": "BatteryLevel",
      "operator": "lt",
      "threshold": 20,
      "value": 18,
      "previous_value": 21,
      "fired_at": "2026-01-31T10:30:00Z",
      "vin": "5YJ3E1EA1NF000000"
    }
  ]
}
```

### Excluded Commands

The following commands are excluded from MCP because they are long-running, interactive, or infrastructure operations:

- `vehicle telemetry stream` -- long-running telemetry session
- `openclaw bridge` -- long-running bridge process
- `auth login` / `auth logout` / `auth register` -- requires browser interaction
- `setup` -- interactive wizard
- `mcp serve` -- the MCP server itself
- `key generate` / `key deploy` / `key enroll` / `key unenroll` -- key management operations

## Tool Parameters

Every tool accepts two optional parameters:

```json
{
  "vin": "5YJ3E1EA1NF000000",
  "args": ["--limit", "80"]
}
```

| Parameter | Type | Description |
|-----------|------|-------------|
| `vin` | string | Vehicle VIN. Optional if `TESLA_VIN` is set. |
| `args` | string[] | Additional CLI arguments passed to the command. |

The `args` array maps directly to CLI flags. For example, to set the charge limit to 80%:

```json
{"name": "charge_limit", "arguments": {"vin": "5YJ3...", "args": ["80"]}}
```

## Response Format

Tool responses are the standard tescmd JSON envelope:

```json
{
  "ok": true,
  "command": "charge.status",
  "data": {
    "battery_level": 72,
    "charging_state": "Disconnected",
    "charge_limit_soc": 80
  },
  "timestamp": "2026-01-31T10:30:00Z"
}
```

Error responses:

```json
{
  "ok": false,
  "command": "charge.start",
  "error": {
    "code": "vehicle_asleep",
    "message": "Vehicle is asleep. Wake it first."
  },
  "timestamp": "2026-01-31T10:30:00Z"
}
```

## Transports

### Streamable HTTP (default)

Starts an HTTP server on `127.0.0.1:PORT/mcp`. Multiple agents can connect simultaneously. The server runs until Ctrl+C.

```bash
tescmd mcp serve --port 8080
```

### stdio

Reads JSON-RPC from stdin and writes responses to stdout. Used by Claude Desktop and Claude Code when tescmd is configured as a subprocess in their MCP server config.

```bash
tescmd mcp serve --transport stdio
```

## Tailscale Funnel

The `--tailscale` option uses Tailscale Funnel, which exposes the MCP server publicly over HTTPS. It does not restrict access to your tailnet.

> **Security warning:** Funnel is public internet exposure. HTTP OAuth requires owner sign-in and explicit approval, but only enable Funnel after verifying the login/approval flow, using a unique strong owner password, and keeping the OAuth database persistent and private. Prefer a tailnet-only route when public access is not needed.

```bash
tescmd mcp serve --tailscale
```

The server starts on `127.0.0.1:8080`; Funnel makes it publicly reachable at `https://<hostname>.tailnet.ts.net/mcp`.

**Requirements:** Tailscale installed, running, and authenticated with Funnel enabled in your tailnet ACL.

**Cannot be combined with `--transport stdio`** (stdio is a local pipe, not an HTTP endpoint).

Use this only when you intend to make the service reachable from the public internet. The owner will need to approve each OAuth client.

## Tesla Authentication

The MCP server uses whatever Tesla authentication is configured for tescmd. Before starting the server, ensure you have a valid token:

```bash
# Check auth status
tescmd auth status

# Login if needed
tescmd auth login
```

The server inherits all tescmd configuration: environment variables, `.env` files, config profiles, and token storage.
