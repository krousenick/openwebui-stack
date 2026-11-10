"""
title: Thinking Off State
description: Sends reasoning_effort none when the Thinking switch is off, so an off switch really stops the model thinking
required_open_webui_version: 0.13.0
version: 1.0.0
"""

from typing import Optional

from pydantic import BaseModel, Field


class Filter:
    # Off-state companion of reasoning_switch.py.
    #
    # Open WebUI runs a toggle filter only while its switch is on, so the
    # switch cannot act when it is off. This filter is not a toggle: it runs
    # on every request to the models it is attached to (meta.filterIds), and
    # when the switch's id is missing from the request's enabled filter ids
    # it sets reasoning_effort to the off value. With the switch on it does
    # nothing and the switch sets the level.
    #
    # Requests that never carry the switch (title/tag generation, direct
    # API calls through Open WebUI) therefore run without thinking unless
    # they send their own reasoning_effort, which is left untouched.
    class Valves(BaseModel):
        priority: int = Field(
            default=0,
            description="Filter order. Runs before the Thinking switch (priority 10).",
        )
        switch_id: str = Field(
            default="reasoning_switch",
            description="Function id of the Thinking switch this filter is the off state for.",
        )
        off_effort: str = Field(
            default="none",
            description="reasoning_effort sent while the switch is off.",
        )

    def __init__(self):
        self.valves = self.Valves()

    async def inlet(self, body: dict, __metadata__: Optional[dict] = None) -> dict:
        if body.get("reasoning_effort"):
            return body

        enabled_filter_ids = (__metadata__ or {}).get("filter_ids") or []
        if self.valves.switch_id in enabled_filter_ids:
            return body

        body["reasoning_effort"] = self.valves.off_effort
        return body
