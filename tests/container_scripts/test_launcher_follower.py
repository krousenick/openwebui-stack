"""launcher_follower.py: LiteLLM and Open WebUI follow the model a launcher serves."""

from __future__ import annotations

import copy
import json
import time
from typing import Any

import pytest

import launcher_follower
from fakes import TOKEN, FakeService, Request
from stack_api import Api, ScriptError

API_BASE = "http://192.0.2.20:8000/v1"
PORTAL = "launcher-node-1.example.test"
EFFORTS = ["none", "low", "medium", "high"]


class FakeGate(FakeService):
    """A node's oauth2-proxy gate with the launcher's GET /api/stack/slot behind it."""

    def __init__(self) -> None:
        super().__init__()
        self.slot: dict[str, Any] = {"version": 1, "node": "node-1", "port": 8000, "state": "empty"}
        self.sign_in_required = False

    def handle(self, request: Request) -> tuple[int, Any]:
        if self.sign_in_required:
            return 302, {"detail": "to the sign-in page"}
        if request.path != "/api/stack/slot" or request.headers.get("Host") != PORTAL:
            return 404, {"detail": "Not Found"}
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            return 401, {"detail": "A valid bearer token is required."}
        return 200, self.slot


def serving(name: str = "Qwen3-8B", **changes: Any) -> dict[str, Any]:
    entry = {
        "model_name": name,
        "api_base": API_BASE,
        "max_input_tokens": 32768,
        "max_tokens": 32768,
        "supports_reasoning": True,
        "reasoning_efforts": EFFORTS,
        "reasoning_effort": None,
        "repetition_detection": {"min_count": 4},
        "adaptive_router_preferences": None,
        "description": f"{name} - vLLM on node-1",
        **changes,
    }
    slot = {"version": 1, "node": "node-1", "port": 8000, "state": "serving", "repo_id": "Qwen/Qwen3-8B"}
    return {**slot, "litellm": entry}


def gemma_entry() -> dict[str, Any]:
    """The node's entry as playbooks/inference.yml registered it (with the key LiteLLM never shows)."""
    return {
        "model_name": "gemma-4-26b-a4b",
        "litellm_params": {
            "model": "openai/gemma-4-26b-a4b",
            "api_base": API_BASE,
            "api_key": "stored-node-key",
            "reasoning_effort": "medium",
            "repetition_detection": {"min_count": 4},
        },
        "model_info": {
            "id": "entry-1",
            "db_model": True,
            "max_input_tokens": 262144,
            "max_tokens": 262144,
            "supports_reasoning": True,
            "supports_none_reasoning_effort": True,
            "supports_low_reasoning_effort": True,
            "supports_medium_reasoning_effort": True,
            "supports_high_reasoning_effort": True,
            "adaptive_router_preferences": {"quality_tier": 2, "strengths": ["general"]},
        },
    }


@pytest.fixture
def gate():
    service = FakeGate()
    yield service
    service.close()


