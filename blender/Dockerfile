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
# Base: python:3.12-slim-bookworm rather than debian:bookworm-slim because
# it is the slim Debian base that ships a working CA store, and the
# deployment network 403s plain HTTP, so apt must run over https from the
# very first layer. The preinstalled Python is not used by the MCP server;
# uv (for the Blender bridge) and nodejs (for supergateway in this container
# too) come from apt once https works.
FROM python:3.12-slim-bookworm

ARG BLENDER_VERSION=5.0.1
# Pin the exact release file; check https://download.blender.org/release/Blender5.0/
# when bumping the version.
ARG BLENDER_URL=https://download.blender.org/release/Blender5.0/blender-5.0.1-linux-x64.tar.xz

# The deployment network 403s plain HTTP; switch apt to https.
RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g; s|http://security.debian.org|https://security.debian.org|g' /etc/apt/sources.list.d/debian.sources 2>/dev/null || sed -i 's|http://deb.debian.org|https://deb.debian.org|g; s|http://security.debian.org|https://security.debian.org|g' /etc/apt/sources.list \
    && apt-get update \
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
        npm \
        tini \
    && rm -rf /var/lib/apt/lists/*

# uv installs the addon and runs the MCP server (uvx mcp-for-blender).
RUN curl -LsSf https://astral.sh/uv/install.sh | sh \
    && mv /root/.local/bin/uv /usr/local/bin/uv \
    && mv /root/.local/bin/uvx /usr/local/bin/uvx

# supergateway bridges the stdio MCP server to streamable HTTP for LiteLLM.
RUN npm install -g supergateway@4.2.1

# Create the runtime user first: the addon must be installed into the
# runtime user's Blender config (~/.config/blender/5.0/scripts/addons), not
# the build user's, because the installer and the addon auto-start both
# resolve the config dir from $HOME at runtime.
RUN useradd --create-home --shell /usr/sbin/nologin blender \
    && mkdir -p /opt/uv-tools \
    && chown blender:blender /opt/uv-tools

# Blender 5.0.1 (the version the printer-mcp bridge docs verified with this
# addon; matches the fork's worked example). The per-user config tree is
# pre-created under the runtime user's HOME so install-addon finds it.
RUN mkdir -p /opt/blender \
    && curl -L -o /tmp/blender.tar.xz "${BLENDER_URL}" \
    && tar -xJf /tmp/blender.tar.xz -C /opt/blender --strip-components=1 \
    && rm /tmp/blender.tar.xz \
    && ln -s /opt/blender/blender /usr/local/bin/blender \
    && mkdir -p /home/blender/.config/blender/5.0/scripts/addons \
    && mkdir -p /home/blender/.config/blender/5.0/extensions/user_default \
    && chown -R blender:blender /home/blender

# Install the addon and enable it, headlessly, AS the runtime user
# (install-addon and setup both resolve the config dir from $HOME).
# `install-addon` copies addon.py (bl_info name "MCP for Blender") to
# <config>/5.0/scripts/addons/blender_mcp.py; `setup` enables it by module
# name and saves userpref. The addon's own auto-start (enabled by default)
# then starts the :9876 listener when the GUI session boots.
#
# The MCP server itself is installed as a uv TOOL (persistent env with a
# real entrypoint binary), not via ephemeral `uvx`: this container is
# backend-network-only with no internet, and `uvx mcp-for-blender` at
# runtime would try to resolve the package from PyPI and die (DNS error)
# the moment supergateway spawns it. UV_TOOL_DIR points at /opt so the
# env survives and is executable regardless of HOME cache state.
# BLENDER_MCP_SAFE_MODE=1 keeps model-authored Python from escaping (blocks
# file/network/exec) while still allowing full bpy scene editing.
USER blender
ENV HOME=/home/blender UV_TOOL_DIR=/opt/uv-tools
WORKDIR /home/blender
RUN BLENDER_MCP_SAFE_MODE=1 uvx mcp-for-blender@2.2.7 install-addon \
    && BLENDER_MCP_SAFE_MODE=1 uvx mcp-for-blender@2.2.7 setup \
    && UV_TOOL_DIR=/opt/uv-tools uv tool install mcp-for-blender@2.2.7 \
    && ls /opt/uv-tools/mcp-for-blender/bin/

# Configure the addon preferences: the addon refuses non-loopback binding
# unless allow_remote_connections is set. The stdio MCP server connects from
# this same container (loopback), but the bind address is set to 0.0.0.0
# anyway so the listener works identically regardless of container
# networking changes. Read on next connection, so setting it in userpref
# before launch is sufficient.
COPY blender_mcp_setup.py /home/blender/blender_mcp_setup.py
RUN blender --background --python /home/blender/blender_mcp_setup.py \
    && rm /home/blender/blender_mcp_setup.py

COPY --chown=blender:blender start.sh /opt/start.sh
RUN chmod +x /opt/start.sh

EXPOSE 8000

# tini is PID 1 so orphaned processes get reaped. start.sh ends in
# `exec supergateway`, and the gateway starts the stdio MCP server through a
# shell for each request and stops the pair together; the server is then
# orphaned onto PID 1. Node only waits on children it spawned itself, so
# with the gateway as PID 1 every request leaves a defunct process behind.
# Do not also set `init: true` on the compose service: tini would not be
# PID 1 and warns about it on every start.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/opt/start.sh"]
