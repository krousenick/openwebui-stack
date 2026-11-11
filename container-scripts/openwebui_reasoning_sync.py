"""Sync the Thinking switch into Open WebUI through its admin API.

Runs in the open-webui container (tasks/openwebui_reasoning.yml). It
converges three things:

  1. The two filter functions (reasoning_switch, reasoning_off): created or
     updated when their source differs, active, never global.
  2. The Workspace entry of every reasoning-capable base model: its
     meta.filterIds carries the switch (plus the off-state companion when
     the model accepts reasoning_effort "none"), and meta.defaultFilterIds
     carries the switch when it should start switched on.
  3. Base-model entries that are no longer in the list: the two managed ids
     are removed and everything else on the entry is left alone.

Only base-model entries (base_model_id is null) are reconciled; preset
models built in the Workspace are never touched. An entry is created
without access grants, which is the same visibility an unconfigured model
has (admins only); an existing entry keeps its grants because the update
endpoint does not write them.

Input, a base64 JSON object on stdin:
  enabled        false detaches the switch everywhere and disables the functions
  default_on     start chats with the switch on
  switch_id      id of the switch filter function
  off_id         id of the off-state companion filter function
  switch_models  base models that get the switch
  off_models     the subset that also gets the off-state companion
  functions      [{id, name, description, content}], the off-state
                 companion first: it has to exist before a model entry
                 references both ids

Environment: OPENWEBUI_API_KEY (an admin API key).
Report keys: attached (models carrying the switch) and missing (requested
models Open WebUI does not serve as a base model).
"""

from __future__ import annotations

import dataclasses
from typing import Any

import stack_api
from stack_api import Api, ScriptError, truthy

FUNCTIONS_API = "/api/v1/functions"
MODELS_API = "/api/v1/models"


@dataclasses.dataclass(frozen=True)
class Settings:
    enabled: bool
    default_on: bool
    switch_id: str
    off_id: str
    switch_models: list[str]
    off_models: frozenset[str]
    functions: list[dict[str, str]]

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> Settings:
        try:
            return cls(
                enabled=truthy(payload.get("enabled", True)),
                default_on=truthy(payload.get("default_on", True)),
                switch_id=payload["switch_id"],
                off_id=payload["off_id"],
                switch_models=list(payload.get("switch_models") or []),
                off_models=frozenset(payload.get("off_models") or []),
                functions=list(payload["functions"]),
            )
        except KeyError as error:
            raise ScriptError(f"the stdin payload has no {error}") from error

    def filter_lists(self, meta: dict[str, Any], filters: list[str]) -> tuple[list[str], list[str]]:
        """Return (filterIds, defaultFilterIds) for an entry: its own ids kept, the managed ones replaced."""
        managed = (self.switch_id, self.off_id)
        keep = [f for f in (meta.get("filterIds") or []) if f not in managed]
        keep_default = [f for f in (meta.get("defaultFilterIds") or []) if f not in managed]
        return keep + filters, keep_default + ([self.switch_id] if filters and self.default_on else [])


def sync_function(api: Api, function: dict[str, str], *, active: bool) -> None:
    """Create or update one filter function and set its active and global flags."""
    function_id = function["id"]
    path = f"{FUNCTIONS_API}/id/{function_id}"
    form = {
        "id": function_id,
        "name": function["name"],
        "content": function["content"],
        "meta": {"description": function["description"]},
    }
    # A missing function answers 401 NOT_FOUND; the key itself was already
    # proven by the model listing in main().
    status, current = api.request("GET", path)
    exists = status == 200 and isinstance(current, dict) and current.get("id") == function_id
    if not exists:
        if not active:
            return
        created = api.write(f"create function {function_id}", "POST", f"{FUNCTIONS_API}/create", json_body=form)
        current = created if isinstance(created, dict) else {"is_active": False, "is_global": False}
    elif active and (
        current.get("content") != function["content"]
        or current.get("name") != function["name"]
        or (current.get("meta") or {}).get("description") != function["description"]
    ):
        api.write(f"update function {function_id}", "POST", f"{path}/update", json_body=form)

    if truthy(current.get("is_active")) != active:
        api.write(f"{'enable' if active else 'disable'} function {function_id}", "POST", f"{path}/toggle")
    if truthy(current.get("is_global")):
        # Global would put the switch on every model; attachment is per
        # model through meta.filterIds.
        api.write(f"unset global on function {function_id}", "POST", f"{path}/toggle/global")