@pytest.fixture
def follow(gate, litellm, openwebui, monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setenv("LAUNCHER_STATUS_TOKEN", TOKEN)
    monkeypatch.setenv("LAUNCHER_FOLLOW_NODES", json.dumps([{"name": "node-1", "gate": gate.url, "host": PORTAL}]))
    monkeypatch.setattr(launcher_follower, "HEARTBEAT", tmp_path / "heartbeat")
    litellm.models = [gemma_entry(), {"model_name": "gemini-flash", "litellm_params": {"model": "gemini/x"}}]
    openwebui.functions = {
        "reasoning_switch": {"id": "reasoning_switch", "is_active": True, "is_global": False},
        "reasoning_off": {"id": "reasoning_off", "is_active": True, "is_global": False},
    }

    def run(*extra: str) -> dict[str, Any]:
        litellm.forget_requests()
        openwebui.forget_requests()
        args = ["--once", "--litellm-url", litellm.url, "--openwebui-url", openwebui.url, *extra]
        return launcher_follower.main(args)

    return run


def test_a_swap_renames_the_entry_and_keeps_its_endpoint_and_key(follow, gate, litellm):
    gate.slot = serving()
    report = follow()

    assert report["changed"] is True
    assert litellm.writes() == ["PATCH /model/entry-1/update"]
    entry = litellm.models[0]
    assert entry["model_name"] == "Qwen3-8B"
    params = entry["litellm_params"]
    assert params == {
        "model": "openai/Qwen3-8B",
        "api_base": API_BASE,
        "api_key": "stored-node-key",
        "repetition_detection": {"min_count": 4},
    }
    info = entry["model_info"]
    assert (info["max_input_tokens"], info["max_tokens"]) == (32768, 32768)
    assert info["supports_reasoning"] is True
    assert info["supports_none_reasoning_effort"] is True
    assert info["supports_minimal_reasoning_effort"] is False
    # Gemma's adaptive-router preferences do not describe the new model.
    assert info["adaptive_router_preferences"] == {}
    assert report["nodes"] == {"node-1": "serving Qwen3-8B; LiteLLM updated"}


def test_the_patch_drops_the_default_effort_through_litellm_params_to_remove(follow, gate, litellm):
    gate.slot = serving()
    follow()
    body = next(r.json() for r in litellm.requests if r.method == "PATCH")
    assert body["litellm_params_to_remove"] == ["reasoning_effort"]
    assert "reasoning_effort" not in body["litellm_params"]


def test_an_entry_that_already_matches_is_left_alone(follow, gate, litellm):
    gate.slot = serving()
    follow()
    report = follow()
    assert litellm.writes() == []
    assert report["changed"] is False
    assert report["nodes"] == {"node-1": "serving Qwen3-8B; LiteLLM up to date"}


def test_retuning_the_same_model_changes_only_its_context(follow, gate, litellm):
    gate.slot = serving()
    follow()
    gate.slot = serving(max_input_tokens=65536, max_tokens=65536)
    report = follow()
    assert litellm.writes() == ["PATCH /model/entry-1/update"]
    assert any("context length" in action for action in report["actions"])
    assert litellm.models[0]["model_info"]["max_input_tokens"] == 65536


@pytest.mark.parametrize("state", ["empty", "loading"])
def test_a_node_without_an_answering_model_leaves_litellm_alone(follow, gate, litellm, state):
    gate.slot = {"version": 1, "node": "node-1", "port": 8000, "state": state}
    report = follow()
    assert litellm.requests == []
    assert report["nodes"]["node-1"].startswith(state)


def test_a_gate_that_wants_a_sign_in_is_reported_not_followed(follow, gate, litellm):
    gate.sign_in_required = True
    report = follow()
    assert "sign-in" in report["nodes"]["node-1"]
    assert litellm.requests == []


def test_a_wrong_token_is_reported(follow, gate, monkeypatch: pytest.MonkeyPatch):
    gate.slot = serving()
    monkeypatch.setenv("LAUNCHER_STATUS_TOKEN", "wrong")
    assert follow()["nodes"]["node-1"] == "the slot status answered 401"


def test_a_missing_entry_is_reported_with_the_fix(follow, gate, litellm):
    gate.slot = serving()
    litellm.models = [{"model_name": "gemini-flash", "litellm_params": {"model": "gemini/x"}}]
    report = follow()
    assert "run playbooks/inference.yml" in report["nodes"]["node-1"]
    assert litellm.writes() == []


def test_check_writes_nothing(follow, gate, litellm):
    gate.slot = serving()
    report = follow("--check")
    assert report["check"] is True
    assert report["actions"] == [
        "point LiteLLM entry gemma-4-26b-a4b at Qwen3-8B (name, upstream model, context length, "
        "default effort, adaptive router)"
    ]
    assert litellm.writes() == []
    assert litellm.models[0]["model_name"] == "gemma-4-26b-a4b"


def long_running(gate, litellm, openwebui) -> launcher_follower.Follower:
    """A follower that keeps its state between passes, as the service loop does."""
    return launcher_follower.Follower(
        [launcher_follower.Node("node-1", gate.url, PORTAL)],
        Api(litellm.url, TOKEN),
        Api(openwebui.url, TOKEN),
        TOKEN,
        launcher_follower.SwitchOptions(),
        log=lambda _: None,
    )


def test_the_same_change_is_not_written_again_while_litellm_workers_reload(follow, gate, litellm, openwebui):
    gate.slot = serving()
    litellm.apply_patches = False
    follower = long_running(gate, litellm, openwebui)
    follower.run_once()
    follower.run_once()
    assert litellm.writes() == ["PATCH /model/entry-1/update"]


def test_the_thinking_switch_waits_until_open_webui_lists_the_new_name(follow, gate, litellm, openwebui):
    gate.slot = serving()
    openwebui.served = [{"id": "gemma-4-26b-a4b", "name": "gemma"}]
    follower = long_running(gate, litellm, openwebui)
    follower.run_once()
    assert "Qwen3-8B" not in openwebui.model_rows

    openwebui.served.append({"id": "Qwen3-8B", "name": "Qwen3-8B"})
    follower.run_once()
    row = openwebui.model_rows["Qwen3-8B"]
    assert row["meta"]["filterIds"] == ["reasoning_switch", "reasoning_off"]
    assert row["meta"]["defaultFilterIds"] == ["reasoning_switch"]


def test_a_model_that_does_not_reason_loses_the_switch(follow, gate, openwebui):
    gate.slot = serving("plain-model", supports_reasoning=False, reasoning_efforts=[])
    openwebui.served = [{"id": "plain-model", "name": "plain-model"}]
    openwebui.model_rows["plain-model"] = {
        "id": "plain-model",
        "name": "plain-model",
        "base_model_id": None,
        "meta": {
            "filterIds": ["own_filter", "reasoning_switch", "reasoning_off"],
            "defaultFilterIds": ["reasoning_switch"],
        },
        "params": {},
    }
    follow()
    meta = openwebui.model_rows["plain-model"]["meta"]
    assert meta["filterIds"] == ["own_filter"]
    assert meta["defaultFilterIds"] == []


def test_no_switch_is_attached_when_the_playbook_has_not_installed_it(follow, gate, openwebui):
    gate.slot = serving()
    openwebui.functions = {}
    openwebui.served = [{"id": "Qwen3-8B"}]
    follow()
    assert openwebui.model_rows == {}


def test_nodes_must_be_a_json_list(monkeypatch: pytest.MonkeyPatch):
    with pytest.raises(ScriptError, match="LAUNCHER_FOLLOW_NODES"):
        launcher_follower.parse_nodes("vllm-node-01=http://x")
    with pytest.raises(ScriptError, match="names no node"):
        launcher_follower.parse_nodes("[]")


def test_check_needs_once(monkeypatch: pytest.MonkeyPatch):
    with pytest.raises(ScriptError, match="--check only works with --once"):
        launcher_follower.main(["--check"])


def test_the_healthcheck_reads_the_heartbeat(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setattr(launcher_follower, "HEARTBEAT", tmp_path / "heartbeat")
    assert launcher_follower.healthy() is False
    launcher_follower.beat(30)
    assert launcher_follower.healthy() is True
    assert launcher_follower.healthy(now=time.time() + 121) is False


def test_patch_body_keeps_unnamed_params_and_names_every_effort_flag():
    entry = gemma_entry()
    body = launcher_follower.patch_body(copy.deepcopy(entry), serving("x", reasoning_efforts=["low"])["litellm"])
    flags = {key: value for key, value in body["model_info"].items() if key.endswith("_reasoning_effort")}
    assert flags == {f"supports_{level}_reasoning_effort": level == "low" for level in launcher_follower.EFFORT_LEVELS}
    assert "api_base" not in body["litellm_params"]
    assert "api_key" not in body["litellm_params"]
