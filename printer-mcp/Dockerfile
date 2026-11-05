# =============================================================================
# Printer MCP - bambu-printer-mcp + Bambu Studio CLI slicer
# =============================================================================
# Built by the openwebui_stack role on the deployment host (Photon), the same
# way the code-sandbox and chatterbox images are. Serves the printer_3d MCP
# tool server (streamable HTTP :3000/mcp) with:
#   - bambu-printer-mcp@1.1.59 (H2C printing support; the parent
#     mcp-3D-printer-server is slicing-only for H2C)
#   - Bambu Studio v02.08.02.60 (first stable line with H2C machine presets;
#     contributor-verified for CLI slicing), extracted from the AppImage so
#     no FUSE is needed in the container
#   - ffmpeg for H2C RTSP camera snapshots
#   - uv so the server can spawn `uvx mcp-for-blender` as its Blender bridge
#     child process (BLENDER_MCP_COMMAND=uvx)
#
# Rebuild: ansible-playbook playbooks/site.yml (docker_compose build: always)
FROM node:24-bookworm-slim

ARG BAMBU_STUDIO_VERSION=v02.08.02.60
ARG BAMBU_STUDIO_ASSET=BambuStudio_ubuntu24.04-v02.08.02.60-20260814171356.AppImage
ARG BAMBU_STUDIO_SHA256=b78d2527a20ee9fbcf70ee82138c3b3ca707aa9c6625881629db29627252acc3

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        xz-utils \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# uv provides the `uvx` command the server uses to spawn mcp-for-blender.
RUN curl -LsSf https://astral.sh/uv/install.sh | sh \
    && mv /root/.local/bin/uv /usr/local/bin/uv \
    && mv /root/.local/bin/uvx /usr/local/bin/uvx

RUN npm install -g bambu-printer-mcp@1.1.59

# Bambu Studio CLI slicer. The AppImage is extracted (--appimage-extract) so
# no FUSE is required; the extracted tree carries resources/profiles with the
# H2C machine presets. Verify the pinned SHA-256 before extracting.
RUN curl -L -o /tmp/bambu-studio.AppImage \
        "https://github.com/bambulab/BambuStudio/releases/download/${BAMBU_STUDIO_VERSION}/${BAMBU_STUDIO_ASSET}" \
    && echo "${BAMBU_STUDIO_SHA256}  /tmp/bambu-studio.AppImage" | sha256sum -c - \
    && chmod +x /tmp/bambu-studio.AppImage \
    && /tmp/bambu-studio.AppImage --appimage-extract \
    && mv squashfs-root /opt/bambu-studio \
    && rm /tmp/bambu-studio.AppImage \
    && chmod +x /opt/bambu-studio/BambuStudio \
    && mkdir -p /opt/bambu-studio/resources/profiles/BBL || true

ENV SLICER_TYPE=bambustudio \
    SLICER_PATH=/opt/bambu-studio/BambuStudio \
    BAMBU_PROFILES_ROOT=/opt/bambu-studio/resources/profiles \
    MCP_TRANSPORT=streamable-http \
    MCP_HTTP_HOST=0.0.0.0 \
    MCP_HTTP_PORT=3000 \
    MCP_HTTP_PATH=/mcp

# Non-root runtime user. The MCP server only reads STLs from the read-only
# /models mount and writes slice artifacts to its own temp dir.
RUN useradd --create-home --shell /usr/sbin/nologin printer \
    && mkdir -p /home/printer/.config \
    && chown -R printer:printer /home/printer /opt/bambu-studio
USER printer
WORKDIR /home/printer

EXPOSE 3000

CMD ["bambu-printer-mcp"]
