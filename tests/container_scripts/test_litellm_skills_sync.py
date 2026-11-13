"""Skill Hub publishing against a fake LiteLLM proxy."""

from __future__ import annotations

import pytest

import litellm_skills_sync
import skill_packages
import stack_api
from fakes import parse_multipart, write_skill
from stack_api import ScriptError

ARCHIVE_BASE = "https://litellm.example.com"


def sync(litellm, skills_dir, *extra: str) -> dict:
    litellm.forget_requests()
    return litellm_skills_sync.main(
        ["--url", litellm.url, "--skills-dir", str(skills_dir), "--archive-base-url", ARCHIVE_BASE, *extra]
    )


def by_name(report: dict, key: str) -> dict[str, object]:
    return {skill["name"]: skill[key] for skill in report["skills"]}


def legacy_layout(litellm, skills_dir) -> dict[str, str]:
    """What the earlier publisher left: a legacy-store copy and a marketplace row per skill."""
    legacy_ids = {}
    for skill in skill_packages.discover(skills_dir):
        legacy_ids[skill.name] = litellm.add_legacy_skill(skill.name, skill.source())
        litellm.add_marketplace_row(skill.name, legacy_ids[skill.name])
    return legacy_ids


def index_names(litellm) -> list[str]:
    return [entry["name"] for entry in litellm.discovery_index()[1]["skills"]]


def test_first_run_creates_every_skill_in_the_hub(litellm, skills_dir):
    report = sync(litellm, skills_dir)

    assert report["changed"] is True
    assert report["actions"] == ["create skill alpha", "create skill beta-two"]
    assert by_name(report, "hub") == {"alpha": "created", "beta-two": "created"}
    assert by_name(report, "version") == {"alpha": "1.0.0", "beta-two": "1.0.0"}
    assert by_name(report, "visibility") == {"alpha": "public", "beta-two": "public"}
    assert report["served"] == {"marketplace": 2, "index": 2}

    row = litellm.hub["beta-two"]
    assert row["skill_md"] == (skills_dir / "beta-two" / "SKILL.md").read_bytes()
    assert row["description"] == "Does the beta thing, over two lines."
    source = row["manifest"]["source"]
    assert source["url"] == f"{ARCHIVE_BASE}/claude-code/plugins/beta-two/archive/{source['sha256']}.zip"
    assert index_names(litellm) == ["alpha", "beta-two"]
    # Nothing goes to the legacy store or the deprecated marketplace routes.
    assert litellm.skills == {}
    assert litellm.plugins == {}


def test_rerun_with_unchanged_skills_writes_nothing(litellm, skills_dir):
    sync(litellm, skills_dir)
    report = sync(litellm, skills_dir)

    assert report["changed"] is False
    assert report["actions"] == []
    assert litellm.writes() == []
    assert set(by_name(report, "hub").values()) == {"unchanged"}
    assert by_name(report, "version") == {"alpha": "1.0.0", "beta-two": "1.0.0"}


def test_a_changed_skill_becomes_its_next_version(litellm, skills_dir):
    sync(litellm, skills_dir)
    write_skill(skills_dir, "alpha", "description: Does the alpha thing.", body="\nNew section.\n")

    report = sync(litellm, skills_dir)

    assert report["actions"] == ["update skill alpha"]
    assert by_name(report, "hub") == {"alpha": "updated", "beta-two": "unchanged"}
    assert by_name(report, "version") == {"alpha": "1.0.1", "beta-two": "1.0.0"}
    assert litellm.hub["alpha"]["skill_md"].endswith(b"New section.\n")
    assert [version["version"] for version in litellm.hub["alpha"]["versions"]] == ["1.0.0", "1.0.1"]
    assert sync(litellm, skills_dir)["changed"] is False


