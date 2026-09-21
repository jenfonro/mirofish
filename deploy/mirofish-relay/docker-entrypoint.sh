#!/usr/bin/env bash
set -euo pipefail
exec python3 -m mirofish --data-dir /data serve --host 0.0.0.0 --port 8787
