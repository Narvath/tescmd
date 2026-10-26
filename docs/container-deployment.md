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

Install tescmd from Unraid Community Applications using the template at:

```
https://raw.githubusercontent.com/oceanswave/tescmd/main/unraid/tescmd.xml
```

Map a persistent appdata directory (for example, `/mnt/user/appdata/tescmd`) to
`/data`. Choose one of the two auth modes and configure the corresponding fields
in the CA template. The image runs as UID/GID `99:100` (`nobody:users`) to
support typical Unraid appdata permissions.

**Before first start**, ensure your appdata directory exists with write permissions for UID/GID `99:100` (`nobody:users`), then run the setup wizard:

```bash
# Replace /mnt/user/appdata/tescmd with your chosen App Data path if customized
mkdir -p /mnt/user/appdata/tescmd && chown -R 99:100 /mnt/user/appdata/tescmd
docker run --rm -it --user 99:100 -v /mnt/user/appdata/tescmd:/data ghcr.io/oceanswave/tescmd:latest setup
```

*(If you customized the App Data path in your Unraid template, substitute your selected path in place of `/mnt/user/appdata/tescmd`. Pre-creating the directory with `chown -R 99:100` ensures proper write access so the setup wizard can write credentials and key files).*

### Generating a static token

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

Copy the output into the **MCP Static Token** field in the CA template (and set
`TESCMD_MCP_TOKEN` in your agent config).

The included Compose file is for local testing and publishes only on
`127.0.0.1`. Keep `/data` persistent and private: it stores Tesla credentials
and the OAuth client/token database. Only expose the service over Tailscale or
behind an HTTPS reverse proxy once you have validated your chosen auth mode.

## Agent Configuration

> [!WARNING]
> **Transport Security Notice**: Do not send your static bearer token over unencrypted plaintext HTTP (`http://`) across a local network or the internet. Anyone observing network traffic can capture the token and invoke vehicle-control tools. Always use an **HTTPS** endpoint (via an HTTPS reverse proxy such as Nginx Proxy Manager, Caddy, or Traefik) or a protected tunnel (such as Tailscale or WireGuard) for connections between machines. Use `http://127.0.0.1:8080/mcp` only when the client and server reside on the same physical host.

### Claude Desktop (Static Token)

Add to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "tescmd": {
      "type": "streamable-http",
      "url": "https://tescmd.example.com/mcp",
      "headers": {
        "Authorization": "Bearer YOUR_TESCMD_MCP_TOKEN"
      }
    }
  }
}
```

Replace `https://tescmd.example.com/mcp` with your HTTPS reverse proxy or Tailscale URL (or `http://127.0.0.1:8080/mcp` if Claude Desktop runs locally on the same host), and `YOUR_TESCMD_MCP_TOKEN` with the value configured in the container.

### Claude Code

Because `.mcp.json` is typically committed to version control, **do not hardcode your bearer token** in `.mcp.json`. Instead, use environment variable expansion (`${TESCMD_MCP_TOKEN}`) or register the server at user scope.

#### Option A: Project `.mcp.json` (environment variable expansion)

Add to `.mcp.json` in your project root:

```json
{
  "mcpServers": {
    "tescmd": {
      "type": "http",
      "url": "https://tescmd.example.com/mcp",
      "headers": {
        "Authorization": "Bearer ${TESCMD_MCP_TOKEN}"
      }
    }
  }
}
```

Set `export TESCMD_MCP_TOKEN="YOUR_STATIC_TOKEN"` in your shell environment (e.g. `~/.bashrc` or `~/.zshrc`).

#### Option B: User Scope via CLI

Register the server globally for your user account so configuration is stored in `~/.claude.json` and never checked into Git:

```bash
claude mcp add --transport http tescmd --scope user https://tescmd.example.com/mcp --header "Authorization: Bearer ${TESCMD_MCP_TOKEN}"
```

### Open-WebUI / LibreChat / Other OpenAI-compatible Agents

Most agents support configuring an MCP server via URL and a custom Authorization header. Use:
- **URL**: `https://tescmd.example.com/mcp` (or `http://127.0.0.1:8080/mcp` on localhost)
- **Authorization**: `Bearer YOUR_TESCMD_MCP_TOKEN`

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