def test_the_legacy_layout_moves_into_the_hub_and_its_copies_go(litellm, skills_dir):
    legacy_ids = legacy_layout(litellm, skills_dir)
    unrelated = litellm.add_legacy_skill("someone-elses", b"---\nname: someone-elses\ndescription: Kept.\n---\n")
    # Before: the index lists the legacy copies under the skills' names.
    assert index_names(litellm) == ["alpha", "beta-two", "someone-elses"]

    report = sync(litellm, skills_dir)

    assert report["actions"] == [
        "migrate skill alpha",
        "migrate skill beta-two",
        f"remove legacy copy {legacy_ids['alpha']} of alpha",
        f"remove legacy copy {legacy_ids['beta-two']} of beta-two",
    ]
    assert by_name(report, "hub") == {"alpha": "migrated", "beta-two": "migrated"}
    assert by_name(report, "version") == {"alpha": "1.0.1", "beta-two": "1.0.1"}
    assert by_name(report, "legacy_removed") == {"alpha": 1, "beta-two": 1}
    assert list(litellm.skills) == [unrelated]
    # The registry fields the earlier publisher set stay on the rows.
    assert litellm.hub["alpha"]["manifest"]["domain"] == "Engineering"
    assert litellm.hub["alpha"]["manifest"]["keywords"] == ["alpha"]
    assert report["served"] == {"marketplace": 2, "index": 2}
    assert sorted(index_names(litellm)) == ["alpha", "beta-two", "someone-elses"]
    assert sync(litellm, skills_dir)["changed"] is False


def test_while_a_legacy_copy_holds_the_name_the_index_renames_the_hub_skill(litellm, skills_dir):
    """Why the copies are removed: the index gives a legacy skill the name and lists the hub one as name-2."""
    sync(litellm, skills_dir)
    litellm.add_legacy_skill("alpha", (skills_dir / "alpha" / "SKILL.md").read_bytes())
    assert index_names(litellm) == ["alpha", "alpha-2", "beta-two"]

    report = sync(litellm, skills_dir)

    assert report["actions"] == ["remove legacy copy skill_1 of alpha"]
    assert index_names(litellm) == ["alpha", "beta-two"]


def test_a_legacy_copy_under_another_title_goes_when_the_index_serves_it_by_name(litellm, skills_dir):
    sync(litellm, skills_dir)
    copy = litellm.add_legacy_skill("Alpha (old upload)", (skills_dir / "alpha" / "SKILL.md").read_bytes())

    report = sync(litellm, skills_dir)

    assert report["actions"] == [f"remove legacy copy {copy} of alpha"]
    assert litellm.skills == {}


def test_check_run_reports_the_plan_and_writes_nothing(litellm, skills_dir):
    legacy_ids = legacy_layout(litellm, skills_dir)
    write_skill(skills_dir, "gamma", "description: A new skill.")

    report = sync(litellm, skills_dir, "--check")

    assert report["check"] is True
    assert report["changed"] is True
    assert report["actions"] == [
        "migrate skill alpha",
        "migrate skill beta-two",
        "create skill gamma",
        f"remove legacy copy {legacy_ids['alpha']} of alpha",
        f"remove legacy copy {legacy_ids['beta-two']} of beta-two",
    ]
    assert by_name(report, "version") == {"alpha": "1.0.0", "beta-two": "1.0.0", "gamma": None}
    assert report["served"] is None
    assert litellm.writes() == []


def test_check_run_after_a_real_run_reports_no_change(litellm, skills_dir):
    sync(litellm, skills_dir)
    report = sync(litellm, skills_dir, "--check")
    assert report["changed"] is False
    assert litellm.writes() == []


def test_uploads_wait_out_the_hub_rate_limit(litellm, skills_dir, monkeypatch):
    for number in range(10):
        write_skill(skills_dir, f"extra-{number}", f"description: Extra skill {number}.")
    waits = []

    def sleep(seconds: float) -> None:
        waits.append(seconds)
        litellm.reset_rate_window()

    monkeypatch.setattr(stack_api.time, "sleep", sleep)

    report = sync(litellm, skills_dir)

    assert waits == [60.0]
    assert len(report["actions"]) == 12  # each upload recorded once
    assert set(by_name(report, "hub").values()) == {"created"}
    assert len(litellm.hub) == 12


