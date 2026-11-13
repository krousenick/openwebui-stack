"""Publish the skill packages to LiteLLM: its Skill Hub, its legacy skill store, or both.

Runs in the litellm container (tasks/mcp/skills_gateway.yml). --store, the
role's litellm_skills_store, picks where the skills live
(https://docs.litellm.ai/docs/skills_gateway):

  legacy  The proxy-hosted skill store (POST /v1/skills,
          custom_llm_provider=litellm_proxy), plus one Claude Code
          marketplace row per skill, registered through the
          /claude-code/plugins routes that LiteLLM now marks deprecated,
          whose archive is that store's /v1/skills/{id}/archive. In LiteLLM
          1.110 only this store's skills can be named in a model request
          ("container": {"skills": [{"skill_id": ...}]} on
          /v1/chat/completions or /v1/messages, which puts their SKILL.md in
          the system prompt) or found by semantic skill search (GET
          /v1/skills?query=, the skill_search MCP tool).
  hub     The Skill Hub (/skill-hub/skills), the path LiteLLM recommends:
          stored archives with version history, in one registry that also
          feeds the marketplace and the hub entries of the discovery index.
          The legacy store's copies of the skills are deleted, because the
          index lists legacy-store skills first and renames a hub skill
          whose name one of them holds (name-2). Requests can then name no
          skill, and semantic search finds none.
  both    The hub as above, plus a copy of each skill kept current in the
          legacy store for those two. The discovery index then lists every
          skill twice: under its name from the store and as name-2 from the
          hub.

Each mode writes nothing on a rerun with unchanged skills, and each one
takes the skills over from the others, so the modes can follow one another
in any order.

Skill Hub, per package:

  not in the hub        POST /skill-hub/skills/upload creates it, version
                        1.0.0, public
  no archive of its own a row registered through /claude-code/plugins:
                        PUT /skill-hub/skills/{name}/upload moves the skill
                        into the hub ("migrated")
  SKILL.md differs      the same PUT stores it as the next patch version
  SKILL.md is the same  nothing: GET /skill-hub/skills/{name}/content
                        returns the stored file, compared byte for byte

An upload keeps the skill's domain, namespace, keywords and visibility. The
script never unpublishes or deletes a hub skill: an admin can unpublish one,
and a package removed from skills/ stays until it is deleted there. The hub
takes 10 uploads a minute per user; a 429 waits for the next window.

Legacy store, per package: a stored skill is matched by display_title (the
frontmatter name). The discovery index publishes the sha256 of each stored
archive, compared with the digest computed here for the checkout's
SKILL.md. The store has no update endpoint, so a drifted skill is deleted
and uploaded again. Without the index (public_skills_index off) there is
nothing to compare, and stored skills count as unchanged. The digest has
to be computed here, with the proxy's own zlib: DEFLATE output differs
between zlib builds. In legacy mode, a marketplace row whose source is not
the skill's current store archive is replaced (PUT, the next patch
version), which also takes a skill back from the hub.

Archive URLs: the hub builds its own from the proxy's PROXY_BASE_URL; the
legacy ones are built here from --archive-base-url (sources must be https).
A real run ends by reading the marketplace and the index as an anonymous
client: each public skill must be served from where the mode puts it,
under --archive-base-url, or the run fails.

Environment: LITELLM_MASTER_KEY.
Report keys: store (the mode); skills, for each skill its name, hub
(created, migrated, updated or unchanged; null in legacy mode), version and
visibility (the marketplace row's, after the run, before it with --check),
legacy_store (created, replaced or unchanged; in hub mode removed or
absent) and marketplace (created, updated or unchanged; null outside legacy
mode, where the hub keeps the row); served (how many skills the anonymous
marketplace and index serve as the mode expects; null with --check, and a
view is null when it is off or requires a key); search (query and result
count of a semantic-search smoke test, null in hub mode or when search is
not answering).
"""

from __future__ import annotations

import hashlib
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

