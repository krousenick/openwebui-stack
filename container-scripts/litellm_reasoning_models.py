"""List the reasoning-capable models LiteLLM serves.

Runs in the litellm container (tasks/openwebui_reasoning.yml) and reads
GET /model/info. Read-only: the report never has changes.

Environment: LITELLM_MASTER_KEY.
Report keys: reasoning (model names whose model_info.supports_reasoning is
true) and can_disable (the subset that also sets
supports_none_reasoning_effort, i.e. accepts reasoning_effort "none").
"""

from __future__ import annotations

from typing import Any

import stack_api
from stack_api import Api


def main(argv: list[str] | None = None) -> dict[str, Any]:
    args = stack_api.argument_parser(__doc__, url="http://localhost:4000").parse_args(argv)
    api = Api.from_args(args, stack_api.secret("LITELLM_MASTER_KEY"))

    reasoning, can_disable = set(), set()
    for entry in (api.get("/model/info") or {}).get("data") or []:
        name, info = entry.get("model_name"), entry.get("model_info") or {}
        if not name or info.get("supports_reasoning") is not True:
            continue
        reasoning.add(name)
        if info.get("supports_none_reasoning_effort") is True:
            can_disable.add(name)

    return api.report(reasoning=sorted(reasoning), can_disable=sorted(can_disable))


if __name__ == "__main__":
    stack_api.run(main)
