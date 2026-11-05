# Plan: Headroom Prompt Compression Sidecar for LiteLLM

Status: **Approved plan, not yet implemented.** This document is self-contained:
a future session can execute it without the original conversation context.

Decisions already made with the stack owner (do not re-ask):

| Decision | Choice | Rationale |
|---|---|---|
| Rollout mode | `default_on: true` | Open WebUI talks to LiteLLM with the master key (ansible/roles/openwebui_stack/templates/docker-compose.yml.j2:96), so per-key opt-in cannot distinguish users. Compress everything; opt-out per request with the `x-headroom-bypass: true` header. |
| User-message compression | `HEADROOM_COMPRESS_USER_MESSAGES=1` on the headroom container | Headroom skips `user`/`system` messages by default, and Open WebUI chat traffic is almost entirely `user`-role. Without this, `requests_compressed` stays 0 and the deployment compresses nothing. This env var lives on the **headroom container**, not in litellm's config. |
| LiteLLM-to-Headroom auth | None | Backend-only Docker network, no published port, no Traefik route — same trust boundary as the code-sandbox container. No api_key on the guardrail entry, no HEADROOM_API_KEY. |

---

## 1. Background: what Headroom is and how the LiteLLM integration works

Headroom is a context-optimization layer for LLM applications. It compresses
tool outputs, database results, file reads, and RAG payloads before they reach
the model, aiming for the same answers at a fraction of the input tokens.

Upstream docs: https://docs.litellm.ai/docs/proxy/headroom (verified 2026-11-05).
Key facts from the docs, which the implementer should re-verify if LiteLLM
has since changed:

- **LiteLLM version requirement: >= v1.92.0.** This stack runs
  `ghcr.io/berriai/litellm:main-latest` (docker-compose.yml.j2:210), which is
  far past that, but confirm at deploy time with
  `docker compose exec litellm litellm --version`.
- **Architecture:** Headroom runs as a sidecar service. LiteLLM calls it
  in-process during the `pre_call` guardrail step: it POSTs
  `{"messages": [...], "model": "<model>"}` to `{api_base}/v1/compress`, and
  the returned `messages` list replaces the request payload before the LLM
  call. Clients and the upstream provider never talk to Headroom directly.
- **Config shape:** a `guardrails:` entry in litellm's config.yaml with
  `guardrail: headroom`, `mode: pre_call` (only meaningful mode; no-op on
  responses), `api_base`, optional `api_key`, optional `default_on` (bool,
  default false). Env-var fallbacks: `HEADROOM_API_BASE`, `HEADROOM_API_KEY`.
- **No official image.** The docs provide a one-file Dockerfile that pip
  installs `headroom-ai[proxy]==0.27.0` on python:3.13-slim and runs
  `headroom proxy --host 0.0.0.0 --port 8787`.
- **Telemetry off:** `HEADROOM_TELEMETRY=off` in the Dockerfile.
- **What Headroom skips by design:**
  - Messages with an Anthropic `cache_control` marker, always (compressing
    them would break prompt-cache byte matching; no override exists). This
    means it coexists safely with this stack's
    `enable_anthropic_prompt_caching: true` (litellm/config.yaml).
  - `user`/`system` messages unless `HEADROOM_COMPRESS_USER_MESSAGES=1` is
    set on the headroom container (see decision table above).
- **Verification signals:**
  - Response header `x-litellm-applied-guardrails: headroom-compression`.
  - Admin UI -> Logs -> Guardrails & Policy Compliance panel shows the
    pre-call step with latency, plus spend-log `guardrail_information`.
  - Headroom's own metrics/logs: `requests_compressed`.
- **Bypass:** header `x-headroom-bypass: true` (literal `true`,
  case-insensitive) skips compression for that request. This header can only
  ever switch compression OFF; there is no header that switches it on.
  Per-request opt-in (when `default_on` is false) is the `guardrails` request
  body field (OpenAI format) or `litellm_metadata.guardrails` (Anthropic
  format) — not needed here because we chose `default_on: true`.
- **Auto-router interplay:** from v1.101.0 an auto router can name a
  compression guardrail per hop via `auto_router_routing_compression` /
  `auto_router_model_compression`, which suppresses key/team/request-level
  guardrail attachments for its own requests. This stack's `auto` and
  `adaptive` model entries (litellm/config.yaml model_list) are auto routers;
  if compression seems absent on `auto`/`adaptive` traffic, this is the
  first place to look.

