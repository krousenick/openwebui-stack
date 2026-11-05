# Plan document: Prompt-to-print pipeline (Open WebUI → Blender/FreeCAD → slice → Bambu H2C)

*Written 2026-11-05, plan mode. Self-contained; no prior context required.*

> **Implementation update (post-plan):** Blender is now a **first-class
> LiteLLM tool server** (`mcp_servers.blender`) instead of being brokered
> through printer-mcp's `blender_mcp_*` bridge tools. mcp-for-blender 2.2.7
> is stdio-only (`mcp.run()`), so the blender container runs its own
> supergateway stdio→streamable-HTTP on `:8000/mcp` — the same pattern as the
> freecad container. printer-mcp no longer needs `uv`/`BLENDER_MCP_COMMAND`
> for Blender (it keeps `uv` in-image only as a general tool). All MCP tool
> calls from Open WebUI go through LiteLLM exclusively; the Blender TCP hop
> (`:9876`, addon) and FreeCAD XML-RPC hop (`:9875`) are *inside* their
> containers, downstream of the gateway. Verified from the 2.2.7 sdist:
> `BLENDER_HOST`/`BLENDER_PORT` env (or `--host/--port`), addon auto-start
> refuses `bpy.app.background` (hence Xvfb), addon prefs `bind_address` +
> `allow_remote_connections` (non-loopback binding is opt-in), addon installs
> as `blender_mcp.py` (module `blender_mcp`), `install-addon`/`setup`
> subcommands run headless.

---

## 1. Goal

Let the user prompt Open WebUI: *"model me a 3D printable X and print it"* — the chat model creates geometry (Blender mesh or FreeCAD parametric CAD), exports a verified STL to a shared volume, slices it (Bambu Studio CLI), and sends the job to a Bambu Lab H2C printer over LAN — with the model asking for confirmation before any print starts.

Stack context: this repo (`openwebui-stack`) is a Docker Compose stack on a VMware Photon VM (deployed at `/mnt/data1/openwebui-stack`; NOT the Mac, NOT OrbStack). Compose/env are Ansible-templated: `ansible/roles/openwebui_stack/templates/docker-compose.yml.j2` and `templates/env.j2`, rendered by `ansible-playbook playbooks/site.yml`. Secrets tier: vault (`ansible/group_vars/all/vault.yml`) → env var → deployed `.env` → generated once + persisted to vault. Existing MCP wiring to copy: `brave_search` (compose service + LiteLLM `mcp_servers` entry in `templates/config.yaml.j2` + `tasks/mcp/brave.yml` tool_server merge + `tasks/mcp/tool_defaults.yml`). LiteLLM exposes each MCP at `http://litellm:4000/mcp/<name>`.

## 2. Researched decisions (and why — the chain of thought)

