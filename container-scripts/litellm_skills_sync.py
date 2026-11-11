"""Sync the skill packages into LiteLLM's skill store and marketplace.

Runs in the litellm container (tasks/mcp/skills_gateway.yml). Two stores
are reconciled (https://docs.litellm.ai/docs/skills_gateway):

  1. The proxy-hosted skill store. Each SKILL.md is uploaded as a zip to
     POST /v1/skills (custom_llm_provider=litellm_proxy) and kept in the
     proxy's Postgres. The store is create/delete only: an upload always
     mints a new skill id and there is no update endpoint, so a changed
     SKILL.md is deleted and uploaded again.
  2. The Claude Code marketplace: one entry per skill (POST
     /claude-code/plugins, PUT .../{name}) whose archive source is the
     proxy's own /v1/skills/{id}/archive. Sources must be https, hence
     --archive-base-url, the public address of the same proxy.

Idempotency: a skill is matched in the store by display_title (its
frontmatter name). The Agent Skills discovery index publishes the sha256 of
each stored archive; it is compared with the digest this script computes
for the checkout's SKILL.md. Equal digests leave the skill alone; drift
means delete, upload and a marketplace update. Without the index
(public_skills_index off) there is nothing to compare, so stored skills
count as unchanged and only missing ones are uploaded.

The digest has to be computed here, with the proxy's own zlib: DEFLATE
output differs between zlib builds, and the host's is a different one.

Environment: LITELLM_MASTER_KEY.
Report keys: index_available, skills (name, store, marketplace, skill_id,
sha256 for each) and search (query and result count of a semantic-search
smoke test, or null when search is not answering).
"""

from __future__ import annotations

import hashlib
import io
import urllib.parse
import zipfile
from typing import Any

import skill_packages
import stack_api
from skill_packages import Skill
from stack_api import Api, ScriptError

SKILL_STORE = {"custom_llm_provider": "litellm_proxy"}
MARKETPLACE_DOMAIN = "Engineering"
MARKETPLACE_INITIAL_VERSION = "1.0.0"
MULTIPART_BOUNDARY = "----ansible-skills-boundary"
PENDING_SKILL_ID = "assigned-on-upload"


def index_digest(source: bytes) -> str:
    """Return the sha256 the discovery index will publish for a SKILL.md.

    The index digests the proxy's repack of an upload (build_skill_archive),
    not the uploaded zip. The repack rules are mirrored here: the skill
    folder is stripped to a root SKILL.md, with a fixed 1980 mtime, mode
    0644, create_system 3 and DEFLATE. Identical SKILL.md bytes therefore
    hash identically wherever the same zlib does the compression.
    """
    info = zipfile.ZipInfo(filename="SKILL.md", date_time=(1980, 1, 1, 0, 0, 0))
    info.create_system = 3
    info.external_attr = 0o644 << 16
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(info, source, compress_type=zipfile.ZIP_DEFLATED)
    return hashlib.sha256(buffer.getvalue()).hexdigest()


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


def published_digests(api: Api) -> dict[str, str] | None:
    """Return {skill name: archive sha256} from the discovery index, or None when it is off.

    The index is public, so it is read without the master key.
    """
    public = Api(api.base_url, timeout=api.timeout)
    status, index = public.request("GET", "/.well-known/agent-skills/index.json")
    if status != 200 or not isinstance(index, dict) or not isinstance(index.get("skills"), list):
        return None
    return {
        entry["name"]: str(entry.get("digest", "")).removeprefix("sha256:")
        for entry in index["skills"]
        if isinstance(entry, dict) and entry.get("name")
    }


def stored_skill_ids(api: Api) -> dict[str, str]:
    """Return {display_title: skill id} for the proxy store; the newest upload wins a duplicate title."""
    listing = api.get("/v1/skills", query={**SKILL_STORE, "limit": "100"}) or {}
    return {
        skill["display_title"]: skill["id"]
        for skill in listing.get("data") or []
        if isinstance(skill, dict) and skill.get("display_title") and skill.get("id")
    }


def sync_store(
    api: Api,
    skills: list[Skill],
    digests: dict[str, str],
    published: dict[str, str] | None,
    stored: dict[str, str],
) -> dict[str, str]:
    """Upload missing skills and replace drifted ones; return {name: created|replaced|unchanged}."""
    states = {}
    for skill in skills:
        exists = skill.name in stored
        drifted = published is not None and published.get(skill.name, "") != digests[skill.name]
        if exists and not drifted:
            states[skill.name] = "unchanged"
            continue
        if exists:
            skill_id = urllib.parse.quote(stored[skill.name], safe="")
            api.write(f"delete drifted skill {skill.name}", "DELETE", f"/v1/skills/{skill_id}", query=SKILL_STORE)
        body, content_type = multipart_form(
            {"display_title": skill.name, "description": skill.description},
            file_field="files[]",
            filename=f"{skill.name}.zip",
            payload=upload_archive(skill),
        )
        api.write(
            f"upload skill {skill.name}", "POST", "/v1/skills", query=SKILL_STORE, body=body, content_type=content_type
        )
        states[skill.name] = "replaced" if exists else "created"
    return states


