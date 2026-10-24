FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/data \
    TESLA_CONFIG_DIR=/data/config \
    TESLA_CACHE_DIR=/data/cache \
    TESLA_TOKEN_FILE=/data/config/tokens.json \
    TESCMD_HOST=0.0.0.0 \
    TESCMD_MCP_PORT=8080

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN python -m pip install --no-cache-dir . \
    && mkdir -p /data/config /data/cache \
    && chown -R 99:100 /data

USER 99:100

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import socket; socket.create_connection(('127.0.0.1', 8080), timeout=2).close()"]

CMD ["tescmd", "serve", "--no-telemetry", "--host", "0.0.0.0", "--port", "8080"]
