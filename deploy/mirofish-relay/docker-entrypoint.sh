#!/usr/bin/env bash
# Single-container launcher: run the relay only. Proxies are hand-maintained
# HTTP/HTTPS/SOCKS5 endpoints dialed directly by the relay; there is no bundled
# proxy engine and no subscription.
set -euo pipefail

exec python3 -m mirofish --data-dir /data serve --host 0.0.0.0 --port 8787
