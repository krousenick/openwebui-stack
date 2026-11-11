"""Reasoning model discovery (LiteLLM) and the Thinking switch sync (Open WebUI)."""

from __future__ import annotations

import pytest

import litellm_reasoning_models
import openwebui_reasoning_sync
from stack_api import ScriptError

FUNCTIONS = [
    {"id": "reasoning_off", "name": "Thinking Off State", "description": "off state", "content": "# off v1"},
    {"id": "reasoning_switch", "name": "Thinking", "description": "the switch", "content": "# switch v1"},
]


def payload(**overrides) -> dict:
    return {
        "enabled": True,
        "default_on": True,
        "switch_id": "reasoning_switch",
        "off_id": "reasoning_off",
        "switch_models": ["gemma", "qwen"],
        "off_models": ["gemma"],
        "functions": FUNCTIONS,
        **overrides,
    }


@pytest.fixture
def sync(openwebui, stdin_payload):
    def run(*extra: str, **overrides) -> dict:
        openwebui.forget_requests()
        stdin_payload(payload(**overrides))
        return openwebui_reasoning_sync.main(["--url", openwebui.url, *extra])

    return run


@pytest.fixture
def workspace(openwebui):
    """gemma has a Workspace entry with its own filter; qwen is served but has no entry yet."""
    openwebui.served = [{"id": "gemma", "name": "Gemma"}, {"id": "qwen", "name": "Qwen"}, {"id": "plain"}]
    openwebui.model_rows["gemma"] = {
        "id": "gemma",
        "name": "Gemma",
        "base_model_id": None,
        "meta": {"filterIds": ["own_filter"], "profile_image_url": None, "description": "kept"},
        "params": {"temperature": 0.2, "model_controls": {"x": 1}},
    }
    return openwebui


def test_reasoning_models_are_read_from_model_info(litellm):
    litellm.models = [
        {"model_name": "gemma", "model_info": {"supports_reasoning": True, "supports_none_reasoning_effort": True}},
        {"model_name": "qwen", "model_info": {"supports_reasoning": True}},
        {"model_name": "qwen", "model_info": {"supports_reasoning": True}},
        {"model_name": "plain", "model_info": {"supports_reasoning": False}},
        {"model_name": "bare", "model_info": None},
    ]
    report = litellm_reasoning_models.main(["--url", litellm.url])
    assert report == {
        "changed": False,
        "check": False,
        "actions": [],
        "reasoning": ["gemma", "qwen"],
        "can_disable": ["gemma"],
        "routers": [],
    }


def router(name: str, **params) -> dict:
    """A router entry as /model/info returns it: no capability flags of its own."""
    return {
        "model_name": name,
        "model_info": {"litellm_provider": "auto_router"},
        "litellm_params": {"model": "auto_router/" + params.pop("kind", "complexity_router"), **params},
    }


def complexity_router(name: str, default: str, **tiers: str) -> dict:
    return router(name, complexity_router_default_model=default, complexity_router_config={"tiers": tiers})


def adaptive_router(name: str, *pool: str) -> dict:
    return router(name, kind="adaptive_router", adaptive_router_config={"available_models": list(pool)})


REASONING_MODELS = [
    {"model_name": "gemma", "model_info": {"supports_reasoning": True, "supports_none_reasoning_effort": True}},
    {"model_name": "qwen", "model_info": {"supports_reasoning": True, "supports_none_reasoning_effort": True}},
    {"model_name": "gemini", "model_info": {"supports_reasoning": True}},
    {"model_name": "plain", "model_info": {"supports_reasoning": False}},
]


def discover(litellm, *routers: dict) -> dict:
    litellm.models = [*REASONING_MODELS, *routers]
    return litellm_reasoning_models.main(["--url", litellm.url])


def test_a_router_reasons_when_every_model_it_routes_to_does(litellm):
    report = discover(
        litellm,
        complexity_router("auto", "gemini", SIMPLE="gemma", COMPLEX="gemini"),
        adaptive_router("adaptive", "gemma", "qwen"),
    )
    assert report["routers"] == ["adaptive", "auto"]
    assert report["reasoning"] == ["adaptive", "auto", "gemini", "gemma", "qwen"]
    # auto can land on gemini, which cannot switch thinking off; adaptive cannot.
    assert report["can_disable"] == ["adaptive", "gemma", "qwen"]


@pytest.mark.parametrize(
    "entry",
    [
        pytest.param(complexity_router("auto", "gemma", SIMPLE="plain"), id="a tier cannot reason"),
        pytest.param(complexity_router("auto", "plain", SIMPLE="gemma"), id="the default model cannot reason"),
        pytest.param(adaptive_router("auto", "gemma", "retired"), id="a pool model is not served"),
        pytest.param(adaptive_router("auto"), id="empty pool"),
        pytest.param(router("auto", kind="some_future_router"), id="no target list this script reads"),
        pytest.param(adaptive_router("auto", "gemma", "nested"), id="routes to another router"),
    ],
)
def test_a_router_is_left_out_unless_every_target_is_a_reasoning_model(litellm, entry):
    report = discover(litellm, entry, adaptive_router("nested", "gemma"))
    assert "auto" not in report["reasoning"]
    assert "auto" not in report["can_disable"]
    assert report["routers"] == ["nested"]


