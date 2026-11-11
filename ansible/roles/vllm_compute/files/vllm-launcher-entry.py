"""Entry point of the stack's LLM Launcher image (role: vllm_compute).

Starts the launcher the same way `python3 -m local_llm_launcher` does. On
this stack people talk to models through Open WebUI or an API client
(opencode, an IDE), never through the launcher, so unless
LAUNCHER_STACK_CHAT is "enabled" the launcher's two chat paths are taken
out of its API first:

  POST /api/servers/{id}/chat   the "Test chat" box on a server card
  /api/openwebui/*              the panel that starts a second Open WebUI
                                on the node, with no login in front of it

The image build hides the matching controls in the page (patch_ui.py and
stack-auth.js); this file is what enforces it, because a request can be
sent without the page. The routes are replaced, not just removed, so a
caller gets a 403 that says where to chat instead of a bare 404.

Fails closed: if the launcher no longer has those routes under these paths
(a new upstream version moved them), the process exits instead of starting
with chat reachable under a path this file does not know.

When the role renders the slot settings (vllm_launcher_swap on a node whose
launcher owns the primary model, LAUNCHER_STACK_SLOT_FILE), stack_slot.py
(vllm-launcher-slot.py in the role) is loaded too: one model at a time on
the primary port, a launch replaces it, a failed launch rolls back, and the
stack can read what is served. It fails closed the same way.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

CHAT_ENV = "LAUNCHER_STACK_CHAT"
SLOT_FILE_ENV = "LAUNCHER_STACK_SLOT_FILE"
CHAT_ROUTE = "/api/servers/{server_id}/chat"
OPENWEBUI_PREFIX = "/api/openwebui"
MESSAGE = (
    "Chat is turned off in this launcher. Talk to the model through Open WebUI or an API client such as opencode."
)


def chat_enabled() -> bool:
    return os.environ.get(CHAT_ENV, "").strip().lower() == "enabled"


def disable_chat() -> None:
    """Swap the launcher's chat routes for refusals.

    Must run before the launcher builds its FastAPI app: create_app() copies
    the router's routes at that point.
    """
    from fastapi import HTTPException
    from local_llm_launcher import api

    kept = []
    removed = set()
    for route in api.router.routes:
        path = getattr(route, "path", "")
        if path == CHAT_ROUTE or path == OPENWEBUI_PREFIX or path.startswith(OPENWEBUI_PREFIX + "/"):
            removed.add(path)
        else:
            kept.append(route)

    missing = sorted({CHAT_ROUTE, OPENWEBUI_PREFIX + "/launch"} - removed)
    if missing:
        raise RuntimeError(
            f"the launcher has no route {', '.join(missing)}: this launcher version moved its chat "
            "endpoints. Update vllm-launcher-entry.py for the new paths, or set "
            "vllm_launcher_chat_enabled: true to run it with chat."
        )
    api.router.routes[:] = kept

    @api.router.post("/servers/{server_id}/chat", include_in_schema=False)
    def chat_disabled(server_id: str):
        raise HTTPException(403, MESSAGE)

    @api.router.get("/openwebui", include_in_schema=False)
    def openwebui_status():
        # The shape the page polls for; "not installed" keeps its panel inert
        # even if the stylesheet that hides it did not load.
        return {
            "installed": False,
            "running": False,
            "port": 0,
            "url": "",
            "install_command": "",
            "log_path": None,
        }

    @api.router.post("/openwebui/launch", include_in_schema=False)
    def openwebui_launch_disabled():
        raise HTTPException(403, MESSAGE)

    @api.router.post("/openwebui/stop", include_in_schema=False)
    def openwebui_stop_disabled():
        raise HTTPException(403, MESSAGE)


def main() -> int:
    if not chat_enabled():
        try:
            disable_chat()
        except Exception as error:  # fail closed, with the reason in the unit's journal
            print(f"stack_entry: cannot switch chat off: {error}", file=sys.stderr)
            return 1
        # flush: stdout is a pipe under systemd and the process never exits.
        print("stack_entry: chat is off (test chat and the Open WebUI panel are refused)", flush=True)

    slot_file = os.environ.get(SLOT_FILE_ENV, "")
    if slot_file:
        if not Path(slot_file).is_file():
            print(f"stack_entry: {SLOT_FILE_ENV} names {slot_file}, which does not exist", file=sys.stderr)
            return 1
        try:
            # stack_slot.py sits next to this file, which Python puts first
            # on sys.path when it runs a script by path.
            import stack_slot

            slot = stack_slot.install(slot_file)
        except Exception as error:  # fail closed, with the reason in the unit's journal
            print(f"stack_entry: cannot set up the single-model slot: {error}", file=sys.stderr)
            return 1
        settings = slot.settings
        print(
            f"stack_entry: single-model slot on port {settings.port} (a launch replaces the running model; "
            f"rollback {'on' if settings.rollback else 'off'})",
            flush=True,
        )

    from local_llm_launcher.__main__ import main as launcher_main

    launcher_main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
