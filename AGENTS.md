# AGENTS.md - Agent Coding Guidelines

This is a Docker Compose stack for Open WebUI with LiteLLM, Keycloak SSO, observability, and security best practices. The main "code" consists of:
- Shell scripts (`*.sh`) for automation
- YAML configuration files (`*.yml`, `*.yaml`) for Docker Compose, Traefik, Grafana, etc.
- Environment files (`.env`)

## Photon VM

The stack runs on VMware Photon OS, not on the Mac and not in the OrbStack Fedora VM. The deployed tree is `/mnt/data1/openwebui-stack`. Run `docker compose` checks there. The stack host's address and SSH user live in `ansible/inventory/hosts.local.yml` (gitignored; see `hosts.yml` for the placeholder version). The site's domain and other settings that stay out of git live in `ansible/group_vars/all/vars.local.yml` (gitignored; see `vars.local.yml.example`): the playbooks load it after `vars.yml`, and `tasks/keycloak.yml` loads it again after its `include_vars` of `vars.yml`, which would otherwise put `domain: localhost` back for the Keycloak clients. Keep real domains, addresses and subnets out of tracked files. See `.magie-coder/skills/photon-vm/SKILL.md`.

## Commands

### Development Workflow

```bash
# Start the full stack (including monitoring)
docker compose --profile monitoring up -d

# Start only core services (no monitoring)
docker compose up -d

# Start with GPU support
docker compose --profile gpu up -d

# View logs
docker compose logs -f [service_name]

# Stop all services
docker compose down

# Run the setup entrypoint (installs collections once, runs the playbook)
./scripts/setup.sh

# Complete reset on the Photon host: compose down -v (all profiles), remove
# every project-prefixed container/volume/network/image, delete the deployed
# stack folder only (/mnt/data1 keeps its other contents, including Docker's
# data-root at /mnt/data1/docker). --dry-run prints the actions.
bash scripts/reset-stack.sh --dry-run
```

The playbook renders `.env` (`templates/env.j2`), `docker-compose.yml`
(`templates/docker-compose.yml.j2`), TLS certs (`ssl_cert_type`), `/etc/hosts`,
and provisions Keycloak. Secrets resolve in order: vault entry in
`ansible/group_vars/all/vault.yml` → environment variable → deployed `.env`
value → generated once and persisted back into the vault file.

```bash
# Provision Keycloak realm, OIDC clients, groups, and users
# (run from ansible/; collections install first)
cd ansible
ansible-galaxy collection install -r requirements.yaml
ansible-playbook playbooks/site.yml

# Access Keycloak admin console
# URL: https://auth.<domain>/admin
# Credentials: admin / $KEYCLOAK_ADMIN_PASSWORD

# Import realm on startup
# Place realm-export.json in keycloak/import/ directory
```

### Testing SSO Integration

```bash
# Test Open WebUI OAuth redirect
curl -skL -i "https://chatbot.localhost/oauth/oidc/login" | head -15

# Test Keycloak discovery endpoint
curl -sk "https://auth.localhost/realms/master/.well-known/openid-configuration"

# Verify user token contains groups claim (after login)
# Use Keycloak admin CLI or decode JWT at jwt.io
```

### SSO Configuration Notes

- **Open WebUI**: Uses `OPENID_PROVIDER_URL` pointing to internal Keycloak URL for discovery
- **LiteLLM**: Uses `GENERIC_*` OIDC environment variables for Generic SSO provider
- **Grafana**: Uses `GF_AUTH_GENERIC_OAUTH_*` variables for OAuth integration
- **Groups Mapper**: Automatically added by the Ansible playbook (`keycloak_app.yml` dedicated scope mappers) to include group membership in tokens
- **Required**: Add `auth.localhost` to `/etc/hosts` for browser access
- **User Assignment**: Users must be assigned to groups in Keycloak for role mapping to work (admin group → Admin role)
- **User management**: `docs/keycloak-users.md` has the URLs for users and administrators, the group table, the three ways to create a user, and password reset. An account created in the admin console cannot sign in until a password is set on its Credentials tab. No SMTP server is configured in the realm, so Keycloak cannot mail reset or verification links; an administrator resets passwords
- **Single Logout**: Every client in `keycloak_apps` (vars.yml) lists `post_logout_redirect_uris`; the app side is `WEBUI_AUTH_SIGNOUT_REDIRECT_URL` (Open WebUI), `GF_AUTH_GENERIC_OAUTH_SIGNOUT_REDIRECT_URL` (Grafana), `PROXY_LOGOUT_URL` (LiteLLM), and the `/oauth2/sign_out?rd=` link (voice, `roles/chatterbox/tasks/ui_auth.yml`). Keycloak matches the URL exactly, so change both sides together. Open WebUI also receives Keycloak back-channel logout (`backchannel_logout_url` on the client, `ENABLE_OAUTH_BACKCHANNEL_LOGOUT`)

