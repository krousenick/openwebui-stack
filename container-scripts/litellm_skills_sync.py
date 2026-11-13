"""Publish the skill packages to LiteLLM's Skill Hub.

Runs in the litellm container (tasks/mcp/skills_gateway.yml). The Skill Hub
(https://docs.litellm.ai/docs/skills_gateway) keeps each skill as a stored
archive with version history in the proxy's Postgres, and one registry
feeds all of its views: the hub (/skill-hub/skills), the Claude Code
marketplace (/claude-code/marketplace.json) and the hub entries of the
Agent Skills discovery index (/.well-known/agent-skills/index.json). Per
skill package:

  not in the hub        POST /skill-hub/skills/upload creates it, version
                        1.0.0, public
  SKILL.md differs      PUT /skill-hub/skills/{name}/upload stores it as the
                        next patch version; the hub keeps the skill's
                        domain, namespace, keywords and visibility
  SKILL.md is the same  nothing: GET /skill-hub/skills/{name}/content
                        returns the stored file, compared byte for byte

A rerun with unchanged skills therefore writes nothing, and a new version
appears only when SKILL.md changed. The script never unpublishes or deletes
a hub skill: an admin can unpublish one and it stays private, and a package
removed from skills/ stays in the hub until it is deleted there. The hub
takes 10 uploads a minute per user; a 429 waits for the next window.

Moving off the legacy layout. Before the Skill Hub this script uploaded
each skill to the proxy-hosted store (POST /v1/skills,
custom_llm_provider=litellm_proxy) and registered a marketplace row,
through the now deprecated /claude-code/plugins routes, pointing at that
store's /v1/skills/{id}/archive. Those rows are hub rows, but with no
archive of their own (no content_url), so an upload replaces each
("migrated"). The discovery index lists legacy-store skills before hub
skills and renames a hub skill whose name a legacy one already uses
(name-2), so every legacy copy of a published skill is deleted after the
uploads. LiteLLM 1.110's semantic skill search (GET /v1/skills?query= and
the skill_search MCP tool) ranks only that legacy store, so it has no
skills to rank once the copies are gone.

The hub builds every archive URL from the proxy's PROXY_BASE_URL. A real
run ends by reading the marketplace and the index as an anonymous client:
each public skill must be served once, from the hub, under
--archive-base-url, or the run fails.

Environment: LITELLM_MASTER_KEY.
Report keys: skills (each skill's name; hub: created, migrated, updated or
unchanged; the hub's version and visibility after the run, before it with
--check; legacy_removed, how many legacy copies were deleted) and served
(how many skills the anonymous marketplace and index serve; null with
--check, and a view is null when it is off or requires a key).
"""

from __future__ import annotations

import io
import json
import re
import urllib.parse
import zipfile
from typing import Any

import skill_packages
import stack_api
from skill_packages import Skill
from stack_api import Api, ScriptError

HUB = "/skill-hub/skills"
MARKETPLACE = "/claude-code/marketplace.json"
INDEX = "/.well-known/agent-skills/index.json"
LEGACY_STORE = "/v1/skills"
LEGACY_QUERY = {"custom_llm_provider": "litellm_proxy"}
HUB_PAGE_SIZE = 100  # the largest page GET /skill-hub/skills returns
# Retries of an upload the hub rate-limited (10 a minute per user, then 429
# with Retry-After: 60). A first run over every package needs one wait.
RATE_LIMIT_RETRIES = 3
MULTIPART_BOUNDARY = "----ansible-skills-boundary"
_LEGACY_ARCHIVE_PATH = re.compile(r"/v1/skills/([^/]+)/archive$")


def quoted(name: str) -> str:
    return urllib.parse.quote(name, safe="")


