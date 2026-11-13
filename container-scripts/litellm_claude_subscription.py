"""Keep LiteLLM's Claude subscription passthrough deployment.

Runs in the litellm container (tasks/claude_subscription.yml). With
--state present it makes sure LiteLLM holds exactly one `anthropic/*`
deployment without an API key, registered through the model API (stored
in LiteLLM's database: no config render, no restart). LiteLLM then
forwards the caller's own claude.ai sign-in token to Anthropic and logs
each request's tokens with used_client_oauth_token set. The caller is
Claude Code started with ANTHROPIC_BASE_URL at the proxy, no gateway
credential, and its LiteLLM key in x-litellm-api-key. With --state absent
the deployment is removed.

The deployment carries two model_info markers: stack_managed says this
script owns it, stack_hidden that no chat app can use it. LiteLLM's
/v1/models and /model/info expand the wildcard into one name per known
Claude model and copy model_info into every row, so each expanded name
carries stack_hidden. The reasoning discovery skips such rows, and
tasks/claude_subscription.yml hides the names in Open WebUI.

The deployment also sets disable_background_health_check: LiteLLM's
background health check has no caller token to forward and would log
"Missing Anthropic API Key" every run. A deployment an earlier version
registered gets fields it lacks through PATCH /model/{id}/update.

LiteLLM can answer from a worker that has not loaded a database change
yet, so after a write the script reads /model/info again until it shows
the change (--settle-attempts reads, --settle-seconds apart).

Environment: LITELLM_MASTER_KEY.
Report keys: deployment_ids (the managed deployments after the run) and
hidden_models (every model name /model/info marks stack_hidden, from any
deployment; with --check, the names as they are now).
"""

from __future__ import annotations

import time
from typing import Any

import stack_api
from stack_api import Api, ScriptError

MARKER = "claude-subscription"
DEPLOYMENT = {
    "model_name": "anthropic/*",
    "litellm_params": {"model": "anthropic/*"},
    "model_info": {
        "description": "Claude Code subscription passthrough: forwards the caller's own claude.ai sign-in, no API key",
        "stack_managed": MARKER,
        "stack_hidden": True,
        # The background health check (every 5 min) has no caller token to
        # forward, so it would log "Missing Anthropic API Key" for each probe.
        "disable_background_health_check": True,
    },
}


def model_rows(api: Api) -> list[dict[str, Any]]:
    data = (api.get("/model/info") or {}).get("data") or []
    return [row for row in data if isinstance(row, dict)]


def managed_ids(rows: list[dict[str, Any]]) -> list[str]:
    """The deployments this script owns. Expanded rows share their deployment's id."""
    ids = {
        (row.get("model_info") or {}).get("id")
        for row in rows
        if (row.get("model_info") or {}).get("stack_managed") == MARKER
    }
    return sorted(i for i in ids if isinstance(i, str) and i)


def stale_fields(rows: list[dict[str, Any]], deployment_id: str) -> list[str]:
    """The DEPLOYMENT model_info keys that deployment lacks or holds a different value for."""
    info = next(
        ((row.get("model_info") or {}) for row in rows if (row.get("model_info") or {}).get("id") == deployment_id),
        {},
    )
    return [key for key, value in DEPLOYMENT["model_info"].items() if info.get(key) != value]


def hidden_names(rows: list[dict[str, Any]]) -> list[str]:
    names = {row.get("model_name") for row in rows if (row.get("model_info") or {}).get("stack_hidden") is True}
    return sorted(name for name in names if isinstance(name, str) and name)


def settle(api: Api, args: Any, done: Any) -> list[dict[str, Any]]:
    """Read /model/info until done(rows) holds, or fail after the last attempt."""
    for attempt in range(max(1, args.settle_attempts)):
        rows = model_rows(api)
        if done(rows):
            return rows
        if attempt + 1 < args.settle_attempts:
            time.sleep(args.settle_seconds)
    raise ScriptError(
        f"/model/info did not show the change after {args.settle_attempts} reads; "
        "rerun once every LiteLLM worker has loaded the model database"
    )


def main(argv: list[str] | None = None) -> dict[str, Any]:
    parser = stack_api.argument_parser(__doc__, url="http://localhost:4000")
    parser.add_argument("--state", choices=("present", "absent"), required=True)
    parser.add_argument("--settle-attempts", type=int, default=15)
    parser.add_argument("--settle-seconds", type=float, default=2.0)
    args = parser.parse_args(argv)
    api = Api.from_args(args, stack_api.secret("LITELLM_MASTER_KEY"))

    rows = model_rows(api)
    owned = managed_ids(rows)
    if args.state == "present":
        if not owned:
            api.write(
                "register the Claude subscription passthrough (anthropic/*)", "POST", "/model/new", json_body=DEPLOYMENT
            )
        else:
            # An older deployment: add what this version sets. PATCH merges
            # model_info key by key and keeps the stored litellm_params.
            missing = stale_fields(rows, owned[0])
            if missing:
                api.write(
                    f"update the Claude subscription passthrough {owned[0]} ({', '.join(missing)})",
                    "PATCH",
                    f"/model/{owned[0]}/update",
                    json_body={"model_info": {key: DEPLOYMENT["model_info"][key] for key in missing}},
                )
        for duplicate in owned[1:]:
            api.write(
                f"remove duplicate passthrough deployment {duplicate}",
                "POST",
                "/model/delete",
                json_body={"id": duplicate},
            )
        if api.actions and not api.check:
            rows = settle(
                api, args, lambda now: len(managed_ids(now)) == 1 and not stale_fields(now, managed_ids(now)[0])
            )
    else:
        for deployment in owned:
            api.write(
                f"remove the Claude subscription passthrough {deployment}",
                "POST",
                "/model/delete",
                json_body={"id": deployment},
            )
        if api.actions and not api.check:
            rows = settle(api, args, lambda now: not managed_ids(now))

    return api.report(deployment_ids=managed_ids(rows), hidden_models=hidden_names(rows))


if __name__ == "__main__":
    stack_api.run(main)
