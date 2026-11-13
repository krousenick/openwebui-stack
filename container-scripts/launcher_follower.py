"""Keep LiteLLM and Open WebUI in step with the model each LLM Launcher serves.

Runs as the launcher-follower service (docker-compose.yml, rendered when
launcher_enabled and launcher_follow_enabled) on the litellm image's Python.
A compute node whose launcher runs the single-model slot (vllm_launcher_swap
in the vllm_compute role) serves one model at a time on its primary port,
and a swap or a retune in the launcher GUI changes what that port serves.
Every --interval seconds, for each node in LAUNCHER_FOLLOW_NODES:

  1. GET <gate>/api/stack/slot with LAUNCHER_STATUS_TOKEN: what the node
     serves and how LiteLLM should list it (stack_slot.py on the node).
  2. While a model answers there and LiteLLM's entry for that endpoint
     (matched by api_base) lists it differently, PATCH /model/{id}/update:
     the name, context length, reasoning flags, default effort, repetition
     detection and adaptive-router preferences. The entry keeps its id, its
     api_base and its stored API key; playbooks/inference.yml creates it.
  3. Once Open WebUI lists the model under that name, its base-model entry
     gets the Thinking switch when the model reasons, plus the off-state
     companion when it accepts reasoning_effort "none", or loses both when
     it does not: the filter lists openwebui_reasoning_sync.py writes.
     Nothing else in Open WebUI changes.

A node that does not answer, is still loading, or serves nothing leaves
LiteLLM as it is; requests to it fail until a model answers again.

--once runs one pass and prints the usual JSON report (with --check it
writes nothing). --healthcheck exits 0 while the loop's heartbeat is fresh.

Environment: LITELLM_MASTER_KEY, OPENWEBUI_API_KEY (an admin API key),
LAUNCHER_STATUS_TOKEN, and LAUNCHER_FOLLOW_NODES: a JSON list of
{"name", "gate", "host"}, where gate is the node's oauth2-proxy URL and
host the portal name its launcher accepts as Host.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

import openwebui_reasoning_sync as thinking
import stack_api
from stack_api import Api, ScriptError, truthy

EFFORT_LEVELS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
# litellm_params keys the slot may want gone; PATCH drops them by name.
REMOVABLE_PARAMS = ("reasoning_effort", "repetition_detection")
HEARTBEAT = Path(os.environ.get("LAUNCHER_FOLLOW_HEARTBEAT", "/tmp/launcher-follower.heartbeat"))
# After a PATCH, LiteLLM workers that have not reloaded from the database yet
# still answer /model/info with the old entry: the same change is not written
# again within this many seconds.
SETTLE_SECONDS = 180.0
# Passes to wait for Open WebUI to list a renamed model before the Thinking
# switch step gives up (it is retried when the served model changes again).
SWITCH_PATIENCE = 20


@dataclasses.dataclass(frozen=True)
class Node:
    name: str
    gate: str
    host: str


@dataclasses.dataclass(frozen=True)
class SwitchOptions:
    enabled: bool = True
    default_on: bool = True
    switch_id: str = "reasoning_switch"
    off_id: str = "reasoning_off"
    exclude: frozenset[str] = frozenset()


@dataclasses.dataclass
class NodeState:
    note: str = ""
    switch_done: str = ""
    switch_pending: dict[str, Any] | None = None
    switch_tries: int = 0
    patched: dict[str, tuple[str, float]] = dataclasses.field(default_factory=dict)


def parse_nodes(raw: str) -> list[Node]:
    try:
        nodes = [
            Node(str(entry["name"]), str(entry["gate"]).rstrip("/"), str(entry.get("host") or ""))
            for entry in json.loads(raw)
        ]
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        raise ScriptError(f"LAUNCHER_FOLLOW_NODES is not a JSON list of {{name, gate, host}}: {error!r}") from error
    if not nodes:
        raise ScriptError("LAUNCHER_FOLLOW_NODES names no node")
    return nodes


def _gate_opener() -> urllib.request.OpenerDirector:
    """An opener for the gates: plain HTTP only, and redirects are not followed.

    A gate without the slot route sends the follower to the sign-in page;
    that is reported, not followed. No HTTPS handler: urllib builds a TLS
    context for one up front, which a FIPS host can refuse even when every
    request is plain HTTP.
    """
    opener = urllib.request.OpenerDirector()
    for handler in (
        urllib.request.HTTPHandler(),
        urllib.request.HTTPDefaultErrorHandler(),
        urllib.request.HTTPErrorProcessor(),
    ):
        opener.add_handler(handler)
    return opener


_OPENER = _gate_opener()


def read_slot(node: Node, token: str, timeout: float) -> tuple[str, dict[str, Any] | None]:
    """Return ("", slot document), or (what went wrong, None)."""
    request = urllib.request.Request(f"{node.gate}/api/stack/slot")
    request.add_header("Authorization", f"Bearer {token}")
    if node.host:
        # The launcher only answers to the names it was started with.
        request.add_header("Host", node.host)
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            document = json.loads(response.read())
    except urllib.error.HTTPError as error:
        if 300 <= error.code < 400:
            return "the gate wants a sign-in for the slot status: is vllm_launcher_swap on for this node?", None
        return f"the slot status answered {error.code}", None
    except (OSError, ValueError) as error:
        return f"the gate does not answer: {error}", None
    if not isinstance(document, dict) or "state" not in document:
        return "the slot status is not a slot document", None
    return "", document


def litellm_targets(entries: list[Any], api_base: str) -> list[dict[str, Any]]:
    """The database entries LiteLLM routes to this endpoint (config entries cannot be patched)."""
    return [
        entry
        for entry in entries
        if isinstance(entry, dict)
        and (entry.get("litellm_params") or {}).get("api_base") == api_base
        and truthy((entry.get("model_info") or {}).get("db_model"))
    ]


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def differences(entry: dict[str, Any], want: dict[str, Any]) -> list[str]:
    """What the entry lists differently from what the node serves, in words for the log."""
    params = entry.get("litellm_params") or {}
    info = entry.get("model_info") or {}
    name = want["model_name"]
    found = []
    if entry.get("model_name") != name:
        found.append("name")
    if params.get("model") != f"openai/{name}":
        found.append("upstream model")
    tokens = _int(want.get("max_input_tokens"))
    if tokens is not None and (
        _int(info.get("max_input_tokens")) != tokens
        or _int(info.get("max_tokens")) != _int(want.get("max_tokens") or tokens)
    ):
        found.append("context length")
    if truthy(info.get("supports_reasoning")) != bool(want.get("supports_reasoning")):
        found.append("reasoning")
    offered = sorted(level for level in EFFORT_LEVELS if truthy(info.get(f"supports_{level}_reasoning_effort")))
    if offered != sorted(set(want.get("reasoning_efforts") or [])):
        found.append("effort levels")
    if (params.get("reasoning_effort") or None) != (want.get("reasoning_effort") or None):
        found.append("default effort")
    if (params.get("repetition_detection") or None) != (want.get("repetition_detection") or None):
        found.append("repetition detection")
    if (info.get("adaptive_router_preferences") or None) != (want.get("adaptive_router_preferences") or None):
        found.append("adaptive router")
    return found


def patch_body(entry: dict[str, Any], want: dict[str, Any]) -> dict[str, Any]:
    """The PATCH /model/{id}/update body that makes the entry list what the node serves.

    PATCH merges: litellm_params it does not name keep their stored values
    (api_base, the encrypted api_key), and a key is dropped only through
    litellm_params_to_remove. model_info keys merge the same way and cannot
    be dropped, so every effort flag is sent as true or false, and stale
    adaptive-router preferences are emptied.
    """
    name = want["model_name"]
    params = entry.get("litellm_params") or {}
    info = entry.get("model_info") or {}
    new_params: dict[str, Any] = {"model": f"openai/{name}"}
    remove = []
    for key in REMOVABLE_PARAMS:
        if want.get(key):
            new_params[key] = want[key]
        elif key in params:
            remove.append(key)
    efforts = set(want.get("reasoning_efforts") or [])
    new_info: dict[str, Any] = {
        "id": info.get("id"),
        "description": want.get("description") or name,
        "supports_reasoning": bool(want.get("supports_reasoning")),
        **{f"supports_{level}_reasoning_effort": level in efforts for level in EFFORT_LEVELS},
    }
    tokens = _int(want.get("max_input_tokens"))
    if tokens is not None:
        new_info["max_input_tokens"] = tokens
        new_info["max_tokens"] = _int(want.get("max_tokens")) or tokens
    if want.get("adaptive_router_preferences") or info.get("adaptive_router_preferences"):
        new_info["adaptive_router_preferences"] = want.get("adaptive_router_preferences") or {}
    body: dict[str, Any] = {"model_name": name, "litellm_params": new_params, "model_info": new_info}
    if remove:
        body["litellm_params_to_remove"] = remove
    return body


def function_active(api: Api, function_id: str) -> bool:
    status, body = api.request("GET", f"/api/v1/functions/id/{function_id}")
    return status == 200 and isinstance(body, dict) and body.get("id") == function_id and truthy(body.get("is_active"))


def sync_switch(api: Api, want: dict[str, Any], options: SwitchOptions) -> str:
    """Attach or detach the Thinking switch on one model's base entry: "done", "waiting" or "skipped"."""
    model_id = want["model_name"]
    if not options.enabled or model_id in options.exclude:
        return "skipped"
    if not function_active(api, options.switch_id):
        # The playbook has not installed the switch (openwebui_reasoning.yml).
        return "skipped"
    listing = api.get("/api/models")
    served = {
        model.get("id"): model
        for model in (listing or {}).get("data") or []
        if isinstance(model, dict) and model.get("id")
    }
    model = served.get(model_id)
    if model is None:
        return "waiting"
    if model.get("preset"):
        return "skipped"
    rows = api.get(f"{thinking.MODELS_API}/all")
    row = (
        next((r for r in rows if isinstance(r, dict) and r.get("id") == model_id), None)
        if isinstance(rows, list)
        else None
    )
    if row is not None and row.get("base_model_id"):
        return "skipped"

    filters: list[str] = []
    if want.get("supports_reasoning"):
        filters = [options.switch_id]
        if "none" in (want.get("reasoning_efforts") or []) and function_active(api, options.off_id):
            filters.append(options.off_id)
    settings = thinking.Settings(
        enabled=True,
        default_on=options.default_on,
        switch_id=options.switch_id,
        off_id=options.off_id,
        switch_models=[],
        off_models=frozenset(),
        functions=[],
    )
    if row is None:
        if filters:
            filter_ids, default_ids = settings.filter_lists({}, filters)
            form = {
                "id": model_id,
                "base_model_id": None,
                "name": model.get("name") or model_id,
                "meta": {"filterIds": filter_ids, "defaultFilterIds": default_ids},
                "params": {},
            }
            api.write(
                f"create model entry {model_id} with the Thinking switch",
                "POST",
                f"{thinking.MODELS_API}/create",
                json_body=form,
            )
        return "done"
    filter_ids, default_ids = settings.filter_lists(row.get("meta") or {}, filters)
    action = "attach the Thinking switch to" if filters else "detach the Thinking switch from"
    thinking.update_entry(api, row, filter_ids, default_ids, f"{action} model entry {model_id}")
    return "done"


