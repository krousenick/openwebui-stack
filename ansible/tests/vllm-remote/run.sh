#!/bin/bash
# Runs the vllm_compute role against the remote GPU node from the photon
# controller. The node address, bastion address, SSH user/key, and deployed
# tree are environment-driven; copy run.local.env.example to
# run.local.env (gitignored) and fill in the real values.
#
# The controller (the stack host) is the only machine with SSH access to
# the node, so ansible runs there. This script syncs the ansible tree from
# this Mac to the controller, then runs the playbook inside the controller
# over SSH (from an OrbStack VM that holds the bastion key; the Mac itself
# does not).
#
# Usage:
#   run.sh [sync|inference|check|eval|all] [ansible-playbook args...]
#     sync      - copy the ansible/litellm/otel/grafana trees to the controller
#     inference - run playbooks/inference.yml against the remote node
#     check     - rerun inference and require zero changes (idempotency)
#     eval      - run playbooks/eval.yml (reproducible model eval suite)
set -euo pipefail

RIG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Local, gitignored values (addresses, users, key paths). The example file
# documents each key. Everything also overridable via the environment.
LOCAL_ENV="$RIG_DIR/run.local.env"
if [[ -f "$LOCAL_ENV" ]]; then
	# shellcheck disable=SC1090
	source "$LOCAL_ENV"
fi

: "${VLLM_RUN_CONTROLLER_HOST:?Set VLLM_RUN_CONTROLLER_HOST (or create $(basename "$LOCAL_ENV") from run.local.env.example)}"
: "${VLLM_RUN_CONTROLLER_USER:?Set VLLM_RUN_CONTROLLER_USER}"
: "${VLLM_RUN_ORB_MACHINE:?Set VLLM_RUN_ORB_MACHINE (OrbStack machine that holds the controller SSH key)}"
: "${VLLM_RUN_SSH_KEY:?Set VLLM_RUN_SSH_KEY (path of the controller SSH key inside the OrbStack machine)}"
: "${VLLM_RUN_REMOTE_TREE:?Set VLLM_RUN_REMOTE_TREE (deployed tree on the controller)}"

CONTROLLER_DIR="$VLLM_RUN_REMOTE_TREE/ansible"

run_on_controller() {
	orb -m "$VLLM_RUN_ORB_MACHINE" ssh -i "$VLLM_RUN_SSH_KEY" \
		-o IdentitiesOnly=yes -o BatchMode=yes \
		"${VLLM_RUN_CONTROLLER_USER}@${VLLM_RUN_CONTROLLER_HOST}" "$@"
}

sync_to_controller() {
	# Sync the repo root so litellm/ sits next to ansible/ (the LiteLLM
	# snippet renders into playbook_dir/../../litellm/) and otel/ + grafana/
	# land in the deployed tree the monitoring containers mount. The ._*
	# excludes block macOS AppleDouble resource forks: grafana's dashboard
	# provisioner refuses to parse them and the lgtm container crash-loops.
	run_on_controller "mkdir -p '$VLLM_RUN_REMOTE_TREE'"
	cd "$RIG_DIR/../../.."
	COPYFILE_DISABLE=1 tar cf - --exclude=.git --exclude=.env --exclude=certs \
		--exclude='*.key' --exclude='*.pem' --exclude='*.bak*' \
		--exclude='._*' --exclude='.DS_Store' \
		ansible litellm otel grafana | run_on_controller "tar xf - -C '$VLLM_RUN_REMOTE_TREE'"
}

run_playbook() {
	local pb="$1"
	shift
	local skip_sync="${1:-}"
	if [[ "$skip_sync" == "--no-sync" ]]; then
		shift
	fi
	if [[ "$skip_sync" != "--no-sync" ]]; then
		sync_to_controller
	fi
	local args=("$@")
	# Source the deployed .env so the playbook sees LITELLM_MASTER_KEY
	# (LiteLLM model registration) and REGISTRY_REDHAT_IO_* (Red Hat image
	# pulls) without exporting secrets on the controller shell. The local
	# inventory (real node IP/user) overrides the committed placeholders
	# when present.
	local inv_args="-i tests/vllm-remote/inventory.yml"
	if [[ -f "$RIG_DIR/inventory.local.yml" ]]; then
		inv_args+=" -i tests/vllm-remote/inventory.local.yml"
	fi
	local cmdline="cd '$CONTROLLER_DIR' && set -a && . '$VLLM_RUN_REMOTE_TREE/.env' && set +a && ansible-playbook $inv_args '$pb'"
	local i
	for i in ${args[@]+"${args[@]}"}; do
		cmdline+=" '$i'"
	done
	run_on_controller "$cmdline"
}

case "${1:-all}" in
sync)
	sync_to_controller
	;;
inference)
	run_playbook playbooks/inference.yml "${@:2}"
	;;
check)
	# Idempotency = a second run without the sync in between changes
	# nothing. The sync legitimately resets the controller-side managed
	# blocks (otel scrape jobs, LiteLLM snippet, dashboard), so the first
	# synced run always reports changed for those renders; the unsynced
	# rerun must report zero changes anywhere.
	run_playbook playbooks/inference.yml "${@:2}" >/dev/null 2>&1
	local_log=/tmp/vllm-remote-check.log
	run_playbook playbooks/inference.yml --no-sync "${@:2}" | tee "$local_log"
	if grep -Eq "changed=[1-9]" "$local_log"; then
		echo "FAIL: role is not idempotent"
		grep -oE "[a-z0-9-]+[[:space:]]+: ok=[0-9]+[[:space:]]+changed=[0-9]+" "$local_log" | head -3
		exit 1
	fi
	echo "PASS: idempotent (second unsynced run: zero changes)"
	;;
eval)
	# Reproducible model evaluation (vllm_eval role): KV probe, long
	# prompt, coding, vision, concurrency - all through the LiteLLM
	# gateway. Same suite used for the Qwen3-Coder vs Gemma-4 A/B.
	# Extra args pass through to ansible-playbook, e.g.:
	#   run.sh eval -e vllm_eval_model=gemini-3.8-flash
	run_playbook playbooks/eval.yml "${@:2}"
	;;
all)
	"$0" inference "${@:2}"
	"$0" check "${@:2}"
	;;
*)
	echo "usage: $0 [sync|inference|check|eval|all]" >&2
	exit 2
	;;
esac
