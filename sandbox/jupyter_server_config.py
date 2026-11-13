"""
Isolated Jupyter sandbox for Open WebUI code execution.

Open WebUI's workspace Tools and the legacy jupyter code-execution engine both
run model-authored Python. Workspace Tools run inside the Open WebUI container
itself, so the stack keeps ENABLE_CODE_EXECUTION / ENABLE_CODE_INTERPRETER off
by default and runs code here instead: a pinned, token-authenticated
jupyter/scipy-notebook image on an internal-only Docker network with memory
and CPU caps, reachable only by the Open WebUI backend container.

Build context deps: numpy-stl and trimesh are pinned in requirements.txt so
model code can generate STL/mesh artifacts without runtime pip installs
(ENABLE_PIP_INSTALL_FRONTMATTER_REQUIREMENTS is disabled for the same reason).
"""

import os

c = get_config()  # noqa: F821

# Token auth only; no password login form. The token comes from the JUPYTER_TOKEN
# env var set by docker-compose.yml (vault-managed secret).
c.IdentityProviderTokenProvider.token = os.environ["JUPYTER_TOKEN"]

# The sandbox never leaves the Docker network; disable the browser-opening
# machinery and the git integration that only make sense on a workstation.
c.ServerApp.open_browser = False
c.ServerApp.port = 8888
c.ServerApp.ip = "0.0.0.0"
c.ServerApp.root_dir = "/home/jovyan/work"

# Kernel hygiene: every Open WebUI execute_code call creates a fresh kernel;
# cull idle ones so chat bursts do not accumulate kernel processes.
c.MappingKernelManager.cull_idle_timeout = 300
c.MappingKernelManager.cull_busy_timeout = 600
c.MappingKernelManager.cull_connected = False

# Keep the API surface to what Open WebUI needs (kernel start, execute, delete,
# file download through the work volume). No terminal server.
c.ServerApp.terminals_enabled = False