1. **Printer MCP = `bambu-printer-mcp` (npm, NOT the parent `mcp-3D-printer-server` the user originally linked).** The parent project prints only on `p1s, p1p, x1c, x1e, a1, a1mini, h2d` — H2C is slicing-only there. The same author's Bambu-only fork fully supports H2C printing: correct `ams_mapping2` parallel arrays, H2-series `push_status` handshake, H2C HMS hidden-code tables, dual-nozzle external spool slots (254=left/deputy, 255=right/main). Pin: **`bambu-printer-mcp@1.1.59`** (latest at research time). Repo: github.com/DMontgomery40/bambu-printer-mcp. Run it with `MCP_TRANSPORT=streamable-http`, `MCP_HTTP_HOST=0.0.0.0`, `MCP_HTTP_PORT=3000`, `MCP_HTTP_PATH=/mcp`.
2. **Slicer = Bambu Studio v02.08.02.60** (NOT v02.01.01.52, which the older docs verified but which predates H2C machine presets — H2C presets first ship in v02.05.01.52; 02.08.02.60 is the contributor-verified version for CLI slicing on that line). Use the `ubuntu24.04` release zip/AppImage from github.com/bambulab/BambuStudio releases, **extracted** (`--appimage-extract` / unzip) — no FUSE in containers. Image env: `SLICER_PATH=/opt/bambu-studio/BambuStudio`, `BAMBU_PROFILES_ROOT=/opt/bambu-studio/resources/profiles`, `SLICER_TYPE=bambustudio`, `BAMBU_MODEL=h2c`, `BED_TYPE` (default `textured_plate`), `NOZZLE_DIAMETER=0.4`.
3. **Blender integration = fork bridge + Xvfb headless container** (user decision). The fork brokers Blender itself: set `BLENDER_MCP_COMMAND=uvx` + `BLENDER_MCP_ARGS=["mcp-for-blender"]` on the printer-mcp container; it spawns the stdio MCP server and forwards tools (`blender_mcp_status`, `blender_mcp_call`, `blender_mcp_export_stl` — validated STL export with `output_verified`, dimension report — and `blender_mcp_edit_model` for decimate/remesh/boolean fixes). This gives the model ONE tool server for the whole pipeline (model+slice+print). **Critical constraint:** mcp-for-blender's addon refuses `blender -b` background mode; it needs the GUI event loop. Solution: run full Blender under **Xvfb** (`xvfb-run blender` or `Xvfb :99 + DISPLAY=:99 blender`). Blender container: **Blender 5.0.1** official linux tarball (`https://download.blender.org/release/Blender5.0/blender-5.0.1-linux-x64.tar.xz`; the fork's worked example ran Blender 5.0.1 + this addon), **mcp-for-blender 2.2.7** (PyPI latest; `uvx mcp-for-blender install-addon` to install the addon, or copy `addon.py` into Blender's addons dir and enable at startup — for headless, enable via `--python-expr` or userpref script and configure the addon's **Bind Address** to `0.0.0.0` + **Allow Remote Connections** so the printer-mcp container can reach it on `:9876`). Set `BLENDER_HOST=blender`, `BLENDER_PORT=9876` on printer-mcp. Optional hardening: `BLENDER_MCP_SAFE_MODE=1` (validates AI scripts, blocks file/net/exec escapes). Note: the addon has **no auth** — keep the Blender container on the `backend` network only (internal, no internet), and never expose :9876 beyond it. User chose: locked-down (backend-only, no internet, so Poly Haven/Sketchfab asset downloads are unavailable; model authors geometry itself), 4G/2CPU limits, non-root, models volume rw.
4. **FreeCAD integration = neka-nat-style headless bridge** (user decision: include in v1). **NOT** proximile/FreeCAD-MCP (the user's link): that stack assumes docker-socket access for container-management tools, bundles GPU services (TRELLIS.2 ~24GB VRAM, ComfyUI, VLM/SAM) Photon doesn't have, and Cloudflare tunnels — conflicts with the locked-down posture. Instead: upstream `neka-nat/freecad-mcp` + its **`alosslessdev` fork** (52 commits ahead, adds `addon/FreeCADMCP/headless_server.py`): a headless XML-RPC bridge run via `freecadcmd` — no GUI/Qt/display. Exposes the same interface as the GUI addon (`execute_code`, documents, object CRUD, parts list; screenshots/FEM unavailable headless, degrade gracefully). Fork env: `FC_MCP_HOST`/`--host`, `FC_MCP_PORT=9875`, `--allow-remote` to bind 0.0.0.0. **Version handshake gotcha:** the MCP server and addon compare versions; the fork is `0.1.24`, PyPI publishes upstream's `0.1.28` — install the server **from the fork's git ref** (`pip install git+https://github.com/alosslessdev/freecad-mcp@<main-ref>` or clone at 0.1.24), NOT PyPI, to keep the handshake clean. The bridge is unauthenticated XML-RPC → bind on `backend` network only. **FreeCAD = 1.1.4 stable AppImage** (`FreeCAD_1.1.4-Linux-x86_64-py311.AppImage`, 782MB, from github.com/FreeCAD/FreeCAD releases tag `1.1.4`), extracted (`--appimage-extract`, ships `freecadcmd` with bundled py311). Base image: `python:3.12-slim-bookworm` or `debian:bookworm-slim` + uv. **Transport gotcha:** `freecad-mcp`'s MCP server is **stdio-only** (its `:9875` is XML-RPC, explicitly not an HTTP MCP endpoint). Bridge stdio→HTTP with **supergateway 4.2.1** (npm): `supergateway --stdio "freecad-mcp --host freecad --port 9875" --port 8000` style (container-internal entrypoint: start headless bridge, then supergateway). LiteLLM connects to the supergateway HTTP endpoint. No docker socket in this container.
5. **AMS: auto-match.** The fork has `auto_match_ams: true` (RFID `tray_info_idx` + color matching against live AMS inventory) and `resolve_3mf_ams_slots` dry-run tool — safer than the model hand-picking tray indices. H2C is dual-nozzle: `254`=left/deputy external, `255`=right/main external spool.
6. **Print confirmation = chat-level rule.** Upstream's MCP gate checks hardware/state (model match, nozzle type/flow match, filament, temps, not-busy, HMS severity) but does **not** prompt a human — the tool call is the approval. Per user decision, add an Open WebUI system-prompt rule: the model must ask the user before calling `print_3mf` / `process_and_print_stl`.
7. **Sandbox role:** Blender primary for modeling; existing trimesh/numpy-stl code sandbox stays for post-processing (decimate/repair/measure) and non-3D code, sharing the models volume.
8. **Network placement:** `printer-mcp` must be on the **frontend** network (needs LAN egress to the printer: MQTT :8883, FTPS :990); the `backend` network is `internal: true`. Blender and FreeCAD containers go on `backend` only. No Traefik routes for any of the three (backend services reached via LiteLLM).

## 3. Architecture

```
Open WebUI chat
 ├─ tool servers (LiteLLM-exposed MCP):
 │   ├─ printer_3d  → http://printer-mcp:3000/mcp
 │   │     ├─ slice_stl / print_3mf / process_and_print_stl / get_printer_status /
 │   │     │   resolve_3mf_ams_slots (dry-run) / camera_snapshot (needs ffmpeg, RTSP for H2C)
 │   │     ├─ blender_mcp_status / blender_mcp_call / blender_mcp_export_stl / blender_mcp_edit_model
 │   │     │    └─ spawns uvx mcp-for-blender (stdio child) → TCP :9876 → blender container
 │   │     └─ MQTT :8883 + FTPS :990 → Bambu H2C (frontend network egress)
 │   └─ freecad    → supergateway HTTP in freecad container → stdio freecad-mcp → XML-RPC :9875
 │                   → freecadcmd headless_server.py (FreeCAD 1.1.4)
 └─ code-sandbox: trimesh/numpy-stl post-processing
 Shared named volume `models` mounted in: code-sandbox (rw), blender (rw), freecad (rw), printer-mcp (ro)
 Path convention: /models inside all four; STL/STEP land there; slice_stl reads from there.
```

## 4. Implementation steps (file-by-file)

1. **`printer-mcp/Dockerfile`** (new): `FROM node:24-bookworm-slim`; `apt-get install ffmpeg ca-certificates`; `npm i -g bambu-printer-mcp@1.1.59`; download + extract Bambu Studio v02.08.02.60 ubuntu24.04 archive to `/opt/bambu-studio`; `ENV SLICER_PATH=/opt/bambu-studio/BambuStudio BAMBU_PROFILES_ROOT=/opt/bambu-studio/resources/profiles SLICER_TYPE=bambustudio`; install `uv` (needed for `BLENDER_MCP_COMMAND=uvx`); non-root user; `CMD ["bambu-printer-mcp"]`. Build on the Photon host (or a build script in `scripts/`); compose `build:` context or prebuilt tag `openwebui-stack/printer-mcp:latest`.
2. **`blender/Dockerfile`** (new): `debian:bookworm-slim` + `xvfb libxrender1 libxi6 libxkbcommon0 libgl1 mesa-utils` + Blender 5.0.1 tarball to `/opt/blender` + `uv` → `uvx mcp-for-blender@2.2.7 install-addon` (or pin the addon file from the repo at that tag); startup script: `Xvfb :99` then `DISPLAY=:99 /opt/blender/blender --background --python-expr "<enable addon, set bind 0.0.0.0, allow remote, start server>"` — **note:** the addon must be started through Blender's event loop; if `--background` blocks the addon (it refuses it), start full GUI Blender under Xvfb (`blender` without `-b`). Mount models volume rw at `/models`. Backend-only, 4G/2CPU, non-root.
3. **`freecad/Dockerfile`** (new): `debian:bookworm-slim`; extract `FreeCAD_1.1.4-Linux-x86_64-py311.AppImage` to `/opt/freecad`; clone alosslessdev/freecad-mcp @0.1.24, install MCP server with Python 3.12+ (`uv`), copy `addon/FreeCADMCP` to `/opt/FreeCADMCP`; `npm i -g supergateway@4.2.1`; entrypoint: `freecadcmd /opt/FreeCADMCP/headless_server.py --allow-remote --port 9875` + `supergateway --stdio freecad-mcp --port 8000 --host 0.0.0.0` (check supergateway flags: `--stdio "<cmd>"`, `--port`, `--outputStream`). Backend-only, no docker socket, models volume rw.
4. **`templates/docker-compose.yml.j2`**: add the three services, all gated `{% if printer_mcp_enabled %}` (blender+freecad gates can be separate: `blender_mcp_enabled`, `freecad_mcp_enabled`, or all three under `printer_mcp_enabled` — prefer separate booleans so each can be disabled independently; all default false). Each service: repo conventions — `security_opt: no-new-privileges:true`, logging anchor, healthcheck, `deploy.resources` limits, appropriate networks (printer-mcp: frontend+backend; blender/freecad: backend), `restart: unless-stopped`, no Traefik labels. printer-mcp env from `.env`: `PRINTER_HOST`, `BAMBU_SERIAL`, `BAMBU_TOKEN`, `BAMBU_MODEL=h2c`, `MCP_TRANSPORT=streamable-http`, `MCP_HTTP_HOST=0.0.0.0`, `BLENDER_MCP_COMMAND=uvx`, `BLENDER_MCP_ARGS=["mcp-for-blender"]`, `BLENDER_HOST=blender`, `BLENDER_PORT=9876`, `FFMPEG_PATH=ffmpeg`. Add named volume `models`; mount into code-sandbox, blender, freecad (rw) and printer-mcp (ro). Healthchecks: printer-mcp `wget -qO- http://localhost:3000/mcp || exit 1` (MCP POST-only — use a TCP/`curl` connect check instead), freecad supergateway port check, blender :9876 check.
5. **`templates/env.j2` + vault vars**: `printer_mcp_enabled`, `blender_mcp_enabled`, `freecad_mcp_enabled` (plain vars in `ansible/group_vars/all/vars.yml`); `bambu_host`, `bambu_serial`, `bambu_token` in **vault** (secrets — never rendered to chat). Follow existing tiering in the role's env tasks.
6. **`templates/config.yaml.j2`** (LiteLLM): add under `mcp_servers:` —
   ```yaml
   {% if printer_mcp_enabled %}
     printer_3d:
       url: "http://printer-mcp:3000/mcp"
       transport: "http"
       auth_type: "none"
       allow_all_keys: true
   {% endif %}
   {% if freecad_mcp_enabled %}
     freecad:
       url: "http://freecad:8000/mcp"   # supergateway streamable HTTP
       transport: "http"
       auth_type: "none"
       allow_all_keys: true
   {% endif %}
   ```
7. **`tasks/mcp/printer.yml` + `tasks/mcp/freecad.yml`** (clone `brave.yml`): assert enabled + credentials present; merge connection `{type: mcp, url: http://litellm:4000/mcp/printer_3d}` (and `.../mcp/freecad`) into Open WebUI's `tool_server.connections` via the same union/reject-by-`info.id` merge so Brave/Playwright entries are never clobbered; append `server:mcp:printer_3d` and `server:mcp:freecad` to `tool_defaults_tool_ids` in `tasks/mcp/tool_defaults.yml`; handlers recreate `printer-mcp`, `blender`, `freecad`, `litellm`, `open-webui` on change.
8. **Prompt guidance (handoff)**: Open WebUI system-prompt addition (workspace model-level or a Knowledge entry) with rules: (a) generated STLs must be saved to `/models/<descriptive-name>.stl` so the printer server can read them; (b) always report exported dimensions and compare with intent before slicing (STL units are raw numbers; slicers read mm); (c) **ask the user before `print_3mf`/`process_and_print_stl`**; (d) prefer `resolve_3mf_ams_slots` dry-run + `auto_match_ams: true` for filament selection; (e) use Blender for organic/mesh modeling, FreeCAD for dimensioned/parametric parts, sandbox trimesh only for repair/measure/decimate.
9. **`README.md`**: document the pipeline section (per repo convention "Common Tasks → Adding a New Service").

## 5. Verification sequence (Photon host, after playbook run)

1. `docker compose config --quiet` renders clean (with all three enables on).
2. `docker compose logs printer-mcp blender freecad` — addon/bridge/supergateway up.
3. Open WebUI Settings → Tools shows both tool servers; `get_printer_status` **status-only** call (no temps, no print — matches upstream's own setup-verification guidance).
4. Blender smoke: via `blender_mcp_export_stl` export a default cube → verify `output_verified: true` + dimensions land in `/models`.
5. FreeCAD smoke: `execute_code` creating a `Part::Box` 40×30×20 → volume 24000 mm³ (the fork's own GUI test values), export STL to `/models`.
6. Slicer smoke: `slice_stl` on the cube STL for `h2c`, 0.4 nozzle → inspect produced `.gcode.3mf`.
7. One supervised test print (20mm cube, PLA) — model asks first per rule 5c.
8. Optional: `camera_snapshot` (H2C is RTSP via ffmpeg).

## 6. Prerequisites / gotchas

- H2C touchscreen: **LAN Only Mode + Developer Mode ON** (Settings → Network). LAN Only kills Bambu Handy cloud while on. HMS `0500-0500-0001-0007` on print start = Developer Mode off; clear with `clear_hms_errors`.
- Serial (Settings → Device Info) + LAN access code (Network page) go into the vault at implementation time — never paste in chat.
- Photon host must be on the printer's L2 (frontend network egress to MQTT 8883 / FTPS 990 / RTSP 322).
- Bambu Studio CLI slicing fails fast for `supertack_plate` (unverified CLI bed identifier) — use pre-sliced 3MF for SuperTack, or textured_plate.
- Nozzle type/flow must match the printer's report (`BAMBU_NOZZLE_TYPE` / `BAMBU_NOZZLE_VOLUME_TYPE` only if non-stock).
- The Blender addon port :9876 and FreeCAD XML-RPC :9875 are unauthenticated — internal `backend` network only.
- Windows-native printing and legacy X2D FTPS are upstream-unsupported; H2C uses the standard `lan_mqtt_ftps` path (default for the model).
- If the MCP client supports elicitation and `BAMBU_MODEL` is omitted the server prompts interactively — we set it explicitly to `h2c` (never fall back to `h2d`).