### Relationship to the existing context-compaction callback

This stack already has `litellm/context_compaction_callback.py` (loaded via
`LITELLM_WORKER_STARTUP_HOOKS` in docker-compose.yml.j2:256). The two solve
different problems and coexist; do not remove or "merge" them:

| | Custom compaction callback | Headroom |
|---|---|---|
| Purpose | Failure prevention: trims histories that would exceed the model's context window (otherwise the provider 400s) | Cost/latency optimization: shrinks input tokens on every request |
| Method | Deletion: drops oldest non-system messages, keeps system prompts + last N (CONTEXT_COMPACTION_KEEP_LAST_N, default 6), aligns the cut to a user turn so tool calls/results aren't orphaned, inserts a `[Earlier conversation was compacted...]` note | Semantic compression of tool outputs / RAG payloads via sidecar; answer-quality preserving |
| Frequency | Rare: no-op unless over budget (window − max_tokens) | Every request (with default_on) |
| Cost | Zero (in-process, char-based token estimate) | One HTTP hop to the sidecar per request |
| Info loss | Total for dropped turns | None by design |
| Opt-out | `metadata: {"context_compaction": false}` per request | `x-headroom-bypass: true` per request |

Either guardrail/hook ordering is safe: if the trimmer runs first, Headroom
compresses what survived; if Headroom runs first, the trimmer may not need to
fire at all. One live check after deployment should confirm oversized
histories still trim and compress without conflict (see verification).

Other pre-call pieces that coexist: the built-in content-filter guardrail
(`default-content-filter`, litellm/config.yaml guardrails list) and the
Brave MCP injector (`brave_mcp_callback.py`, which correctly skips
`stream: true` requests). None conflict with Headroom.

---

## 2. Files to create or change

The deployed stack is rendered by Ansible: the live files under
`/mnt/data1/openwebui-stack` are generated from templates in
`ansible/roles/openwebui_stack/templates/`, and `litellm/config.yaml` is
excluded from the rsync sync precisely because it is rendered from
`templates/config.yaml.j2` (see ansible/roles/openwebui_stack/tasks/sync.yml
and tasks/litellm_config.yml). **Edit the templates, not the rendered files.**
The repo-root `litellm/config.yaml` is a working copy; the deployed one comes
from the .j2 template, so the guardrail entry must go in the .j2.

### 2.1 New file: `headroom/Dockerfile`

Content per the upstream docs (verbatim except the added
HEADROOM_COMPRESS_USER_MESSAGES line):

```dockerfile
FROM python:3.13-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && pip install --no-cache-dir "headroom-ai[proxy]==0.27.0" \
    && apt-get purge -y build-essential \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*
EXPOSE 8787
ENV HEADROOM_TELEMETRY=off
# Open WebUI chat traffic is almost entirely user-role; without this the
# headroom default skips user/system messages and compresses nothing.
ENV HEADROOM_COMPRESS_USER_MESSAGES=1
CMD ["headroom", "proxy", "--host", "0.0.0.0", "--port", "8787"]
```