STORES = ("legacy", "hub", "both")
HUB = "/skill-hub/skills"
PLUGINS = "/claude-code/plugins"
MARKETPLACE = "/claude-code/marketplace.json"
INDEX = "/.well-known/agent-skills/index.json"
LEGACY_STORE = "/v1/skills"
LEGACY_QUERY = {"custom_llm_provider": "litellm_proxy"}
HUB_PAGE_SIZE = 100  # the largest page GET /skill-hub/skills returns
# Retries of an upload the hub rate-limited (10 a minute per user, then 429
# with Retry-After: 60). A first run over every package needs one wait.
RATE_LIMIT_RETRIES = 3
MARKETPLACE_DOMAIN = "Engineering"
MARKETPLACE_INITIAL_VERSION = "1.0.0"
MULTIPART_BOUNDARY = "----ansible-skills-boundary"
PENDING_SKILL_ID = "assigned-on-upload"
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


def read_public(api: Api, path: str) -> tuple[int, Any]:
    """GET a route the way an anonymous client sees it: without the key."""
    return Api(api.base_url, timeout=api.timeout).request("GET", path)


# --- Skill Hub ---------------------------------------------------------------


def hub_skills(api: Api, *, required: bool) -> dict[str, dict[str, Any]]:
    """Return {name: skill} for every skill in the hub, as GET /skill-hub/skills lists them.

    Every marketplace row is a hub skill, whatever registered it. When not
    required (legacy mode), a proxy without the hub gives an empty listing.
    """
    skills: dict[str, dict[str, Any]] = {}
    offset = 0
    while True:
        status, page = api.request("GET", HUB, query={"limit": str(HUB_PAGE_SIZE), "offset": str(offset)})
        if status == 404 and not required:
            return {}
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
    """Send a skill's archive to a hub upload route; return the hub's skill (None on a check run)."""
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


# --- Legacy store ------------------------------------------------------------


def index_digest(source: bytes) -> str:
    """Return the sha256 the discovery index publishes for a SKILL.md in the legacy store.

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


def published_digests(index: Any) -> dict[str, str] | None:
    """Return {skill name: archive sha256} from a discovery index, or None when there is none."""
    if not isinstance(index, dict) or not isinstance(index.get("skills"), list):
        return None
    return {
        entry["name"]: str(entry.get("digest", "")).removeprefix("sha256:")
        for entry in index["skills"]
        if isinstance(entry, dict) and entry.get("name")
    }


def stored_skill_ids(api: Api) -> dict[str, str]:
    """Return {display_title: skill id} for the legacy store."""
    listing = api.get(LEGACY_STORE, query={**LEGACY_QUERY, "limit": "100"}) or {}
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
    """Upload missing skills to the legacy store and replace drifted ones; return {name: state}."""
    states = {}
    for skill in skills:
        exists = skill.name in stored
        drifted = published is not None and published.get(skill.name, "") != digests[skill.name]
        if exists and not drifted:
            states[skill.name] = "unchanged"
            continue
        if exists:
            path = f"{LEGACY_STORE}/{quoted(stored[skill.name])}"
            api.write(f"delete drifted skill {skill.name}", "DELETE", path, query=LEGACY_QUERY)
        body, content_type = multipart_form(
            {"display_title": skill.name, "description": skill.description},
            file_field="files[]",
            filename=f"{skill.name}.zip",
            payload=upload_archive(skill),
        )
        api.write(
            f"upload skill {skill.name}",
            "POST",
            LEGACY_STORE,
            query=LEGACY_QUERY,
            body=body,
            content_type=content_type,
        )
        states[skill.name] = "replaced" if exists else "created"
    return states


def resolve_skill_ids(api: Api, skills: list[Skill], store: dict[str, str], stored: dict[str, str]) -> dict[str, str]:
    """Return {name: legacy skill id} as the store holds them after sync_store.

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
        raise ScriptError(f"not in the legacy store after the upload: {', '.join(missing)}")
    return stored


