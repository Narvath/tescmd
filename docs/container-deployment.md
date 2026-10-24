# Docker deployment

The Docker image runs the MCP server without telemetry and stores tescmd's
configuration, Tesla tokens, keys, and cache under `/data`. Keep `/data`
persistent across container replacement to avoid repeating setup.

## Local Docker test

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
# Set unique TESCMD_MCP_AUTH_USERNAME and TESCMD_MCP_AUTH_PASSWORD values in .env.
docker compose run --rm tescmd setup
docker compose up --build -d
docker compose ps
```

The MCP endpoint is available at `http://127.0.0.1:8080/mcp` on the Docker
host. The Compose port is deliberately bound to loopback. Use
`docker compose logs -f` to inspect startup and `docker compose down` to stop.
The named `tescmd-data` volume is retained by `docker compose down`; remove
that volume only if you intend to erase the saved configuration and tokens.

Run the setup wizard interactively against the same persistent volume:

```powershell
docker compose run --rm tescmd setup
```

For an already configured installation, copy its config directory into
`/data/config` in the named volume and ensure the container's `99:100` user can
read it.

## Unraid

Create an Unraid Community Applications template using the published tescmd
image once an image registry and release workflow are configured. Map a
persistent appdata directory (for example, `/mnt/user/appdata/tescmd`) to
`/data` and configure `TESCMD_MCP_AUTH_USERNAME` plus a unique
`TESCMD_MCP_AUTH_PASSWORD` of at least 16 characters. The image runs as UID/GID `99:100`
(`nobody:users`) to support typical Unraid appdata permissions.

The included Compose file is for local testing and publishes only on
`127.0.0.1`. The HTTP MCP server requires an owner login and explicit approval
for each OAuth client. Keep `/data` persistent and private: it stores Tesla
credentials and the OAuth client/token database. Tailscale Funnel is publicly
reachable; only expose the service after validating the owner login/approval
flow and configuring HTTPS. The MCP server remains on the app's default bridge
network; Tailscale integration and the eventual Community Applications
template are separate follow-up work.

## Image behavior

- Base image: `python:3.12-slim`
- Default command: `tescmd serve --no-telemetry`
- Container port: `8080`
- Persistent data: `/data/config`, `/data/cache` (including OAuth registrations and tokens)
- Health check: verifies the local server port accepts TCP connections
- Runtime user: UID/GID `99:100`

Tesla OAuth credentials and vehicle enrollment remain part of the normal
`tescmd setup` process. The MCP owner credentials protect client authorization;
they do not replace Tesla authentication.
