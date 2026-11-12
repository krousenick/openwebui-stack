"""The Claude subscription passthrough (LiteLLM) and hiding its names (Open WebUI)."""

from __future__ import annotations

import pytest

import litellm_claude_subscription
import openwebui_hidden_models
from stack_api import ScriptError

FAST = ["--settle-attempts", "3", "--settle-seconds", "0"]
NAMES = ["anthropic/claude-haiku-9", "anthropic/claude-opus-9"]


def passthrough(litellm, state: str, *extra: str) -> dict:
    litellm.forget_requests()
    return litellm_claude_subscription.main(["--url", litellm.url, "--state", state, *FAST, *extra])


def vllm_model() -> dict:
    return {
        "model_name": "gemma",
        "litellm_params": {"model": "openai/gemma", "api_base": "http://node:8000/v1"},
        "model_info": {"id": "vllm-1", "supports_reasoning": True},
    }


@pytest.fixture
def hide(openwebui, stdin_payload):
    def run(models, *extra: str) -> dict:
        openwebui.forget_requests()
        stdin_payload({"models": models})
        return openwebui_hidden_models.main(["--url", openwebui.url, *extra])

    return run


# --- LiteLLM: the passthrough deployment -----------------------------------


def test_present_registers_one_keyless_hidden_wildcard(litellm):
    litellm.models = [vllm_model()]

    report = passthrough(litellm, "present")

    assert report["actions"] == ["register the Claude subscription passthrough (anthropic/*)"]
    (new,) = [request.json() for request in litellm.requests if request.path == "/model/new"]
    assert new == litellm_claude_subscription.DEPLOYMENT
    assert "api_key" not in new["litellm_params"]
    assert report["deployment_ids"] == ["deployment-1"]
    # The expanded names, from the marker LiteLLM copies into every row; gemma stays visible.
    assert report["hidden_models"] == NAMES


def test_rerun_writes_nothing(litellm):
    passthrough(litellm, "present")
    report = passthrough(litellm, "present")
    assert report["changed"] is False
    assert litellm.writes() == []
    assert report["hidden_models"] == NAMES


def test_absent_removes_the_deployment_and_unhides_its_names(litellm):
    passthrough(litellm, "present")
    report = passthrough(litellm, "absent")
    assert report["actions"] == ["remove the Claude subscription passthrough deployment-1"]
    assert report["deployment_ids"] == []
    assert report["hidden_models"] == []


def test_absent_without_a_deployment_writes_nothing(litellm):
    litellm.models = [vllm_model()]
    report = passthrough(litellm, "absent")
    assert report["changed"] is False
    assert litellm.writes() == []


def test_a_duplicate_deployment_is_removed(litellm):
    passthrough(litellm, "present")
    litellm.add_model(litellm_claude_subscription.DEPLOYMENT)
    report = passthrough(litellm, "present")
    assert report["actions"] == ["remove duplicate passthrough deployment deployment-2"]
    assert report["deployment_ids"] == ["deployment-1"]


def test_check_run_writes_nothing(litellm):
    report = passthrough(litellm, "present", "--check")
    assert report["check"] is True
    assert report["actions"] == ["register the Claude subscription passthrough (anthropic/*)"]
    assert litellm.writes() == []
    assert report["hidden_models"] == []


def test_hidden_models_cover_every_marked_row_and_only_those(litellm):
    litellm.models = [vllm_model(), {"model_name": "other", "model_info": {"id": "x", "stack_hidden": True}}]
    assert passthrough(litellm, "absent")["hidden_models"] == ["other"]


def test_a_deployment_that_never_shows_is_an_error(litellm):
    litellm.list_new_models = False
    with pytest.raises(ScriptError, match="did not show the change"):
        passthrough(litellm, "present")


# --- Open WebUI: hiding the names ------------------------------------------


def test_listed_names_get_inactive_marked_entries(openwebui, hide):
    report = hide(NAMES)

    assert report["actions"] == [f"hide {name}" for name in NAMES]
    assert report["hidden"] == NAMES
    row = openwebui.model_rows["anthropic/claude-opus-9"]
    assert row["is_active"] is False
    assert row["base_model_id"] is None
    assert row["meta"]["stackHidden"] is True


def test_hiding_again_writes_nothing(openwebui, hide):
    hide(NAMES)
    report = hide(NAMES)
    assert report["changed"] is False
    assert openwebui.writes() == []


def test_a_managed_entry_switched_back_on_is_hidden_again(openwebui, hide):
    hide(NAMES)
    openwebui.model_rows[NAMES[0]]["is_active"] = True
    report = hide(NAMES)
    assert report["actions"] == [f"hide {NAMES[0]} again"]
    assert openwebui.model_rows[NAMES[0]]["is_active"] is False


def test_a_name_that_leaves_the_list_shows_again(openwebui, hide):
    hide(NAMES)
    report = hide(NAMES[:1])
    assert report["actions"] == [f"show {NAMES[1]} again"]
    assert report["removed"] == [NAMES[1]]
    assert NAMES[1] not in openwebui.model_rows
    assert NAMES[0] in openwebui.model_rows


def test_an_admins_own_entry_is_left_alone(openwebui, hide):
    mine = {"id": NAMES[0], "name": "Mine", "base_model_id": None, "meta": {"description": "kept"}, "params": {}}
    openwebui.model_rows[NAMES[0]] = {**mine, "is_active": True}
    report = hide(NAMES)
    assert report["unmanaged"] == [NAMES[0]]
    assert report["hidden"] == [NAMES[1]]
    assert openwebui.model_rows[NAMES[0]] == {**mine, "is_active": True}


def test_hide_check_run_writes_nothing(openwebui, hide):
    report = hide(NAMES, "--check")
    assert report["check"] is True
    assert len(report["actions"]) == 2
    assert openwebui.writes() == []
    assert openwebui.model_rows == {}


@pytest.mark.parametrize("models", [None, "anthropic/x", [""], [1]])
def test_the_payload_needs_a_list_of_ids(openwebui, hide, models):
    with pytest.raises(ScriptError, match="models"):
        hide(models)
    assert openwebui.requests == []