def resolve_skill_ids(api: Api, skills: list[Skill], store: dict[str, str], stored: dict[str, str]) -> dict[str, str]:
    """Return {name: skill id} as the store holds them after sync_store.

    An upload mints a new id, so the ids are read back after a change. A
    check run uploaded nothing: its new and replaced skills get a
    placeholder, which only ever appears in the report.
    """
    if api.check:
        return {
            skill.name: stored[skill.name] if store[skill.name] == "unchanged" else PENDING_SKILL_ID for skill in skills
        }
    if any(state != "unchanged" for state in store.values()):
        stored = stored_skill_ids(api)
    missing = [skill.name for skill in skills if skill.name not in stored]
    if missing:
        raise ScriptError(f"not in the proxy store after the upload: {', '.join(missing)}")
    return stored


def sync_marketplace(
    api: Api,
    skills: list[Skill],
    digests: dict[str, str],
    skill_ids: dict[str, str],
    archive_base_url: str,
    *,
    store_changed: bool,
) -> dict[str, str]:
    """Create missing marketplace entries and refresh existing ones; return {name: state}.

    PUT is a full replace that bumps the entry's patch version, so existing
    entries are only refreshed on a run that changed the store. Reruns with
    unchanged skills then leave every version where it is.
    """
    states = {}
    for skill in skills:
        entry = {
            "source": {
                "source": "archive",
                "url": f"{archive_base_url.rstrip('/')}/v1/skills/{skill_ids[skill.name]}/archive",
                "sha256": digests[skill.name],
            },
            "description": skill.description,
            "domain": MARKETPLACE_DOMAIN,
            "keywords": [skill.name],
        }
        path = "/claude-code/plugins/" + urllib.parse.quote(skill.name, safe="")
        status, _ = api.request("GET", path)
        if status == 404:
            new_entry = {"name": skill.name, **entry, "version": MARKETPLACE_INITIAL_VERSION}
            api.write(f"create marketplace entry {skill.name}", "POST", "/claude-code/plugins", json_body=new_entry)
            states[skill.name] = "created"
        elif status == 200 and store_changed:
            api.write(f"update marketplace entry {skill.name}", "PUT", path, json_body=entry)
            states[skill.name] = "updated"
        elif status == 200:
            states[skill.name] = "unchanged"
        else:
            states[skill.name] = f"skipped (GET {path} returned {status})"
    return states


def search_smoke_test(api: Api, skills: list[Skill]) -> dict[str, Any] | None:
    """Ask semantic skill search for the first skill by name; None when search is not answering."""
    if not skills:
        return None
    query = skills[0].name
    status, found = api.request("GET", "/v1/skills", query={**SKILL_STORE, "query": query, "top_k": "3"})
    if status != 200 or not isinstance(found, dict):
        return None
    return {"query": query, "results": len(found.get("data") or [])}


def main(argv: list[str] | None = None) -> dict[str, Any]:
    parser = stack_api.argument_parser(__doc__, url="http://localhost:4000")
    parser.add_argument(
        "--archive-base-url", required=True, help="public https base URL of the proxy, e.g. https://litellm.example.com"
    )
    parser.add_argument(
        "--skills-dir", default=skill_packages.SKILLS_DIR, help="skill packages root (default: %(default)s)"
    )
    args = parser.parse_args(argv)
    api = Api.from_args(args, stack_api.secret("LITELLM_MASTER_KEY"))

    skills = skill_packages.discover(args.skills_dir)
    digests = {skill.name: index_digest(skill.source()) for skill in skills}

    published = published_digests(api)
    stored = stored_skill_ids(api)
    store = sync_store(api, skills, digests, published, stored)
    skill_ids = resolve_skill_ids(api, skills, store, stored)
    marketplace = sync_marketplace(
        api,
        skills,
        digests,
        skill_ids,
        args.archive_base_url,
        store_changed=any(state != "unchanged" for state in store.values()),
    )

    return api.report(
        index_available=published is not None,
        skills=[
            {
                "name": skill.name,
                "store": store[skill.name],
                "marketplace": marketplace[skill.name],
                "skill_id": skill_ids[skill.name],
                "sha256": digests[skill.name],
            }
            for skill in skills
        ],
        search=search_smoke_test(api, skills),
    )


if __name__ == "__main__":
    stack_api.run(main)
