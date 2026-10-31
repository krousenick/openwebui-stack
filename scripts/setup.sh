#!/bin/bash
# =============================================================================
# Open WebUI Stack - Setup Entrypoint
# =============================================================================
# Thin wrapper around the Ansible playbook. All configuration formerly done
# by this script (password generation, .env rendering, TLS certificates,
# /etc/hosts, compose templating, stack bring-up, Keycloak provisioning)
# now lives in ansible/roles/openwebui_stack:
#
#   ansible-galaxy collection install -r requirements.yaml
#   ansible-playbook playbooks/site.yml
#
# This script only ensures Ansible and its collections are present and runs
# the playbook. Extra CLI args are forwarded to ansible-playbook.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
ANSIBLE_DIR="$PROJECT_ROOT/ansible"

error_exit() {
	echo "ERROR: $1" >&2
	exit 1
}

# Colors
BLUE='\033[0;34m'
NC='\033[0m'

# ---------------------------------------------------------------------------
# Prerequisites
# ---------------------------------------------------------------------------

if ! command -v ansible-playbook >/dev/null 2>&1; then
	echo "ansible-playbook not found. Install Ansible (ansible-core 2.16+), then:"
	echo "  cd ansible && ansible-galaxy collection install -r requirements.yaml"
	echo "  ansible-playbook playbooks/site.yml"
	exit 1
fi

if [ ! -d "$ANSIBLE_DIR" ]; then
	error_exit "ansible/ directory not found at $ANSIBLE_DIR"
fi

# Install collections when the required keycloak collection is absent.
if ! ansible-galaxy collection list 2>/dev/null | grep -q "middleware_automation.keycloak"; then
	echo -e "${BLUE}Installing Ansible collections...${NC}"
	(cd "$ANSIBLE_DIR" && ansible-galaxy collection install -r requirements.yaml)
fi

# ---------------------------------------------------------------------------
# Run the playbook (all extra args are forwarded)
# ---------------------------------------------------------------------------

echo -e "${BLUE}Running: cd ansible && ansible-playbook playbooks/site.yml $*${NC}"
cd "$ANSIBLE_DIR"
exec ansible-playbook playbooks/site.yml "$@"