def test_an_unpublished_skill_stays_private(litellm, skills_dir):
    sync(litellm, skills_dir)
    litellm.hub["alpha"]["enabled"] = False  # an admin unpublished it in the hub
    write_skill(skills_dir, "alpha", "description: Does the alpha thing.", body="\nChanged.\n")

    report = sync(litellm, skills_dir)

    assert report["actions"] == ["update skill alpha"]
    assert by_name(report, "visibility") == {"alpha": "private", "beta-two": "public"}
    assert report["served"] == {"marketplace": 1, "index": 1}


def test_new_skills_are_published_if_the_hub_starts_them_private(litellm, skills_dir):
    litellm.hub_new_private = True

    report = sync(litellm, skills_dir)

    assert report["actions"] == [
        "create skill alpha",
        "publish skill alpha",
        "create skill beta-two",
        "publish skill beta-two",
    ]
    assert by_name(report, "visibility") == {"alpha": "public", "beta-two": "public"}
    assert report["served"] == {"marketplace": 2, "index": 2}


def test_a_replacement_names_the_row_it_replaces(litellm, skills_dir):
    sync(litellm, skills_dir)
    listed = litellm.hub["alpha"]["updated_at"]
    write_skill(skills_dir, "alpha", "description: Does the alpha thing.", body="\nChanged.\n")

    sync(litellm, skills_dir)

    (upload,) = [request for request in litellm.requests if request.method == "PUT"]
    assert parse_multipart(upload)["expected_updated_at"] == listed.encode()


def test_archive_urls_off_the_public_base_fail_the_run(litellm, skills_dir):
    litellm.proxy_base_url = "http://localhost:4000"  # PROXY_BASE_URL unset: the proxy uses the request's
    with pytest.raises(ScriptError, match=r"marketplace serves alpha from http://localhost:4000/.*PROXY_BASE_URL"):
        sync(litellm, skills_dir)


def test_a_marketplace_that_requires_a_key_skips_the_served_check(litellm, skills_dir):
    litellm.marketplace_auth_required = True
    report = sync(litellm, skills_dir)
    assert report["served"] == {"marketplace": None, "index": None}


def test_without_the_index_only_the_marketplace_is_checked(litellm, skills_dir):
    litellm.index_enabled = False
    sync(litellm, skills_dir)
    report = sync(litellm, skills_dir)
    assert report["changed"] is False
    assert report["served"] == {"marketplace": 2, "index": None}


def test_the_hub_listing_is_read_page_by_page(litellm, skills_dir):
    write_skill(skills_dir, "gamma", "description: A third skill.")
    sync(litellm, skills_dir)
    litellm.hub_page_cap = 2
    assert sync(litellm, skills_dir)["changed"] is False


def test_an_upload_held_for_review_is_an_error(litellm, skills_dir):
    litellm.hub_review_uploads = True
    with pytest.raises(ScriptError, match="not a proxy admin key"):
        sync(litellm, skills_dir)


def test_a_proxy_without_the_skill_hub_is_an_error(litellm, skills_dir):
    litellm.hub_enabled = False
    with pytest.raises(ScriptError, match="no Skill Hub API"):
        sync(litellm, skills_dir)
    assert litellm.writes() == []


def test_a_bad_package_fails_before_anything_is_published(litellm, skills_dir):
    write_skill(skills_dir, "zeta", "description: Display titles are not slugs.", name="Zeta Skill")
    with pytest.raises(ScriptError, match=r"skills/zeta/SKILL\.md"):
        sync(litellm, skills_dir)
    assert litellm.writes() == []


def test_a_rejected_master_key_is_an_error(litellm, skills_dir, monkeypatch):
    monkeypatch.setenv("LITELLM_MASTER_KEY", "wrong")
    with pytest.raises(ScriptError, match="GET /skill-hub/skills returned 401"):
        sync(litellm, skills_dir)
