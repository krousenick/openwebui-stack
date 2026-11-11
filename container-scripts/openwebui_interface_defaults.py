"""Set keys of Open WebUI's default interface settings.

Runs in the open-webui container (tasks/openwebui_interface_defaults.yml).
The default interface settings (admin Settings > General, stored as
DEFAULT_INTERFACE_SETTINGS) are the starting values of every user's
Settings > Interface; a user's own choice still wins. Only the keys in the
payload are set. Every other key already in the defaults is kept.

The value is one field of the admin config, which Open WebUI saves as a
whole:

  GET  /api/v1/auths/admin/config  the admin config
  POST /api/v1/auths/admin/config  save it. The GET result is posted back
                                   with only DEFAULT_INTERFACE_SETTINGS
                                   changed, so every other admin setting
                                   keeps its value.

The three MFA fields are left out of the POST. Open WebUI refuses them from
an API key ("A human administrator session is required to change MFA
settings"), even unchanged, and a changed MFA policy signs every user out.
A field missing from the form is not saved, so the MFA policy stays as it
is. Open WebUI 0.13 reads the setting from its database on every request,
so it applies without a restart.

Input: base64 JSON on stdin, {"settings": {"<key>": <value>, ...}}.
Environment: OPENWEBUI_API_KEY (an admin API key).
Report keys: settings (the default interface settings after the run; with
--check, what a real run would store).
"""

from __future__ import annotations

import json
from typing import Any

import stack_api
from stack_api import Api, ScriptError

CONFIG = "/api/v1/auths/admin/config"
FIELD = "DEFAULT_INTERFACE_SETTINGS"
# Open WebUI's MfaConfigForm (open_webui/utils/mfa.py, MFA_CONFIG_KEYS).
MFA_FIELDS = ("ENABLE_MFA", "MFA_ALLOW_OAUTH_BYPASS", "MFA_ALLOW_TRUSTED_HEADER_BYPASS")
_MISSING = object()


def requested_settings(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the settings to enforce from the stdin payload."""
    settings = payload.get("settings")
    if not isinstance(settings, dict) or not settings:
        raise ScriptError('the payload needs a non-empty "settings" object')
    return settings


def admin_form(config: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    """Build the POST body: the stored admin config with the new defaults, minus the MFA fields.

    I18N is left out when it is null: Open WebUI's form rejects a null
    I18N, and a missing one is not saved.
    """
    form = {field: value for field, value in config.items() if field not in MFA_FIELDS}
    if form.get("I18N", _MISSING) is None:
        del form["I18N"]
    form[FIELD] = defaults
    return form


def main(argv: list[str] | None = None) -> dict[str, Any]:
    args = stack_api.argument_parser(__doc__, url="http://localhost:8080").parse_args(argv)
    wanted = requested_settings(stack_api.read_payload())
    api = Api.from_args(args, stack_api.secret("OPENWEBUI_API_KEY"))

    config = api.get(CONFIG)
    if not isinstance(config, dict):
        raise ScriptError(f"GET {CONFIG} did not return a JSON object")
    current = config.get(FIELD) or {}
    if not isinstance(current, dict):
        raise ScriptError(f"{FIELD} is not a JSON object: {json.dumps(current)[:200]}")

    desired = {**current, **wanted}
    changes = [f"{key}={json.dumps(value)}" for key, value in wanted.items() if current.get(key, _MISSING) != value]
    if changes:
        saved = api.write(
            "set default interface settings " + ", ".join(changes),
            "POST",
            CONFIG,
            json_body=admin_form(config, desired),
        )
        if not api.check:
            stored = saved.get(FIELD) if isinstance(saved, dict) else None
            if stored != desired:
                raise ScriptError(f"POST {CONFIG} did not store {FIELD}={json.dumps(desired)}: {json.dumps(stored)}")
            if saved.get("sessions_revoked"):
                raise ScriptError(
                    f"the defaults were saved, but Open WebUI reports that saving {CONFIG} signed every user out"
                )

    return api.report(settings=desired)


if __name__ == "__main__":
    stack_api.run(main)