class Follower:
    def __init__(
        self,
        nodes: list[Node],
        litellm: Api,
        openwebui: Api,
        token: str,
        options: SwitchOptions,
        *,
        timeout: float = 15.0,
        clock: Callable[[], float] = time.monotonic,
        log: Callable[[str], None] | None = None,
    ):
        self.nodes = nodes
        self.litellm = litellm
        self.openwebui = openwebui
        self.token = token
        self.options = options
        self.timeout = timeout
        self.clock = clock
        self.log = log or (lambda message: print(message, flush=True))
        self.states = {node.name: NodeState() for node in nodes}

    def say(self, node: Node, note: str) -> None:
        """Log a node's state when it changes, so a steady node logs nothing."""
        state = self.states[node.name]
        if state.note != note:
            state.note = note
            self.log(f"{node.name}: {note}")

    def run_once(self) -> dict[str, str]:
        notes = {}
        cache: dict[str, Any] = {}
        for node in self.nodes:
            try:
                notes[node.name] = self.follow(node, cache)
            except ScriptError as error:
                notes[node.name] = f"error: {error}"
                self.say(node, notes[node.name])
        return notes

    def follow(self, node: Node, cache: dict[str, Any]) -> str:
        problem, slot = read_slot(node, self.token, self.timeout)
        if slot is None:
            self.say(node, problem)
            return problem
        want = slot.get("litellm")
        if slot.get("state") != "serving" or not isinstance(want, dict) or not want.get("model_name"):
            note = f"{slot.get('state') or 'unknown state'}"
            if slot.get("repo_id"):
                note += f" {slot['repo_id']}"
            note += "; LiteLLM left as it is"
            self.say(node, note)
            return note

        if "entries" not in cache:
            cache["entries"] = (self.litellm.get("/model/info") or {}).get("data") or []
        targets = litellm_targets(cache["entries"], str(want.get("api_base") or ""))
        if not targets:
            note = (
                f"serving {want['model_name']}, but LiteLLM has no entry for {want.get('api_base')}: "
                "run playbooks/inference.yml once to create it"
            )
            self.say(node, note)
            return note

        state = self.states[node.name]
        fingerprint = json.dumps(want, sort_keys=True)
        updated = False
        for entry in targets:
            diffs = differences(entry, want)
            entry_id = str((entry.get("model_info") or {}).get("id") or "")
            if not diffs or not entry_id:
                continue
            recent = state.patched.get(entry_id)
            if recent and recent[0] == fingerprint and self.clock() - recent[1] < SETTLE_SECONDS:
                continue
            self.litellm.write(
                f"point LiteLLM entry {entry.get('model_name')} at {want['model_name']} ({', '.join(diffs)})",
                "PATCH",
                f"/model/{urllib.parse.quote(entry_id, safe='')}/update",
                json_body=patch_body(entry, want),
            )
            state.patched[entry_id] = (fingerprint, self.clock())
            updated = True

        if state.switch_done != fingerprint and (state.switch_pending is None or updated):
            state.switch_pending = want
            state.switch_tries = 0
        if state.switch_pending is not None:
            result = sync_switch(self.openwebui, state.switch_pending, self.options)
            if result == "waiting":
                state.switch_tries += 1
                if state.switch_tries >= SWITCH_PATIENCE:
                    self.log(
                        f"{node.name}: Open WebUI still does not list {want['model_name']}; "
                        "the Thinking switch was not updated for it"
                    )
                    state.switch_pending = None
                    state.switch_done = fingerprint
            else:
                state.switch_pending = None
                state.switch_done = fingerprint

        outcome = ("would be updated" if self.litellm.check else "updated") if updated else "up to date"
        note = f"serving {want['model_name']}; LiteLLM {outcome}"
        self.say(node, note)
        return note

    def report(self, notes: dict[str, str], *, check: bool) -> dict[str, Any]:
        actions = self.litellm.actions + self.openwebui.actions
        return {"changed": bool(actions), "check": check, "actions": actions, "nodes": notes}

    def loop(self, interval: float) -> None:
        self.log(f"following {', '.join(node.name for node in self.nodes)} every {interval:.0f} s")
        while True:
            self.litellm.actions.clear()
            self.openwebui.actions.clear()
            try:
                self.run_once()
            except Exception as error:  # noqa: BLE001 - the service must outlive one bad pass
                self.log(f"pass failed: {error!r}")
            for action in self.litellm.actions + self.openwebui.actions:
                self.log(f"wrote: {action}")
            beat(interval)
            time.sleep(interval)