def legacy_archive_url(base: str, legacy_id: str) -> str:
    return f"{base}{LEGACY_STORE}/{quoted(legacy_id)}/archive"


def sync_legacy_marketplace(
    api: Api, skills: list[Skill], digests: dict[str, str], legacy_ids: dict[str, str], base: str
) -> dict[str, tuple[str, str | None]]:
    """Point each skill's marketplace row at its legacy-store archive; return {name: (state, version)}.

    A row is replaced (PUT, a full replace that bumps its patch version)
    only when its source is not that archive: after a store upload, or when
    the row is a hub skill, which this takes back from the hub.
    """
    states = {}
    for skill in skills:
        source = {
            "source": "archive",
            "url": legacy_archive_url(base, legacy_ids[skill.name]),
            "sha256": digests[skill.name],
        }
        entry = {
            "source": source,
            "description": skill.description,
            "domain": MARKETPLACE_DOMAIN,
            "keywords": [skill.name],
        }
        path = f"{PLUGINS}/{quoted(skill.name)}"
        status, current = api.request("GET", path)
        if status == 404:
            new_entry = {"name": skill.name, **entry, "version": MARKETPLACE_INITIAL_VERSION}
            api.write(f"create marketplace entry {skill.name}", "POST", PLUGINS, json_body=new_entry)
            states[skill.name] = ("created", MARKETPLACE_INITIAL_VERSION)
        elif status == 200 and isinstance(current, dict) and current.get("source") == source:
            states[skill.name] = ("unchanged", current.get("version"))
        elif status == 200:
            updated = api.write(f"update marketplace entry {skill.name}", "PUT", path, json_body=entry)
            plugin = updated.get("plugin") if isinstance(updated, dict) else None
            states[skill.name] = ("updated", plugin.get("version") if isinstance(plugin, dict) else None)
        else:
            raise ScriptError(f"GET {path} returned {status}: {json.dumps(current)[:300]}")
    return states


def legacy_copies(api: Api, names: list[str], index: Any) -> dict[str, list[str]]:
    """Return {skill name: ids of its copies in the legacy store}.

    A copy is a legacy skill whose display_title is the skill's name (how
    this script uploads them) or one the discovery index serves under that
    name, whatever its title.
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


def search_smoke_test(api: Api, skills: list[Skill]) -> dict[str, Any] | None:
    """Ask semantic skill search for the first skill by name; None when search is not answering."""
    if not skills:
        return None
    query = skills[0].name
    status, found = api.request("GET", LEGACY_STORE, query={**LEGACY_QUERY, "query": query, "top_k": "3"})
    if status != 200 or not isinstance(found, dict):
        return None
    return {"query": query, "results": len(found.get("data") or [])}


# --- What clients see --------------------------------------------------------


def served_from(url: Any, base: str, route: str | None) -> bool:
    """Whether url is the proxy's route under the public base URL."""
    return isinstance(url, str) and bool(route) and url.startswith(f"{base}/") and route in url