def update_entry(api: Api, row: dict[str, Any], filter_ids: list[str], default_ids: list[str], action: str) -> None:
    """Write an entry's filter lists when they differ from the stored ones."""
    meta = row.get("meta") or {}
    if sorted(meta.get("filterIds") or []) == sorted(filter_ids) and sorted(
        meta.get("defaultFilterIds") or []
    ) == sorted(default_ids):
        return
    # Unset fields are left out so the endpoint keeps the stored profile
    # image, model controls, base model and active state.
    new_meta = {key: value for key, value in meta.items() if value is not None}
    new_meta["filterIds"] = filter_ids
    new_meta["defaultFilterIds"] = default_ids
    params = {key: value for key, value in (row.get("params") or {}).items() if key != "model_controls"}
    form = {"id": row["id"], "name": row["name"], "meta": new_meta, "params": params}
    api.write(action, "POST", f"{MODELS_API}/model/update", json_body=form)


def sync_models(api: Api, settings: Settings) -> tuple[list[str], list[str]]:
    """Attach the filters to the wanted base models, detach them elsewhere; return (attached, missing)."""
    listing = api.get("/api/models")
    if not isinstance(listing, dict):
        raise ScriptError("GET /api/models did not return a JSON object")
    served = {model["id"]: model for model in listing.get("data") or [] if isinstance(model, dict) and model.get("id")}

    rows = api.get(f"{MODELS_API}/all")
    if not isinstance(rows, list):
        raise ScriptError(f"GET {MODELS_API}/all did not return a JSON list")
    rows_by_id = {row["id"]: row for row in rows if isinstance(row, dict) and row.get("id")}

    wanted: dict[str, list[str]] = {}
    missing = []
    if settings.enabled:
        for model_id in settings.switch_models:
            model = served.get(model_id)
            row = rows_by_id.get(model_id)
            # Not served yet, or the id belongs to a Workspace preset.
            if model is None or model.get("preset") or (row and row.get("base_model_id")):
                missing.append(model_id)
                continue
            wanted[model_id] = [settings.switch_id] + ([settings.off_id] if model_id in settings.off_models else [])

    for model_id, filters in wanted.items():
        row = rows_by_id.get(model_id)
        if row is None:
            filter_ids, default_ids = settings.filter_lists({}, filters)
            form = {
                "id": model_id,
                "base_model_id": None,
                "name": served[model_id].get("name") or model_id,
                "meta": {"filterIds": filter_ids, "defaultFilterIds": default_ids},
                "params": {},
            }
            api.write(f"create model entry {model_id}", "POST", f"{MODELS_API}/create", json_body=form)
            continue
        filter_ids, default_ids = settings.filter_lists(row.get("meta") or {}, filters)
        update_entry(api, row, filter_ids, default_ids, f"attach to model entry {model_id}")

    for model_id, row in rows_by_id.items():
        if model_id in wanted or row.get("base_model_id"):
            continue
        filter_ids, default_ids = settings.filter_lists(row.get("meta") or {}, [])
        update_entry(api, row, filter_ids, default_ids, f"detach from model entry {model_id}")

    return list(wanted), missing


def main(argv: list[str] | None = None) -> dict[str, Any]:
    args = stack_api.argument_parser(__doc__, url="http://localhost:8080").parse_args(argv)
    api = Api.from_args(args, stack_api.secret("OPENWEBUI_API_KEY"))
    settings = Settings.from_payload(stack_api.read_payload())

    status, _ = api.request("GET", "/api/models")
    if status != 200:
        raise ScriptError(f"admin API key rejected or Open WebUI unreachable: GET /api/models returned {status}")

    if settings.enabled:
        for function in settings.functions:
            sync_function(api, function, active=True)
        attached, missing = sync_models(api, settings)
    else:
        # Detach first: a model entry must not reference a disabled filter.
        attached, missing = sync_models(api, settings)
        for function in settings.functions:
            sync_function(api, function, active=False)

    return api.report(attached=attached, missing=missing)


if __name__ == "__main__":
    stack_api.run(main)
