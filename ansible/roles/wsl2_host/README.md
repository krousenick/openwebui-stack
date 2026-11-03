---
# ===========================================================================
# WSL2 Windows host provisioning role
# ===========================================================================
# This role configures Windows hosts to run Fedora WSL2 with Docker,
# bridged networking, and NVIDIA GPU passthrough for remote inference.
#
# Usage
# -----
# 1. Configure Windows host inventory with winrm or ssh connection:
#
#   all:
#      children:
#        wsl2_hosts:
#          hosts:
#            windows-desktop:
#              ansible_host: 192.168.1.100
#              ansible_user: admin
#              ansible_password: <password>
#              ansible_connection: winrm
#              ansible_winrm_transport: ntlm
#              ansible_winrm_server_cert_validation: ignore
#
# 2. Run the playbook:
#
#    ansible-playbook playbooks/wsl2.yml -l windows-desktop
#
# Requirements
# ------------
# - Windows 10 2004+ or Windows 11
# - WSL2 support ( virtualization enabled in BIOS)
# - Ansible 2.14+ with pywinrm or sh (for native SSH to Windows)
#
# Features
# --------
# - Installs WSL2 and Fedora distribution
# - Configures bridged networking for direct LAN IP
# - Installs Docker inside WSL2 Fedora
# - Enables NVIDIA GPU passthrough (requires NVIDIA driver on Windows)
# - Configures Windows firewall for WSL2 network access
#
# After provisioning, the WSL2 instance gets a LAN IP that can be added
# to the vllm_compute inventory group for inference deployment.