# =============================================================================
# FreeCAD MCP - headless FreeCAD + neka-nat-style XML-RPC bridge
# =============================================================================
# Built by the openwebui_stack role on the deployment host (Photon), the same
# way the code-sandbox and chatterbox images are.
#
# The headless bridge (alosslessdev fork of neka-nat/freecad-mcp, commit
# 6f0b530 "Add headless mode") serves the SAME XML-RPC interface as the GUI
# addon but runs under freecadcmd with no Qt/display. The freecad-mcp MCP
# server talks stdio only, so supergateway bridges it to streamable HTTP for
# LiteLLM (mcp_servers.freecad -> :8000/mcp here).
#
# Version handshake gotcha: the MCP server and the bridge compare versions;
# this fork is 0.1.24 while PyPI publishes upstream's 0.1.28. Both pieces are
# therefore taken from the fork at the same commit (pinned below), keeping the
# handshake clean. Do NOT swap the pip install for `pip install freecad-mcp`.
#
# The XML-RPC bridge has NO authentication and executes model-authored Python
# in FreeCAD. This container is backend-network-only (no internet, no Traefik
# route, no docker socket); its only client is the freecad-mcp stdio server
# in this same container over loopback.
# Base: python:3.12-slim-bookworm rather than debian:bookworm-slim because
# it is the slim Debian base that ships a working CA store, and the
# deployment network 403s plain HTTP, so apt must run over https from the
# very first layer.
FROM python:3.12-slim-bookworm

ARG FREECAD_TAG=1.1.4
ARG FREECAD_APPIMAGE=FreeCAD_1.1.4-Linux-x86_64-py311.AppImage
ARG FREECAD_SHA256=f6dc6ba676e5ac96a565ebc8d657232f94c6158e85b4352141bd1a46f6b43434
ARG FREECAD_MCP_REF=6f0b530b3e562cb9b7fd79709d84191413fbd17d

# The fork source is vendored into the build context (fetched from
# https://github.com/alosslessdev/freecad-mcp/archive/<ref>.tar.gz) because
# the deployment network's DNS for github.com is unreliable from inside
# build containers; the build never needs the network for this source.

# The deployment network 403s plain HTTP; switch apt to https.
RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g; s|http://security.debian.org|https://security.debian.org|g' /etc/apt/sources.list.d/debian.sources 2>/dev/null || sed -i 's|http://deb.debian.org|https://deb.debian.org|g; s|http://security.debian.org|https://security.debian.org|g' /etc/apt/sources.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        xz-utils \
        libgl1 \
        libegl1 \
        libxext6 \
        libxfixes3 \
        libxi6 \
        libxkbcommon0 \
        libxrender1 \
        libsm6 \
        libice6 \
        libfontconfig1 \
        libfreetype6 \
        libgomp1 \
        libharfbuzz0b \
        libb2-1 \
        nodejs \
        npm \
        git \
    && rm -rf /var/lib/apt/lists/*

# uv installs the freecad-mcp server (python >=3.12; FreeCAD's bundled
# py311 is only for the bridge inside freecadcmd, never for the MCP server).
RUN curl -LsSf https://astral.sh/uv/install.sh | sh \
    && mv /root/.local/bin/uv /usr/local/bin/uv \
    && mv /root/.local/bin/uvx /usr/local/bin/uvx

# supergateway bridges the stdio MCP server to streamable HTTP.
RUN npm install -g supergateway@4.2.1

# FreeCAD 1.1.4 stable, extracted from the AppImage (no FUSE at runtime).
# The extracted tree ships freecadcmd with its bundled Python 3.11 and all
# native libs; the debian packages above cover the host-side GL/X deps the
# bundled binaries still dlopen.
RUN curl -L -o /tmp/freecad.AppImage \
        "https://github.com/FreeCAD/FreeCAD/releases/download/${FREECAD_TAG}/${FREECAD_APPIMAGE}" \
    && echo "${FREECAD_SHA256}  /tmp/freecad.AppImage" | sha256sum -c - \
    && chmod +x /tmp/freecad.AppImage \
    && /tmp/freecad.AppImage --appimage-extract \
    && mv squashfs-root /opt/freecad \
    && rm /tmp/freecad.AppImage \
    && ln -s /opt/freecad/bin/freecadcmd /usr/local/bin/freecadcmd \
    && chmod +x /opt/freecad/bin/freecadcmd \
    && freecadcmd --version || true

# MCP server + bridge from the same fork commit (see version note above),
# from the vendored tarball — no network access needed. One COPY feeds both:
# the addon tree for the headless bridge, and the source dir for uv.
COPY freecad-mcp-6f0b530.tar.gz /tmp/freecad-mcp-src.tar.gz
RUN mkdir -p /tmp/freecad-mcp-src \
    && tar -xzf /tmp/freecad-mcp-src.tar.gz -C /tmp/freecad-mcp-src --strip-components=1 \
    && mv /tmp/freecad-mcp-src/addon/FreeCADMCP /opt/FreeCADMCP \
    && uv tool install --python 3.12 /tmp/freecad-mcp-src \
    && ln -s /root/.local/bin/freecad-mcp /usr/local/bin/freecad-mcp \
    && rm -rf /tmp/freecad-mcp-src /tmp/freecad-mcp-src.tar.gz

# Non-root user owning /models writes and the FreeCAD config dir.
# FREECAD_USER_HOME points FreeCAD's user data (getUserAppDataDir) at the
# user's home so settings/parts-library writes work as this user.
RUN useradd --create-home --shell /usr/sbin/nologin freecad \
    && mkdir -p /home/freecad/.local \
    && chown -R freecad:freecad /home/freecad /opt/freecad /opt/FreeCADMCP
USER freecad
ENV FREECAD_USER_HOME=/home/freecad
WORKDIR /home/freecad

COPY --chown=freecad:freecad start.sh /opt/start.sh
RUN chmod +x /opt/start.sh

EXPOSE 8000

CMD ["/opt/start.sh"]