Note: `headroom-ai[proxy]==0.27.0` is the version pinned in the LiteLLM docs
as of 2026-11-05. During implementation, check whether a newer
`headroom-ai` release exists (https://pypi.org/project/headroom-ai/) and
prefer the docs' pinned version unless there is a reason to bump.

### 2.2 `ansible/group_vars/all/vars.yml`

Add near the other optional-integration toggles (`brave_search_enabled`,
`code_sandbox_enabled`, `litellm_gemini_models_enabled` — around line 109):

```yaml
# Headroom prompt-compression sidecar for LiteLLM (guardrail
# headroom-compression, default_on). Compresses tool outputs / RAG /
# long-chat input before the provider call. Backend-only network; no
# auth between litellm and the sidecar. See docs/plans/headroom-litellm-integration.md
# and https://docs.litellm.ai/docs/proxy/headroom
headroom_enabled: true
```

### 2.3 `ansible/roles/openwebui_stack/templates/docker-compose.yml.j2`

Add a `headroom` service, wrapped in `{% if headroom_enabled | default(false) | bool %}`.
Follow the code-sandbox service as the structural pattern (see the existing
`build:` service at docker-compose.yml.j2:706):

```yaml
{% if headroom_enabled | default(false) | bool %}
  # ===========================================================================
  # Headroom - prompt compression sidecar for LiteLLM
  # LiteLLM calls http://headroom:8787/v1/compress in its pre_call guardrail
  # step (guardrails entry "headroom-compression" in litellm/config.yaml).
  # Backend-only network: no published port, no Traefik route, no auth —
  # same trust boundary as code-sandbox.
  # https://docs.litellm.ai/docs/proxy/headroom
  # ===========================================================================
  headroom:
    build:
      context: ./headroom
      dockerfile: Dockerfile
    image: ${HEADROOM_IMAGE:-openwebui-stack/headroom:latest}
    container_name: ${COMPOSE_PROJECT_NAME:-openwebui-stack}_headroom
    restart: unless-stopped
    logging: *default-logging
    security_opt:
      - no-new-privileges:true
    networks:
      - backend
    healthcheck:
      test: ["CMD", "bash", "-c", "exec 3<>/dev/tcp/127.0.0.1/8787"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 30s
{% endif %}
```

Also add to the `litellm` service's `depends_on` block
(docker-compose.yml.j2:308) so LiteLLM never calls a cold sidecar:

```yaml
      headroom:
        condition: service_healthy
```

(guard with the same `{% if headroom_enabled ... %}` Jinja conditional,
matching the file's existing style for conditional service blocks).

Verify the healthcheck syntax against what the headroom proxy actually
serves: if `GET /` or a health endpoint exists, prefer an HTTP probe; the
TCP probe above is a safe minimum because `headroom proxy` opens :8787 at
startup. If the image lacks bash, switch to a python-based check
(`python -c "import socket; ..."`).

### 2.4 `ansible/roles/openwebui_stack/templates/config.yaml.j2`

Append an entry to the existing `guardrails:` list (config.yaml.j2:102,
the `default-content-filter` block ends around line 172). Keep the same
comment style as the file uses. Wrapped in the toggle:

```yaml
{% if headroom_enabled | default(false) | bool %}
  # Headroom prompt compression. Sidecar container (headroom service,
  # backend network, no auth). default_on compresses every request;
  # per-request opt-out header: x-headroom-bypass: true. User/system
  # message compression is enabled via HEADROOM_COMPRESS_USER_MESSAGES=1
  # on the headroom container, not here. Requires LiteLLM >= 1.92.0.
  # https://docs.litellm.ai/docs/proxy/headroom
  - guardrail_name: headroom-compression
    litellm_params:
      guardrail: headroom
      mode: pre_call
      api_base: http://headroom:8787
      default_on: true
{% endif %}
```

Note: also update the repo-root working copy `litellm/config.yaml` with the
same entry (unconditionally or commented) so the two files stay consistent —
the repo copy is what people read; the .j2 is what deploys.

### 2.5 `ansible/roles/openwebui_stack/tasks/sync.yml`

Add `headroom` to the synchronize loop (sync.yml:34) so runs from a remote
control machine ship the new directory to the deployed tree:

```yaml
  loop:
    - otel
    - grafana
    - traefik
    - json-exporter
    - litellm
    - sandbox
    - headroom
```

### 2.6 `.env.example`

No new required vars (no auth, no key). Optionally add a commented pin:

```bash
# -----------------------------------------------------------------------------
# Headroom prompt compression (docs/plans/headroom-litellm-integration.md)
# -----------------------------------------------------------------------------
# HEADROOM_IMAGE=openwebui-stack/headroom:latest
```

### 2.7 `README.md`

Add a "Prompt Compression (Headroom)" section near the Brave Search MCP /
Context Compaction sections. Cover:

- What it is and the architecture (sidecar; litellm pre_call; client and
  provider never talk to headroom directly).
- How it's enabled (`headroom_enabled` in ansible vars; guardrail
  `headroom-compression` with `default_on: true`).
- The distinction vs. the context-compaction callback (see the table in
  section 1 above — copy it in condensed form).
- Per-request opt-out: `x-headroom-bypass: true` header.
- Verification: `x-litellm-applied-guardrails` response header,
  `requests_compressed` in `docker compose logs headroom`,
  guardrail_information in spend logs / Admin UI Logs panel.
- The `HEADROOM_COMPRESS_USER_MESSAGES=1` note (why requests_compressed
  would otherwise be 0 for Open WebUI traffic).

---

## 3. Verification (on the Photon VM, /mnt/data1/openwebui-stack)

The stack runs on VMware Photon OS, not the Mac; the deployed tree is
`/mnt/data1/openwebui-stack`. SSH user/host live in
`ansible/inventory/hosts.local.yml`. See `.magie-coder/skills/photon-vm/SKILL.md`.

1. From the control machine: `cd ansible && ansible-galaxy collection install
   -r requirements.yaml && ansible-playbook playbooks/site.yml` — this
   renders config.yaml + docker-compose.yml and restarts affected services.
2. `docker compose build headroom && docker compose up -d headroom litellm`.
3. Confirm LiteLLM version: `docker compose exec litellm litellm --version`
   — must be >= 1.92.0.
4. Confirm the guardrail loaded: `docker compose logs litellm` (look for
   headroom guardrail init; config errors fail fast).
5. Send a test chat request through the proxy (Open WebUI-style, master key
   from .env `LITELLM_MASTER_KEY`) and check for the
   `x-litellm-applied-guardrails: headroom-compression` response header:

   ```bash
   KEY=$(grep -m1 "^LITELLM_MASTER_KEY=" .env | cut -d= -f2-)
   curl -sk https://litellm.localhost/v1/chat/completions \
     -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
     -d '{"model": "gemini-3.8-flash", "messages": [{"role": "user", "content": "Summarize the prior conversation about the deploy pipeline..."}]}' \
     -i | grep -i x-litellm-applied-guardrails
   ```

6. Check `docker compose logs headroom` for `requests_compressed > 0`.
   If it stays 0 for Open WebUI traffic, HEADROOM_COMPRESS_USER_MESSAGES
   is not set on the container (this is the documented failure mode).
7. Oversized-history interaction check: drive a chat long enough (or send a
   synthetic oversized payload at a small-window model) that the
   context-compaction callback would fire, and confirm the request still
   succeeds — i.e., trim + compress compose without conflict, in whatever
   order LiteLLM applies them.
8. Bypass check: same curl with `-H "x-headroom-bypass: true"` and confirm
   the header shows the guardrail did not apply.
9. Spend log: `docker compose exec postgres psql -U litellm -d litellm` and
   inspect guardrail_information on a recent LiteLLM_SpendLogs row, or use
   the Admin UI Logs panel.
10. Optional: verify voice-assistant traffic (the
    `voice-assistant -> gemini-3.5-flash-lite` alias) still works — it now
    carries one extra pre-call hop (~tens of ms). Acceptable; if it ever
    matters, the bypass header is the escape hatch.

Lint/validate commands per AGENTS.md: `docker compose config --quiet` (after
render), `shellcheck scripts/*.sh` (no new scripts in this plan).

---

## 4. Risks / open items

- **No official image** — we build the docs' Dockerfile ourselves. The
  `headroom-ai` package version should be checked at implementation time.
- **Sidecar availability**: with `default_on: true`, every request makes a
  pre-call HTTP hop to headroom; if the sidecar is down, LiteLLM requests
  may fail or bypass depending on guardrail failure semantics — verify the
  failure mode once (stop headroom, send a request, observe) and document
  the observed behavior in the README section.
- **`main-latest` LiteLLM image**: if it ever regressed below 1.92.0 the
  headroom guardrail would fail to load (config error at startup — visible,
  not silent). Pinning `LITELLM_IMAGE` to a known-good version is a
  separate, general improvement.
- **Auto-router hops**: if compression appears missing on `auto`/`adaptive`
  model traffic, check the auto-router compression fields described in
  section 1.
- **Anthropic prompt caching**: safe — headroom never touches
  cache_control-marked messages. No config change needed.
- **Ordering vs. the context-compaction callback**: safe either way (section
  1); the verification step 7 is the live confirmation.

## 5. Execution checklist

- [ ] headroom/Dockerfile created
- [ ] ansible/group_vars/all/vars.yml: headroom_enabled added
- [ ] docker-compose.yml.j2: headroom service (toggle-wrapped) + litellm depends_on entry
- [ ] config.yaml.j2: headroom-compression guardrail entry (toggle-wrapped)
- [ ] litellm/config.yaml (repo working copy): same entry added for consistency
- [ ] tasks/sync.yml: headroom added to loop
- [ ] .env.example: optional HEADROOM_IMAGE comment
- [ ] README.md: Prompt Compression (Headroom) section
- [ ] Photon verification steps 1-8 executed
- [ ] README updated with the observed sidecar-down failure mode
