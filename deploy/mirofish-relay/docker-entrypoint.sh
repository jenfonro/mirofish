#!/usr/bin/env bash
# Single-container launcher: run the Mihomo proxy engine (when a subscription is
# configured) and the relay side by side in one container. If either process
# exits, tear the other down and exit non-zero so Docker restarts the container
# with both processes in a known-good state.
set -euo pipefail

MIHOMO_PID=""
RELAY_PID=""

start_mihomo() {
  local home=/data/mihomo
  mkdir -p "$home"
  # Regenerate the sidecar config from the current subscription settings on
  # every start; the provider cache under $home persists across restarts.
  python3 -m mirofish --data-dir /data mihomo-config --output "$home/config.yaml"
  mihomo -d "$home" &
  MIHOMO_PID=$!
  # The relay reaches the engine over the loopback interface of this container.
  export MIROFISH_MIHOMO_CONTROLLER="http://127.0.0.1:9090"
  export MIROFISH_MIHOMO_PROXY="http://127.0.0.1:7890"
  echo "mirofish: mihomo proxy engine started (pid ${MIHOMO_PID})"
}

if [ -n "${MIROFISH_PROXY_SUBSCRIPTION_URL:-}" ] || [ -n "${MIROFISH_PROXY_SUBSCRIPTION_FILE:-}" ]; then
  start_mihomo
else
  echo "mirofish: no proxy subscription configured; running in direct mode (no mihomo)"
fi

python3 -m mirofish --data-dir /data serve --host 0.0.0.0 --port 8787 &
RELAY_PID=$!

shutdown() {
  trap - TERM INT
  [ -n "${MIHOMO_PID}" ] && kill -TERM "${MIHOMO_PID}" 2>/dev/null || true
  [ -n "${RELAY_PID}" ] && kill -TERM "${RELAY_PID}" 2>/dev/null || true
}
trap shutdown TERM INT

# Return as soon as either managed process exits.
wait -n
echo "mirofish: a managed process exited; stopping the container for restart"
shutdown
wait 2>/dev/null || true
exit 1
