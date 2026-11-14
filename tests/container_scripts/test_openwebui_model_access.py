"""Sharing the chat models LiteLLM serves with every Open WebUI user (openwebui_model_access.py)."""

from __future__ import annotations

from typing import Any

import pytest

import openwebui_model_access
from stack_api import ScriptError

PUBLIC = {"principal_type": "user", "principal_id": "*", "permission": "read"}
GROUP = {"principal_type": "group", "principal_id": "group-friends", "permission": "read"}


@pytest.fixture
def share(openwebui):
    def run(*extra: str) -> dict:
        openwebui.forget_requests()
        return openwebui_model_access.main(["--url", openwebui.url, *extra])

    return run


def served(model_id: str, info: dict[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
    """A model as Open WebUI's /api/models lists it from the OpenAI-compatible connection."""
    model = {"id": model_id, "name": model_id.title(), "owned_by": "openai", "connection_type": "external", **extra}
    if info is not None:
        model["info"] = info
    return model


def entry(model_id: str, *grants: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "id": model_id,
        "base_model_id": None,
        "name": model_id,
        "meta": {"filterIds": ["reasoning_switch"]},
        "params": {},
        "is_active": True,
        "access_grants": [dict(grant, id=f"stored-{n}") for n, grant in enumerate(grants)],
        **extra,
    }


def grants_of(openwebui, model_id: str) -> list[dict[str, Any]]:
    return [
        {key: grant[key] for key in ("principal_type", "principal_id", "permission")}
        for grant in openwebui.model_rows[model_id]["access_grants"]
    ]


def test_served_base_models_get_the_public_grant(openwebui, share) -> None:
    openwebui.model_rows["gemma"] = entry("gemma")
    openwebui.served = [served("gemma", info=openwebui.model_rows["gemma"]), served("qwen")]

    report = share()

    assert report["shared"] == ["gemma", "qwen"]
    assert report["actions"] == ["share gemma with every user", "share qwen with every user"]
    assert grants_of(openwebui, "gemma") == [PUBLIC]
    assert openwebui.model_rows["gemma"]["meta"] == {"filterIds": ["reasoning_switch"]}, "settings untouched"
    assert openwebui.model_rows["qwen"]["name"] == "Qwen", "a model without an entry gets a plain one"
    assert grants_of(openwebui, "qwen") == [PUBLIC]


def test_other_grants_are_kept(openwebui, share) -> None:
    openwebui.model_rows["gemma"] = entry("gemma", GROUP)
    openwebui.served = [served("gemma", info=openwebui.model_rows["gemma"])]

    share()

    assert grants_of(openwebui, "gemma") == [GROUP, PUBLIC]


def test_a_public_model_needs_no_write(openwebui, share) -> None:
    openwebui.model_rows["gemma"] = entry("gemma", GROUP, PUBLIC)
    openwebui.served = [served("gemma", info=openwebui.model_rows["gemma"])]

    report = share()

    assert report["actions"] == [] and report["changed"] is False
    assert report["public"] == ["gemma"]
    assert grants_of(openwebui, "gemma") == [GROUP, PUBLIC]


def test_a_second_run_writes_nothing(openwebui, share) -> None:
    openwebui.served = [served("gemma")]
    share()
    openwebui.served = [served("gemma", info=openwebui.model_rows["gemma"])]

    report = share()

    assert report["actions"] == [] and report["public"] == ["gemma"]


def test_only_base_models_from_the_connection_are_shared(openwebui, share) -> None:
    openwebui.model_rows["my-preset"] = entry("my-preset", base_model_id="gemma")
    openwebui.served = [
        served("my-preset", info=openwebui.model_rows["my-preset"]),
        served("arena-model", arena=True),
        served("my-pipe", pipe={"type": "pipe"}),
        served("llama3", owned_by="ollama"),
        {"name": "no id"},
        "not a model",
        served("kept-private"),
        served("gemma"),
    ]

    report = share("--exclude", " kept-private , ,")

    assert report["shared"] == ["gemma"]
    assert report["excluded"] == ["kept-private"]
    assert set(openwebui.model_rows) == {"my-preset", "gemma"}
    assert grants_of(openwebui, "my-preset") == [], "a preset's owner decides who sees it"


def test_hidden_entries_stay_hidden(openwebui, share) -> None:
    # Open WebUI leaves inactive entries out of /api/models, so they never come up.
    openwebui.model_rows["claude-opus-9"] = entry("claude-opus-9", is_active=False, meta={"stackHidden": True})
    openwebui.served = [served("gemma")]

    share()

    assert grants_of(openwebui, "claude-opus-9") == []


def test_check_run_writes_nothing(openwebui, share) -> None:
    openwebui.served = [served("gemma")]

    report = share("--check")

    assert report["check"] is True and report["changed"] is True
    assert report["actions"] == ["share gemma with every user"]
    assert report["shared"] == ["gemma"]
    assert openwebui.model_rows == {}
    assert all(request.method == "GET" for request in openwebui.requests)


def test_a_grant_that_does_not_stick_is_an_error(openwebui, share) -> None:
    openwebui.served = [served("gemma")]
    openwebui.ignore_access_writes = True

    with pytest.raises(ScriptError, match="did not store the public grant for gemma"):
        share()


@pytest.mark.parametrize("listing", ["not a list", None])
def test_an_unreadable_model_list_is_an_error(openwebui, share, listing) -> None:
    openwebui.served = listing

    with pytest.raises(ScriptError, match="did not return a model list"):
        share()
