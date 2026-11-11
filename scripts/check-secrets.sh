#!/bin/bash
# Strict secret scan of the git history. The Secret Scan workflow
# (.github/workflows/secret-scan.yml) runs it, and a local run checks the
# same things.
#
# Usage:
#   scripts/check-secrets.sh [--all-refs] [--no-verify] [command...]
#
# Commands (default: all):
#   install     download the pinned gitleaks and trufflehog, sha256-checked
#   gitleaks    gitleaks over the history, rules from .gitleaks.toml
#   trufflehog  trufflehog over the history, every detector but JDBC
#   policy      fail on tracked secret files and inline allow comments
#   all         gitleaks, trufflehog and policy
#
# Options:
#   --all-refs   scan every branch and tag instead of the history of HEAD
#                (the weekly run; SECRET_SCAN_ALL_REFS=true does the same)
#   --no-verify  keep trufflehog from checking found keys with their
#                providers; use it on hosts that can reach internal services
#
# Any finding fails, verified or not. Output names the rule or detector,
# commit, file and line, never the value: gitleaks runs with --redact, and
# trufflehog's JSON is reduced to those fields. The scanners live in
# $SECRET_SCAN_BIN (default: $RUNNER_TEMP/secret-scanners in GitHub Actions,
# ~/.cache/openwebui-stack/secret-scanners elsewhere). install needs Linux
# x86_64, curl and sha256sum; the scans need git and jq.
#
# Exits 0 when clean, 1 on findings, 2 on usage or tool errors.
set -euo pipefail

# Pinned scanners. To update one, change its version and the sha256 of its
# linux x64/amd64 tarball (from the release's checksums file) together.
readonly GITLEAKS_VERSION="8.30.1"
readonly GITLEAKS_SHA256="551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb"
readonly TRUFFLEHOG_VERSION="3.99.7"
readonly TRUFFLEHOG_SHA256="bdbea0508e439b144f49de14e775908de5f902f3d14ea7ae8354bae72b0b72ef"

# Files that hold secrets by nature: rendered env files, local vault and
# inventory overrides, the debugging token file, keys and certificates,
# database dumps, credential stores. .gitignore keeps them out; this catches
# git add -f and a weakened .gitignore.
readonly FORBIDDEN_PATHS=(
    '\.env$'
    '(^|/)\.env\.[^/]+$'
    '\.local\.ya?ml$'
    '(^|/)token\.txt$'
    '\.(pem|key|crt|cer|der|p12|pfx|jks|keystore|kdbx|ppk)$'
    '(^|/)id_(rsa|dsa|ecdsa|ed25519)$'
    '(^|/)(certs|backups)/'
    '\.(sql|dump)(\.gz)?$'
    '(^|/)\.?(netrc|git-credentials|pypirc|npmrc|htpasswd)$'
    '(^|/)\.docker/config\.json$'
    '(^|/)\.?vault[-_]pass(word)?(\.txt)?$'
)

# Tracked on purpose: examples carry placeholders, and the Cloudflare Origin
# CA root is a public certificate the opencode role installs.
readonly ALLOWED_PATHS=(
    '\.example$'
    '^ansible/roles/opencode/files/origin_ca_rsa_root\.pem$'
)

# Inline allow comments would hide a finding from review. Built from two
# strings so this file does not match itself.
readonly ALLOW_COMMENT_RE="gitleaks"":allow|trufflehog"":ignore|pragma: ?allowlist"

REPO_ROOT="$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)"
readonly REPO_ROOT

if [[ -n ${SECRET_SCAN_BIN:-} ]]; then
    BIN_DIR="$SECRET_SCAN_BIN"
elif [[ -n ${RUNNER_TEMP:-} ]]; then
    BIN_DIR="$RUNNER_TEMP/secret-scanners"
else
    BIN_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/openwebui-stack/secret-scanners"
fi
readonly BIN_DIR

WORK_DIR="$(mktemp -d)"
readonly WORK_DIR
trap 'rm -rf "$WORK_DIR"' EXIT

usage() {
    echo "usage: $0 [--all-refs] [--no-verify] [install|gitleaks|trufflehog|policy|all]..." >&2
    echo "(see the comment at the top of the script)" >&2
    exit 2
}

ALL_REFS=false
if [[ ${SECRET_SCAN_ALL_REFS:-false} == "true" ]]; then
    ALL_REFS=true
fi
VERIFY=true
COMMANDS=()
while [[ $# -gt 0 ]]; do
    case "$1" in
    --all-refs) ALL_REFS=true ;;
    --no-verify) VERIFY=false ;;
    install | gitleaks | trufflehog | policy | all) COMMANDS+=("$1") ;;
    *) usage ;;
    esac
    shift
