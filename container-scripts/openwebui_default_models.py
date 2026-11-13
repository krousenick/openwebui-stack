"""Set Open WebUI's default chat models (admin Settings > Models).

Runs in the open-webui container (tasks/openwebui_default_models.yml).
DEFAULT_MODELS in the compose environment only seeds a fresh database;
afterwards the stored value wins, so the setting goes through the admin
API, which persists it and updates the running app without a restart:

  GET  /api/v1/configs/models  current models config
  POST /api/v1/configs/models  replace it. The GET result is posted back
                               with only DEFAULT_MODELS changed, so pinned
                               models and the model order are preserved.
  GET  /api/models             what the chat picker offers, to report a
                               default that is not served

A default Open WebUI does not list yet is still saved: it starts working
the moment the model appears, and until then Open WebUI ignores it. A
model list that cannot be read never blocks the setting itself.

Environment: OPENWEBUI_API_KEY (an admin API key).
Report keys: default_models (the requested value) and missing (requested
models the chat picker does not offer).
"""

from __future__ import annotations

from typing import Any

import stack_api
from stack_api import Api, ScriptError

CONFIG = "/api/v1/configs/models"


def unserved_models(api: Api, models: str) -> list[str]:
    """Return the requested models the chat picker does not offer; empty when the list is unreadable."""
    status, listing = api.request("GET", "/api/models")
    if status != 200 or not isinstance(listing, dict):
        return []
    served = {model.get("id") for model in listing.get("data") or [] if isinstance(model, dict)}
    return [model for model in models.split(",") if model not in served]


def main(argv: list[str] | None = None) -> dict[str, Any]:
    parser = stack_api.argument_parser(__doc__, url="http://localhost:8080")
    parser.add_argument("--models", required=True, help="default model ids, comma-separated")
    args = parser.parse_args(argv)
    api = Api.from_args(args, stack_api.secret("OPENWEBUI_API_KEY"))

    missing = unserved_models(api, args.models)
    config = api.get(CONFIG)
    if not isinstance(config, dict):
        raise ScriptError(f"GET {CONFIG} did not return a JSON object")

    if (config.get("DEFAULT_MODELS") or "") != args.models:
        config["DEFAULT_MODELS"] = args.models
        saved = api.write(f"set default models to {args.models}", "POST", CONFIG, json_body=config)
        if not api.check and (not isinstance(saved, dict) or saved.get("DEFAULT_MODELS") != args.models):
            raise ScriptError(f"POST {CONFIG} did not store DEFAULT_MODELS={args.models}: {saved}")

    return api.report(default_models=args.models, missing=missing)


if __name__ == "__main__":
    stack_api.run(main)