### Troubleshooting SSO Issues

**Common Errors and Solutions:**

| Error | Cause | Solution |
|-------|-------|----------|
| Open WebUI: "email or password incorrect" | Missing `OPENWEBUI_OIDC_CLIENT_SECRET` or local login used when OAuth fails | Rerun the Ansible playbook, ensure `ENABLE_OAUTH_SIGNUP: "true"` |
| LiteLLM: "authentication database unreachable" | OIDC config incomplete, missing client secret | Rerun the Ansible playbook, verify `DATABASE_URL` is correct |
| Grafana: "Login failed - Failed to get token" | Missing `GF_OIDC_CLIENT_SECRET` or missing user group assignment | Rerun the Ansible playbook, assign user to group in Keycloak |

**Required Environment Variables (rendered into `.env` by the playbook):**

```bash
# Keycloak Admin (required for client configuration)
KEYCLOAK_ADMIN_PASSWORD=your_secure_password

# OIDC Client Secrets (generated on first run, persisted in vault.yml)
OPENWEBUI_OIDC_CLIENT_SECRET=<auto-generated>
LITELLM_OIDC_CLIENT_SECRET=<auto-generated>
GF_OIDC_CLIENT_SECRET=<auto-generated>
```

**Setup Steps:**

```bash
# 1. Set secrets by hand, export them, or let the playbook generate them once
#    (tiering: vault entry -> environment -> deployed .env -> generated)

# 2. Run the Ansible playbook (creates realm, OIDC clients, groups, and users)
cd ansible && ansible-galaxy collection install -r requirements.yaml && ansible-playbook playbooks/site.yml

# 3. Restart services to pick up new client secrets
docker compose restart open-webui litellm

# 4. Verify Keycloak clients exist
docker compose exec keycloak /opt/keycloak/bin/kc.sh list-clients --realm master

# 5. Ensure admin user is in admin group:
#    Keycloak Admin Console → Users → admin → Groups → Join "admin"
```

### Backup Commands

```bash
# Run Keycloak database backup
docker compose --profile backup up keycloak-backup

# Schedule automated backups (add to crontab)
# 0 2 * * * cd /path/to/project && docker compose --profile backup up -d keycloak-backup
```

### Brave Search MCP (web search for all chat models)

```bash
# Check the startup hook loaded
docker compose logs litellm | grep "startup hook"

# Confirm a plain chat request got the injected mcp tool
docker compose exec postgres psql -U litellm -d litellm -c \
  "SELECT request_id, jsonb_array_length(proxy_server_request->'tools') AS ntools,
          proxy_server_request->'tools'->0->>'type' AS t0
   FROM \"LiteLLM_SpendLogs\" ORDER BY \"startTime\" DESC LIMIT 3;"

# End-to-end test (model should answer with current web results)
KEY=$(grep -m1 "^LITELLM_MASTER_KEY=" .env | cut -d= -f2-)
curl -sk https://litellm.localhost/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model": "gemini-3.5-flash-lite", "messages": [{"role": "user", "content": "Search the web for today top tech news"}]}'
# Any served chat model works; on an Anthropic-only stack use "claude-haiku-5-5".
```

Pieces: `brave-search-mcp` container (`BRAVE_SEARCH_API_KEY` required), `mcp_servers.brave_search` in `litellm/config.yaml`, and the `BraveMCPInjector` in `litellm/brave_mcp_callback.py` loaded via `LITELLM_WORKER_STARTUP_HOOKS`. Full details in README "Web Search (Brave Search MCP)".

### Centralized Skills (LiteLLM Skills Gateway)

Agent know-how lives in `skills/<name>/SKILL.md` (Claude/Anthropic Agent Skills format): `3d-print-pipeline`, `stack-ops`, `sso-troubleshooting`, `guardrail-tuning`, `web-search-verify`, `deep-research`. The playbook publishes every skill to three consumers (no MCP server needs to be enabled):

