#!/usr/bin/env bash
# Run the relay as PID 1's only managed child.
#
# This used to also launch a Mihomo engine and tear both down together when
# either died. Proxy nodes are now entered by an operator and dialled directly,
# so there is no second process to supervise.
set -euo pipefail

exec python3 -m mirofish --data-dir /data serve --host 0.0.0.0 --port 8787
