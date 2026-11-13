"""Skill store and marketplace reconciliation against a fake LiteLLM proxy."""

from __future__ import annotations

import hashlib
import io
import zipfile

import pytest

import litellm_skills_sync
import skill_packages
from fakes import repack_digest, write_skill
from stack_api import ScriptError

ARCHIVE_BASE = "https://litellm.example.com"


def sync(litellm, skills_dir, *extra: str) -> dict:
    litellm.forget_requests()
    return litellm_skills_sync.main(
        ["--url", litellm.url, "--skills-dir", str(skills_dir), "--archive-base-url", ARCHIVE_BASE, *extra]
    )


def states(report: dict, key: str) -> dict[str, str]:
    return {skill["name"]: skill[key] for skill in report["skills"]}


def legacy_digest(source: bytes) -> str:
    """The inline one-liner skills_gateway.yml ran before this script existed."""
    info = zipfile.ZipInfo(filename="SKILL.md", date_time=(1980, 1, 1, 0, 0, 0))
    info.create_system = 3
    info.external_attr = 0o644 << 16
    buf = io.BytesIO()
    z = zipfile.ZipFile(buf, "w")
    z.writestr(info, source, compress_type=zipfile.ZIP_DEFLATED)
    z.close()
    return hashlib.sha256(buf.getvalue()).hexdigest()


def test_index_digest_is_what_the_proxy_publishes_for_the_upload(skills_dir):
    for skill in skill_packages.discover(skills_dir):
        digest = litellm_skills_sync.index_digest(skill.source())
        assert digest == legacy_digest(skill.source())
        assert (digest, skill.source()) == repack_digest(litellm_skills_sync.upload_archive(skill))


def test_first_run_uploads_every_skill_and_registers_it(litellm, skills_dir):
    report = sync(litellm, skills_dir)

    assert report["changed"] is True
    assert states(report, "store") == {"alpha": "created", "beta-two": "created"}
    assert states(report, "marketplace") == {"alpha": "created", "beta-two": "created"}
    assert litellm.titles() == ["alpha", "beta-two"]

    stored = next(skill for skill in litellm.skills.values() if skill["display_title"] == "beta-two")
    assert stored["description"] == "Does the beta thing, over two lines."
    assert stored["source"] == (skills_dir / "beta-two" / "SKILL.md").read_text(encoding="utf-8")
    assert litellm.plugins["beta-two"] == {
        "name": "beta-two",
        "source": {
            "source": "archive",
            "url": f"{ARCHIVE_BASE}/v1/skills/{stored['id']}/archive",
            "sha256": stored["digest"],
        },
        "description": "Does the beta thing, over two lines.",
        "domain": "Engineering",
        "keywords": ["beta-two"],
        "version": "1.0.0",
    }
    assert report["index_available"] is True
    assert report["search"] == {"query": "alpha", "results": 1}


def test_rerun_with_unchanged_skills_writes_nothing(litellm, skills_dir):
    sync(litellm, skills_dir)
    report = sync(litellm, skills_dir)

    assert report["changed"] is False
    assert report["actions"] == []
    assert litellm.writes() == []
    assert set(states(report, "store").values()) == {"unchanged"}
    assert litellm.plugins["alpha"]["version"] == "1.0.0"


def test_a_changed_skill_is_replaced_and_existing_entries_are_refreshed(litellm, skills_dir):
    first = sync(litellm, skills_dir)
    old_id = next(skill["skill_id"] for skill in first["skills"] if skill["name"] == "alpha")
    write_skill(skills_dir, "alpha", "description: Does the alpha thing.", body="\nNew section.\n")

    report = sync(litellm, skills_dir)

    assert states(report, "store") == {"alpha": "replaced", "beta-two": "unchanged"}
    assert states(report, "marketplace") == {"alpha": "updated", "beta-two": "updated"}
    assert report["actions"] == [
        "delete drifted skill alpha",
        "upload skill alpha",
        "update marketplace entry alpha",
        "update marketplace entry beta-two",
    ]
    new_id = next(skill["skill_id"] for skill in report["skills"] if skill["name"] == "alpha")
    assert new_id != old_id
    assert old_id not in litellm.skills
    assert litellm.plugins["alpha"]["source"]["url"] == f"{ARCHIVE_BASE}/v1/skills/{new_id}/archive"
    assert litellm.plugins["alpha"]["version"] == "1.0.1"
    assert sync(litellm, skills_dir)["changed"] is False


def test_without_the_index_stored_skills_count_as_unchanged(litellm, skills_dir):
    sync(litellm, skills_dir)
    litellm.index_enabled = False
    write_skill(skills_dir, "alpha", "description: Does the alpha thing.", body="\nDrift nobody can see.\n")
    write_skill(skills_dir, "gamma", "description: A new skill.")

    report = sync(litellm, skills_dir)

    assert report["index_available"] is False
    assert states(report, "store") == {"alpha": "unchanged", "beta-two": "unchanged", "gamma": "created"}
    assert litellm.titles() == ["alpha", "beta-two", "gamma"]


def test_check_run_reports_the_plan_and_writes_nothing(litellm, skills_dir):
    report = sync(litellm, skills_dir, "--check")

    assert report["check"] is True
    assert report["changed"] is True
    assert report["actions"] == [
        "upload skill alpha",
        "upload skill beta-two",
        "create marketplace entry alpha",
        "create marketplace entry beta-two",
    ]
    assert states(report, "skill_id") == {"alpha": "assigned-on-upload", "beta-two": "assigned-on-upload"}
    assert litellm.writes() == []
    assert litellm.skills == {}


def test_check_run_after_a_real_run_reports_no_change(litellm, skills_dir):
    sync(litellm, skills_dir)
    report = sync(litellm, skills_dir, "--check")
    assert report["changed"] is False
    assert litellm.writes() == []


def test_a_bad_package_fails_before_anything_is_published(litellm, skills_dir):
    write_skill(skills_dir, "zeta", "description: Display titles are not slugs.", name="Zeta Skill")
    with pytest.raises(ScriptError, match=r"skills/zeta/SKILL\.md"):
        sync(litellm, skills_dir)
    assert litellm.writes() == []


def test_search_is_reported_as_null_when_it_does_not_answer(litellm, skills_dir):
    litellm.search_enabled = False
    assert sync(litellm, skills_dir)["search"] is None


def test_a_rejected_master_key_is_an_error(litellm, skills_dir, monkeypatch):
    monkeypatch.setenv("LITELLM_MASTER_KEY", "wrong")
    with pytest.raises(ScriptError, match="GET /v1/skills returned 401"):
        sync(litellm, skills_dir)