1. `ansible/roles/openwebui_stack/tasks/mcp/skills_gateway.yml` (logic in `container-scripts/litellm_skills_sync.py`) uploads each skill's zip to the proxy-hosted store (`POST /v1/skills`, `custom_llm_provider=litellm_proxy`, stored in the proxy's Postgres) and registers a Claude Code marketplace entry (`POST`/`PUT /claude-code/plugins/<name>`; the archive source must be https, so it points at `https://litellm.<domain>/v1/skills/<id>/archive`). Idempotent by deterministic archive digest (the discovery index publishes the sha256 of LiteLLM's repack; the task recomputes it byte-identically — root SKILL.md, 1980 mtimes, mode 0644, DEFLATE): the store has no update endpoint, so drift = delete + re-upload. `litellm_settings.public_skills_index: true` (toggle `skills_public_index_enabled`) serves the Agent Skills discovery index at `/.well-known/agent-skills/index.json` for `npx skills add`. Semantic skill search needs `litellm_settings.skill_search_embedding_model: text-embedding` (gemini-embedding-001).
2. `ansible/roles/openwebui_stack/tasks/openwebui_skills.yml` (logic in `container-scripts/openwebui_skills_sync.py`) pushes each SKILL.md into Open WebUI's workspace through its Skills API (`/api/v1/skills/create`, `/id/<name>/update` with `expected_version_id`), authenticated by a `skill-publisher` admin api key upserted into the `user`/`api_key` tables from the vault's `openwebui_api_admin_key`. Users then `$`-mention skills or bind them to models.
3. The opencode role (`opencode_sync_skills: true`) copies `skills/*` into each user's `~/.config/opencode/skills/` for opencode's native `skill` tool.

The compose template mounts `./skills` read-only into `litellm` (`/app/skills`) and `open-webui` (`/app/skills`). Frontmatter rules (enforced by `container-scripts/skill_packages.py` before anything is published, and by `pytest` in CI): `name` must equal the directory name and be a valid slug; `description` 1–1024 chars drives discovery. Only `SKILL.md` reaches the proxy store and Open WebUI, so keep each skill in that one file (`deep-research` inlines its researcher and report-writer briefs for this reason). Full flow and consume commands in README "Centralized Skills".

### Reasoning selector (Thinking switch, opencode variants)

Open WebUI: `ansible/roles/openwebui_stack/tasks/openwebui_reasoning.yml` installs two filter functions through the admin API (`container-scripts/openwebui_reasoning_sync.py`, no container recreate) and attaches them per model, never globally. `reasoning_switch` is a toggle filter ("Thinking" in the chat input) whose inlet sets `reasoning_effort` to the user's UserValves level; `reasoning_off` is its always-on companion that sets `none` when the switch id is missing from `__metadata__["filter_ids"]`, because Open WebUI does not run a toggle filter while it is off. The model list comes from LiteLLM `/model/info`: `supports_reasoning` attaches the switch, `supports_none_reasoning_effort` also attaches the companion. Rerun `playbooks/openwebui_reasoning.yml` after `inference.yml` changes the reasoning models. Both inlets leave a `reasoning_effort` already on the body alone (chat Advanced Params), and a Workspace model's own Reasoning Effort is applied after filters and wins.

opencode needs no plugin: `/variants` and `ctrl+t` are built in, and the levels are the variants `opencode-plugin-litellm` builds from the `supports_<level>_reasoning_effort` flags (`vllm_reasoning_efforts`). The role only seeds `tui.json` with a key for the picker (`opencode_variant_picker_keybind`, never overwriting an existing file). Details in `docs/vllm-inference.md` "The Thinking switch in Open WebUI".

### LLM Launcher portals (web GUI per vLLM inference server)

```bash
# On a compute node: both units up, GUI on loopback, gate answering
systemctl status vllm-launcher vllm-launcher-auth
curl -s http://127.0.0.1:8765/api/about
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:4180/ping

# On the stack host: an anonymous request is redirected to Keycloak
curl -sk -o /dev/null -w '%{http_code} %{redirect_url}\n' https://launcher-vllm-node-01.<domain>/
```

Local-LLM-Launcher-GUI (pinned by `vllm_launcher_version`) runs on the compute nodes, one per independent inference server: every node in standalone mode, the Ray head in ray mode (`tasks/launcher_placement.yml`). Pieces: `vllm-launcher` (container built on the node's vLLM image, GUI on `127.0.0.1:8765`), `vllm-launcher-auth` (oauth2-proxy on 4180, `OAUTH2_PROXY_ALLOWED_ROLES=launcher:launcher_admin`), and on the stack host the `launcher` Keycloak client plus `traefik/dynamic/launcher.yml` (`roles/openwebui_stack/tasks/launcher.yml`). Both sides are off by default (`launcher_enabled`, `vllm_launcher_enabled`). Full details in `docs/vllm-inference.md` "LLM Launcher".

**Critical implementation notes:**
- Run `site.yml` before `inference.yml`. The node play reads `LAUNCHER_OIDC_CLIENT_SECRET` from the controller environment or the deployed `.env` and fails fast without it.
- The portal name is `launcher-<inventory hostname>.<domain>` on both sides. The stack inventory and the inventory the node is deployed from must name the node the same, or Keycloak rejects the redirect URI. `launcher_nodes` / `launcher_node_addresses` (set in `inventory/hosts.local.yml`) cover a node deployed from `tests/vllm-remote`.
- The launcher must stay on loopback. It has no login; the gate is its only protection. Never open `vllm_launcher_port` in the firewall or bind it elsewhere.
- Models started from the GUI are child processes of the launcher container. Anything that restarts `vllm-launcher` (image rebuild, changed script or unit) stops them, and nothing relaunches them.
- `vllm_launcher_owns_primary: true` keeps `vllm-serve` stopped and disabled; the `Restart vllm-serve` handler is skipped on such a node so a changed script cannot start it.
- The sign-in chip is added by the image build (`files/vllm-launcher-patch-ui.py`), not patched into the checkout: the launcher ships a compiled React bundle.
- Chat is off in the launcher (`vllm_launcher_chat_enabled: false`): its "Test chat" box and its "Open WebUI" panel, which would start an Open WebUI on the node with no login. The image's entry point (`files/vllm-launcher-entry.py`, `stack_entry.py` in the image) replaces `POST /api/servers/{id}/chat` and `/api/openwebui/*` with 403s before the launcher builds its app; `patch_ui.py` and the page script only hide the controls. Keep the entry point: starting the image with `python3 -m local_llm_launcher` brings chat back. Both files fail closed when a new `vllm_launcher_version` moved those routes; update the paths in both.
- Backslash group references in Ansible: inside `{{ }}` in a block scalar (`>-`, `|`) write `'\1'`; `'\\1'` there makes `regex_search` fail and `regex_replace` insert a literal `\1`. In double-quoted YAML and in bare `when:` expressions write `'\\1'` (the forms `tasks/auxiliary.yml` uses). The same goes for `'\n'` passed to `join()`.

### Rootless podman and tini on the vLLM nodes

```bash
# On a rootless podman node: the units run as the service account
systemctl show -p User,Group vllm-serve
sudo -u vllm XDG_RUNTIME_DIR=/run/user/$(id -u vllm) podman ps

# Dry run of the account, lingering and move-from-root steps (changes nothing)
ansible-playbook playbooks/inference.yml --check
```

`roles/vllm_compute/tasks/container_runtime.yml` decides how every container of the role starts. `vllm_container_init` puts tini in as PID 1 (`--init`; on podman with `--init-path /usr/bin/tini-static`, because podman's own init is catatonit). `vllm_container_rootless` (on for every podman node, off for docker) runs the containers as the unprivileged `vllm_container_user`. Details in `docs/vllm-inference.md` "Rootless containers and tini".

**Critical implementation notes:**
- The units stay **system** units with `User=<account>`, not `systemctl --user` units or quadlets: the names, `journalctl -u`, the log shipper's `_SYSTEMD_UNIT` match and every `systemctl` command in the docs stay the same. This works because they are `Type=simple` and run `podman run` in the foreground; do not switch them to `Type=notify`.
- Three settings keep the logs where they were, and each one was found by a failing test on the node. `Delegate=yes` on the unit: without it rootless podman moves itself into `podman-<pid>.scope` under the account's user manager, journald files the server's output under `user@<uid>.service`, `journalctl -u vllm-serve` goes empty and the log shipper ships nothing. `--log-driver none` on the container: with `Delegate=yes` conmon is in the unit's cgroup too, so podman's default journald logging files every line a second time under the same unit and Loki gets each line twice; `passthrough` is not the answer, it files the output under the account's `libpod-*.scope`. Consequence: `podman logs` is empty for these containers, the journal is the log.
- Keep the units' `ExecStop`/`ExecStopPost` (`podman rm -f <name>`). With `Delegate=yes` conmon and the container are in the unit's cgroup, and a bare `KillMode=control-group` stop kills conmon before it cleans up, which leaves a stale container record that blocks the next start by name.
- `SuccessExitStatus=143` on every unit: tini exits 143 when it stops the server on `systemctl stop`; without it each stop marks the unit failed.
- A rootless unit must not carry `NoNewPrivileges=true`: `newuidmap` then fails and no container starts after a reboot. It only looks fine while a pause process from an earlier podman command still holds the user namespace open. The scripts pass `--security-opt no-new-privileges` to the container instead.
- The account needs lingering (`loginctl enable-linger`). The containers' cgroup scopes live under `user@<uid>.service`; without lingering logind stops it, and the containers, when the account's last session ends.
- A task that calls the container CLI itself must build its command from `vllm_container_argv`, never from `vllm_container_cli`: root's podman has a different image and container store than the account's, so a bare `podman build` or `podman exec` talks to the wrong one. The prefix uses `runuser`, not `become_user`, so it needs neither ACLs nor pipelining on the node, and `env -C /` because podman refuses to start from a directory the account cannot enter.
- Scripts and `auth.env` are `root:<account's group>` with mode `0750`/`0640`: the account can run and read them, not rewrite them. Anything a container writes (the Hugging Face cache, the launcher's data directory) is owned by the account.
- The init binary must be static (`tini-static`): podman bind-mounts it into images that may not have its libraries.
- Moving a node from rootful happens on the first rootless run: units whose file lacks `User=<account>` are stopped, the role's containers are removed from root's store by name, and the Hugging Face cache is chowned. The account's image store starts empty, so that run pulls every image again.
- Copying an image between stores (`podman save | podman load`) spools the whole image in the temp directory on both sides, `/var/tmp` by default. For a 25 GB image set `TMPDIR` to a volume with room or the root filesystem fills up.
- Exporters are root quadlets on rootful podman and system units (`templates/vllm-exporter.service.j2`) on rootless podman, under the same `<name>.service` names.

### Chat only through Open WebUI and API clients

```bash
# The hook is loaded (first in the list)
docker compose exec litellm printenv LITELLM_WORKER_STARTUP_HOOKS

# The Chat page is off, and the value comes from config.yaml
KEY=$(grep -m1 "^LITELLM_MASTER_KEY=" .env | cut -d= -f2-)
curl -sk https://litellm.localhost/get/ui_settings -H "Authorization: Bearer $KEY" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["values"]["enable_chat_ui"], d["source"]["enable_chat_ui"])'
# False config

# A request that looks like the Playground is refused; the same request without the Referer answers
curl -sk -o /dev/null -w '%{http_code}\n' https://litellm.localhost/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -H "Referer: https://litellm.localhost/ui/playground/" \
  -d '{"model": "gemini-3.5-flash-lite", "messages": [{"role": "user", "content": "hi"}]}'
# 403
```

Models are chatted with through Open WebUI or an API client (opencode, an IDE). The two other web chats in the stack are off: the LLM Launcher's (see its notes above) and the LiteLLM admin UI's Playground and Chat pages (`litellm_ui_chat_enabled: false`). The admin UI stays for keys, models, usage and settings.

**Critical implementation notes:**
- LiteLLM has a switch for its Chat page only: `general_settings.enable_chat_ui` in `litellm/config.yaml`. A `general_settings` value overrides the one saved from Admin Settings > UI Settings, so the toggle there does nothing while the line exists. There is no switch for the Playground; its menu entry stays.
- `litellm/ui_chat_block_callback.py` is what turns both pages off. Its `async_pre_call_hook` answers 403 to a model call when the key is a UI session key (`team_id == "litellm-dashboard"`, minted at admin UI sign-in; the Playground and Chat pages send it) or when the `Referer` is the proxy's own `/ui` pages (a virtual key pasted into the Playground). The Chat page uses `/v1/responses` (`aresponses`), not chat completions, so the blocked call types cover every endpoint type the Playground offers.
- It must not block `call_mcp_tool`, `list_mcp_tools`, `asearch` or the vector store types: the admin UI's MCP tool test, search tool test and vector store test use the same session key. Model health checks never reach the hook.
- Open WebUI and API clients are untouched because they send neither signal: Open WebUI calls LiteLLM server-side with the master key and no `Referer`.
- The hook is registered first (`litellm.callbacks.insert(0, ...)` and first in `LITELLM_WORKER_STARTUP_HOOKS`) so a refused request skips the Brave injector and the compactor.
- The refusal is an `HTTPException` with a plain string `detail`: LiteLLM returns it as the error message the Playground shows. `UI_CHAT_BLOCK_OPENWEBUI_URL` (set in the compose template) names where to chat instead.

### Context Compaction (oversized histories)

`litellm/context_compaction_callback.py` is a second `LITELLM_WORKER_STARTUP_HOOKS` callback. Its `async_pre_call_hook` trims an oversized message history to the target model's `max_input_tokens` (resolved from `proxy_server.llm_router.get_model_list()` → `model_info`, then LiteLLM's cost map, then `CONTEXT_COMPACTION_MODEL_<name>`) before the provider sees it, keeping system prompts, the newest `CONTEXT_COMPACTION_KEEP_LAST_N` (default 6) messages, and a `[Earlier conversation was compacted...]` note. Trimmed, not summarized. Works on streaming too (no tool deltas involved). Opt out per request with `metadata: {"context_compaction": false}`. Full details in README "Context Compaction".

**Critical implementation notes:**
- LiteLLM's `/v1/chat/completions` route invokes `async_pre_call_hook` with `call_type="acompletion"`, not `"completion"`. The injector must match both or it silently never fires on proxy traffic.
- The injector must skip `stream: true` requests. Streaming Open WebUI chats run their own function-calling loop; LiteLLM's MCP streaming iterator forwards tool-call deltas to the client, the client cannot resolve the MCP-namespaced tool name, and the resulting "Error: Tool not found" tool responses corrupt the history. The next Gemini turn then fails with "Missing corresponding tool call for tool response message".
- The injector must also skip any request that already carries function-type tools. One `mcp`-type tool flips LiteLLM into gateway auto-execution for the WHOLE turn (`acompletion_with_mcp`): with `require_approval: "never"` it executes every tool call the model returns, including Open WebUI's client-side `blender_blender-*` names it cannot resolve (KeyError). Non-streaming chats with client-side MCP tool servers (Blender/FreeCAD/Playwright) hit this whenever the injector added the Brave tool alongside them.
- Streaming chats get web search through Open WebUI's client-side MCP tool server: Workspace model entries for the Gemini models carry `meta.toolIds: ["server:mcp:brave_search"]`, so the frontend merges the tool server into every chat request automatically. Claude and every other model without an entry of its own get the same IDs from `models.default_metadata` (`mcp/tool_defaults.yml`).
- Tool calls on the Claude models (`anthropic/`) use the same paths and need nothing per model, but two `litellm_settings` flags carry Anthropic's rules and must stay on. `drop_params: true`: a forced `tool_choice` (`required` or a named function) returns 400 on `claude-opus-5-5`, `claude-sonnet-5-5`, and `claude-fable-5-2`; LiteLLM downgrades it to `auto` only with `drop_params`, otherwise it answers 400 itself. `modify_params: true`: Claude rejects a history where a tool call has no tool result or a tool result has no call; LiteLLM then adds a placeholder result and drops orphaned or duplicate results.
- LiteLLM's gateway runs one tool round per request; a second search would go back to the client as tool calls it cannot run. The injector adds the later rounds through LiteLLM's agentic-loop hooks: `async_should_run_agentic_loop` (the gate) and `async_build_agentic_loop_plan`, which executes the calls with the gateway's own helpers (`LiteLLM_Proxy_MCP_Handler`) and lets LiteLLM make the follow-up call, up to `max_agentic_loops` (default 3) more rounds. Rules that must hold: the gate claims only chat completions (`kwargs["_agentic_loop_api_surface"] == "chat_completions"`; `/v1/messages` and `/v1/responses` call the same hook names), non-streaming, requests the pre-call hook flagged (`metadata.brave_mcp_auto_execute`), and only when the last message is a tool result, because the gateway owns round one. The plan builder must never raise: LiteLLM does not catch it and the request would fail, so every error falls back to the default plan (tool calls returned as before). The last permitted round must end in text. Claude gets `tool_choice: "none"` with tools and history untouched (the Anthropic API enforces it, and its thinking blocks are bound to both). Every other provider gets the tool rounds folded into the user turn as plain text and no tools: Gemini ignores `tool_choice: "none"` (and a note in the tool result, and even removed tools) while the history shows tool calls, and LiteLLM's ceiling then strips the call and returns an empty answer (seen live, 2026-11-10). `BRAVE_SEARCH_MAX_EXTRA_ROUNDS` in the litellm environment lowers the ceiling; `0` turns the extra rounds off.
- `mcp/brave.yml` must not require a provider key. It asserted `gemini_api_key`, which failed the playbook on Anthropic-only deployments; the Brave wiring is the same for every provider.
- The compaction note is a `user` turn for Claude models and an `assistant` turn for everything else. The Anthropic API rejects a conversation that opens with an assistant turn and LiteLLM does not repair it; the hook picks the role from the deployment's `litellm_params.model` (`anthropic` or `claude` in it).

### Python run by the playbook (container scripts)

```bash
# Lint, format check and tests (config in pyproject.toml)
pip install ruff pytest
ruff check && ruff format --check && pytest

# Run a script by hand on the stack host; --check writes nothing and
# prints what a real run would change
docker compose exec litellm python3 /opt/stack-scripts/litellm_skills_sync.py \
  --archive-base-url https://litellm.<domain> --check
```

Task files hold no inline Python. Every Python job of the `openwebui_stack` role is a script in `container-scripts/`, which the compose template mounts read-only at `/opt/stack-scripts` (`container_scripts_mount`) in `litellm` and `open-webui`. A task runs one through `tasks/container_script.yml` (`import_tasks` with `container_script`, `container_script_service`, and optional `container_script_args`, `container_script_env`, `container_script_payload`) and reads the result from `container_script_report`.

| Script | Runs in | Called from |
|--------|---------|-------------|
| `litellm_skills_sync.py` | litellm | `mcp/skills_gateway.yml` |
| `litellm_reasoning_models.py` | litellm | `openwebui_reasoning.yml` |
| `openwebui_skills_sync.py` | open-webui | `openwebui_skills.yml` |
| `openwebui_default_models.py` | open-webui | `openwebui_default_models.yml` |
| `openwebui_reasoning_sync.py` | open-webui | `openwebui_reasoning.yml` |

Shared code: `stack_api.py` (HTTP client, argument parser, exit contract) and `skill_packages.py` (SKILL.md discovery and the frontmatter rules).

**Critical implementation notes:**
- New Python for a task goes in `container-scripts/<job>.py` with a test in `tests/container_scripts/`, never in a `python3 -c` argument. Standard library only: the scripts run on whatever Python the two images ship (3.12 and 3.13 today).
- The contract is the same for every script. Options are arguments. Secrets are environment variables: `container_script_env` puts only their names on the docker command line (`--env=NAME`), so a key never shows in `ps` or in a failed task's output. Structured input is base64 JSON on stdin (`container_script_payload`). Success prints one JSON report (`changed`, `check`, `actions`, then the script's own keys); failure exits 1 with `error: ...` on stderr.
- Every write goes through `Api.write`. Under `ansible-playbook --check` the task still runs, with `--check`: the script reads the live state, writes nothing, and reports what would change.
- `container_script_report` is the report of the latest run (a lazy variable in `defaults/main.yml`). Read it right after the import; the next import replaces it, and it is empty when the run was skipped.
- The scripts run inside the containers on purpose. The admin APIs listen on container-local ports only, and the skill archive digest must be computed with LiteLLM's zlib: DEFLATE output differs from the host's build, and a host-side digest would never match the discovery index.
- A container that predates the mount fails with a hint to run `--tags environment,compose` once. `tasks/sync.yml` carries the `always` tag, so the deployed `container-scripts/` is current on tag-limited runs.
- `roles/vllm_compute/files/ray_nodes_ready.py` is the one script outside this scheme: the Ray head container has no such mount, so `tasks/services.yml` pipes it to `python -` over stdin.

### Linting & Quality Checks

**Shell Scripts:**
```bash
shellcheck scripts/*.sh
shfmt -i 4 -w scripts/*.sh
```

**Python (container scripts and their tests):**
```bash
ruff check && ruff format --check && pytest
```

**YAML Validation:**
```bash
docker compose config --quiet && echo "Valid"
```

### Testing Changes

```bash
docker compose config
docker compose up --dry-run
docker compose pull
```

## Code Style Guidelines

### General Principles
1. **Configuration over Code**: Prefer declarative YAML configs over custom scripts.
2. **Idempotency**: Scripts must be re-runnable without side effects.
3. **Security First**: Never commit secrets. Use `.env` files and `${VAR:?error}` for required variables.

### SSO/OIDC Configuration

**Open WebUI OAuth:**
```yaml
ENABLE_OAUTH_SIGNUP: "true"
OAUTH_PROVIDER_NAME: "Keycloak"
OPENID_PROVIDER_URL: "http://openwebui-stack_keycloak:8080/realms/master/.well-known/openid-configuration"
OAUTH_CLIENT_ID: "open-webui"
OAUTH_CLIENT_SECRET: "${OPENWEBUI_OIDC_CLIENT_SECRET:?error}"
ENABLE_OAUTH_GROUP_MANAGEMENT: "true"
OAUTH_GROUP_CLAIM: "groups"
OAUTH_ADMIN_ROLES: "admin"
```

**LiteLLM Generic OIDC:**
```yaml
GENERIC_CLIENT_ID: "litellm"
GENERIC_CLIENT_SECRET: "${LITELLM_OIDC_CLIENT_SECRET:?error}"
GENERIC_AUTHORIZATION_ENDPOINT: "https://auth.${DOMAIN:-localhost}/realms/master/protocol/openid-connect/auth"
GENERIC_TOKEN_ENDPOINT: "https://auth.${DOMAIN:-localhost}/realms/master/protocol/openid-connect/token"
GENERIC_USERINFO_ENDPOINT: "https://auth.${DOMAIN:-localhost}/realms/master/protocol/openid-connect/userinfo"
PROXY_BASE_URL: "https://litellm.${DOMAIN:-localhost}"
```

**Notes:**
- Open WebUI uses internal container URL for discovery (server-side fetch)
- LiteLLM uses public URLs (browser redirects)
- Groups must be mapped in Keycloak client scope to appear in tokens

### YAML Configuration

- Use 2-space indentation
- Use anchors (`&anchor`) and aliases (`*alias`) to reduce duplication
- Include section headers with `====` for major sections
- Always define `logging` configuration using anchors
- Use `healthcheck` for all persistent services
- Use `profiles` for optional features (monitoring, gpu, backup)
- Set `security_opt` with `no-new-privileges:true`
- Use internal networks for backend services
- Add `deploy.resources` for production services (memory/CPU limits)

### Environment Variables

**Naming Convention:**
- Use SCREAMING_SNAKE_CASE
- Prefix with service name (e.g., `OPENWEBUI_`, `LITELLM_`)
- Suffix passwords with `_PASSWORD`, keys with `_KEY`

**Security:**
- Use `${VAR:?error}` for required variables (fails if missing)
- Provide defaults only for optional: `${VAR:-default}`

### Shell Scripts

**Formatting:**
- Use `#!/bin/bash` (not `/bin/sh`)
- 4-space indentation
- Use `set -euo pipefail` for error handling
- Use `[[ ]]` for tests (not `[ ]`)
- Quote all variable expansions: `"$VAR"`

**Best Practices:**
- Use `local` for all function variables
- Use `readonly` for constants
- Cross-platform compatibility (macOS vs Linux): use helper functions for `sed -i`

### Error Handling

- Docker Compose: Use `condition: service_healthy` for service dependencies
- Shell scripts: Always use `set -euo pipefail`
- Validate required environment variables at startup

## File Structure

```
.
├── AGENTS.md
├── docker-compose.yml
├── .env.example
├── .env (never commit)
├── traefik/
│   ├── traefik.yml
│   └── dynamic/
├── grafana/provisioning/
├── litellm/config.yaml
├── otel/config.yaml
├── qdrant/config.yaml
├── keycloak/
│   └── import/          # Realm import files
├── ansible/             # Keycloak realm/client/user provisioning
│   ├── playbooks/site.yml
│   └── roles/openwebui_stack/
├── container-scripts/   # Python the playbook runs inside litellm/open-webui
├── tests/container_scripts/  # pytest suite for container-scripts/
├── pyproject.toml       # ruff and pytest configuration
└── scripts/
    ├── setup.sh
    ├── generate-certs.sh
    ├── backup-keycloak.sh
    ├── reset-stack.sh
    └── sync-ollama-models.sh
```

## Common Tasks

### Adding a New Service
1. Add service to `docker-compose.yml` with labels, networks, healthcheck
2. Add required environment variables to `.env.example`
3. Update README.md with service documentation

### Updating an Image Version
1. Check version in `docker-compose.yml`
2. Test with `docker compose up -d [service]`
3. Verify healthcheck passes

### Modifying Configuration
1. Make changes to appropriate YAML file
2. Validate: `docker compose config`
3. Test: `docker compose up -d`
4. Verify service health

## Security Notes

- Never commit `.env` files or secrets
- Use strong, randomly generated passwords (the playbook generates missing secrets once and persists them to `ansible/group_vars/all/vault.yml`)
- Use `readonly` volumes where possible
- Restrict network access using internal networks
- Configure admin IP restriction for Keycloak via `KEYCLOAK_ADMIN_IP_RANGE`
- Use OIDC for authentication instead of service-specific credentials