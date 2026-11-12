"""Hide model names from Open WebUI's chat picker.

Runs in the open-webui container (tasks/claude_subscription.yml). Open WebUI
lists every model LiteLLM serves, including some no chat can use, such as
the names LiteLLM's Claude subscription passthrough expands to: they need
the caller's own claude.ai sign-in, which only Claude Code sends. For each
name in the payload the script keeps an inactive Workspace entry for that
base model, and Open WebUI leaves inactive base models out of the picker for
every user. Entries the script creates carry meta.stackHidden; when a name
leaves the payload its entry is deleted, so the model shows again.

A listed name that already has an entry the script did not create is left
alone and reported under unmanaged: that entry holds an admin's own
settings. A managed entry someone switched back on is switched off again.

Input: base64 JSON on stdin, {"models": ["<model id>", ...]}.
Environment: OPENWEBUI_API_KEY (an admin API key).
Report keys: hidden (the listed names now hidden), removed (entries deleted
because their name left the list), unmanaged (listed names with an entry
the script does not own).
"""

from __future__ import annotations

from typing import Any

import stack_api
from stack_api import Api, ScriptError

MARKER = "stackHidden"
DESCRIPTION = (
    "Hidden from chat by the playbook: LiteLLM serves it for Claude Code only (tasks/claude_subscription.yml)."
)


def requested_models(payload: dict[str, Any]) -> list[str]:
    models = payload.get("models")
    if not isinstance(models, list) or not all(isinstance(m, str) and m for m in models):
        raise ScriptError('the payload needs "models", a list of model ids')
    return sorted(set(models))


def main(argv: list[str] | None = None) -> dict[str, Any]:
    args = stack_api.argument_parser(__doc__, url="http://localhost:8080").parse_args(argv)
    wanted = requested_models(stack_api.read_payload())
    api = Api.from_args(args, stack_api.secret("OPENWEBUI_API_KEY"))

    rows = api.get("/api/v1/models/all")
    if not isinstance(rows, list):
        raise ScriptError("GET /api/v1/models/all did not return a list")
    by_id = {row["id"]: row for row in rows if isinstance(row, dict) and row.get("id")}

    hidden: list[str] = []
    unmanaged: list[str] = []
    for model_id in wanted:
        row = by_id.get(model_id)
        if row is None:
            api.write(
                f"hide {model_id}",
                "POST",
                "/api/v1/models/create",
                json_body={
                    "id": model_id,
                    "base_model_id": None,
                    "name": model_id,
                    "meta": {MARKER: True, "description": DESCRIPTION},
                    "params": {},
                    "is_active": False,
                },
            )
        elif not (row.get("meta") or {}).get(MARKER):
            unmanaged.append(model_id)
            continue
        elif row.get("is_active"):
            api.write(
                f"hide {model_id} again",
                "POST",
                "/api/v1/models/model/update",
                json_body={
                    "id": model_id,
                    "name": row.get("name") or model_id,
                    "meta": row.get("meta") or {MARKER: True},
                    "params": row.get("params") or {},
                    "is_active": False,
                },
            )
        hidden.append(model_id)

    removed = sorted(
        model_id for model_id, row in by_id.items() if (row.get("meta") or {}).get(MARKER) and model_id not in wanted
    )
    for model_id in removed:
        api.write(f"show {model_id} again", "POST", "/api/v1/models/model/delete", json_body={"id": model_id})

    return api.report(hidden=hidden, removed=removed, unmanaged=unmanaged)


if __name__ == "__main__":
    stack_api.run(main)
