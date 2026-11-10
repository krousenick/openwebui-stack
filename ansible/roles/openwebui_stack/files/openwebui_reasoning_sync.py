"""Sync the Thinking switch into Open WebUI through its admin API.

Runs inside the open-webui container (tasks/openwebui_reasoning.yml pipes a
base64 JSON payload on stdin). It converges three things:

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

Always prints one JSON object and exits 0; "failed" carries the reason when
the sync could not complete. With dry_run nothing is written and "changed"
lists what a real run would do.
"""

import base64
import json
import sys
import urllib.error
import urllib.request

BASE = "http://localhost:8080"


class SyncError(Exception):
    pass


def truthy(value):
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def main():
    payload = json.loads(base64.b64decode(sys.stdin.read()))
    key = payload["api_key"]
    dry_run = truthy(payload.get("dry_run"))
    enabled = truthy(payload.get("enabled", True))
    default_on = truthy(payload.get("default_on", True))
    switch_id = payload["switch_id"]
    off_id = payload["off_id"]
    managed = (switch_id, off_id)
    summary = {"changed": [], "attached": [], "missing": [], "failed": "", "dry_run": dry_run}

    def call(method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(BASE + path, data=data, method=method)
        request.add_header("Authorization", "Bearer " + key)
        request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                raw = response.read()
                return response.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as error:
            return error.code, {"detail": error.read().decode(errors="replace")[:500]}
        except (urllib.error.URLError, TimeoutError, ValueError) as error:
            return 0, {"detail": str(error)}

    def write(action, method, path, body=None):
        """Record a change and perform it unless this is a dry run."""
        summary["changed"].append(action)
        if dry_run:
            return None
        status, result = call(method, path, body)
        if status != 200 or result is None:
            raise SyncError(f"{action}: {method} {path} returned {status} {json.dumps(result)[:300]}")
        return result

    def sync_function(function, active):
        function_id = function["id"]
        form = {
            "id": function_id,
            "name": function["name"],
            "content": function["content"],
            "meta": {"description": function["description"]},
        }
        # A missing function answers 401 NOT_FOUND; the key itself was
        # already proven by the model listing above.
        status, current = call("GET", f"/api/v1/functions/id/{function_id}")
        exists = status == 200 and isinstance(current, dict) and current.get("id") == function_id
        if not exists:
            if not active:
                return
            created = write(f"create function {function_id}", "POST", "/api/v1/functions/create", form)
            current = created or {"is_active": False, "is_global": False}
        elif active and (
            current.get("content") != function["content"]
            or current.get("name") != function["name"]
            or (current.get("meta") or {}).get("description") != function["description"]
        ):
            write(f"update function {function_id}", "POST", f"/api/v1/functions/id/{function_id}/update", form)

        if truthy(current.get("is_active")) != active:
            write(
                f"{'enable' if active else 'disable'} function {function_id}",
                "POST",
                f"/api/v1/functions/id/{function_id}/toggle",
            )
        if truthy(current.get("is_global")):
            # Global would put the switch on every model; attachment is
            # per model through meta.filterIds.
            write(f"unset global on function {function_id}", "POST", f"/api/v1/functions/id/{function_id}/toggle/global")

    def sync_models():
        status, listing = call("GET", "/api/models")
        if status != 200 or not isinstance(listing, dict):
            raise SyncError(f"GET /api/models returned {status} {json.dumps(listing)[:300]}")
        served = {m["id"]: m for m in listing.get("data", []) if isinstance(m, dict) and m.get("id")}

        status, rows = call("GET", "/api/v1/models/all")
        if status != 200 or not isinstance(rows, list):
            raise SyncError(f"GET /api/v1/models/all returned {status} {json.dumps(rows)[:300]}")
        rows_by_id = {row["id"]: row for row in rows if isinstance(row, dict) and row.get("id")}

        off_models = set(payload.get("off_models") or [])
        wanted = {}
        if enabled:
            for model_id in payload.get("switch_models") or []:
                model = served.get(model_id)
                row = rows_by_id.get(model_id)
                # Not served yet, or the id belongs to a Workspace preset.
                if model is None or model.get("preset") or (row and row.get("base_model_id")):
                    summary["missing"].append(model_id)
                    continue
                wanted[model_id] = [switch_id] + ([off_id] if model_id in off_models else [])

        def lists_for(meta, filters):
            keep = [f for f in (meta.get("filterIds") or []) if f not in managed]
            keep_default = [f for f in (meta.get("defaultFilterIds") or []) if f not in managed]
            return keep + filters, keep_default + ([switch_id] if filters and default_on else [])

        for model_id, filters in wanted.items():
            summary["attached"].append(model_id)
            row = rows_by_id.get(model_id)
            if row is None:
                filter_ids, default_ids = lists_for({}, filters)
                write(
                    f"create model entry {model_id}",
                    "POST",
                    "/api/v1/models/create",
                    {
                        "id": model_id,
                        "base_model_id": None,
                        "name": served[model_id].get("name") or model_id,
                        "meta": {"filterIds": filter_ids, "defaultFilterIds": default_ids},
                        "params": {},
                    },
                )
                continue
            update_row(row, *lists_for(row.get("meta") or {}, filters), f"attach to model entry {model_id}")

        for model_id, row in rows_by_id.items():
            if model_id in wanted or row.get("base_model_id"):
                continue
            update_row(row, *lists_for(row.get("meta") or {}, []), f"detach from model entry {model_id}")

    def update_row(row, filter_ids, default_ids, action):
        meta = row.get("meta") or {}
        if sorted(meta.get("filterIds") or []) == sorted(filter_ids) and sorted(
            meta.get("defaultFilterIds") or []
        ) == sorted(default_ids):
            return
        # Unset fields are left out so the endpoint keeps the stored
        # profile image, model controls, base model and active state.
        new_meta = {k: v for k, v in meta.items() if v is not None}
        new_meta["filterIds"] = filter_ids
        new_meta["defaultFilterIds"] = default_ids
        params = {k: v for k, v in (row.get("params") or {}).items() if k != "model_controls"}
        write(
            action,
            "POST",
            "/api/v1/models/model/update",
            {"id": row["id"], "name": row["name"], "meta": new_meta, "params": params},
        )

    try:
        status, listing = call("GET", "/api/models")
        if status != 200:
            raise SyncError(f"admin API key rejected or Open WebUI unreachable: GET /api/models returned {status}")
        if enabled:
            for function in payload["functions"]:
                sync_function(function, True)
            sync_models()
        else:
            # Detach first: a model entry must not reference a disabled filter.
            sync_models()
            for function in payload["functions"]:
                sync_function(function, False)
    except SyncError as error:
        summary["failed"] = str(error)
    except Exception as error:  # noqa: BLE001 - always report as JSON
        summary["failed"] = f"{type(error).__name__}: {error}"

    print(json.dumps(summary))


main()