def verify_served(api: Api, base: str, store: str, public: dict[str, str | None]) -> dict[str, int | None]:
    """Check what an anonymous client is served for each public skill; return how many each view serves.

    public maps each public skill to its legacy-store id (None in hub
    mode). The marketplace must serve the hub archive, or in legacy mode
    the store's. The index must list the skill once under its name: from
    the hub in hub mode, from the store otherwise, and in both mode also
    once from the hub (as name-2). A view is None when it is off (the
    index) or requires a key (claude_code_marketplace_auth_required, which
    also keeps hub skills out of the index).
    """
    status, marketplace = read_public(api, MARKETPLACE)
    if status in (401, 403):
        return {"marketplace": None, "index": None}
    if status != 200 or not isinstance(marketplace, dict):
        raise ScriptError(f"GET {MARKETPLACE} returned {status}: {json.dumps(marketplace)[:300]}")
    plugins = {plugin.get("name"): plugin for plugin in marketplace.get("plugins") or [] if isinstance(plugin, dict)}
    status, index = read_public(api, INDEX)
    entries = index.get("skills") if status == 200 and isinstance(index, dict) else None
    problems = []
    for name, legacy_id in public.items():
        hub_route = f"{HUB}/{quoted(name)}/archive"
        legacy_route = f"{LEGACY_STORE}/{quoted(legacy_id)}/archive" if legacy_id else None
        source = (plugins.get(name) or {}).get("source")
        url = source.get("url") if isinstance(source, dict) else None
        route = legacy_route if store == "legacy" else f"{PLUGINS}/{quoted(name)}/archive/"
        if not served_from(url, base, route):
            problems.append(f"the marketplace serves {name} from {url or 'nowhere'}")
        if entries is None:
            continue
        named, from_hub = [], []
        for entry in entries:
            if isinstance(entry, dict) and entry.get("name") == name:
                named.append(entry.get("url"))
            if isinstance(entry, dict) and hub_route in str(entry.get("url")):
                from_hub.append(entry.get("url"))
        served = len(named) == 1 and served_from(named[0], base, hub_route if store == "hub" else legacy_route)
        if store == "legacy":
            served = served and not from_hub
        elif store == "both":
            served = served and len(from_hub) == 1 and served_from(from_hub[0], base, hub_route)
        if not served:
            urls = list(dict.fromkeys(map(str, named + from_hub)))
            problems.append(f"the discovery index serves {name} from {', '.join(urls) or 'nowhere'}")
    if problems:
        expected = f"expected from where --store {store} puts it, under {base}"
        raise ScriptError("; ".join(problems) + f" ({expected}; the hub builds its URLs from PROXY_BASE_URL)")
    return {"marketplace": len(public), "index": None if entries is None else len(public)}


def main(argv: list[str] | None = None) -> dict[str, Any]:
    parser = stack_api.argument_parser(__doc__, url="http://localhost:4000")
    parser.add_argument("--store", required=True, choices=STORES, help="where the skills live: %(choices)s")
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
    names = [skill.name for skill in skills]
    current = hub_skills(api, required=args.store != "legacy")
    status, index = read_public(api, INDEX)
    index = index if status == 200 else None
    report = {
        name: {
            "name": name,
            "hub": None,
            "version": (current.get(name) or {}).get("version"),
            "visibility": (current.get(name) or {}).get("visibility"),
            "legacy_store": None,
            "marketplace": None,
        }
        for name in names
    }

    if args.store != "legacy":
        for skill in skills:
            state, stored = sync_skill(api, skill, current.get(skill.name))
            report[skill.name].update(
                hub=state,
                version=stored.get("version") if stored else None,
                visibility=stored.get("visibility") if stored else None,
            )

    legacy_ids: dict[str, str] = {}
    if args.store == "hub":
        copies = legacy_copies(api, names, index)
        remove_legacy_copies(api, copies)
        for name in names:
            report[name]["legacy_store"] = "removed" if copies[name] else "absent"
    else:
        digests = {skill.name: index_digest(skill.source()) for skill in skills}
        stored_ids = stored_skill_ids(api)
        store = sync_store(api, skills, digests, published_digests(index), stored_ids)
        legacy_ids = resolve_skill_ids(api, skills, store, stored_ids)
        for name in names:
            report[name]["legacy_store"] = store[name]
        if args.store == "legacy":
            for name, (state, version) in sync_legacy_marketplace(api, skills, digests, legacy_ids, base).items():
                if state == "created" and not api.check:
                    report[name]["visibility"] = "public"
                report[name].update(marketplace=state, version=version if version else report[name]["version"])

    public = {name: legacy_ids.get(name) for name in names if report[name]["visibility"] in ("public", None)}
    return api.report(
        store=args.store,
        skills=list(report.values()),
        served=None if api.check else verify_served(api, base, args.store, public),
        search=None if args.store == "hub" else search_smoke_test(api, skills),
    )


if __name__ == "__main__":
    stack_api.run(main)
