---
name: stack-ops
description: >-
  Operate the Open WebUI / LiteLLM / Keycloak stack: re-run the Ansible
  playbook, verify service health through docker compose, diagnose the
  LiteLLM proxy (SpendLogs, guardrails, MCP injection, context
  compaction), and read the deployed tree on the Photon host instead of
  guessing. Use whenever a question or task is about deploying, healing,
  or inspecting this stack.
license: MIT
---

# Stack Ops (openwebui-stack)

Source of truth is the checkout's AGENTS.md, README, and the deployed tree
on the Photon host. Read those before improvising.

## Layout facts

| Thing | Where |
|---|---|
| Deployed tree | `/mnt/data1/openwebui-stack` (Photon VM, not the Mac) |
| Inventory | `ansible/inventory/hosts.local.yml` (gitignored; placeholder in `hosts.yml`) |
| Secrets | `ansible/group_vars/all/vault.local.yml` (gitignored) |
| Rendered files | `.env` (`templates/env.j2`), `docker-compose.yml` (`templates/docker-compose.yml.j2`), `litellm/config.yaml` (`templates/config.yaml.j2`) |
| Deploy command | `./scripts/setup.sh` or `cd ansible && ansible-playbook playbooks/site.yml` |

## Safe routine checks (run on the Photon host)

```bash
cd /mnt/data1/openwebui-stack
docker compose ps                       # service status + health
docker compose logs --since 15m litellm # recent proxy logs
docker compose exec postgres psql -U litellm -d litellm -c \
  "SELECT model_name, count(*) FROM \"LiteLLM_SpendLogs\"
   WHERE \"startTime\" > now() - interval '1 day' GROUP BY 1 ORDER BY 2 DESC;"
```

Verify web-search injection (non-streaming path) worked:

```bash
docker compose exec postgres psql -U litellm -d litellm -c \
  "SELECT request_id, jsonb_array_length(proxy_server_request->'tools') AS ntools
   FROM \"LiteLLM_SpendLogs\" ORDER BY \"startTime\" DESC LIMIT 3;"
```

## Rules

1. **Never treat the checkout as live.** The stack runs from the deployed
   tree; config changes need `ansible-playbook` (or an rsync of the synced
   trees) plus `docker compose up -d` on the host.
2. **Playbook re-run is the heal.** SSO client secrets, LiteLLM model
   lists, Traefik labels, and guardrail wiring are all declarative;
   rerun instead of hand-editing containers.
3. **Secrets stay in vault.local.yml.** Never echo them into logs, chat,
   or commands you print. Generation tiering: vault entry → environment
   variable → deployed `.env` → generated once.
4. **Streaming is special.** The Brave MCP injector must skip `stream:
   true` requests; streaming chats get web search through Open WebUI's
   client-side tool server instead. Do not "fix" this asymmetry.
5. **Before `docker compose up`, check bind-mount sources.** `docker
   compose` auto-creates missing short-syntax bind sources as empty
   directories; the playbook's mount check fails the run and names the
   exact `rm -rf` to run instead.
6. **Ansible on Photon has no sudo.** The deploy user is root; rsync runs
   without the sudo wrapper. Keep this in mind when writing new tasks.

## Known failure modes

| Symptom | Meaning | Fix |
|---|---|---|
| Model list empty in a chat | OAuth flow rewrote connection `auth_type` | Rerun playbook (`openwebui_config.yml` heals it), recreate open-webui |
| "Tool not found" tool responses, then Gemini "Missing corresponding tool call" | MCP injection ran on a streaming request | Do not inject on stream; rerun playbook |
| OTEL collector crash-loop | `otel/config.yaml` unreadable (uid 10001) | Must be 0644, not 0640 |
| LiteLLM "authentication database unreachable" | OIDC config incomplete | Rerun playbook, check `DATABASE_URL` |
