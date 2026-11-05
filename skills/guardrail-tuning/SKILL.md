---
name: guardrail-tuning
description: >-
  Tune the LiteLLM content-filter guardrails in the openwebui-stack: edit
  the category override files in litellm/guardrail_categories/*.yaml to
  stop false blocks while keeping always-block payloads, adjust severity
  thresholds and actions in litellm config, and verify changes after
  deploy. Use when a legitimate request got blocked or guardrail behavior
  needs changing.
license: MIT
---

# Guardrail Tuning (LiteLLM content filter)

The stack runs LiteLLM's built-in content filter in `pre_call` +
`post_call`, `default_on`, with `skip_system_message_in_guardrail: true`
and `skip_tool_message_in_guardrail: true`. Categories with local
overrides live in `litellm/guardrail_categories/` and are mounted at
`/app/guardrail_categories/` inside the litellm container.

## Architecture

- Config: `guardrails:` block in `litellm/config.yaml` (rendered from
  `ansible/roles/openwebui_stack/templates/config.yaml.j2`).
- Override mechanism: a category entry's `category_file:` points at a
  YAML file under `/app/guardrail_categories/<category>.yaml`, replacing
  the built-in conditional match (roleplay/"enable"/"show" style
  word-pairs) while keeping the always-block payload list.
- The mount requires `LITELLM_CONTENT_FILTER_ALLOW_EXTERNAL_PATHS:
  "true"` (already set in the compose template).
- `severity_threshold` per category: `medium` or `high`.

## Tuning procedure

1. **Reproduce.** Find the exact blocked request in LiteLLM logs
   (`docker compose logs litellm` or the LiteLLM UI Logs tab) and note
   the category and matched pattern.
2. **Decide the class of fix.**
   - False block from a *payload* entry → remove/trim that entry in the
     category file.
   - False block from a *conditional match* → confirm the local override
     is actually mounted (see checklist below); the built-in conditional
     matcher is the usual culprit ("show"/"list" + "system prompt",
     "run"/"process" + "subprocess", "export"/"collect" + "user data").
   - Legitimate content flagged → raise `severity_threshold` (medium →
     high) or, for masking categories, keep action `MASK` so the model
     still answers.
3. **Edit the checkout**, not the deployed tree: the file is rsynced by
   the playbook (`sync.yml` copies `litellm/` minus the rendered
   `config.yaml`).
4. **Deploy and verify**:
   ```bash
   cd /mnt/data1/openwebui-stack
   ansible-playbook playbooks/site.yml --tags environment,compose   # from ansible/
   docker compose up -d litellm
   docker compose logs litellm | grep -i "content filter\|guardrail"
   ```
   Then replay the original request and confirm it passes.

## Checklist when a category override stops working

- The compose file mounts the category file (see
  `templates/docker-compose.yml.j2` litellm volumes) — a new file needs a
  new mount line.
- The config's `category_file:` path matches the container path
  (`/app/guardrail_categories/...`).
- Before `docker compose up`, the mount-source check passes: a missing
  file-kind bind source makes `docker compose` create an empty directory
  and the guardrail silently loads nothing — the playbook fails the run
  and names the exact `rm -rf` to run.

## Non-negotiables

1. Never remove the secret/credential patterns (`aws_access_key`,
   `github_token`, `us_ssn`) or their `BLOCK` actions.
2. Keep `prompt_injection_*` categories enabled with the always-block
   payload lists — tuning means trimming conditional false-positives, not
   disabling injection defense.
3. Harmful categories (`harmful_*`) have no local override file; their
   behavior changes via `enabled`, `action`, and `severity_threshold` in
   the config template only.
4. Changes must be idempotent: same files, same mounts, rerunnable.
