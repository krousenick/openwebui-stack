#!/bin/bash
# Runs the vllm_compute role against the disposable OrbStack "vllm-test" VM.
#
# Topology: the OrbStack "fedora" VM is the ansible controller (root SSH to
# the test VM is wired by scripts/vllm-rig-prepare.sh). The test VM stands in
# for the GPU compute nodes with GPU stubs; the role runs unmodified except
# for image/model overrides.
#
# Usage:
#   run.sh [sync|setup|inference|check|all]
#     sync      - copy the ansible tree to the controller VM
#     setup     - install GPU stubs + build the CPU vLLM image on the VM
#     inference - run playbooks/inference.yml (the real role)
#     check     - rerun inference and require zero changes (idempotency)
set -euo pipefail

RIG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONTROLLER_DIR=/opt/vllm-rig/ansible

# Rig overrides. The image is the CPU stub built by setup; the model is a
# local path so no Hugging Face download happens; standalone mode avoids
# needing a second real node for the Ray cluster formation test.
export VLLM_IMAGE_OVERRIDE=vllm/vllm-openai:test-stub
export VLLM_MODEL_OVERRIDE=/opt/models/test-model

sync_to_controller() {
	orb -m fedora sudo mkdir -p "$CONTROLLER_DIR"
	cd "$RIG_DIR/../.."
	tar cf - --exclude=.git --exclude=.env --exclude=certs \
		--exclude='*.key' --exclude='*.pem' --exclude='*.bak*' \
		. | orb -m fedora sudo tar xf - -C "$CONTROLLER_DIR"
}

run_playbook() {
	local pb="$1"
	shift
	sync_to_controller
	orb -m fedora sudo env \
		ANSIBLE_CONFIG="$CONTROLLER_DIR/ansible.cfg" \
		VLLM_IMAGE_OVERRIDE="$VLLM_IMAGE_OVERRIDE" \
		VLLM_MODEL_OVERRIDE="$VLLM_MODEL_OVERRIDE" \
		VLLM_API_KEY=test-rig-key \
		ansible-playbook \
		-i "$CONTROLLER_DIR/tests/vllm-rig/inventory.yml" \
		"$pb" "$@"
}

case "${1:-all}" in
sync)
	sync_to_controller
	;;
setup)
	run_playbook "$CONTROLLER_DIR/tests/vllm-rig/stub-setup.yml"
	;;
inference)
	run_playbook "$CONTROLLER_DIR/playbooks/inference.yml"
	;;
check)
	local_log=/tmp/vllm-rig-run2.log
	run_playbook "$CONTROLLER_DIR/playbooks/inference.yml" | tee "$local_log"
	if grep -Eq "changed=[1-9]" "$local_log"; then
		echo "FAIL: role is not idempotent"
		exit 1
	fi
	echo "PASS: idempotent"
	;;
all)
	"$0" setup
	"$0" inference
	"$0" check
	;;
*)
	echo "usage: $0 [sync|setup|inference|check|all]" >&2
	exit 2
	;;
esac