def beat(interval: float) -> None:
    with contextlib.suppress(OSError):
        HEARTBEAT.write_text(json.dumps({"at": time.time(), "interval": interval}), encoding="utf-8")


def healthy(now: float | None = None) -> bool:
    """True while the loop wrote its heartbeat within three intervals (at least two minutes)."""
    try:
        beat_data = json.loads(HEARTBEAT.read_text(encoding="utf-8"))
        age = (now if now is not None else time.time()) - float(beat_data["at"])
        return age < max(3 * float(beat_data.get("interval") or 30), 120)
    except (OSError, ValueError, KeyError, TypeError):
        return False


def argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--litellm-url", default="http://litellm:4000", help="LiteLLM admin API (%(default)s)")
    parser.add_argument("--openwebui-url", default="http://open-webui:8080", help="Open WebUI API (%(default)s)")
    parser.add_argument("--interval", type=float, default=30.0, help="seconds between passes (%(default)s)")
    parser.add_argument("--timeout", type=float, default=15.0, help="seconds to wait for each answer (%(default)s)")
    parser.add_argument("--once", action="store_true", help="run one pass, print the JSON report and exit")
    parser.add_argument("--check", action="store_true", help="with --once: write nothing; report what would change")
    parser.add_argument("--healthcheck", action="store_true", help="exit 0 while the loop's heartbeat is fresh")
    parser.add_argument("--no-thinking-switch", action="store_true", help="leave the Open WebUI Thinking switch alone")
    parser.add_argument("--thinking-default-off", action="store_true", help="new chats start with the switch off")
    parser.add_argument("--thinking-exclude", default="", help="comma-separated models that never get the switch")
    return parser


def main(argv: list[str] | None = None) -> dict[str, Any]:
    args = argument_parser().parse_args(argv)
    if args.check and not args.once:
        raise ScriptError("--check only works with --once")
    nodes = parse_nodes(os.environ.get("LAUNCHER_FOLLOW_NODES", ""))
    token = stack_api.secret("LAUNCHER_STATUS_TOKEN")
    litellm = Api(args.litellm_url, stack_api.secret("LITELLM_MASTER_KEY"), check=args.check, timeout=args.timeout)
    openwebui = Api(args.openwebui_url, stack_api.secret("OPENWEBUI_API_KEY"), check=args.check, timeout=args.timeout)
    options = SwitchOptions(
        enabled=not args.no_thinking_switch,
        default_on=not args.thinking_default_off,
        exclude=frozenset(name.strip() for name in args.thinking_exclude.split(",") if name.strip()),
    )
    follower = Follower(nodes, litellm, openwebui, token, options, timeout=args.timeout)
    if args.once:
        return follower.report(follower.run_once(), check=args.check)
    follower.loop(args.interval)
    return follower.report({}, check=False)


if __name__ == "__main__":
    if "--healthcheck" in sys.argv[1:]:
        sys.exit(0 if healthy() else 1)
    stack_api.run(main)
