#!/bin/bash
# Start Blender (full GUI) on a virtual display so the mcp-for-blender addon's
# event-loop-dependent socket server runs, then bridge the stdio MCP server to
# streamable HTTP for LiteLLM via supergateway.
#
# - The addon auto-starts its :9876 listener once the UI/scene context is
#   ready (it retries on a timer if the first attempt races startup).
# - mcp-for-blender is stdio-only; supergateway exposes it as HTTP :8000/mcp,
#   which is the endpoint LiteLLM's mcp_servers.blender entry points at.
set -euo pipefail

# Fresh virtual display for this container.
rm -f /tmp/.X99-lock
Xvfb :99 -screen 0 1280x800x24 -nolisten tcp &
XVFB_PID=$!

cleanup() {
	kill "${XVFB_PID}" "${BLENDER_PID}" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# Wait for Xvfb before launching Blender.
for _ in $(seq 1 30); do
	if [ -e /tmp/.X11-unix/X99 ]; then
		break
	fi
	sleep 0.5
done

export DISPLAY=:99
export BLENDER_MCP_SAFE_MODE=1

# Blender in the background; it becomes the addon's TCP host on :9876.
/opt/blender/blender &
BLENDER_PID=$!

# Wait for the addon listener before starting the MCP bridge, so the first
# client connection does not race a not-yet-started addon. Bash /dev/tcp
# probe: zero-dependency (no system python in this image).
addon_ready=0
for _ in $(seq 1 120); do
	if (exec 3<>"/dev/tcp/127.0.0.1/9876") 2>/dev/null; then
		exec 3>&- 3<&- || true
		addon_ready=1
		break
	fi
	if ! kill -0 "${BLENDER_PID}" 2>/dev/null; then
		echo "blender exited before the addon listener came up" >&2
		exit 1
	fi
	sleep 2
done
if [ "${addon_ready}" -ne 1 ]; then
	echo "addon listener on :9876 never came up" >&2
	exit 1
fi

# stdio -> streamable HTTP bridge (--outputTransport streamableHttp serves
# POST /mcp on :8000, the shape LiteLLM's transport:"http" expects).
# BLENDER_HOST=127.0.0.1 (default) points the stdio server at the addon in
# this same container. Dies with the container; the Xvfb/Blender processes
# are cleaned up by the trap.
exec supergateway \
	--stdio "uvx mcp-for-blender" \
	--outputTransport streamableHttp \
	--host 0.0.0.0 \
	--port 8000
