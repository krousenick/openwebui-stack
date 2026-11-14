"""Share the chat models LiteLLM serves with every Open WebUI user.

Runs in the open-webui container (tasks/openwebui_model_access.yml). Open
WebUI 0.13 shows a user who is not an admin only the models whose Workspace
entry grants them read access, and a base model without an entry to admins
only. The playbook never granted any, so such users saw no model at all
(found on 2026-11-13 with a new account). For every base model the chat
picker lists from the OpenAI-compatible connection (LiteLLM), the script
keeps Open WebUI's public read grant: principal user "*", permission read,
which is what the Workspace's Visibility: Public sets.

  GET  /api/models                        what the chat picker offers
  GET  /api/v1/models/all                 the Workspace entries, with grants
  POST /api/v1/models/model/access/update replace one model's grants; makes
                                          a plain entry for a model that has
                                          none

A model keeps its other grants. Left as they are: models --exclude names,
Workspace presets (entries built on a base model; their owner shares them),
arena models, pipes, and models from other connections. Hidden entries
(inactive, such as the Claude passthrough names and the embedding models)
never reach the picker's list, so they stay hidden; the task runs after the
one that hides them.

Environment: OPENWEBUI_API_KEY (an admin API key).
Report keys: shared (models given the grant now), public (models that had
it already), excluded (served models left alone because --exclude names
them).
"""

from __future__ import annotations

from typing import Any

import stack_api
from stack_api import Api, ScriptError

ACCESS_UPDATE = "/api/v1/models/model/access/update"
# Open WebUI's "public": every signed-in user may read the model.
PUBLIC_READ = ("user", "*", "read")
# owned_by of the models Open WebUI lists from an OpenAI-compatible
# connection; LiteLLM is the stack's only one.
CONNECTION_OWNER = "openai"


def grant_key(grant: dict[str, Any]) -> tuple[Any, Any, Any]:
    return grant.get("principal_type"), grant.get("principal_id"), grant.get("permission")


def as_grant(key: tuple[Any, Any, Any]) -> dict[str, Any]:
    return dict(zip(("principal_type", "principal_id", "permission"), key, strict=True))


def connection_models(listing: list[Any]) -> list[tuple[str, str]]:
    """(id, name) of the base models the chat picker lists from the OpenAI-compatible connection."""
    found: dict[str, str] = {}
    for model in listing:
        if not isinstance(model, dict) or not model.get("id"):
            continue
        if model.get("arena") or "pipe" in model or model.get("owned_by") != CONNECTION_OWNER:
            continue
        info = model.get("info") if isinstance(model.get("info"), dict) else {}
        if info.get("base_model_id"):
            continue  # a Workspace preset: its owner decides who sees it
        found[str(model["id"])] = str(model.get("name") or model["id"])
    return sorted(found.items())


def main(argv: list[str] | None = None) -> dict[str, Any]:
    parser = stack_api.argument_parser(__doc__, url="http://localhost:8080")
    parser.add_argument("--exclude", default="", help="model ids to leave as they are, comma-separated")
    args = parser.parse_args(argv)
    excluded_ids = {item.strip() for item in args.exclude.split(",") if item.strip()}
    api = Api.from_args(args, stack_api.secret("OPENWEBUI_API_KEY"))

    listing = api.get("/api/models")
    if not isinstance(listing, dict) or not isinstance(listing.get("data"), list):
        raise ScriptError("GET /api/models did not return a model list")
    grants_by_id = stored_grants(api)

    shared: list[str] = []
    public: list[str] = []
    excluded: list[str] = []
    for model_id, name in connection_models(listing["data"]):
        if model_id in excluded_ids:
            excluded.append(model_id)
            continue
        keys = grants_by_id.get(model_id, [])
        if PUBLIC_READ in keys:
            public.append(model_id)
            continue
        wanted = [as_grant(key) for key in dict.fromkeys(keys)] + [as_grant(PUBLIC_READ)]
        api.write(
            f"share {model_id} with every user",
            "POST",
            ACCESS_UPDATE,
            json_body={"id": model_id, "name": name, "access_grants": wanted},
        )
        shared.append(model_id)

    if shared and not api.check:
        stored = stored_grants(api)
        missing = [model_id for model_id in shared if PUBLIC_READ not in stored.get(model_id, [])]
        if missing:
            raise ScriptError(f"POST {ACCESS_UPDATE} did not store the public grant for {', '.join(missing)}")

    return api.report(shared=shared, public=public, excluded=excluded)


def stored_grants(api: Api) -> dict[str, list[tuple[Any, Any, Any]]]:
    """Each Workspace entry's grants, as (principal type, principal id, permission)."""
    rows = api.get("/api/v1/models/all")
    if not isinstance(rows, list):
        raise ScriptError("GET /api/v1/models/all did not return a list")
    return {
        row["id"]: [grant_key(grant) for grant in row.get("access_grants") or [] if isinstance(grant, dict)]
        for row in rows
        if isinstance(row, dict) and row.get("id")
    }


if __name__ == "__main__":
    stack_api.run(main)