def upload_archive(skill: Skill) -> bytes:
    """Zip a skill in the Skills API layout: SKILL.md inside a folder named after the package."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(f"{skill.package}/SKILL.md", skill.source())
    return buffer.getvalue()


def multipart_form(fields: dict[str, str], *, file_field: str, filename: str, payload: bytes) -> tuple[bytes, str]:
    """Encode text fields and one zip file as multipart/form-data; return (body, content type)."""
    boundary = MULTIPART_BOUNDARY.encode()
    body = bytearray()
    for name, value in fields.items():
        body += b"--" + boundary + b"\r\n"
        body += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        body += value.encode() + b"\r\n"
    body += b"--" + boundary + b"\r\n"
    body += f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'.encode()
    body += b"Content-Type: application/zip\r\n\r\n"
    body += payload + b"\r\n--" + boundary + b"--\r\n"
    return bytes(body), f"multipart/form-data; boundary={MULTIPART_BOUNDARY}"


def hub_skills(api: Api) -> dict[str, dict[str, Any]]:
    """Return {name: skill} for every skill in the hub, as GET /skill-hub/skills lists them."""
    skills: dict[str, dict[str, Any]] = {}
    offset = 0
    while True:
        status, page = api.request("GET", HUB, query={"limit": str(HUB_PAGE_SIZE), "offset": str(offset)})
        if status != 200 or not isinstance(page, dict):
            hint = "; this LiteLLM has no Skill Hub API" if status == 404 else ""
            raise ScriptError(f"GET {HUB} returned {status}{hint}: {json.dumps(page)[:300]}")
        for skill in page.get("skills") or []:
            if isinstance(skill, dict) and skill.get("name"):
                skills[skill["name"]] = skill
        next_offset = page.get("next_offset")
        if not isinstance(next_offset, int) or next_offset <= offset:
            return skills
        offset = next_offset


def stored_skill_md(api: Api, name: str) -> bytes | None:
    """Return the SKILL.md the hub stores for a skill, or None when it holds no archive for it."""
    path = f"{HUB}/{quoted(name)}/content"
    status, content = api.request_raw("GET", path)
    if status == 200:
        return content
    if status == 404:
        return None
    raise ScriptError(f"GET {path} returned {status}: {content[:300].decode(errors='replace')}")


def upload(api: Api, action: str, method: str, path: str, skill: Skill, **fields: str) -> dict[str, Any] | None:
    """Send a skill's archive to an upload route; return the hub's skill (None on a check run)."""
    body, content_type = multipart_form(
        fields, file_field="file", filename=f"{skill.name}.zip", payload=upload_archive(skill)
    )
    stored = api.write(action, method, path, body=body, content_type=content_type)
    if isinstance(stored, dict) and stored.get("status") == "pending_review":
        raise ScriptError(f"{action}: the hub queued it for review, so LITELLM_MASTER_KEY is not a proxy admin key")
    return stored


def sync_skill(api: Api, skill: Skill, current: dict[str, Any] | None) -> tuple[str, dict[str, Any] | None]:
    """Bring one skill in the hub up to date; return its state and the hub's skill after the run."""
    path = f"{HUB}/{quoted(skill.name)}"
    if current is None:
        created = upload(api, f"create skill {skill.name}", "POST", f"{HUB}/upload", skill)
        if created is not None and created.get("visibility") != "public":
            # New hub skills are public in LiteLLM 1.110; this keeps them so
            # if a release starts them private.
            api.write(f"publish skill {skill.name}", "POST", f"{path}/publish")
            created = {**created, "visibility": "public"}
        return "created", created
    if not current.get("content_url"):
        state = "migrated"  # a marketplace row whose archive lives outside the hub
    elif stored_skill_md(api, skill.name) == skill.source():
        return "unchanged", current
    else:
        state = "updated"
    # A skill edited in the hub since it was listed answers 409 instead of
    # being overwritten.
    fields = {"expected_updated_at": current["updated_at"]} if current.get("updated_at") else {}
    verb = "migrate" if state == "migrated" else "update"
    stored = upload(api, f"{verb} skill {skill.name}", "PUT", f"{path}/upload", skill, **fields)
    return state, stored or current


def read_public(api: Api, path: str) -> tuple[int, Any]:
    """GET a route the way an anonymous client sees it: without the key."""
    return Api(api.base_url, timeout=api.timeout).request("GET", path)


def legacy_copies(api: Api, names: list[str], index: Any) -> dict[str, list[str]]:
    """Return {skill name: ids of its copies in the legacy store}.

    A copy is a legacy skill whose display_title is the skill's name (how
    this script used to upload them) or one the discovery index serves
    under that name, whatever its title.
    """
    copies: dict[str, list[str]] = {name: [] for name in names}
    status, listing = api.request("GET", LEGACY_STORE, query={**LEGACY_QUERY, "limit": "100"})
    stored = listing.get("data") if status == 200 and isinstance(listing, dict) else None
    for legacy in stored or []:
        if isinstance(legacy, dict) and legacy.get("display_title") in copies and legacy.get("id"):
            copies[legacy["display_title"]].append(legacy["id"])
    entries = index.get("skills") if isinstance(index, dict) else None
    for entry in entries or []:
        if not isinstance(entry, dict) or entry.get("name") not in copies:
            continue
        match = _LEGACY_ARCHIVE_PATH.search(urllib.parse.urlsplit(str(entry.get("url", ""))).path)
        legacy_id = urllib.parse.unquote(match.group(1)) if match else None
        if legacy_id and legacy_id not in copies[entry["name"]]:
            copies[entry["name"]].append(legacy_id)
    return copies


