"""List the reasoning-capable models LiteLLM serves.

Runs in the litellm container (tasks/openwebui_reasoning.yml) and reads
GET /model/info. Read-only: the report never has changes.

A model is reasoning-capable when its model_info.supports_reasoning is
true. A router entry (litellm_params.model "auto_router/...", such as
`auto` and `adaptive`) carries no capability flags of its own, so it takes
them from the models it can route to: it is reasoning-capable when every
one of them is, and it accepts reasoning_effort "none" when every one of
them does. A router with a target that cannot reason, a target that is not
served, or no readable target list is left out.

Rows whose model_info.stack_hidden is true are skipped: no chat app can call
them (the Claude subscription passthrough's names), so they get no switch.

Environment: LITELLM_MASTER_KEY.
Report keys: reasoning (model names, routers included), can_disable (the
subset that accepts reasoning_effort "none", from
supports_none_reasoning_effort) and routers (the entries of reasoning that
are routers).
"""

from __future__ import annotations

import json
from typing import Any

import stack_api
from stack_api import Api

ROUTER_PREFIX = "auto_router/"


def _mapping(value: Any) -> dict[str, Any]:
    """Return a config block as a dict; LiteLLM also accepts it as a JSON string."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def router_targets(params: dict[str, Any]) -> set[str] | None:
    """Return the model names a router entry can route to, or None for a plain model.

    Complexity router: every tier's model and the default model. Adaptive
    router: the available_models pool.
    """
    if not str(params.get("model") or "").startswith(ROUTER_PREFIX):
        return None
    names: list[Any] = [params.get("complexity_router_default_model")]
    tiers = _mapping(params.get("complexity_router_config")).get("tiers")
    if isinstance(tiers, dict):
        names.extend(tiers.values())
    pool = _mapping(params.get("adaptive_router_config")).get("available_models")
    if isinstance(pool, list):
        names.extend(pool)
    return {name for name in names if isinstance(name, str) and name}


def main(argv: list[str] | None = None) -> dict[str, Any]:
    args = stack_api.argument_parser(__doc__, url="http://localhost:4000").parse_args(argv)
    api = Api.from_args(args, stack_api.secret("LITELLM_MASTER_KEY"))

    reasoning: set[str] = set()
    can_disable: set[str] = set()
    router_pools: dict[str, set[str]] = {}
    for entry in (api.get("/model/info") or {}).get("data") or []:
        name, info = entry.get("model_name"), entry.get("model_info") or {}
        # Not for chat apps (the Claude subscription passthrough's names, see
        # litellm_claude_subscription.py): no switch, and not a router target.
        if not name or info.get("stack_hidden") is True:
            continue
        targets = router_targets(entry.get("litellm_params") or {})
        if targets is not None:
            router_pools.setdefault(name, set()).update(targets)
            continue
        if info.get("supports_reasoning") is not True:
            continue
        reasoning.add(name)
        if info.get("supports_none_reasoning_effort") is True:
            can_disable.add(name)

    # Judged against the plain models only, so a router that routes to
    # another router is left out rather than guessed at.
    routers = sorted(name for name, targets in router_pools.items() if targets and targets <= reasoning)
    routers_can_disable = [name for name in routers if router_pools[name] <= can_disable]

    return api.report(
        reasoning=sorted(reasoning.union(routers)),
        can_disable=sorted(can_disable.union(routers_can_disable)),
        routers=routers,
    )


if __name__ == "__main__":
    stack_api.run(main)
