# CLAUDE.md — Project Context for Claude Code

## Project Overview

**tescmd** is a Python 3.11+ CLI for querying and controlling Tesla vehicles via the [Tesla Fleet API](https://developer.tesla.com/docs/fleet-api). It covers auth, vehicle queries/commands, energy products, Supercharger billing, Fleet Telemetry streaming, OpenClaw bridge, and an MCP server for agent integration.

## Tech Stack

- **Python 3.11+**, **pydantic v2**, **pydantic-settings**, **click**, **httpx** (async), **rich**, **textual**, **cryptography**, **protobuf**, **keyring**, **python-dotenv**
- **websockets** (telemetry + OpenClaw), **mcp** (MCP server), **uvicorn**, **starlette** (HTTP transport) — core dependencies
- Optional: **bleak** (`[ble]` extra — BLE key enrollment)

## Project Structure

```
src/tescmd/
├── cli/                   # Click CLI layer
│   ├── main.py            # Root group, AppContext, _register_commands()
│   ├── _options.py        # Shared Click options/decorators (@global_options)
│   ├── _client.py         # API clients, cache helpers, TTLs, wake handling
│   ├── auth.py, billing.py, cache.py, charge.py, climate.py, energy.py
│   ├── key.py, media.py, mcp_cmd.py, nav.py, openclaw.py, partner.py
│   ├── raw.py, security.py, serve.py, setup.py, sharing.py, software.py
│   └── status.py, trunk.py, user.py, vehicle.py
├── api/                   # HTTP client + domain APIs (composition pattern)
│   ├── client.py          # TeslaFleetClient (base HTTP, auth headers, retries)
│   ├── vehicle.py, command.py, signed_command.py, energy.py
│   ├── charging.py, partner.py, sharing.py, user.py
│   └── errors.py          # AuthError, VehicleAsleepError, TierError, TunnelError, etc.
├── models/                # Pydantic models for auth, command, config, energy, sharing, user, vehicle
├── auth/                  # OAuth2 PKCE, token storage, callback server
├── protocol/              # Vehicle Command Protocol, encoding, signing, protobuf
├── crypto/                # EC key gen, ECDH, Schnorr signatures
├── cache/                 # File-based JSON cache with tiered TTLs
├── output/                # Rich/JSON formatter and display-unit handling
├── telemetry/             # Fleet Telemetry streaming
│   ├── setup.py           # Reusable telemetry_session() context manager
│   ├── server.py, decoder.py, fields.py, mapper.py, flatbuf.py
│   ├── fanout.py, cache_sink.py, csv_sink.py, dashboard.py, tui.py
│   └── tailscale.py, protos/
├── openclaw/              # Gateway bridge, event filters, dispatcher, telemetry store
├── triggers/              # Trigger models, evaluation, cooldowns, notifications
├── mcp/                   # MCP server and tool registration
├── deploy/                # Key hosting (GitHub Pages, Tailscale Serve)
├── ble/                   # Optional BLE key enrollment support
├── config/                # Configuration package
└── _internal/             # VIN, async, permissions, and unit helpers
```

## Coding Conventions

- **Type hints everywhere** — all function signatures, all variables where non-obvious
- **async/await** — all API calls are async; CLI entry points use `run_async()` helper
- **Pydantic models** — all API request/response payloads; all configuration
- **src layout** — code in `src/tescmd/`, tests in `tests/`
- **No star imports** — explicit imports only
- **Single responsibility** — CLI modules handle args + output, API modules handle HTTP
- **Composition over inheritance** — API classes wrap `TeslaFleetClient`, don't extend it
- **Error stream routing** — JSON/piped mode writes errors to stderr; Rich/TTY mode uses stdout

## Key Patterns

**CLI command registration:** New command groups are registered in `cli/main.py:_register_commands()`. Each CLI module defines a Click group and is imported there.

**Global options propagation:** Use `@global_options` decorator from `cli/_options.py` on commands that need VIN, format, wake, cache flags. Options flow through `AppContext` via `@click.pass_obj`.

**API client construction:** `cli/_client.py` provides `get_vehicle_api(app_ctx)` → returns `(client, api)` tuple. Also `get_command_api()` for write commands (handles signed vs unsigned routing).

**Cache:** Read paths that opt into caching use `cached_api_call()` or `cached_vehicle_data()` with scope-aware TTLs (STATIC 1h, SLOW 5m, DEFAULT 1m, FAST 30s). Vehicle and energy-site write commands invalidate relevant entries via `invalidate_cache_for_vin()` / `invalidate_cache_for_site()`.

**Output:** `OutputFormatter` auto-detects TTY → Rich and piped output → JSON; `--quiet` sends Rich output to stderr. JSON success responses have `{ok, command, data, timestamp}` (and optional `_cache` metadata); errors have `{ok, command, error, timestamp}`.

**Telemetry session lifecycle:** `telemetry/setup.py:telemetry_session()` is an async context manager handling server/tunnel setup, partner-domain registration, fleet configuration, and cleanup. It is used by telemetry streaming and the OpenClaw bridge; `cli/serve.py` also uses it when telemetry is enabled.

**OpenClaw pipeline:** `TelemetryServer.on_frame` → `DualGateFilter.should_emit()` → `EventEmitter.to_event()` → `GatewayClient.send_event()`. `TelemetryBridge` wires the components together. `build_openclaw_pipeline()` is shared by `cli/openclaw.py` and `cli/serve.py`. Inbound commands flow: gateway `node.invoke.request` → `CommandDispatcher.dispatch()` → handler → `node.invoke.result`.

**Trigger system:** `TriggerManager` evaluates conditions on every telemetry frame (independent of the dual-gate filter). Supports numeric comparison (`lt`, `gt`, `eq`, etc.), `changed` detection, and geofence `enter`/`leave` with haversine distance. One-shot and persistent (with cooldown) firing modes. Notifications delivered via OpenClaw push and MCP polling (`trigger.poll`).

**MCP tools:** `mcp/server.py` maps CLI-backed tool names to arguments via `_CliToolDef`; `invoke_tool()` runs them through Click's `CliRunner` in JSON mode with `--wake`. Custom tools use direct handlers registered via `register_custom_tool()`. Read/write tools have corresponding `readOnlyHint` annotations. HTTP transport uses an in-memory OAuth provider that auto-approves authorization and accepts permissive client redirect URIs/scopes; DNS-rebinding host/origin checks are transport protections, not user authentication. Do not expose the HTTP MCP server publicly without appropriate network access control and authentication.

## Build & Test

```bash
# Build
python -m build                    # hatchling via pyproject.toml

# Test (pytest-xdist parallelism is enabled by project defaults)
pytest                             # tests, excluding the e2e marker by default
pytest tests/openclaw/ -x -v       # openclaw tests
pytest tests/triggers/ -x -v       # trigger tests
pytest tests/mcp/ -x -v            # mcp tests
pytest -m e2e                      # live API smoke tests (needs TESLA_ACCESS_TOKEN)

# Lint
ruff check src/ tests/
ruff format src/ tests/
mypy src/
```

## Environment Variables

| Variable | Description |
|---|---|
| `TESLA_CLIENT_ID` / `TESLA_CLIENT_SECRET` | OAuth2 app credentials |
| `TESLA_VIN` | Default vehicle VIN |
| `TESLA_REGION` | API region: `na`, `eu`, `cn` |
| `TESLA_ACCESS_TOKEN` / `TESLA_REFRESH_TOKEN` | Direct token override |
| `TESLA_TOKEN_FILE` | File path for token storage (skips keyring) |
| `TESLA_CONFIG_DIR` | Config directory (default: `~/.config/tescmd`) |
| `TESLA_CACHE_DIR` / `TESLA_CACHE_TTL` / `TESLA_CACHE_ENABLED` | Cache settings |
| `TESLA_COMMAND_PROTOCOL` | `auto` (default), `signed`, `unsigned` |
| `TESLA_TEMP_UNIT` / `TESLA_DISTANCE_UNIT` / `TESLA_PRESSURE_UNIT` | Display units |
| `TESLA_DOMAIN` / `TESLA_HOSTING_METHOD` / `TESLA_GITHUB_REPO` | Key hosting |
| `TESLA_SETUP_TIER` | `readonly` or `full` |
| `OPENCLAW_GATEWAY_TOKEN` | OpenClaw gateway auth token |


## Additional Resources

Tesla's published vehicle-command proto omits some VehicleAction fields (e.g. navigation commands). The Teslemetry project maintains a more complete proto: https://github.com/Teslemetry/python-tesla-fleet-api/blob/main/proto/car_server.proto