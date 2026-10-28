#!/bin/bash
# Runs the vllm_compute role against the disposable OrbStack "vllm-test" VM.
#
# Topology: the OrbStack "fedora" VM is the ansible controller (it has root
# SSH to the test VM). The test VM stands in for the GPU compute nodes with
# GPU stubs; the role runs unmodified except for image/model overrides set
# below.
#
# Prereqs (one-time, from the Mac):
#   orb create fedora:44 vllm-test
#   scripts/vllm-rig-prepare.sh     # installs sshd on both VMs, wires root SSH
#   ansible/tests/vllm-rig/run.sh setup   # stubs + docker image on the VM
#
# Usage:
#   run.sh [setup|inference|check|idempotency|all]
set -euo pipefail

RIG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INVENTORY="$RIG_DIR/inventory.yml"
# Rig overrides for the role. The image is the CPU stub; the "model" is a
# local path so no Hugging Face download happens.
export VLLM_IMAGE_OVERRIDE=vllm/vllm-openai:test-stub
export VLLM_MODEL_OVERRIDE=/opt/models/test-model

run_in_fedora() {
	orb run -m fedora "$@"
}

prepare_controller() {
	# The repo must exist on the controller VM. OrbStack mounts the Mac home;
	# copy the ansible tree to keep the controller self-contained.
	run_in_fedora sudo mkdir -p /opt/vllm-rig
	orb cp "$RIG_DIR/../../../." vllm-test:/tmp/ 2>/dev/null || true
	rsync -a --delete \
		--exclude .git --exclude .env --exclude certs --exclude '*.key' --exclude '*.pem' \
		-e "orb run -m fedora sudo ssh" \
		"$RIG_DIR/../../" vllm-rig:/opt/vllm-rig/ansible/
}

run_playbook() {
	local pb="$1"
	shift
	prepare_controller
	run_in_fedora sudo env \
		ANSIBLE_CONFIG=/opt/vllm-rig/ansible/ansible.cfg \
		VLLM_IMAGE_OVERRIDE="$VLLM_IMAGE_OVERRIDE" \
		VLLM_MODEL_OVERRIDE="$VLLM_MODEL_OVERRIDE" \
		VLLM_API_KEY=test-rig-key \
		ansible-playbook \
		-i /opt/vllm-rig/ansible/tests/vllm-rig/inventory.yml \
		/opt/vllm-rig/ansible/playbooks/"$pb" "$@"
}

case "${1:-all}" in
setup)
	run_playbook vllm-rig-stub-setup.yml
	;;
inference)
	run_playbook inference.yml
	;;
check)
	# Second run must report zero changed tasks: the role is idempotent.
	run_playbook inference.yml | tee /tmp/vllm-rig-run2.log
	if grep -E "changed=[1-9]" /tmp/vllm-rig-run2.log; then
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
	echo "usage: $0 [setup|inference|check|all]" >&2
	exit 2
	;;
esac
