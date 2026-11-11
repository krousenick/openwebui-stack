#!/bin/bash
# Compare the pinned guardrail category overrides in
# litellm/guardrail_categories/ against the same category files shipped in
# upstream LiteLLM, and fail when upstream has detection data the overrides
# dropped. The overrides intentionally removed the built-in conditional
# matches (identifier + block word pairs that false-blocked ordinary
# questions about SQL, Python, and prompt design), but they must keep every
# always-block keyword upstream ships so real payloads still block. Upstream
# adds keywords over time; this check surfaces that drift so the override
# files can be refreshed deliberately.
#
# Usage:
#   scripts/check-guardrail-drift.sh [version]
#
#   version  LiteLLM sdist version to compare against. Defaults to the
#            latest release on PyPI. Pin it in CI to keep runs
#            reproducible.
#
# Exits 0 when every override covers upstream's always-block keywords, 1
# on drift, 2 on usage/availability errors.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
OVERRIDES_DIR="${SCRIPT_DIR}/../litellm/guardrail_categories"
readonly OVERRIDES_DIR
WORK_DIR="$(mktemp -d)"
readonly WORK_DIR

trap 'rm -rf "${WORK_DIR}"' EXIT

# Categories the stack pins with category_file overrides.
readonly PINNED_CATEGORIES=(
    prompt_injection_sql
    prompt_injection_jailbreak
    prompt_injection_system_prompt
    prompt_injection_malicious_code
    prompt_injection_data_exfiltration
)

usage() {
    echo "usage: $0 [litellm-version]" >&2
    exit 2
}

cleanup_on_error() {
    echo "error: $1" >&2
    exit 2
}

[[ $# -le 1 ]] || usage

# ---------------------------------------------------------------------------
# Resolve the LiteLLM version to check against.
# ---------------------------------------------------------------------------
if [[ $# -eq 1 ]]; then
    LITELLM_VERSION="${1}"
else
    LITELLM_VERSION="$(curl -fsSL https://pypi.org/pypi/litellm/json |
        python3 -c 'import json,sys; print(json.load(sys.stdin)["info"]["version"])' ||
        cleanup_on_error "could not resolve the latest LiteLLM version from PyPI")"
fi
readonly LITELLM_VERSION

echo "Comparing guardrail overrides against LiteLLM ${LITELLM_VERSION}"

# ---------------------------------------------------------------------------
# Download the LiteLLM sdist and extract only the overridden category files.
# ---------------------------------------------------------------------------
readonly SDIST_URL="https://files.pythonhosted.org/packages/source/l/litellm/litellm-${LITELLM_VERSION}.tar.gz"
readonly SDIST_PATH="${WORK_DIR}/litellm.tar.gz"

curl -fsSL "${SDIST_URL}" -o "${SDIST_PATH}" ||
    cleanup_on_error "could not download ${SDIST_URL}"

EXTRACT_ARGS=()
for category in "${PINNED_CATEGORIES[@]}"; do
    EXTRACT_ARGS+=("litellm-${LITELLM_VERSION}/litellm/proxy/guardrails/content_filter_data/categories/${category}.yaml")
done

tar -xzf "${SDIST_PATH}" -C "${WORK_DIR}" "${EXTRACT_ARGS[@]}" ||
    cleanup_on_error "sdist ${LITELLM_VERSION} does not contain the guardrail category files"

readonly UPSTREAM_DIR="${WORK_DIR}/litellm-${LITELLM_VERSION}/litellm/proxy/guardrails/content_filter_data/categories"

# ---------------------------------------------------------------------------
# Compare each override's always-block coverage against upstream. The
# comparison itself is Python (PyYAML is the same parser LiteLLM uses);
# it prints one keyword per line that upstream has and the override lacks.
# ---------------------------------------------------------------------------
readonly DRIFT_PY="${WORK_DIR}/drift.py"
cat >"${DRIFT_PY}" <<'PYEOF'
import sys

import yaml


def load_keywords(path):
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    keywords = set()
    for entry in data.get("always_block_keywords") or []:
        if isinstance(entry, str):
            keywords.add(entry.lower())
        else:
            keywords.add(entry["keyword"].lower())
    return keywords


upstream = load_keywords(sys.argv[1])
override = load_keywords(sys.argv[2])
for keyword in sorted(upstream - override):
    print(keyword)
PYEOF

drift=0

for category in "${PINNED_CATEGORIES[@]}"; do
    override_file="${OVERRIDES_DIR}/${category}.yaml"
    upstream_file="${UPSTREAM_DIR}/${category}.yaml"

    if [[ ! -f ${override_file} ]]; then
        echo "MISSING override: ${override_file}"
        drift=1
        continue
    fi

    missing="$(python3 "${DRIFT_PY}" "${upstream_file}" "${override_file}")"

    if [[ -n ${missing} ]]; then
        echo "DRIFT in ${category}: upstream keywords missing from the override:"
        while IFS= read -r keyword; do
            echo "    ${keyword}"
        done <<<"${missing}"
        drift=1
    else
        echo "OK ${category}: override covers all upstream always-block keywords"
    fi
done

if [[ ${drift} -ne 0 ]]; then
    echo "Guardrail drift detected. Port the new upstream keywords into the"
    echo "override files in litellm/guardrail_categories/ (keeping the"
    echo "conditional matches removed), then re-run this check."
    exit 1
fi

echo "No drift: all overrides cover the upstream always-block keywords."
