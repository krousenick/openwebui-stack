"""Frontmatter parsing and the rules every published skill must meet."""

from __future__ import annotations

import pytest

import skill_packages
from fakes import REPO_ROOT, write_skill
from stack_api import ScriptError


def test_parses_a_single_line_description():
    text = "---\nname: alpha\ndescription: Does   the alpha thing.\n---\n\nbody\n"
    assert skill_packages.parse_frontmatter(text) == ("alpha", "Does the alpha thing.")


def test_parses_a_folded_description_and_stops_at_the_next_key():
    text = "---\nname: beta\ndescription: >-\n  First line,\n  second line.\nlicense: MIT\n---\n\nbody\n"
    assert skill_packages.parse_frontmatter(text) == ("beta", "First line, second line.")


def test_strips_quotes_and_trailing_whitespace_from_the_name():
    text = '---\nname: "gamma"  \r\ndescription: x\n---\n\nbody\n'
    assert skill_packages.parse_frontmatter(text)[0] == "gamma"


@pytest.mark.parametrize("text", ["no frontmatter at all\n", "---\ndescription: x\n---\n\nbody\n"])
def test_rejects_a_file_without_frontmatter_or_name(text):
    with pytest.raises(ScriptError):
        skill_packages.parse_frontmatter(text)


def test_discover_returns_packages_sorted_by_directory(skills_dir):
    skills = skill_packages.discover(skills_dir)
    assert [(skill.package, skill.name) for skill in skills] == [("alpha", "alpha"), ("beta-two", "beta-two")]
    assert skills[1].description == "Does the beta thing, over two lines."
    assert skills[0].source() == (skills_dir / "alpha" / "SKILL.md").read_bytes()


def test_discover_ignores_hidden_directories_and_loose_files(skills_dir):
    (skills_dir / ".cache").mkdir()
    (skills_dir / "README.md").write_text("not a package")
    assert len(skill_packages.discover(skills_dir)) == 2


@pytest.mark.parametrize(
    ("name", "frontmatter"),
    [
        ("Deep Research Protocol (DRP)", "description: A display title is not a slug."),
        ("other-name", "description: The name is not the directory name."),
        (None, "license: MIT"),
        (None, "description: " + "x" * 1025),
    ],
)
def test_discover_rejects_a_package_that_breaks_the_frontmatter_rules(tmp_path, name, frontmatter):
    write_skill(tmp_path, "broken", frontmatter, name=name)
    with pytest.raises(ScriptError, match=r"skills/broken/SKILL\.md: frontmatter name"):
        skill_packages.discover(tmp_path)


def test_discover_rejects_a_package_without_skill_md(tmp_path):
    (tmp_path / "empty").mkdir()
    with pytest.raises(ScriptError, match=r"skills/empty/SKILL\.md: cannot read the file"):
        skill_packages.discover(tmp_path)


def test_every_skill_in_the_repo_is_publishable():
    """Guards the checkout itself: a bad SKILL.md fails here, not mid-deploy."""
    skills = skill_packages.discover(REPO_ROOT / "skills")
    assert skills
    assert all(skill.name == skill.package for skill in skills)
