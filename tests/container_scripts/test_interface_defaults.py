"""Default interface settings (Show Input Selections) and admin-config switches against a fake Open WebUI."""

from __future__ import annotations

import pytest

import openwebui_interface_defaults
from fakes import MFA_FIELDS
from stack_api import ScriptError

SHOW_SELECTIONS = {"showInputSelections": True}
NO_MEMORIES = {"ENABLE_MEMORIES": False}


@pytest.fixture
def set_defaults(openwebui, stdin_payload):
    def run(settings: dict, *extra: str, config: dict | None = None) -> dict:
        openwebui.forget_requests()
        stdin_payload({"settings": settings, **({"config": config} if config is not None else {})})
        return openwebui_interface_defaults.main(["--url", openwebui.url, *extra])

    return run


def posted_forms(openwebui) -> list[dict]:
    return [request.json() for request in openwebui.requests if request.method == "POST"]


def test_a_new_default_keeps_the_other_defaults_and_admin_settings(openwebui, set_defaults):
    before = dict(openwebui.admin_config)

    report = set_defaults(SHOW_SELECTIONS)

    assert report["actions"] == ["set default interface settings showInputSelections=true"]
    assert report["settings"] == {"chatBubble": False, "showInputSelections": True}
    assert openwebui.admin_config["DEFAULT_INTERFACE_SETTINGS"] == {"chatBubble": False, "showInputSelections": True}
    (form,) = posted_forms(openwebui)
    # Everything but the MFA fields goes back unchanged; the fake refuses
    # MFA fields from an API key, as Open WebUI does.
    unchanged = {key: value for key, value in before.items() if key not in MFA_FIELDS}
    unchanged["DEFAULT_INTERFACE_SETTINGS"] = report["settings"]
    assert form == unchanged


def test_rerun_writes_nothing(openwebui, set_defaults):
    set_defaults(SHOW_SELECTIONS)
    report = set_defaults(SHOW_SELECTIONS)
    assert report["changed"] is False
    assert openwebui.writes() == []


def test_a_value_set_otherwise_is_replaced(openwebui, set_defaults):
    openwebui.admin_config["DEFAULT_INTERFACE_SETTINGS"] = {"showInputSelections": False}
    report = set_defaults(SHOW_SELECTIONS)
    assert report["actions"] == ["set default interface settings showInputSelections=true"]
    assert openwebui.admin_config["DEFAULT_INTERFACE_SETTINGS"] == SHOW_SELECTIONS


def test_unset_defaults_start_from_an_empty_object(openwebui, set_defaults):
    openwebui.admin_config["DEFAULT_INTERFACE_SETTINGS"] = None
    assert set_defaults(SHOW_SELECTIONS)["settings"] == SHOW_SELECTIONS
    assert openwebui.admin_config["DEFAULT_INTERFACE_SETTINGS"] == SHOW_SELECTIONS


def test_a_null_i18n_is_left_out_of_the_form(openwebui, set_defaults):
    openwebui.admin_config["I18N"] = None
    set_defaults(SHOW_SELECTIONS)
    (form,) = posted_forms(openwebui)
    assert "I18N" not in form


def test_check_run_writes_nothing_and_reports_the_change(openwebui, set_defaults):
    report = set_defaults(SHOW_SELECTIONS, "--check")
    assert report["check"] is True
    assert report["actions"] == ["set default interface settings showInputSelections=true"]
    assert report["settings"] == {"chatBubble": False, "showInputSelections": True}
    assert openwebui.writes() == []
    assert openwebui.admin_config["DEFAULT_INTERFACE_SETTINGS"] == {"chatBubble": False}


def test_a_save_that_does_not_stick_is_an_error(openwebui, set_defaults):
    openwebui.ignore_config_writes = True
    with pytest.raises(ScriptError, match="did not store"):
        set_defaults(SHOW_SELECTIONS)


def test_a_save_that_signed_users_out_is_reported(openwebui, set_defaults):
    openwebui.sessions_revoked_on_save = True
    with pytest.raises(ScriptError, match="signed every user out"):
        set_defaults(SHOW_SELECTIONS)


@pytest.mark.parametrize("settings", [{}, None, ["showInputSelections"]])
def test_the_payload_needs_settings(openwebui, set_defaults, settings):
    with pytest.raises(ScriptError, match="settings"):
        set_defaults(settings)
    assert openwebui.requests == []


def test_admin_switches_go_in_the_same_save(openwebui, set_defaults):
    report = set_defaults(SHOW_SELECTIONS, config=NO_MEMORIES)
    assert report["actions"] == [
        "set default interface settings showInputSelections=true; set admin config ENABLE_MEMORIES=false"
    ]
    assert report["config"] == NO_MEMORIES
    (form,) = posted_forms(openwebui)
    assert form["ENABLE_MEMORIES"] is False and form["ENABLE_MEMORY_SYSTEM_CONTEXT"] is True
    assert openwebui.admin_config["ENABLE_MEMORIES"] is False
    assert set_defaults(SHOW_SELECTIONS, config=NO_MEMORIES)["changed"] is False


def test_a_switch_alone_is_enough(openwebui, set_defaults):
    report = set_defaults({}, config=NO_MEMORIES)
    assert report["actions"] == ["set admin config ENABLE_MEMORIES=false"]
    assert openwebui.admin_config["DEFAULT_INTERFACE_SETTINGS"] == {"chatBubble": False}


def test_a_boolean_switch_given_as_text_is_stored_as_a_boolean(openwebui, set_defaults):
    set_defaults({}, config={"ENABLE_MEMORIES": "False"})
    assert openwebui.admin_config["ENABLE_MEMORIES"] is False
    assert set_defaults({}, config={"ENABLE_MEMORIES": "false"})["changed"] is False


@pytest.mark.parametrize(
    ("config", "message"),
    [
        ({"ENABLE_MFA": False}, "MFA settings cannot be set"),
        ({"ENABLE_MEMORYS": False}, "not admin config fields"),
        ({"DEFAULT_INTERFACE_SETTINGS": {}}, 'through "settings"'),
    ],
)
def test_switches_that_cannot_be_set(openwebui, set_defaults, config, message):
    with pytest.raises(ScriptError, match=message):
        set_defaults({}, config=config)
    assert openwebui.writes() == []