def remove_legacy_copies(api: Api, copies: dict[str, list[str]]) -> None:
    for name, ids in copies.items():
        for legacy_id in ids:
            path = f"{LEGACY_STORE}/{quoted(legacy_id)}"
            api.write(f"remove legacy copy {legacy_id} of {name}", "DELETE", path, query=LEGACY_QUERY)


def served_from(url: Any, base: str, route: str) -> bool:
    """Whether url is the proxy's route under the public base URL."""
    return isinstance(url, str) and url.startswith(f"{base}/") and route in url


def verify_served(api: Api, base: str, public: list[str]) -> dict[str, int | None]:
    """Check that the marketplace and the index serve each public skill once, from the hub.

    Reads both as an anonymous client and returns how many skills each
    serves. A view is None when it is off (the index) or requires a key
    (claude_code_marketplace_auth_required, which also keeps hub skills out
    of the index).
    """
    status, marketplace = read_public(api, MARKETPLACE)
    if status in (401, 403):
        return {"marketplace": None, "index": None}
    if status != 200 or not isinstance(marketplace, dict):
        raise ScriptError(f"GET {MARKETPLACE} returned {status}: {json.dumps(marketplace)[:300]}")
    plugins = {plugin.get("name"): plugin for plugin in marketplace.get("plugins") or [] if isinstance(plugin, dict)}
    problems = []
    for name in public:
        source = (plugins.get(name) or {}).get("source")
        url = source.get("url") if isinstance(source, dict) else None
        if not served_from(url, base, f"/claude-code/plugins/{quoted(name)}/archive/"):
            problems.append(f"the marketplace serves {name} from {url or 'nowhere'}")
    status, index = read_public(api, INDEX)
    entries = index.get("skills") if status == 200 and isinstance(index, dict) else None
    for name in public if entries is not None else []:
        urls = [entry.get("url") for entry in entries if isinstance(entry, dict) and entry.get("name") == name]
        if len(urls) != 1 or not served_from(urls[0], base, f"{HUB}/{quoted(name)}/archive"):
            problems.append(f"the discovery index serves {name} from {', '.join(map(str, urls)) or 'nowhere'}")
    if problems:
        raise ScriptError(
            "; ".join(problems) + f" (expected once each, from the hub under {base}, which is PROXY_BASE_URL)"
        )
    return {"marketplace": len(public), "index": None if entries is None else len(public)}


def main(argv: list[str] | None = None) -> dict[str, Any]:
    parser = stack_api.argument_parser(__doc__, url="http://localhost:4000")
    parser.add_argument(
        "--archive-base-url",
        required=True,
        help="public https base URL of the proxy, its PROXY_BASE_URL, e.g. https://litellm.example.com",
    )
    parser.add_argument(
        "--skills-dir", default=skill_packages.SKILLS_DIR, help="skill packages root (default: %(default)s)"
    )
    args = parser.parse_args(argv)
    api = Api.from_args(args, stack_api.secret("LITELLM_MASTER_KEY"), rate_limit_retries=RATE_LIMIT_RETRIES)
    base = args.archive_base_url.rstrip("/")

    skills = skill_packages.discover(args.skills_dir)
    current = hub_skills(api)
    synced: dict[str, tuple[str, dict[str, Any] | None]] = {}
    for skill in skills:
        synced[skill.name] = sync_skill(api, skill, current.get(skill.name))

    _, index = read_public(api, INDEX)
    copies = legacy_copies(api, list(synced), index)
    remove_legacy_copies(api, copies)

    public = [name for name, (_, skill) in synced.items() if skill and skill.get("visibility") == "public"]
    return api.report(
        skills=[
            {
                "name": name,
                "hub": state,
                "version": skill.get("version") if skill else None,
                "visibility": skill.get("visibility") if skill else None,
                "legacy_removed": len(copies[name]),
            }
            for name, (state, skill) in synced.items()
        ],
        served=None if api.check else verify_served(api, base, public),
    )


if __name__ == "__main__":
    stack_api.run(main)
