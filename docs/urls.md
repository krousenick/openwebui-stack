# URLs and ports

Every address the stack publishes or uses, in one place. Replace `example.com` with your `DOMAIN` (set in `ansible/group_vars/all/vars.local.yml`) and `<node>` with a compute node's inventory hostname, for example `vllm-node-01`.

## Names and DNS

Every public name points at the **stack host**, where Traefik terminates TLS with the `*.example.com` certificate and routes by name. That includes the launcher portals: `launcher-<node>.example.com` points at the stack host too, not at the node. Traefik forwards to the node itself.

| Name | Serves |
|------|--------|
| `chatbot.example.com` | Open WebUI |
| `voice.example.com` | Chatterbox voice UI, behind a Keycloak sign-in gate |
| `auth.example.com` | Keycloak: sign-in, account console, admin console |
| `litellm.example.com` | LiteLLM: API and admin UI |
| `grafana.example.com` | Grafana |
| `traefik.example.com` | Traefik dashboard |
| `launcher-<node>.example.com` | LLM Launcher GUI of one compute node (when `launcher_enabled`) |

## For everyone

| What | URL |
|------|-----|
| Chat | `https://chatbot.example.com` |
| Voice | `https://voice.example.com` |
| Account console: profile, password, passkeys, sessions | `https://auth.example.com/realms/platform/account/` |
| Request a password reset (needs a mail server, see [keycloak-users.md](keycloak-users.md#password-reset)) | `https://auth.example.com/realms/platform/login-actions/reset-credentials?client_id=account-console` |
| Sign out of every application | `https://auth.example.com/realms/platform/protocol/openid-connect/logout` |

New accounts register from the **Register** link on the sign-in page. [keycloak-users.md](keycloak-users.md) covers accounts, groups and self-service in detail.

## For administrators

| What | URL | Who gets in |
|------|-----|-------------|
| Keycloak admin console, `platform` realm | `https://auth.example.com/admin/platform/console/` | Members of `platform-admins` |
| Keycloak admin console, `master` realm | `https://auth.example.com/admin/` | The bootstrap `admin` (`keycloak_admin_password` in the vault) |
| LiteLLM admin UI: keys, models, usage | `https://litellm.example.com/ui` | Groups `platform-admins`, `admin` and `litellm-admin` (full), `litellm-viewer` (read only), `user` (own keys) |
| Grafana | `https://grafana.example.com` (**Sign in with SSO**) | Groups `platform-admins` (server admin), `admin`, `editor`, `viewer` |
| Traefik dashboard | `https://traefik.example.com/dashboard/` | Basic auth: `admin` and `traefik_dashboard_password` from the vault |
| LLM Launcher GUI | `https://launcher-<node>.example.com` | `launcher_admin`: groups `platform-admins` and `admin` |

### Grafana dashboards

Each is at `https://grafana.example.com/d/<uid>`.

| uid | Dashboard |
|-----|-----------|
| `stack-openwebui` | Open WebUI |
| `stack-litellm` | LiteLLM |
| `tokenization` | Tokenization |
| `keycloak-auth` | Keycloak Authentication |
| `chatterbox-tts` | Chatterbox TTS |
| `stack-traefik` | Traefik |
| `stack-postgres` | PostgreSQL |
| `stack-node-exporter` | Node Exporter (the stack host) |
| `stack-nvidia-dcgm` | NVIDIA DCGM (the stack host's GPU; needs the `gpu` compose profile) |
| `container-resources` | Container resources |
| `stack-opentelemetry` | OpenTelemetry Collector |
| `stack-errors` | Stack errors |
| `vllm-remote-nodes` | vLLM Remote Nodes (written by `playbooks/inference.yml`) |
| `vllm-inference-deployments` | Inference Deployments (written by `playbooks/inference.yml`) |

## APIs

| What | URL | Credential |
|------|-----|------------|
| OpenAI-compatible API, every model LiteLLM serves | `https://litellm.example.com/v1` (`/chat/completions`, `/models`, `/embeddings`, ...) | A LiteLLM virtual key, or the master key |
| Anthropic-compatible API (Claude models) | `https://litellm.example.com/v1/messages` | Same |
| Open WebUI API | `https://chatbot.example.com/api/chat/completions`, `/api/models` | An Open WebUI API key (Settings, Account) |
| MCP servers through the LiteLLM gateway | `https://litellm.example.com/mcp/<server>`: `brave_search`, `playwright_cluster`, `blender`, `freecad`, `memory` (saved memories), and `printer` when enabled | A LiteLLM key; `memory` also needs the key to carry a `user_id` |
| Saved memories over REST (admin) | `https://litellm.example.com/v1/memory` (`?key_prefix=user:<owner>:`) | The master key; LiteLLM 1.110 refuses every other key on this route, so other callers use the `memory` MCP server |
| Skills discovery index (`npx skills add`) | `https://litellm.example.com/.well-known/agent-skills/index.json` | None |
| Skills store and Claude Code marketplace | `https://litellm.example.com/v1/skills`, `/claude-code/plugins/<name>` | A LiteLLM key |
| OpenID Connect discovery | `https://auth.example.com/realms/platform/.well-known/openid-configuration` | None |
| Health checks | `https://chatbot.example.com/health`, `https://litellm.example.com/health/liveliness` and `/health/readiness`, `https://grafana.example.com/api/health` | None |

## Ports on the stack host

| Port | What | Used by |
|------|------|---------|
| 80 | Redirects to 443 | Browsers |
| 443 | Traefik: every name above | Browsers, API clients |
| 3100 (on `STACK_HOST_IP`) | Loki push API, `http://<stack host>:3100/loki/api/v1/push` | The log shippers on the compute nodes |

Traefik's own ports 8080 (API and metrics) and 8082 (the `playwright-mcp` entry point) stay inside the Docker network.

## Compute nodes

| Port | What | Reached by |
|------|------|------------|
| 8000 (`vllm_port`) | vLLM OpenAI API: `/v1` needs the node's API key; `/health` and `/metrics` do not. With the launcher's single-model slot, whatever model the GUI last launched | LiteLLM, the OTel collector |
| 9100 | node-exporter | The OTel collector |
| 9835 or 9400 | GPU exporter: nvidia-smi (GeForce) or DCGM (datacenter GPUs) | The OTel collector |
| 4180 (`vllm_launcher_auth_port`) | Launcher sign-in gate (oauth2-proxy). `GET /api/stack/slot` passes it without a sign-in and needs the bearer `LAUNCHER_STATUS_TOKEN` instead: what the node serves, for the stack's launcher follower. Traefik does not publish that path | Traefik on the stack host; the `launcher-follower` container |
| 8765, loopback only | Launcher GUI | The gate on the same node; never reachable from the network |
| 8100-8109 (`vllm_launcher_model_ports`) | Extra models started from the launcher GUI (only with `vllm_launcher_swap: false`) | LiteLLM, once registered there by hand |
| 8005 and up | Flux image servers (`vllm_flux_base_port`) | LiteLLM |
| 8010 and up | Aux model servers (`vllm_aux_base_port`) | LiteLLM |
| 6379, 8265, 13377-13379, 14000-15000 | Ray (ray mode only) | The other compute nodes |

The node's `vllm-cluster` firewall zone admits these ports from `vllm_firewall_zone_source`. A router between the stack host and the nodes has to pass them too. If it cannot pass the gate port, choose one it does pass and set it on both sides: `launcher_auth_port` for the stack (`site.yml`), `vllm_launcher_auth_port` for the node (`inference.yml`).

## Inside the stack

The addresses the containers use to reach each other on the Docker networks. None is published on the host.

| Address | What |
|---------|------|
| `http://litellm:4000/v1` | LiteLLM, as Open WebUI calls it; MCP at `http://litellm:4000/mcp/<server>` |
| `http://open-webui:8080` | Open WebUI; Keycloak sends back-channel logout to `/oauth/backchannel-logout` |
| `http://keycloak:8080/realms/platform/.well-known/openid-configuration` | Keycloak discovery, as Open WebUI reads it; metrics on `keycloak:9000` |
| `postgres:5432`, `valkey:6379`, `http://qdrant:6333` | Databases, cache, vector store |
| `http://lgtm:4317` (gRPC), `http://lgtm:4318` (HTTP) | OpenTelemetry ingest; Grafana on `lgtm:3000`, Loki on `lgtm:3100` |
| `http://brave-search-mcp:8080/mcp`, `http://blender:8000/mcp`, `http://freecad:8000/mcp`, `http://printer-mcp:3000/mcp`, `http://traefik:8082/mcp` (Playwright), `http://memory-mcp:8080/mcp` | MCP servers behind the LiteLLM gateway |
| `http://memory-mcp:8080/v1/index` | A user's memory index, as LiteLLM's memory hook reads it (signed like the tool calls) |
| `http://code-sandbox:8888` | Jupyter for Open WebUI's code execution |
| `http://chatterbox-tts-server:8004/v1` | Text to speech, as Open WebUI calls it |
| `oauth2-proxy:4180` | Voice sign-in gate; metrics on 4181 |
| `node-exporter:9100`, `nvidia-gpu-exporter:9400`, `postgres-exporter:9187`, `traefik:8080` | Metrics the OTel collector scrapes |

## Quick checks

```bash
# From the stack host. A signed-out request to an application is sent to Keycloak.
curl -sk -o /dev/null -w '%{http_code} %{redirect_url}\n' https://chatbot.example.com/oauth/oidc/login
curl -sk -o /dev/null -w '%{http_code} %{redirect_url}\n' https://launcher-vllm-node-01.example.com/

# Open endpoints
curl -sk https://auth.example.com/realms/platform/.well-known/openid-configuration | head -c 200; echo
curl -sk -o /dev/null -w '%{http_code}\n' https://litellm.example.com/health/liveliness

# A compute node, from the stack host
curl -s -o /dev/null -w '%{http_code}\n' http://<node>:8000/health
curl -s -o /dev/null -w '%{http_code}\n' http://<node>:4180/ping    # the launcher gate
# What the node's launcher serves (in the deployed tree, for the token)
curl -s -H 'Host: launcher-vllm-node-01.example.com' \
  -H "Authorization: Bearer $(grep -m1 '^LAUNCHER_STATUS_TOKEN=' .env | cut -d= -f2-)" \
  http://<node>:4180/api/stack/slot
```
