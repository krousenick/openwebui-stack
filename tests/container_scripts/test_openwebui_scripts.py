"""Skill publishing and the default-model setting against a fake Open WebUI."""

from __future__ import annotations

import pytest

import openwebui_default_models
import openwebui_skills_sync
from fakes import write_skill
from stack_api import ScriptError


def publish(openwebui, skills_dir, *extra: str) -> dict:
    openwebui.forget_requests()
    return openwebui_skills_sync.main(["--url", openwebui.url, "--skills-dir", str(skills_dir), *extra])


def set_default(openwebui, models: str, *extra: str) -> dict:
    openwebui.forget_requests()
    return openwebui_default_models.main(["--url", openwebui.url, "--models", models, *extra])


def test_first_run_creates_every_skill(openwebui, skills_dir):
    report = publish(openwebui, skills_dir)

    assert report["skills"] == [{"name": "alpha", "state": "created"}, {"name": "beta-two", "state": "created"}]
    assert report["actions"] == ["create skill alpha", "create skill beta-two"]
    assert openwebui.skills["beta-two"]["description"] == "Does the beta thing, over two lines."
    assert openwebui.skills["alpha"]["content"] == (skills_dir / "alpha" / "SKILL.md").read_text(encoding="utf-8")


def test_rerun_with_unchanged_skills_writes_nothing(openwebui, skills_dir):
    publish(openwebui, skills_dir)
    report = publish(openwebui, skills_dir)
    assert report["changed"] is False
    assert openwebui.writes() == []


def test_changed_content_is_updated_against_the_stored_version(openwebui, skills_dir):
    publish(openwebui, skills_dir)
    openwebui.skills["alpha"]["name"] = "Renamed in the workspace"
    write_skill(skills_dir, "alpha", "description: Does the alpha thing.", body="\nNew section.\n")

    report = publish(openwebui, skills_dir)

    assert report["skills"][0] == {"name": "alpha", "state": "updated"}
    assert openwebui.writes() == ["POST /api/v1/skills/id/alpha/update"]
    # The fake answers 409 unless expected_version_id matched the stored row.
    assert openwebui.skills["alpha"]["version_id"] == "v2"
    assert openwebui.skills["alpha"]["content"].endswith("New section.\n")
    assert openwebui.skills["alpha"]["name"] == "Renamed in the workspace"


def test_a_skill_the_id_route_hides_is_found_through_the_listing(openwebui, skills_dir):
    publish(openwebui, skills_dir)
    openwebui.skill_lookup_by_id = False
    assert publish(openwebui, skills_dir)["changed"] is False


def test_skill_check_run_writes_nothing(openwebui, skills_dir):
    report = publish(openwebui, skills_dir, "--check")
    assert report["actions"] == ["create skill alpha", "create skill beta-two"]
    assert openwebui.skills == {}


def test_default_models_are_saved_without_touching_the_rest_of_the_config(openwebui):
    openwebui.served = [{"id": "auto"}, {"id": "other"}]

    report = set_default(openwebui, "auto")

    assert report == {
        "changed": True,
        "check": False,
        "actions": ["set default models to auto"],
        "default_models": "auto",
        "missing": [],
    }
    assert openwebui.config == {"DEFAULT_MODELS": "auto", "MODEL_ORDER_LIST": ["kept"]}


def test_default_models_already_set_writes_nothing(openwebui):
    openwebui.config["DEFAULT_MODELS"] = "auto"
    assert set_default(openwebui, "auto")["changed"] is False
    assert openwebui.writes() == []


def test_an_unserved_default_is_saved_and_reported(openwebui):
    openwebui.served = [{"id": "auto"}]
    report = set_default(openwebui, "auto,not-there-yet")
    assert report["missing"] == ["not-there-yet"]
    assert openwebui.config["DEFAULT_MODELS"] == "auto,not-there-yet"


def test_a_default_that_does_not_stick_is_an_error(openwebui):
    openwebui.ignore_config_writes = True
    with pytest.raises(ScriptError, match="did not store DEFAULT_MODELS=auto"):
        set_default(openwebui, "auto")


def test_default_models_check_run_writes_nothing(openwebui):
    report = set_default(openwebui, "auto", "--check")
    assert report["changed"] is True
    assert openwebui.config["DEFAULT_MODELS"] == ""
