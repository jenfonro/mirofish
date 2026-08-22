# Stage 1: build the Vue WebUI into mirofish/static
FROM node:22-slim AS webui
WORKDIR /build/webui
COPY webui/package.json webui/package-lock.json ./
RUN npm ci --no-fund --no-audit
COPY webui/ ./
# vite outDir is ../mirofish/static, i.e. /build/mirofish/static
RUN npm run build

# Stage 2: Python runtime
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MIROFISH_IN_DOCKER=1 \
    MIROFISH_CRED_BACKEND=file

WORKDIR /app
COPY pyproject.toml /app/
COPY mirofish/ /app/mirofish/
COPY --from=webui /build/mirofish/static /app/mirofish/static
RUN pip install --no-cache-dir /app && rm -rf /root/.cache

RUN useradd -r -u 10001 -m mirofish \
    && mkdir -p /data \
    && chown -R mirofish:mirofish /data
USER mirofish

EXPOSE 8787
VOLUME ["/data"]

CMD ["python3", "-m", "mirofish", "--data-dir", "/data", "serve", "--host", "0.0.0.0", "--port", "8787"]
