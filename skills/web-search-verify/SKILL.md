---
name: web-search-verify
description: >-
  Verify end-to-end web search through the openwebui-stack: confirm the
  brave-search-mcp container is healthy, the LiteLLM startup hook loaded,
  non-streaming chat requests carry the injected MCP tool, streaming
  chats use the Open WebUI client-side tool server, and the model answers
  with current web results. Use when web search in chat seems broken or
  after changing the Brave MCP wiring.
license: MIT
---

# Web Search Verification (Brave Search MCP)

Pieces: `brave-search-mcp` container (needs `BRAVE_SEARCH_API_KEY`),
`mcp_servers.brave_search` in `litellm/config.yaml`, the
`BraveMCPInjector` in `litellm/brave_mcp_callback.py` loaded via
`LITELLM_WORKER_STARTUP_HOOKS`, and per-model `meta.toolIds:
["server:mcp:brave_search"]` in Open WebUI for streaming chats.

## Verify sequence (stack host)

```bash
cd /mnt/data1/openwebui-stack

# 1. Container healthy
docker compose ps brave-search-mcp

# 2. Startup hook loaded
docker compose logs litellm | grep "startup hook"

# 3. A plain non-streaming chat request got the injected mcp tool
docker compose exec postgres psql -U litellm -d litellm -c \
  "SELECT request_id, jsonb_array_length(proxy_server_request->'tools') AS ntools,
          proxy_server_request->'tools'->0->>'type' AS t0
   FROM \"LiteLLM_SpendLogs\" ORDER BY \"startTime\" DESC LIMIT 3;"
# Expect ntools >= 1 and t0 = mcp (on non-streaming requests).

# 4. End-to-end: the model answers with current web results
KEY=$(grep -m1 "^LITELLM_MASTER_KEY=" .env | cut -d= -f2-)
curl -sk https://litellm.localhost/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model": "gemini-3.5-flash-lite",
       "messages": [{"role": "user", "content": "Search the web for today top tech news"}]}'
# Any served chat model works. On an Anthropic-only stack use
# "claude-haiku-5-5"; web search does not need a Gemini key.
```

Streaming chats are verified from the Open WebUI UI: send a streaming
message that needs current facts and confirm the response cites fresh
results (the frontend merges the tool server into the request via the
model's `meta.toolIds`).

## Failure isolation

| Check fails | Likely cause | Fix |
|---|---|---|
| Container down / restarts | Missing `BRAVE_SEARCH_API_KEY` | Set it in vault.local.yml, rerun playbook (`brave_search_enabled: true`) |
| No "startup hook" line in logs | `LITELLM_WORKER_STARTUP_HOOKS` env dropped or callback file not mounted | Rerun playbook; confirm the `brave_mcp_callback.py` mount in compose |
| ntools = 0 on non-streaming requests | Injector did not fire | The hook must match `call_type` of both `"completion"` and `"acompletion"`; it must skip `stream: true` |
| "Tool not found" tool responses in history | Injection ran on a streaming request | Never inject on stream: LiteLLM forwards tool-call deltas the client cannot resolve; the next Gemini turn then fails with "Missing corresponding tool call for tool response" |
| Streaming chat has no search | Model entry missing `meta.toolIds` | Rerun playbook (`tool_defaults.yml` wires `server:mcp:brave_search` as a global default); recreate open-webui |
| 401/403 from brave-search-mcp | Bad API key | Rotate the key in vault.local.yml, rerun |
| Non-streaming answer is empty with `tool_calls` named `brave_search-...` | The model asked for a second search and the injector's further rounds did not run | `docker compose logs litellm \| grep "Brave MCP"`: "tool round N of M" means they run; "could not run a further tool round" plus a traceback means a LiteLLM internal used by `brave_mcp_callback.py` changed. `BRAVE_SEARCH_MAX_EXTRA_ROUNDS=0` also turns them off |
| Claude 400: "`tool_use` ids were found without `tool_result` blocks" or "unexpected `tool_use_id`" | History has a tool call with no result (or a result with no call) and LiteLLM did not repair it | `litellm_settings.modify_params` must be `true` in `litellm/config.yaml`; rerun playbook |
| Claude 400 on `tool_choice` (Opus, Sonnet, Fable) | Client forced a tool call and LiteLLM did not downgrade it to `auto` | `litellm_settings.drop_params` must be `true`; rerun playbook |

## Non-negotiables

1. The injector skips `stream: true` — this is required behavior, not a
   bug to fix. Non-streaming requests get up to 1 + 3 search rounds: the
   LiteLLM gateway runs the first, the injector's agentic-loop hooks the
   rest, and the last one must answer in text.
2. Built-in web search is off (`web.search.enable` false in Open WebUI);
   the MCP path is the default search path.
3. After any config change: `docker compose up -d litellm` and re-run
   checks 2–4 in order — a stale log line proves nothing.
