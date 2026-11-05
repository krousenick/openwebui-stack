# =============================================================================
# Blender MCP - full Blender GUI under Xvfb + mcp-for-blender addon
# =============================================================================
# Built by the openwebui_stack role on the deployment host (Photon), the same
# way the code-sandbox and chatterbox images are.
#
# Why full GUI under Xvfb: the mcp-for-blender addon's auto-start refuses
# `bpy.app.background` (it needs Blender's timer/event loop, and its UI-scene
# context only exists in a real session). Running `blender -b` leaves :9876
# dead. A real (unheaded) Blender on a virtual display keeps the event loop
# alive, so the addon's socket server runs and its auto-start retry timer
# works.
#
# The addon listener has NO authentication and can execute arbitrary Python
# in Blender. This container is therefore backend-network-only (no internet,
# no Traefik route). The MCP client is the supergateway in this same
# container; nothing else connects on :9876.
#
# MCP tool calls arrive exclusively via LiteLLM (mcp_servers.blender ->
# supergateway :8000/mcp -> stdio mcp-for-blender -> TCP :9876 -> addon).
# mcp-for-blender is stdio-only (mcp.run(), no HTTP server), hence the
# supergateway bridge in-container.
FROM debian:bookworm-slim

ARG BLENDER_VERSION=5.0.1
# Pin the exact release file; check https://download.blender.org/release/Blender5.0/
# when bumping the version.
ARG BLENDER_URL=https://download.blender.org/release/Blender5.0/blender-5.0.1-linux-x64.tar.xz

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        xz-utils \
        xvfb \
        libxrender1 \
        libxi6 \
        libxkbcommon0 \
        libgl1 \
        libegl1 \
        libxext6 \
        libxfixes3 \
        libxcursor1 \
        libxrandr2 \
        libxinerama1 \
        libsm6 \
        libice6 \
        libgomp1 \
        libfontconfig1 \
        libfreetype6 \
        mesa-utils \
        nodejs \
    && rm -rf /var/lib/apt/lists/*

# uv installs the addon and runs the MCP server (uvx mcp-for-blender).
RUN curl -LsSf https://astral.sh/uv/install.sh | sh \
    && mv /root/.local/bin/uv /usr/local/bin/uv \
    && mv /root/.local/bin/uvx /usr/local/bin/uvx

# supergateway bridges the stdio MCP server to streamable HTTP for LiteLLM.
RUN npm install -g supergateway@4.2.1

# Blender 5.0.1 (the version the printer-mcp bridge docs verified with this
# addon; matches the fork's worked example).
RUN mkdir -p /opt/blender \
    && curl -L -o /tmp/blender.tar.xz "${BLENDER_URL}" \
    && tar -xJf /tmp/blender.tar.xz -C /opt/blender --strip-components=1 \
    && rm /tmp/blender.tar.xz \
    && ln -s /opt/blender/blender /usr/local/bin/blender

# Install the addon into Blender's user addons dir and enable it, both
# headlessly. `install-addon` copies addon.py (bl_info name "MCP for Blender")
# to <config>/5.0/scripts/addons/blender_mcp.py; `setup` enables it by module
# name and saves userpref. The addon's own auto-start (enabled by default)
# then starts the :9876 listener when the GUI session boots.
# BLENDER_MCP_SAFE_MODE=1 keeps model-authored Python from escaping (blocks
# file/network/exec) while still allowing full bpy scene editing.
RUN BLENDER_MCP_SAFE_MODE=1 uvx mcp-for-blender@2.2.7 install-addon \
    && BLENDER_MCP_SAFE_MODE=1 uvx mcp-for-blender@2.2.7 setup

# Configure the addon preferences for the in-container listener: the addon
# refuses non-loopback binding unless allow_remote_connections is set. The
# stdio MCP server connects from this same container (loopback), but the
# bind address is set to 0.0.0.0 anyway so the listener works identically
# regardless of container networking changes. Read on next connection, so
# setting it in userpref before launch is sufficient.
COPY blender_mcp_setup.py /tmp/blender_mcp_setup.py
RUN blender --background --python /tmp/blender_mcp_setup.py \
    && rm /tmp/blender_mcp_setup.py

# Non-root user owning the config (addons, userpref) and /models writes.
RUN useradd --create-home --shell /usr/sbin/nologin blender \
    && mkdir -p /home/blender/.config \
    && chown -R blender:blender /home/blender/.config
USER blender
WORKDIR /home/blender

COPY --chown=blender:blender start.sh /opt/start.sh
RUN chmod +x /opt/start.sh

EXPOSE 8000

CMD ["/opt/start.sh"]
