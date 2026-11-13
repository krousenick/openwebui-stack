"""Publish the skill packages into the Open WebUI workspace.

Runs in the open-webui container (tasks/openwebui_skills.yml). Open WebUI
keeps skills in its own database with version history, so this goes
through the Skills API instead of writing rows:

  POST /api/v1/skills/create          create (400 when the id is taken)
  POST /api/v1/skills/id/{id}/update  replace the content; needs
                                      expected_version_id to match the
                                      stored row's version_id

The skill id is the SKILL.md frontmatter name. A skill whose stored
content already equals the checkout's SKILL.md is left alone, so a rerun
with unchanged skills writes nothing.

Environment: OPENWEBUI_API_KEY (an admin API key).
Report keys: skills (each skill's name and state: created, updated or
unchanged).
"""

from __future__ import annotations

import urllib.parse
from typing import Any

import skill_packages
import stack_api
from skill_packages import Skill
from stack_api import Api, ScriptError

SKILLS_API = "/api/v1/skills"
COMMIT_MESSAGE = "Synced from openwebui-stack skills/ checkout"


def stored_skill(api: Api, skill_id: str) -> dict[str, Any] | None:
    """Return the stored skill, or None when Open WebUI has none with this id."""
    path = f"{SKILLS_API}/id/{urllib.parse.quote(skill_id, safe='')}"
    status, current = api.request("GET", path)
    if status == 200 and isinstance(current, dict):
        return current
    if status != 404:
        raise ScriptError(f"GET {path} returned {status}: {current}")
    # The by-id route can answer 404 for a skill the listing still shows.
    status, listing = api.request("GET", f"{SKILLS_API}/")
    if status != 200 or not isinstance(listing, list):
        return None
    return next((skill for skill in listing if isinstance(skill, dict) and skill.get("id") == skill_id), None)


def publish(api: Api, skill: Skill) -> str:
    """Create or update one skill; return created, updated or unchanged."""
    content = skill.text()
    current = stored_skill(api, skill.name)
    if current is None:
        form = {"id": skill.name, "name": skill.name, "description": skill.description, "content": content}
        api.write(f"create skill {skill.name}", "POST", f"{SKILLS_API}/create", json_body=form)
        return "created"
    if current.get("content") == content:
        return "unchanged"
    form = {
        "id": skill.name,
        "name": current.get("name", skill.name),
        "description": skill.description,
        "content": content,
        "expected_version_id": current.get("version_id"),
        "commit_message": COMMIT_MESSAGE,
    }
    path = f"{SKILLS_API}/id/{urllib.parse.quote(skill.name, safe='')}/update"
    api.write(f"update skill {skill.name}", "POST", path, json_body=form)
    return "updated"


def main(argv: list[str] | None = None) -> dict[str, Any]:
    parser = stack_api.argument_parser(__doc__, url="http://localhost:8080")
    parser.add_argument(
        "--skills-dir", default=skill_packages.SKILLS_DIR, help="skill packages root (default: %(default)s)"
    )
    args = parser.parse_args(argv)
    api = Api.from_args(args, stack_api.secret("OPENWEBUI_API_KEY"))

    skills = skill_packages.discover(args.skills_dir)
    return api.report(skills=[{"name": skill.name, "state": publish(api, skill)} for skill in skills])


if __name__ == "__main__":
    stack_api.run(main)
