#!/bin/bash
# Start the headless FreeCAD XML-RPC bridge, then expose the freecad-mcp stdio
# MCP server over streamable HTTP via supergateway (for LiteLLM).
#
# - headless_server.py runs inside freecadcmd (FreeCAD's bundled Python); it
#   needs no display and no Qt. It binds 0.0.0.0:9875 (--allow-remote) only
#   because the XML-RPC client (freecad-mcp in this same container) connects
#   via the container's network namespace; there is no auth, so the service
#   stays on the backend network and is never exposed further.
# - freecad-mcp connects to the bridge at localhost:9875 and serves MCP on
#   stdio; supergateway bridges that to POST /mcp on :8000.
set -euo pipefail

# Headless bridge in the background. freecadcmd eats unknown flags itself;
# --pass forwards everything after it to the script's argparse. (Port can
# also come from FC_MCP_PORT; the MCP client side hardcodes 9875.)
freecadcmd /opt/FreeCADMCP/headless_server.py --pass --allow-remote &
BRIDGE_PID=$!

cleanup() {
	kill "${BRIDGE_PID}" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# Wait for the XML-RPC bridge (bash /dev/tcp probe, zero deps).
bridge_ready=0
for _ in $(seq 1 120); do
	if (exec 3<>"/dev/tcp/127.0.0.1/9875") 2>/dev/null; then
		exec 3>&- 3<&- || true
		bridge_ready=1
		break
	fi
	if ! kill -0 "${BRIDGE_PID}" 2>/dev/null; then
		echo "freecadcmd bridge exited before listening" >&2
		exit 1
	fi
	sleep 2
done
if [ "${bridge_ready}" -ne 1 ]; then
	echo "XML-RPC bridge on :9875 never came up" >&2
	exit 1
fi

# stdio -> streamable HTTP. freecad-mcp's --host is the *bridge* address
# (the bridge port is hardcoded to 9875 in the fork's client); the server
# itself speaks stdio only.
exec supergateway \
	--stdio "freecad-mcp --host localhost" \
	--outputTransport streamableHttp \
	--host 0.0.0.0 \
	--port 8000
