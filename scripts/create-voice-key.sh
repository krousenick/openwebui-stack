#!/bin/bash
# Create or reuse the LiteLLM virtual key for the voice assistant.
# The key is stored in .env as LITELLM_VOICE_API_KEY and is not printed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
ENV_FILE="${ENV_FILE:-$PROJECT_ROOT/.env}"
KEY_ALIAS="${VOICE_KEY_ALIAS:-voice-assistant}"
VOICE_MODEL="${VOICE_KEY_MODEL:-gemini-3.5-flash-lite}"
LITELLM_URL="${LITELLM_URL:-http://127.0.0.1:4000}"

die() {
    printf 'ERROR: %s\n' "$1" >&2
    exit 1
}

env_value() {
    local key="$1"
    local line

    [[ -f "$ENV_FILE" ]] || die ".env not found at $ENV_FILE"
    line="$(grep -E "^${key}=" "$ENV_FILE" | tail -n 1 || true)"
    [[ -n "$line" ]] || die "$key is not set in $ENV_FILE"
    printf '%s' "${line#*=}"
}

upsert_env() {
    local key="$1"
    local value="$2"
    local tmp

    tmp="$(mktemp)"
    if grep -qE "^${key}=" "$ENV_FILE"; then
        awk -v key="$key" -v value="$value" '
            BEGIN { done = 0 }
            $0 ~ ("^" key "=") && done == 0 { print key "=" value; done = 1; next }
            { print }
        ' "$ENV_FILE" >"$tmp"
    else
        cat "$ENV_FILE" >"$tmp"
        printf '\n%s=%s\n' "$key" "$value" >>"$tmp"
    fi
    mv "$tmp" "$ENV_FILE"
}

command -v curl >/dev/null 2>&1 || die "curl is required"
command -v python3 >/dev/null 2>&1 || die "python3 is required"

MASTER_KEY="$(env_value LITELLM_MASTER_KEY)"
[[ -n "$MASTER_KEY" && "$MASTER_KEY" != changeme_* ]] || die "LITELLM_MASTER_KEY is missing or still a placeholder"

EXISTING_KEY="$( (grep -E '^LITELLM_VOICE_API_KEY=' "$ENV_FILE" || true) | tail -n 1)"
EXISTING_KEY="${EXISTING_KEY#LITELLM_VOICE_API_KEY=}"
if [[ -n "$EXISTING_KEY" && "$EXISTING_KEY" != changeme_* ]]; then
    if ! INFO_CODE="$(curl -s -o /tmp/voice-key-info.json -w '%{http_code}' \
        -H "Authorization: Bearer ${MASTER_KEY}" \
        --get "${LITELLM_URL}/key/info" \
        --data-urlencode "key=${EXISTING_KEY}")"; then
        rm -f /tmp/voice-key-info.json
        die "Could not reach LiteLLM at ${LITELLM_URL}"
    fi
    if [[ "$INFO_CODE" == "200" ]]; then
        rm -f /tmp/voice-key-info.json
        printf 'Voice key already exists in %s. Nothing changed.\n' "$ENV_FILE"
        exit 0
    fi
    rm -f /tmp/voice-key-info.json
fi

if ! LIST_CODE="$(curl -s -o /tmp/voice-key-list.json -w '%{http_code}' \
    -H "Authorization: Bearer ${MASTER_KEY}" \
    "${LITELLM_URL}/key/list?return_full_object=true&size=100")"; then
    rm -f /tmp/voice-key-list.json
    die "Could not reach LiteLLM at ${LITELLM_URL}"
fi
if [[ "$LIST_CODE" == "200" ]]; then
    ALIAS_STATE="$(KEY_ALIAS="$KEY_ALIAS" python3 -c '
import json
import os

payload = json.load(open("/tmp/voice-key-list.json"))
keys = payload.get("keys", payload if isinstance(payload, list) else [])
alias = os.environ["KEY_ALIAS"]
found = any(isinstance(item, dict) and item.get("key_alias") == alias for item in keys)
total = payload.get("total_count", len(keys) if isinstance(keys, list) else 0)
complete = isinstance(keys, list) and total <= len(keys)
if found:
    print("taken")
elif complete:
    print("free")
else:
    print("unknown")
')"
    rm -f /tmp/voice-key-list.json
    if [[ "$ALIAS_STATE" == "taken" ]]; then
        die "LiteLLM already has key alias ${KEY_ALIAS}, but ${ENV_FILE} has no working LITELLM_VOICE_API_KEY. Delete that key in LiteLLM, then rerun."
    fi
    if [[ "$ALIAS_STATE" == "unknown" ]]; then
        die "Could not confirm key alias ${KEY_ALIAS} is unused. Refusing to create a second key."
    fi
else
    rm -f /tmp/voice-key-list.json
fi

PAYLOAD="$(KEY_ALIAS="$KEY_ALIAS" VOICE_MODEL="$VOICE_MODEL" python3 -c '
import json
import os

print(json.dumps({
    "key_alias": os.environ["KEY_ALIAS"],
    "models": [os.environ["VOICE_MODEL"]],
    "key_type": "llm_api",
    "metadata": {"purpose": "voice-assistant"},
}))
')"

if ! HTTP_CODE="$(curl -s -o /tmp/voice-key-generate.json -w '%{http_code}' \
    -X POST "${LITELLM_URL}/key/generate" \
    -H "Authorization: Bearer ${MASTER_KEY}" \
    -H "Content-Type: application/json" \
    -d "$PAYLOAD")"; then
    rm -f /tmp/voice-key-generate.json
    die "Could not reach LiteLLM at ${LITELLM_URL}"
fi

if [[ "$HTTP_CODE" != "200" ]]; then
    python3 -c 'import pathlib; print(pathlib.Path("/tmp/voice-key-generate.json").read_text()[:500])' >&2 || true
    die "LiteLLM /key/generate returned HTTP ${HTTP_CODE}. Is the proxy healthy at ${LITELLM_URL}?"
fi

NEW_KEY="$(python3 -c 'import json; print(json.load(open("/tmp/voice-key-generate.json")).get("key", ""))')"
rm -f /tmp/voice-key-info.json /tmp/voice-key-generate.json
[[ -n "$NEW_KEY" ]] || die "LiteLLM did not return a key"

upsert_env "LITELLM_VOICE_API_KEY" "$NEW_KEY"
printf 'Wrote LITELLM_VOICE_API_KEY to %s. Alias %s allows only %s.\n' "$ENV_FILE" "$KEY_ALIAS" "$VOICE_MODEL"
