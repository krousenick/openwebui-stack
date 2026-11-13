"""Set keys of Open WebUI's default interface settings, and admin-config switches.

Runs in the open-webui container (tasks/openwebui_interface_defaults.yml).
The default interface settings (admin Settings > General, stored as
DEFAULT_INTERFACE_SETTINGS) are the starting values of every user's
Settings > Interface; a user's own choice still wins. Only the keys in the
payload are set. Every other key already in the defaults is kept.

The same save also sets top-level admin-config switches named in the
payload, such as ENABLE_MEMORIES (Open WebUI's own memory, which the stack
turns off while the shared memory server is on).

Both are fields of the admin config, which Open WebUI saves as a whole:

  GET  /api/v1/auths/admin/config  the admin config
  POST /api/v1/auths/admin/config  save it. The GET result is posted back
                                   with only the requested fields changed,
                                   so every other admin setting keeps its
                                   value.

The three MFA fields are left out of the POST. Open WebUI refuses them from
an API key ("A human administrator session is required to change MFA
settings"), even unchanged, and a changed MFA policy signs every user out.
A field missing from the form is not saved, so the MFA policy stays as it
is; the payload may not name them either. Open WebUI 0.13 reads these
settings from its database on every request, so they apply without a
restart.

Input: base64 JSON on stdin, {"settings": {"<key>": <value>, ...},
"config": {"<ADMIN_CONFIG_FIELD>": <value>, ...}}. Either may be left out,
not both.
Environment: OPENWEBUI_API_KEY (an admin API key).
Report keys: settings (the default interface settings after the run; with
--check, what a real run would store) and config (the requested admin
fields, likewise).
"""

from __future__ import annotations

import json
from typing import Any

import stack_api
from stack_api import Api, ScriptError, truthy

CONFIG = "/api/v1/auths/admin/config"
FIELD = "DEFAULT_INTERFACE_SETTINGS"
# Open WebUI's MfaConfigForm (open_webui/utils/mfa.py, MFA_CONFIG_KEYS).
MFA_FIELDS = ("ENABLE_MFA", "MFA_ALLOW_OAUTH_BYPASS", "MFA_ALLOW_TRUSTED_HEADER_BYPASS")
_MISSING = object()


def requested(payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the interface settings and the admin-config fields to enforce from the stdin payload."""
    settings = payload.get("settings") or {}
    fields = payload.get("config") or {}
    if not isinstance(settings, dict) or not isinstance(fields, dict) or not (settings or fields):
        raise ScriptError('the payload needs a non-empty "settings" or "config" object')
    if FIELD in fields:
        raise ScriptError(f'set {FIELD} through "settings", not "config"')
    refused = sorted(field for field in fields if field in MFA_FIELDS)
    if refused:
        raise ScriptError(f"the MFA settings cannot be set with an API key: {', '.join(refused)}")
    return settings, fields


def admin_form(config: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    """Build the POST body: the stored admin config with the changes, minus the MFA fields.

    I18N is left out when it is null: Open WebUI's form rejects a null
    I18N, and a missing one is not saved.
    """
    form = {field: value for field, value in config.items() if field not in MFA_FIELDS}
    if form.get("I18N", _MISSING) is None:
        del form["I18N"]
    form.update(changes)
    return form


def main(argv: list[str] | None = None) -> dict[str, Any]:
    args = stack_api.argument_parser(__doc__, url="http://localhost:8080").parse_args(argv)
    wanted, fields = requested(stack_api.read_payload())
    api = Api.from_args(args, stack_api.secret("OPENWEBUI_API_KEY"))

    config = api.get(CONFIG)
    if not isinstance(config, dict):
        raise ScriptError(f"GET {CONFIG} did not return a JSON object")
    current = config.get(FIELD) or {}
    if not isinstance(current, dict):
        raise ScriptError(f"{FIELD} is not a JSON object: {json.dumps(current)[:200]}")
    unknown = sorted(field for field in fields if field not in config)
    if unknown:
        raise ScriptError(f"not admin config fields of this Open WebUI: {', '.join(unknown)}")
    # Ansible may deliver a templated boolean as "False"; a switch Open WebUI
    # stores as a boolean gets one.
    fields = {field: truthy(value) if isinstance(config.get(field), bool) else value for field, value in fields.items()}

    desired = {**current, **wanted}
    changes: dict[str, Any] = {}
    setting_changes = [
        f"{key}={json.dumps(value)}" for key, value in wanted.items() if current.get(key, _MISSING) != value
    ]
    if setting_changes:
        changes[FIELD] = desired
    field_changes = {field: value for field, value in fields.items() if config.get(field) != value}
    changes.update(field_changes)

    if changes:
        actions = []
        if setting_changes:
            actions.append("set default interface settings " + ", ".join(setting_changes))
        if field_changes:
            actions.append(
                "set admin config "
                + ", ".join(f"{field}={json.dumps(value)}" for field, value in field_changes.items())
            )
        saved = api.write("; ".join(actions), "POST", CONFIG, json_body=admin_form(config, changes))
        if not api.check:
            stored = saved if isinstance(saved, dict) else {}
            for field, value in changes.items():
                if stored.get(field) != value:
                    raise ScriptError(
                        f"POST {CONFIG} did not store {field}={json.dumps(value)}: {json.dumps(stored.get(field))}"
                    )
            if stored.get("sessions_revoked"):
                raise ScriptError(
                    f"the settings were saved, but Open WebUI reports that saving {CONFIG} signed every user out"
                )

    return api.report(settings=desired, config={**{field: config.get(field) for field in fields}, **field_changes})


if __name__ == "__main__":
    stack_api.run(main)
