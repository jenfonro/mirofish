# Stage 1: build the Vue WebUI into mirofish/static
FROM node:22-slim AS webui
WORKDIR /build/webui
COPY webui/package.json webui/package-lock.json ./
RUN npm ci --no-fund --no-audit
COPY webui/ ./
# vite outDir is ../mirofish/static, i.e. /build/mirofish/static
RUN npm run build

# Stage 2: Python runtime + bundled Mihomo (single self-contained container)
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MIROFISH_IN_DOCKER=1 \
    MIROFISH_CRED_BACKEND=file \
    PATH="/app/.venv/bin:$PATH"

# Bundle the Mihomo proxy engine so one container serves both the relay and the
# subscription-native proxy pool instead of a separate sidecar + init container.
COPY --from=metacubex/mihomo:v1.19.29 /mihomo /usr/local/bin/mihomo

WORKDIR /app
COPY pyproject.toml uv.lock /app/
COPY mirofish/ /app/mirofish/
COPY --from=webui /build/mirofish/static /app/mirofish/static
RUN uv sync --locked --no-dev --no-editable && uv cache clean

COPY deploy/mirofish-relay/docker-entrypoint.sh /usr/local/bin/mirofish-entrypoint
RUN chmod +x /usr/local/bin/mirofish-entrypoint /usr/local/bin/mihomo

RUN useradd -r -u 10001 -m mirofish \
    && mkdir -p /data \
    && chown -R mirofish:mirofish /data
USER mirofish

EXPOSE 8787
VOLUME ["/data"]

# The entrypoint starts mihomo (when a subscription is set) then the relay.
CMD ["/usr/local/bin/mirofish-entrypoint"]
