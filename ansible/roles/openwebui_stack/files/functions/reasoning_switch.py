"""
title: Thinking
description: Chat-input switch that turns model thinking on at the level picked in the switch's settings
required_open_webui_version: 0.13.0
version: 1.0.0
"""

from typing import Literal, Optional

from pydantic import BaseModel, Field


class Filter:
    # Reasoning selector for reasoning-capable models, the Open WebUI half
    # of the pair with reasoning_off.py.
    #
    # toggle = True makes Open WebUI list this filter as a switch in the
    # chat input (Integrations menu) for every model whose Workspace entry
    # carries it in meta.filterIds, and run its inlet only while the switch
    # is on for the chat (the request's filter_ids). UserValves render as
    # the switch's settings (the Valves button on its row), so each user
    # picks their own level from a dropdown.
    #
    # On: the request gets reasoning_effort = the user's level. Off: this
    # filter does not run at all; reasoning_off.py (always on for the same
    # models) then sends reasoning_effort "none". Without that companion an
    # off switch would fall through to the LiteLLM default effort and the
    # model would keep thinking.
    #
    # Precedence, highest first:
    #   1. Reasoning Effort in the Workspace model's Advanced Params
    #      (applied by the OpenAI router after every filter).
    #   2. Reasoning Effort in the chat's Controls > Advanced Params
    #      (already on the body when the inlet runs; left untouched).
    #   3. This switch.
    class Valves(BaseModel):
        priority: int = Field(
            default=10,
            description="Filter order. Runs after the off-state companion (priority 0).",
        )

    class UserValves(BaseModel):
        effort: Literal["low", "medium", "high"] = Field(
            default="medium",
            description="How hard the model thinks while the Thinking switch is on.",
        )

    def __init__(self):
        self.valves = self.Valves()
        self.toggle = True
        # Lightbulb outline. Open WebUI inverts data:image/svg icons in
        # dark mode, so the stroke is plain black.
        self.icon = (
            "data:image/svg+xml;base64,"
            "PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAyNCAy"
            "NCIgZmlsbD0ibm9uZSIgc3Ryb2tlPSIjMDAwIiBzdHJva2Utd2lkdGg9IjEuNzUiIHN0cm9rZS1s"
            "aW5lY2FwPSJyb3VuZCIgc3Ryb2tlLWxpbmVqb2luPSJyb3VuZCI+PHBhdGggZD0iTTkgMThoNiIv"
            "PjxwYXRoIGQ9Ik0xMCAyMWg0Ii8+PHBhdGggZD0iTTEyIDNhNiA2IDAgMCAwLTMuNSAxMC45Yy42"
            "LjUgMSAxLjIgMSAyVjE2aDV2LS4xYzAtLjguNC0xLjUgMS0yQTYgNiAwIDAgMCAxMiAzeiIvPjwv"
            "c3ZnPg=="
        )

    async def inlet(self, body: dict, __user__: Optional[dict] = None) -> dict:
        # An explicit Reasoning Effort from the chat's Advanced Params is
        # more specific than the switch, so it wins.
        if body.get("reasoning_effort"):
            return body

        effort = self.UserValves().effort
        user_valves = (__user__ or {}).get("valves")
        if user_valves is not None:
            effort = getattr(user_valves, "effort", effort) or effort

        body["reasoning_effort"] = effort
        return body
