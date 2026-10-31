# Complete Setup and Usage Guide

This guide takes a bare machine to a fully working Open WebUI AI platform with Keycloak SSO, LiteLLM gateway, observability, optional local inference (llama.cpp edge nodes and vLLM distributed compute), and backups. It reflects the stack as of October 2026.

- [Stack overview](#stack-overview)
- [Prerequisites](#prerequisites)
- [Fresh install](#fresh-install)
- [Keycloak SSO setup](#keycloak-sso-setup)
- [Adding users and passkeys](#adding-users-and-passkeys)
- [Using the platform](#using-the-platform)
- [Local inference: llama.cpp edge nodes](#local-inference-llamacpp-edge-nodes)
- [Local inference: vLLM distributed compute](#local-inference-vllm-distributed-compute) — full reference in [vllm-inference.md](vllm-inference.md)
- [Monitoring](#monitoring)
- [Backups](#backups)
- [Upgrades and version pinning](#upgrades-and-version-pinning)
- [Troubleshooting](#troubleshooting)

## Stack overview

| Layer | Component | Image in `docker-compose.yml` | Latest release (2026-10-27) |
|---|---|---|---|
| Reverse proxy | Traefik | `traefik:v3.7` | v3.7.16 (2026-10-20) |
| Chat UI | Open WebUI | `ghcr.io/open-webui/open-webui:main` | v0.12.4 (2026-10-25) |
| LLM gateway | LiteLLM proxy | `ghcr.io/berriai/litellm:main-latest` | v1.106.4 (2026-10-25) |
| SSO / IdP | Keycloak | `quay.io/keycloak/keycloak:26.8` | 26.8.1 (2026-10-13) |
| Database | PostgreSQL | `postgres:18-alpine` | 18.x (18.6+) |
| Cache | Valkey | `valkey/valkey:9-alpine` | 9.1.3 (2026-10-26) |
| Vector DB | Qdrant | `qdrant/qdrant:latest` | v1.20.0 (2026-10-22) |
| Voice (TTS) | Chatterbox | built from source (CUDA or CPU Dockerfile) | upstream `main` |
| Voice auth | oauth2-proxy | `quay.io/oauth2-proxy/oauth2-proxy:v7.15.1` | v7.15.5 (2026-10-01) |
| Web search | Brave Search MCP | `mcp/brave-search:latest` | `latest` only |
| Observability | Grafana LGTM | `grafana/otel-lgtm:latest` | 0.38.1 |
| Observability | OTel Collector | `otel/opentelemetry-collector-contrib:latest` | v0.164.1 (2026-10-26) |
| Exporters | node/postgres/json/DCGM | `latest` tags | node v1.12.1, postgres v0.20.1, json v0.8.0, DCGM 4.8.4 |
| Distributed inference | vLLM | `vllm/vllm-openai:latest` (Ansible role) | v0.32.0 (2026-10-18) |
| Cluster runtime | Ray (inside vLLM image) | bundled | 2.59.0 |

Ansible toolchain (controller, e.g. your laptop or the Photon VM):

| Tool | Required version | Latest (2026-10-27) |
|---|---|---|
| Docker Engine | 24.0+ | 29.9.2 |
| Docker Compose v2 | 2.20+ | 5.6.1 |
| ansible-core | 2.21+ | 2.21.5 |
| community.docker collection | >=5.0.0 | 5.4.0 |
| ansible.posix collection | >=2.0.0 | 2.2.2 |
| middleware_automation.keycloak | >=3.1.2,<4.0.0 | 3.1.2 (pinned at latest) |

The deployment target in this repo is a VMware Photon OS VM (`root@10.0.105.14`, tree at `/mnt/data1/openwebui-stack`). Everything below works the same on any Linux host with Docker; Photon notes are called out where they differ (`tdnf` instead of `apt`/`dnf`).

## Prerequisites

1. **A Linux host** (bare metal or VM) with:
   - 8 GB RAM minimum (16 GB+ recommended with monitoring and TTS)
   - 50 GB+ disk for images, models, and volumes
   - Docker Engine 24.0+ and Docker Compose v2.20+. On Photon: `tdnf install docker docker-compose` and `systemctl enable --now docker`. Verify:
     ```bash
     docker version          # engine 24.0+ (latest stable: 29.9.2)
     docker compose version  # v2.20+ (latest stable: 5.6.1)
     ```
   - Python 3 for the setup scripts (`tdnf install python3` on Photon).
2. **(Optional) NVIDIA GPU** with current drivers and the NVIDIA Container Toolkit, for GPU TTS, DCGM metrics, or vLLM inference. Verify with `nvidia-smi` and `docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi`.
3. **API keys you want** (all optional; the stack runs without them, but models need providers):
   - Google Gemini API key from [Google AI Studio](https://aistudio.google.com/apikey)
   - AWS access key for Bedrock (OSS models: MiniMax, Qwen, Kimi)
   - [Brave Search API key](https://brave.com/search/api/) for web search
   - Hugging Face token if a vLLM model is gated
4. **DNS or hosts entries.** For a real domain, point `chatbot.`, `grafana.`, `litellm.`, `auth.`, `voice.` (and optionally `traefik.`) at the host IP. For local testing, `/etc/hosts` entries are added below.

## Fresh install

### 1. Get the repository

```bash
git clone https://github.com/mhajder/openwebui-stack.git
cd openwebui-stack
```

### 2. Run the setup script

```bash
chmod +x scripts/*.sh
./scripts/setup.sh
```

The script walks through six steps. Answer the prompts; defaults are safe:

| Step | What it does |
|---|---|
| 1. Environment file | Creates `.env` from `.env.example` (say yes on a fresh machine) |
| 2. Domain | Sets `DOMAIN` (default `localhost`). For a non-localhost domain it also appends the service hostnames to `/etc/hosts` |
| 3. Passwords | Generates strong random passwords for Postgres, LiteLLM, Grafana, Keycloak, Valkey, Qdrant, Traefik dashboard, and all OIDC client secrets into `.env` |
| 4. SSL certificates | (1) use your wildcard cert/key in `certs/`, (2) generate persistent self-signed via `scripts/generate-certs.sh`, (3) Traefik default. Choose 1 for production with a real cert, 2 for homelab |
| 5. Start Docker stack | Pulls images, starts Postgres + Keycloak, waits for health, runs the Ansible playbook to provision the realm/clients/users (installs collections and exports secrets from `.env`), then brings up the rest |
| 6. LLM providers | Optional: Discord webhook for Grafana alerts, Gemini API key, AWS Bedrock credentials, llama.cpp edge-node wizard |

If you skip step 5, start the stack manually:

```bash
docker compose up -d                    # core only
docker compose --profile monitoring up -d   # + observability exporters
```

Compose profiles in this stack:

| Profile | Services |
|---|---|
| *(none)* | traefik, open-webui, litellm, postgres, valkey, qdrant, keycloak, lgtm, brave-search-mcp, chatterbox-frontend, oauth2-proxy |
| `monitoring` | node-exporter, postgres-exporter, json-exporter, alloy, otel-collector |
| `gpu` | nvidia-gpu-exporter (DCGM) |
| `chatterbox-gpu` | chatterbox-tts-gpu (CUDA build) |
| `chatterbox-cpu` | chatterbox-tts-cpu |
| `backup` | keycloak-backup |

Enable exactly one of `chatterbox-gpu` / `chatterbox-cpu`. The GPU build needs the NVIDIA Container Toolkit.

### 3. Resolve the local hostnames (local testing only)

For `DOMAIN=localhost`, add to `/etc/hosts` **on the client machine** (the machine with the browser):

```
127.0.0.1 localhost chatbot.localhost grafana.localhost litellm.localhost auth.localhost traefik.localhost voice.localhost
```

Replace `127.0.0.1` with the host IP when the stack runs on another machine (e.g. `10.0.105.14`). The certificates are self-signed, so accept the browser warning the first time (see [Troubleshooting](#troubleshooting) to trust the CA instead).

### 4. Verify

```bash
docker compose ps                          # all Up/healthy
curl -sk https://chatbot.localhost/ | head # Open WebUI HTML
curl -sk https://auth.localhost/realms/master/.well-known/openid-configuration
```

Service URLs (`$DOMAIN` = your domain or `localhost`):

| Service | URL | Credentials |
|---|---|---|
| Open WebUI | https://chatbot.$DOMAIN | Keycloak SSO (or first registered local user) |
| Grafana | https://grafana.$DOMAIN | `admin` / `GF_SECURITY_ADMIN_PASSWORD` in `.env` |
| LiteLLM Admin UI | https://litellm.$DOMAIN/ui | Keycloak SSO |
| Keycloak Admin | https://auth.$DOMAIN/admin | `admin` / `KEYCLOAK_ADMIN_PASSWORD` |
| Voice library | https://voice.$DOMAIN | Keycloak SSO (oauth2-proxy) |
| Traefik dashboard | https://traefik.$DOMAIN | `admin` / `TRAEFIK_DASHBOARD_PASSWORD_HASH` (hash in `.env`) |

## Keycloak SSO setup

The stack uses Keycloak as the single identity provider for Open WebUI, Grafana, LiteLLM, and the voice UI. There are two ways to finish the configuration; the Ansible playbook is the complete, recommended path.

### Option A: Ansible playbook (recommended)

The playbook creates the `platform` realm, the `keycloak-provisioner` service account, the confidential clients with client roles, group-to-role mappings, one login user per group, passkeys on the realm, and syncs `otel/`, `grafana/`, `traefik/`, and `json-exporter/` to the deployment root with mode `0644` (the OTel collector runs as uid 10001 and crash-loops on `0640` root-owned configs).

1. Install collections:
   ```bash
   cd ansible
   ansible-galaxy collection install -r requirements.yaml
   ```
   This installs `middleware_automation.keycloak` 3.1.2, `community.docker` >=5.0.0 (latest 5.4.0), and `ansible.posix` >=2.0.0 (latest 2.2.2), with ansible-core 2.21.x (latest 2.21.5).

2. Export the credentials the playbook reads from the environment (or store them in Ansible Vault — see `ansible/group_vars/all/vault.yml`):
   ```bash
   export KEYCLOAK_ADMIN_PASSWORD="$(grep '^KEYCLOAK_ADMIN_PASSWORD=' .env | cut -d= -f2-)"
   export KEYCLOAK_PROVISIONER_CLIENT_SECRET="$(grep '^KEYCLOAK_PROVISIONER_CLIENT_SECRET=' .env | cut -d= -f2-)"
   export OPENWEBUI_OIDC_CLIENT_SECRET="$(grep '^OPENWEBUI_OIDC_CLIENT_SECRET=' .env | cut -d= -f2-)"
   export LITELLM_OIDC_CLIENT_SECRET="$(grep '^LITELLM_OIDC_CLIENT_SECRET=' .env | cut -d= -f2-)"
   export GF_OIDC_CLIENT_SECRET="$(grep '^GF_OIDC_CLIENT_SECRET=' .env | cut -d= -f2-)"
   export VOICE_OIDC_CLIENT_SECRET="$(grep '^VOICE_OIDC_CLIENT_SECRET=' .env | cut -d= -f2-)"
   export KEYCLOAK_USER_PLATFORM_ADMIN_PASSWORD='choose-a-password'
   export KEYCLOAK_USER_GRAFANA_EDITOR_PASSWORD='choose-a-password'
   export KEYCLOAK_USER_GRAFANA_VIEWER_PASSWORD='choose-a-password'
   export KEYCLOAK_USER_OPENWEBUI_USER_PASSWORD='choose-a-password'
   export KEYCLOAK_USER_LITELLM_USER_PASSWORD='choose-a-password'
   export KEYCLOAK_USER_LITELLM_VIEWER_PASSWORD='choose-a-password'
   export DOMAIN="$(grep '^DOMAIN=' .env | cut -d= -f2-)"
   ```

3. Run the playbook (inventory `ansible/inventory/hosts.yml` targets the Photon VM with `ansible_connection: local` — run it on the VM itself, or change the connection if running remotely):
   ```bash
   ansible-playbook playbooks/site.yml
   ```
   Use `--tags keycloak` to run only the Keycloak parts, or `--skip-tags keycloak` to skip them.

### Setup script integration

`./scripts/setup.sh` step 5 runs the playbook for you. It:

1. Installs the Ansible collections (`ansible-galaxy collection install -r requirements.yaml`) when `middleware_automation.keycloak` is missing.
2. Generates and stores `KEYCLOAK_PROVISIONER_CLIENT_SECRET` in `.env` when absent.
3. Exports `.env` to the playbook's environment (master password, client secrets, domain).
4. Generates run-only passwords for the six `KEYCLOAK_USER_*_PASSWORD` values when they are not set (these are not stored in `.env`; set them in the environment or use Ansible Vault to keep the same user passwords across runs).
5. Runs `ansible-playbook playbooks/site.yml`.

Prerequisite: `ansible-playbook` on the machine running setup.sh (ansible-core 2.21+). When it is missing, the script prints the manual commands and continues.

### Verify SSO end to end

```bash
# Open WebUI redirects to Keycloak
curl -skL -i https://chatbot.localhost/oauth/oidc/login | head -15

# Keycloak discovery (platform realm)
curl -sk https://auth.localhost/realms/platform/.well-known/openid-configuration
```

Then log in at https://chatbot.$DOMAIN with `platform-admin` (or your user) and confirm the redirect chain works.

### Role mapping

| Keycloak group | Grafana | Open WebUI | LiteLLM | Playbook user |
|---|---|---|---|---|
| `platform-admins` | GrafanaAdmin | `openwebui_admin` | `proxy_admin` | `platform-admin` |
| `admin` | Admin | `openwebui_admin` | `proxy_admin` | — |
| `editor` | Editor | — | — | `grafana-editor` |
| `viewer` | Viewer | — | — | `grafana-viewer` |
| `user` | — | `openwebui_user` | `internal_user` | `openwebui-user`, `litellm-user` |
| `litellm-admin` | — | — | `proxy_admin` | — |
| `litellm-viewer` | — | — | `proxy_admin_viewer` | `litellm-viewer` |

Users must be in a group to receive application roles. Add more users in the Keycloak admin console (Users → Add user → Credentials → set password, Temporary off → Groups → join), or append to `keycloak_users` in `ansible/group_vars/all/vars.yml` and rerun the playbook.

## Adding users and passkeys

Follow [docs/passkey-user.md](passkey-user.md): create the user in the `platform` realm, have them sign in once with a password, register a passkey from the account console (`https://auth.$DOMAIN/realms/platform/account` → Account security → Signing in → Passwordless → Set up a Passkey), then sign in to any app with **Sign in with Passkey**. Disable this by setting `keycloak_passkeys_enabled: false` in `ansible/group_vars/all/vars.yml`.

## Using the platform

### Chatting (Open WebUI)

- Sign in at https://chatbot.$DOMAIN via Keycloak.
- Model dropdown shows everything LiteLLM serves: the Gemini models, Bedrock models, `auto`, any local llama.cpp/vLLM models you registered, and the `voice-assistant` alias.
- First local user becomes admin if you kept `ENABLE_SIGNUP` true during install; turn it off in `docker-compose.yml` afterwards.
- RAG: upload documents and use the Qdrant-backed knowledge features; embeddings run through LiteLLM.

### Models available out of the box

`litellm/config.yaml` ships (checked 2026-10-21):

| Model name | Provider | Notes |
|---|---|---|
| `gemini-3.8-flash` | Gemini | fast default |
| `gemini-3.5-flash-lite` | Gemini | lightweight; voice target |
| `gemini-3.1-pro` | Gemini | reasoning |
| `gemini-4-argon` | Gemini | current flagship |
| `auto` | complexity router | heuristic tiering to the above, no extra API call |
| `minimax-m2.5`, `qwen3-next`, `qwen3-coder`, `kimi-k3` | AWS Bedrock | needs AWS keys |
| `voice-assistant` | alias → `gemini-3.5-flash-lite` | for voice key |

Add a model to `model_list` in `litellm/config.yaml`, then `docker compose restart litellm` (not needed when `store_model_in_db` picked it up from the Admin UI).

### Guardrails

`default-content-filter` runs on every request (LiteLLM built-in `litellm_content_filter`): blocks SSNs, AWS keys, GitHub tokens, prompt injection, and harmful categories; masks emails and phone numbers. System and tool turns are skipped. Blocked requests return HTTP 400 before billing. Tune patterns in `guardrails:` in `litellm/config.yaml`.

### Web search in chat

With `BRAVE_SEARCH_API_KEY` set, every chat model can search the web with no per-chat setup: the `brave-search-mcp` container serves the official Brave MCP tools on the backend network, LiteLLM mounts them at `/mcp/brave_search`, and a startup hook (`litellm/brave_mcp_callback.py`) injects them into every `/v1/chat/completions` request. Verify:

```bash
docker compose logs litellm | grep "startup hook"
# end-to-end: the model should answer with fresh web results
KEY=$(grep -m1 '^LITELLM_MASTER_KEY=' .env | cut -d= -f2-)
curl -sk https://litellm.localhost/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model": "gemini-3.8-flash", "messages": [{"role": "user", "content": "Search the web for today top tech news"}]}'
```

### Voice (TTS)

Chatterbox is the only TTS engine, OpenAI-compatible.

- In chat: Open WebUI calls `http://chatterbox-tts:4123/v1` on the backend network directly.
- Voice library UI: https://voice.$DOMAIN, protected by oauth2-proxy with Keycloak.
- First speech request downloads the model into the `chatterbox_models` volume and can take minutes.
- For Home Assistant or other external speech clients, create the restricted voice key after LiteLLM is healthy:
  ```bash
  ./scripts/create-voice-key.sh   # writes LITELLM_VOICE_API_KEY to .env
  ```
  Point the client at `https://litellm.$DOMAIN/v1`, model `voice-assistant`, with that key. Never use the master key.

### Calling LiteLLM directly

```bash
KEY=$(grep -m1 '^LITELLM_MASTER_KEY=' .env | cut -d= -f2-)
curl -sk https://litellm.localhost/v1/models -H "Authorization: Bearer $KEY"
```

Virtual keys, budgets, and teams are managed in the LiteLLM Admin UI (https://litellm.$DOMAIN/ui) with your Keycloak login.

## Local inference: llama.cpp edge nodes

For GGUF models on a single GPU host (typically Fedora with an NVIDIA GPU):

1. Run the wizard (also reachable from setup step 6):
   ```bash
   ./scripts/setup-edge-nodes.sh
   ```
   It asks for each node's host, port, and model, then GPU layers, context size, parallel slots, model dir, and `llama-server` path. It writes `LLAMA_CPP_EDGE_NODES` to `.env`, systemd unit templates under `edge-nodes/`, a per-node install script, and a monitoring script, and prints the LiteLLM snippet.
2. Install on each node: copy `edge-nodes/setup-edge-node.sh` and the node's unit file, run the install script (needs sudo), and put the GGUF at `/opt/models/<model>.gguf`. Current `llama-server` uses `--parallel` (`-np`), not `--slots`.
3. Register in LiteLLM: add the printed snippet to `model_list` in `litellm/config.yaml` (api_base `http://<host>:<port>/v1`, api_key `local`), then `docker compose restart litellm`.
4. Check health: `./edge-nodes/monitor-edge-nodes.sh` (reads `LLAMA_CPP_EDGE_NODES` and curls `/health`).

## Local inference: vLLM distributed compute

For HF-precision models across multiple GPU nodes connected by 10/25GbE, use the `vllm_compute` Ansible role. It deploys `vllm/vllm-openai` (latest release v0.32.0) with Docker + NVIDIA runtime on Fedora nodes, in three modes. **Full reference — architecture diagram, all parallelism modes with the exact flags each renders, variable reference, setup and operations: [vllm-inference.md](vllm-inference.md).**

| Mode | Topology | Best for |
|---|---|---|
| `ray` (default) | 1 Ray head + workers; `vllm serve` on the head; pipeline parallel across nodes | 10GbE; large models; recommended default |
| `ray` + `vllm_cross_node_tensor_parallel: true` | TP spread across the cluster | 25GbE+; latency-sensitive; verify fabric first |
| `standalone` | full replica per node; LiteLLM load balances | high aggregate throughput, small-enough models |

1. **Inventory**: list your nodes in the `vllm_compute` group in `ansible/inventory/hosts.yml`; mark exactly one `vllm_ray_head: true`:
   ```yaml
   vllm_compute:
     hosts:
       vllm-node-01:
         ansible_host: 10.0.105.21
         ansible_user: root
         vllm_ray_head: true
       vllm-node-02:
         ansible_host: 10.0.105.22
         ansible_user: root
   ```
2. **Configure** `ansible/group_vars/vllm_compute/vars.yml`: model (`vllm_model`), GPUs per node (`vllm_tensor_parallel_size`), pipeline size (`vllm_pipeline_parallel_size`), firewall CIDR (`vllm_firewall_zone_source`), and set `VLLM_API_KEY`/`HF_TOKEN` in the controller environment (they are in `.env.example`).
3. **Deploy**:
   ```bash
   cd ansible
   ansible-galaxy collection install -r requirements.yaml
   ansible-playbook playbooks/inference.yml
   ```
    Per node the global `prerequisites` role first checks that Docker, `docker compose`, and the NVIDIA driver are present (the driver via `prerequisites_check_nvidia: true` for the `vllm_compute` group). The `vllm_compute` role then opens the pinned Ray ports (6379, 8265, 13377–13379, 14000–15000) in a `vllm-cluster` firewalld zone restricted to the compute CIDR, deploys `vllm-ray` and `vllm-serve` systemd units (containers run with `--network host --gpus all --ipc host --shm-size 16g`), waits for all Ray nodes to register, then waits for `GET /health` on port 8000. Docker Engine, the compose plugin, `nvidia-container-toolkit`, the nvidia runtime registration (`nvidia-ctk runtime configure --runtime=docker`), and the NVIDIA driver itself are host prerequisites: install them on each node first (for example on Fedora: `dnf install docker docker-compose nvidia-container-toolkit` and `dnf install akmod-nvidia xorg-x11-drv-nvidia-cuda` for the driver).
 4. **Register with LiteLLM**: the run renders `litellm/vllm-models.yaml` (one cluster endpoint in ray mode, one entry per node in standalone). Merge it into `model_list` in `litellm/config.yaml` on the stack host, then `docker compose restart litellm`.

    **Flux image generation** is deployed by the same role via `vllm_flux_enabled: true` and the `vllm_flux_models` list in `ansible/group_vars/vllm_compute/vars.yml` - one standalone container and `vllm-flux-<name>` systemd unit per configured model, each on its own host port. It is no longer a `docker-compose.yml` service; flux entries (with `mode: image_generation`) render into `litellm/vllm-models.yaml` with the rest.

Notes:
- Ray traffic is unauthenticated and can execute code; keep `vllm_firewall_zone_source` to the compute segment and always set `VLLM_API_KEY`.
- Model weights must exist at the same path or HF id on every node; the role mounts a shared cache dir (`vllm_hf_cache_dir`, default `/mnt/data1/huggingface`). Pre-download large models with `huggingface-cli download` on each node.
- Every node needs the same vLLM image version.

## Monitoring

Start with `--profile monitoring` (the Ansible playbook enables it by default via `docker_compose_profiles`).

- Grafana at https://grafana.$DOMAIN, `admin` / `GF_SECURITY_ADMIN_PASSWORD`.
- Pre-provisioned dashboards: LiteLLM, node-exporter, Open WebUI, PostgreSQL, Traefik, OpenTelemetry, NVIDIA DCGM (with `gpu` profile), tokenization, Keycloak, Chatterbox, stack logs, and container resources.
- Traces: Grafana → Explore → Tempo.
- Logs: Loki via the stack-logs dashboard. Grafana Alloy tails every container through the Docker API with label `service_name`.
- Alerts: provisioned in `grafana/provisioning/alerting/`; add your Discord webhook as `GRAFANA_DISCORD_WEBHOOK_URL` in `.env` (setup step 6 can do it).

## Backups

```bash
# Run the Keycloak database backup once
docker compose --profile backup up keycloak-backup

# Schedule it (crontab on the host)
0 2 * * * cd /mnt/data1/openwebui-stack && docker compose --profile backup up keycloak-backup
```

Retention is `BACKUP_RETENTION_DAYS` (default 7) in `.env`. Backups land in the `keycloak_backups` volume. For full-stack protection, also back up the Postgres data volume and `.env` off-host; `.env` holds every secret in the stack.

## Upgrades and version pinning

Most images track a floating tag (`latest` or `main`). To upgrade a rolling image:

```bash
docker compose pull && docker compose --profile monitoring up -d
```

Pinned tags in `docker-compose.yml` (Traefik `v3.7`, Keycloak `26.8`, Valkey `9-alpine`, oauth2-proxy `v7.15.1`, PostgreSQL `18-alpine`) advance only when you edit the file. Current latest releases to consider pinning to:

- Traefik v3.7.16, Keycloak 26.8.1, oauth2-proxy v7.15.5, Valkey 9.1.3, PostgreSQL 18.x, Qdrant v1.20.0, vLLM v0.32.0, OTel Collector v0.164.1, LGTM 0.38.1 (Grafana 13.3.0 inside).
- Keycloak minor upgrades (26.8 → 26.8.1) are safe; check [Keycloak upgrade docs](https://www.keycloak.org/docs/latest/upgrading/) before major versions.
- After upgrading LiteLLM or Open WebUI, re-verify the SSO flow and the Brave MCP startup hook (see [Troubleshooting](#troubleshooting)).

Update the Ansible collections with `ansible-galaxy collection install -r ansible/requirements.yaml --upgrade`.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Open WebUI "email or password incorrect" | OAuth failed and it fell back to local login, or client secret missing | Check `OPENWEBUI_OIDC_CLIENT_SECRET` in `.env`; rerun the Ansible playbook; keep `ENABLE_OAUTH_SIGNUP: "true"` |
| LiteLLM "authentication database unreachable" | DB creds or OIDC config incomplete | Verify `DATABASE_URL` in the compose env, `LITELLM_OIDC_CLIENT_SECRET`; restart litellm |
| Grafana "Login failed - Failed to get token" | Missing client secret or user not in a group | Set `GF_OIDC_CLIENT_SECRET`; put the user in a Grafana group in Keycloak |
| Browser cert warnings | self-signed certificates | Accept the warning, or import the CA/certificate your setup produced: `certs/server.crt` when using generated self-signed certs, or your wildcard CA for production |
| 404 on `auth.localhost` | hosts entry missing | Add `auth.$DOMAIN` (and the others) to the client machine's `/etc/hosts` |
| Keycloak admin console unreachable | `KEYCLOAK_ADMIN_IP_RANGE` excludes you | Set it to your CIDR (or `0.0.0.0/0` to allow all) in `.env` |
| First speech request times out | model downloading; Traefik header timeout | Wait for the download; the `chatterbox@file` serversTransport disables the response header timeout for TTS only |
| Brave MCP tools not injected | startup hook failed to load | `docker compose logs litellm \| grep "startup hook"`; confirm `LITELLM_WORKER_STARTUP_HOOKS` env; note the hook must match `acompletion`, not just `completion` |
| OTel collector crash-loops | config unreadable at 0640 | Run the Ansible playbook (syncs 0644), or `chmod 644 otel/config.yaml` |
| Ray workers never join | firewall or MTU issues on the compute NIC | Check the `vllm-cluster` firewalld zone covers all node IPs; verify `ray status` inside the head container; 10GbE jumbo frames (MTU 9000) help but are not required |
| vLLM OOM at startup | `gpu_memory_utilization` too high or model too big | Lower `vllm_gpu_memory_utilization`, reduce `vllm_max_model_len`, or add nodes/stages |
| Docker log volume fills disk | default json-file rotation | The compose `logging` anchor sets rotation; for adopters, ensure `max-size` is set |

Debug commands:

```bash
docker compose ps
docker compose logs --tail=50 <service>
docker compose exec postgres psql -U postgres -l   # list databases
docker compose exec keycloak /opt/keycloak/bin/kc.sh --version
```