done
if [[ ${#COMMANDS[@]} -eq 0 ]]; then
    COMMANDS=(all)
fi
readonly ALL_REFS VERIFY

# The functions below run with errexit suspended (they are called as
# `cmd || ...`), so each checks its own failures: 0 clean, 1 findings,
# 2 errors.

need() {
    if ! command -v "$1" >/dev/null 2>&1; then
        echo "error: $1 is required" >&2
        return 2
    fi
}

tool() {
    local name="$1"
    if [[ -x "$BIN_DIR/$name" ]]; then
        printf '%s\n' "$BIN_DIR/$name"
    elif command -v "$name" >/dev/null 2>&1; then
        command -v "$name"
    else
        echo "error: $name is not in $BIN_DIR or on PATH; run: $0 install" >&2
        return 2
    fi
}

scope() {
    if [[ $ALL_REFS == true ]]; then
        echo "every branch and tag"
    else
        echo "the history of HEAD ($(git -C "$REPO_ROOT" rev-parse --short HEAD))"
    fi
}

# Escaping for GitHub workflow commands.
escape_data() {
    local value="$1"
    value="${value//'%'/%25}"
    value="${value//$'\r'/%0D}"
    value="${value//$'\n'/%0A}"
    printf '%s' "$value"
}

escape_property() {
    local value
    value="$(escape_data "$1")"
    value="${value//:/%3A}"
    value="${value//,/%2C}"
    printf '%s' "$value"
}

# finding <file> <line> <title> <message>: one line on stdout, plus an error
# annotation in GitHub Actions. Never pass a secret value in here.
finding() {
    local file="$1" line="$2" title="$3" message="$4"
    printf '  %s:%s  %s\n' "$file" "$line" "$message"
    if [[ ${GITHUB_ACTIONS:-} == "true" ]]; then
        printf '::error file=%s,line=%s,title=%s::%s\n' \
            "$(escape_property "$file")" "$(escape_property "$line")" \
            "$(escape_property "$title")" "$(escape_data "$message")"
    fi
}

fetch_scanner() {
    local name="$1" sha256="$2" url="$3"
    local stamp="$BIN_DIR/$name.sha256" tarball="$WORK_DIR/$name.tar.gz"
    if [[ -x "$BIN_DIR/$name" && "$(cat "$stamp" 2>/dev/null)" == "$sha256" ]]; then
        echo "$name: already installed in $BIN_DIR"
        return 0
    fi
    if ! curl -fsSL --retry 3 --max-time 600 -o "$tarball" "$url"; then
        echo "error: downloading $url failed" >&2
        return 2
    fi
    if ! echo "$sha256  $tarball" | sha256sum -c --quiet -; then
        echo "error: $url does not match the sha256 pinned in $0" >&2
        return 2
    fi
    if ! tar -xzf "$tarball" -C "$WORK_DIR" "$name" ||
        ! install -m 0755 "$WORK_DIR/$name" "$BIN_DIR/$name"; then
        echo "error: unpacking $name failed" >&2
        return 2
    fi
    printf '%s\n' "$sha256" >"$stamp"
    echo "$name: installed from $url"
}

install_scanners() {
    local os arch
    os="$(uname -s)"
    arch="$(uname -m)"
    if [[ $os != "Linux" || $arch != "x86_64" ]]; then
        echo "error: install fetches Linux x86_64 builds only ($os $arch here);" \
            "put gitleaks $GITLEAKS_VERSION and trufflehog $TRUFFLEHOG_VERSION on PATH instead" >&2
        return 2
    fi
    need curl || return 2
    need sha256sum || return 2
    mkdir -p "$BIN_DIR" || return 2
    fetch_scanner gitleaks "$GITLEAKS_SHA256" \
        "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz" ||
        return 2
    fetch_scanner trufflehog "$TRUFFLEHOG_SHA256" \
        "https://github.com/trufflesecurity/trufflehog/releases/download/v${TRUFFLEHOG_VERSION}/trufflehog_${TRUFFLEHOG_VERSION}_linux_amd64.tar.gz" ||
        return 2
}

scan_gitleaks() {
    local gitleaks report log_opts count result=0
    local rule commit file line fingerprint
    gitleaks="$(tool gitleaks)" || return 2
    need jq || return 2
    report="$WORK_DIR/gitleaks.json"
    log_opts="HEAD"
    if [[ $ALL_REFS == true ]]; then
        log_opts="--all"
    fi
    echo "gitleaks $("$gitleaks" version): $(scope)"
    "$gitleaks" git "$REPO_ROOT" \
        --config="$REPO_ROOT/.gitleaks.toml" \
        --gitleaks-ignore-path="$REPO_ROOT" \
        --log-opts="$log_opts" \
        --ignore-gitleaks-allow \
        --redact \
        --max-archive-depth=5 \
        --no-banner \
        --log-level=warn \
        --report-format=json \
        --report-path="$report" \
        --exit-code=3 || result=$?
    if [[ $result -ne 0 && $result -ne 3 ]]; then
        echo "error: gitleaks failed (exit $result)" >&2
        return 2
    fi
    count="$(jq 'length' "$report")" || return 2
    if [[ $count -eq 0 ]]; then
        echo "gitleaks: no findings"
        return 0
    fi
    echo "gitleaks: $count finding(s), values redacted:"
    while IFS=$'\t' read -r rule commit file line fingerprint; do
        finding "$file" "$line" "gitleaks: $rule" \
            "$rule in commit ${commit:0:12} (fingerprint $fingerprint)"
    done < <(jq -r '.[] | [.RuleID, .Commit, .File, (.StartLine | tostring), .Fingerprint] | @tsv' "$report")
    return 1
}

scan_trufflehog() {
    local trufflehog results log count result=0
    local detector status commit file line
    local args=()
    trufflehog="$(tool trufflehog)" || return 2
    need jq || return 2
    results="$WORK_DIR/trufflehog.jsonl"
    log="$WORK_DIR/trufflehog.log"
    # JDBC is off: it flags the compose template's credential-free
    # jdbc:postgresql://postgres:5432/... URL. The url-credentials and
    # connection-string-password rules in .gitleaks.toml cover passwords in
    # connection strings instead.
    args=(git "file://$REPO_ROOT" --no-update --fail --json
        "--results=verified,unknown,unverified" --exclude-detectors=JDBC)
    if [[ $ALL_REFS != true ]]; then
        args+=(--branch="$(git -C "$REPO_ROOT" rev-parse HEAD)")
    fi
    if [[ $VERIFY != true ]]; then
        args+=(--no-verification)
    fi
    echo "$("$trufflehog" --version 2>&1): $(scope)"
    "$trufflehog" "${args[@]}" >"$results" 2>"$log" || result=$?
    if [[ $result -ne 0 && $result -ne 183 ]]; then
        echo "error: trufflehog failed (exit $result):" >&2
        tail -n 5 "$log" >&2
        return 2
    fi
    count="$(grep -c '^{' "$results" || true)"
    if [[ $count -eq 0 && $result -eq 0 ]]; then
        echo "trufflehog: no findings"
        return 0
    fi
    echo "trufflehog: $count finding(s), values not shown:"
    while IFS=$'\t' read -r detector status commit file line; do
        finding "$file" "$line" "trufflehog: $detector" \
            "$detector ($status) in commit ${commit:0:12}"
    done < <(jq -r 'select(.DetectorName != null) | [
            .DetectorName,
            (if .Verified then "verified live"
             elif (.VerificationError // "") != "" then "unknown"
             else "unverified" end),
            .SourceMetadata.Data.Git.commit,
            .SourceMetadata.Data.Git.file,
            (.SourceMetadata.Data.Git.line | tostring)
        ] | @tsv' "$results")
    return 1
}

check_policy() {
    local path re allowed location count=0
    echo "policy: tracked files ($(git -C "$REPO_ROOT" rev-parse --short HEAD) plus the index)"
    while IFS= read -r -d '' path; do
        allowed=false
        for re in "${ALLOWED_PATHS[@]}"; do
            if [[ $path =~ $re ]]; then
                allowed=true
                break
            fi
        done
        if [[ $allowed == true ]]; then
            continue
        fi
        for re in "${FORBIDDEN_PATHS[@]}"; do
            if [[ $path =~ $re ]]; then
                finding "$path" 1 "policy: secret file tracked" \
                    "holds secrets by nature; git rm --cached it, purge it from the history, rotate what it held"
                count=$((count + 1))
                break
            fi
        done
    done < <(git -C "$REPO_ROOT" ls-files -z)
    while IFS= read -r location; do
        finding "${location%%:*}" "${location#*:}" "policy: inline allow comment" \
            "inline allow comment; accept a finding by its fingerprint in .gitleaksignore instead"
        count=$((count + 1))
    done < <(git -C "$REPO_ROOT" grep -nIE "$ALLOW_COMMENT_RE" -- . | cut -d: -f1,2 || true)
    if [[ $count -eq 0 ]]; then
        echo "policy: no findings"
        return 0
    fi
    echo "policy: $count finding(s)"
    return 1
}

STATUS=0
run() {
    local result=0
    "$@" || result=$?
    if [[ $result -gt $STATUS ]]; then
        STATUS="$result"
    fi
}

for command in "${COMMANDS[@]}"; do
    case "$command" in
    install) run install_scanners ;;
    gitleaks) run scan_gitleaks ;;
    trufflehog) run scan_trufflehog ;;
    policy) run check_policy ;;
    all)
        run scan_gitleaks
        run scan_trufflehog
        run check_policy
        ;;
    esac
done
exit "$STATUS"