def test_a_router_config_delivered_as_a_json_string_is_read(litellm):
    entry = router("auto", complexity_router_default_model="gemma", complexity_router_config='{"tiers": {"A": "qwen"}}')
    assert discover(litellm, entry)["can_disable"] == ["auto", "gemma", "qwen"]


def test_first_run_installs_the_functions_and_attaches_them(workspace, sync):
    report = sync()

    assert report["attached"] == ["gemma", "qwen"]
    assert report["missing"] == []
    assert report["actions"] == [
        "create function reasoning_off",
        "enable function reasoning_off",
        "create function reasoning_switch",
        "enable function reasoning_switch",
        "attach to model entry gemma",
        "create model entry qwen",
    ]
    assert all(function["is_active"] and not function["is_global"] for function in workspace.functions.values())
    # gemma keeps its own filter and metadata; the unset field and model_controls are not sent back.
    gemma = workspace.model_rows["gemma"]
    assert gemma["meta"] == {
        "filterIds": ["own_filter", "reasoning_switch", "reasoning_off"],
        "defaultFilterIds": ["reasoning_switch"],
        "description": "kept",
    }
    assert gemma["params"] == {"temperature": 0.2}
    # qwen cannot switch thinking off, so it gets the switch alone.
    assert workspace.model_rows["qwen"] == {
        "id": "qwen",
        "base_model_id": None,
        "name": "Qwen",
        "meta": {"filterIds": ["reasoning_switch"], "defaultFilterIds": ["reasoning_switch"]},
        "params": {},
    }


def test_rerun_writes_nothing(workspace, sync):
    sync()
    report = sync()
    assert report["changed"] is False
    assert workspace.writes() == []


def test_changed_function_source_is_updated(workspace, sync):
    sync()
    newer = [FUNCTIONS[0], {**FUNCTIONS[1], "content": "# switch v2"}]
    report = sync(functions=newer)
    assert report["actions"] == ["update function reasoning_switch"]
    assert workspace.functions["reasoning_switch"]["content"] == "# switch v2"


def test_a_global_function_is_made_per_model_again(workspace, sync):
    sync()
    workspace.functions["reasoning_switch"]["is_global"] = True
    assert sync()["actions"] == ["unset global on function reasoning_switch"]
    assert workspace.functions["reasoning_switch"]["is_global"] is False


def test_a_model_that_stops_reasoning_loses_only_the_managed_filters(workspace, sync):
    sync()
    report = sync(switch_models=["qwen"], off_models=[])
    assert report["attached"] == ["qwen"]
    assert report["actions"] == ["detach from model entry gemma"]
    assert workspace.model_rows["gemma"]["meta"]["filterIds"] == ["own_filter"]
    assert workspace.model_rows["gemma"]["meta"]["defaultFilterIds"] == []


def test_unserved_models_and_presets_are_reported_missing_and_left_alone(workspace, sync):
    workspace.served.append({"id": "preset", "preset": True})
    workspace.model_rows["workspace-preset"] = {
        "id": "workspace-preset",
        "name": "Preset",
        "base_model_id": "gemma",
        "meta": {"filterIds": ["reasoning_switch"]},
        "params": {},
    }
    report = sync(switch_models=["gemma", "not-served", "preset"])
    assert report["attached"] == ["gemma"]
    assert report["missing"] == ["not-served", "preset"]
    assert workspace.model_rows["workspace-preset"]["meta"] == {"filterIds": ["reasoning_switch"]}


def test_default_off_leaves_the_switch_out_of_the_default_filters(workspace, sync):
    sync(default_on=False)
    assert workspace.model_rows["gemma"]["meta"]["defaultFilterIds"] == []


def test_disabling_detaches_first_and_then_disables_the_functions(workspace, sync):
    sync()
    report = sync(enabled="False")
    assert report["attached"] == []
    assert report["actions"] == [
        "detach from model entry gemma",
        "detach from model entry qwen",
        "disable function reasoning_off",
        "disable function reasoning_switch",
    ]
    assert not any(function["is_active"] for function in workspace.functions.values())


def test_check_run_reports_the_plan_and_writes_nothing(workspace, sync):
    report = sync("--check")
    assert report["check"] is True
    assert "create function reasoning_switch" in report["actions"]
    assert "create model entry qwen" in report["actions"]
    assert workspace.writes() == []
    assert workspace.functions == {}


def test_a_rejected_api_key_is_an_error(workspace, sync, monkeypatch):
    monkeypatch.setenv("OPENWEBUI_API_KEY", "wrong")
    with pytest.raises(ScriptError, match="admin API key rejected or Open WebUI unreachable"):
        sync()


def test_a_payload_without_the_function_ids_is_an_error(workspace, stdin_payload):
    stdin_payload({"functions": FUNCTIONS})
    with pytest.raises(ScriptError, match="no 'switch_id'"):
        openwebui_reasoning_sync.main(["--url", workspace.url])
