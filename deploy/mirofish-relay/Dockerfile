FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MIROFISH_IN_DOCKER=1 \
    MIROFISH_CRED_BACKEND=file

WORKDIR /app
COPY scripts/general/mirofish_relay.py /app/mirofish_relay.py

RUN useradd -r -u 10001 -m mirofish \
    && mkdir -p /data \
    && chown -R mirofish:mirofish /data
USER mirofish

EXPOSE 8787
VOLUME ["/data"]

CMD ["python3", "/app/mirofish_relay.py", "--data-dir", "/data", "serve", "--host", "0.0.0.0", "--port", "8787"]
