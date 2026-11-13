"""Fake LiteLLM and Open WebUI admin APIs for the container script tests.

Each fake is a real HTTP server on a local port with in-memory state, so
the scripts run unmodified: only --url points somewhere else.
"""

from __future__ import annotations

import dataclasses
import hashlib
import io
import json
import pathlib
import re
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "container-scripts"
TOKEN = "test-token"


@dataclasses.dataclass
class Request:
    method: str
    path: str
    query: dict[str, str]
    headers: Any
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body) if self.body else None


class FakeService:
    """An in-memory HTTP API on a local port (a free one by default).

    Subclasses route requests in handle(), which returns (status, payload)
    or (status, payload, response headers). A payload is sent as JSON, or as
    is when it is bytes.
    """

    def __init__(self, port: int = 0) -> None:
        service = self

        class Handler(BaseHTTPRequestHandler):
            def serve(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                url = urlsplit(self.path)
                request = Request(
                    method=self.command,
                    path=unquote(url.path),
                    query={key: values[0] for key, values in parse_qs(url.query).items()},
                    headers=self.headers,
                    body=self.rfile.read(length) if length else b"",
                )
                service.requests.append(request)
                status, payload, *extra = service.handle(request)
                if isinstance(payload, bytes):
                    raw, content_type = payload, "application/octet-stream"
                else:
                    raw, content_type = b"" if payload is None else json.dumps(payload).encode(), "application/json"
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                for name, value in (extra[0] if extra else {}).items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = serve

            def log_message(self, *args: Any) -> None:
                pass

        self.requests: list[Request] = []
        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def handle(self, request: Request) -> tuple[Any, ...]:
        raise NotImplementedError

    def authorized(self, request: Request) -> bool:
        return request.headers.get("Authorization") == f"Bearer {TOKEN}"

    def writes(self) -> list[str]:
        """Every non-GET request so far, as "METHOD path"."""
        return [f"{request.method} {request.path}" for request in self.requests if request.method != "GET"]

    def forget_requests(self) -> None:
        self.requests.clear()


def parse_multipart(request: Request) -> dict[str, bytes]:
    boundary = request.headers["Content-Type"].split("boundary=")[1].encode()
    fields = {}
    for part in request.body.split(b"--" + boundary):
        head, separator, content = part.partition(b"\r\n\r\n")
        if not separator:
            continue
        name = re.search(rb'name="([^"]+)"', head).group(1).decode()
        fields[name] = content.removesuffix(b"\r\n")
    return fields


def repack_digest(upload: bytes) -> tuple[str, bytes]:
    """Digest an uploaded skill zip the way the proxy's discovery index does.

    Written from the documented repack rules, independently of the script,
    so a matching digest proves the script predicts the index correctly.
    """
    with zipfile.ZipFile(io.BytesIO(upload)) as archive:
        (member,) = [name for name in archive.namelist() if name.endswith("/SKILL.md")]
        source = archive.read(member)
    info = zipfile.ZipInfo("SKILL.md", date_time=(1980, 1, 1, 0, 0, 0))
    info.create_system = 3
    info.external_attr = 0o644 << 16
    repacked = io.BytesIO()
    with zipfile.ZipFile(repacked, "w") as archive:
        archive.writestr(info, source, compress_type=zipfile.ZIP_DEFLATED)
    return hashlib.sha256(repacked.getvalue()).hexdigest(), source


def skill_md_fields(skill_md: bytes) -> dict[str, str]:
    """The frontmatter fields of a SKILL.md: one-line values and >- folded blocks.

    Enough YAML for the test skills, written apart from skill_packages so the
    fake reads uploads independently of the scripts.
    """
    match = re.match(r"---\n(.*?)\n---\n", skill_md.decode("utf-8"), re.S)
    lines = match.group(1).split("\n") if match else []
    fields: dict[str, str] = {}
    while lines:
        key, _, value = lines.pop(0).partition(":")
        value = value.strip()
        if value in (">", ">-"):
            folded = []
            while lines and lines[0][:1] in (" ", "\t"):
                folded.append(lines.pop(0).strip())
            value = " ".join(folded)
        fields[key.strip()] = value.strip("\"'")
    return fields


def slug(text: str) -> str:
    """A name as the discovery index writes it: lowercase words joined by hyphens."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:64].rstrip("-")


def deduplicated(names: list[str], reserved: frozenset[str] = frozenset()) -> list[str]:
    """Give each name the first of name, name-2, name-3 ... that nothing else holds, like the index."""
    taken = set(reserved)
    natural = set(names) | taken
    chosen_names = []
    for name in names:
        chosen, ordinal = name, 1
        while chosen in taken or (chosen != name and chosen in natural):
            ordinal += 1
            chosen = f"{name}-{ordinal}"
        taken.add(chosen)
        chosen_names.append(chosen)
    return chosen_names


def skill_files(upload: bytes) -> dict[str, bytes] | None:
    """An uploaded skill zip's files relative to its SKILL.md: at the root or in one top-level folder."""
    with zipfile.ZipFile(io.BytesIO(upload)) as archive:
        files = {info.filename: archive.read(info) for info in archive.infolist() if not info.is_dir()}
    roots = {path.split("/", 1)[0] for path in files}
    prefix = f"{roots.pop()}/" if len(roots) == 1 and all("/" in path for path in files) else ""
    members = {path.removeprefix(prefix): data for path, data in files.items() if path.startswith(prefix)}
    return members if "SKILL.md" in members else None


def packed(files: dict[str, bytes]) -> bytes:
    """A zip of the files in name order with fixed timestamps, so equal files give equal bytes."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name in sorted(files):
            archive.writestr(zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0)), files[name])
    return buffer.getvalue()


class FakeLiteLLM(FakeService):
    def __init__(self, port: int = 0) -> None:
        super().__init__(port)
        self.index_enabled = True
        self.search_enabled = True
        self.skills: dict[str, dict[str, Any]] = {}
        self.plugins: dict[str, dict[str, Any]] = {}
        # Skill Hub rows by name (see the hub methods at the end). Rows the
        # /claude-code/plugins routes registered wait in self.plugins until a
        # hub view reads them (hub_row).
        self.hub: dict[str, dict[str, Any]] = {}
        self.hub_enabled = True  # False: an older proxy without /skill-hub
        self.hub_clock = 0
        self.proxy_base_url = "https://litellm.example.com"  # PROXY_BASE_URL
        self.hub_page_cap = 100  # the most rows one GET /skill-hub/skills page holds
        self.hub_upload_limit = 10  # uploads per rate window, then 429
        self.hub_uploads_in_window = 0
        self.hub_new_private = False  # True: new uploads start unpublished
        self.hub_review_uploads = False  # True: uploads become submissions, as for a non-admin key
        self.marketplace_auth_required = False  # general_settings.claude_code_marketplace_auth_required
        self.models: list[dict[str, Any]] = []
        self.minted = 0
        # False: PATCH answers 200 but /model/info keeps the old entry, like a
        # worker that has not reloaded the database yet.
        self.apply_patches = True
        # What a wildcard deployment expands to in /model/info: LiteLLM 1.110
        # (expand_wildcard_deployments_for_model_info) adds one row per known
        # provider model, copying the deployment's model_info, id included.
        self.wildcard_names: dict[str, list[str]] = {
            "anthropic/*": ["anthropic/claude-haiku-9", "anthropic/claude-opus-9"],
        }
        # False: /model/new stores the deployment but /model/info does not
        # show it, like a worker that has not loaded the database change.
        self.list_new_models = True
        self.deployments_created = 0

    def handle(self, request: Request) -> tuple[int, Any]:
        method, path = request.method, request.path
        # Routes an anonymous client may read.
        if path == "/.well-known/agent-skills/index.json":
            return self.discovery_index()
        if path == "/claude-code/marketplace.json":
            return self.marketplace(request)
        content = re.fullmatch(r"/skill-hub/skills/([^/]+)/content", path)
        if content and method == "GET":
            return self.hub_content(request, content.group(1))
        if not self.authorized(request):
            return 401, {"error": "invalid proxy key"}
        if path == "/model/info":
            return 200, {"data": self.models}
        patch = re.fullmatch(r"/model/([^/]+)/update", path)
        if patch and method == "PATCH":
            return self.patch_model(patch.group(1), request.json())
        if path == "/model/new" and method == "POST":
            return self.add_model(request.json())
        if path == "/model/delete" and method == "POST":
            return self.delete_model(request.json().get("id"))
        if path == "/skill-hub/skills" or path.startswith("/skill-hub/skills/"):
            return self.handle_hub(method, path.removeprefix("/skill-hub/skills"), request)
        plugin = re.fullmatch(r"/claude-code/plugins(?:/([^/]+))?", path)
        answered = self.plugin_route_on_hub_row(method, plugin.group(1), request) if plugin else None
        if answered is not None:
            return answered
        if path == "/v1/skills" and method == "GET":
            skills = list(self.skills.values())
            if "query" in request.query:
                if not self.search_enabled:
                    return 500, {"error": "no embedding model"}
                skills = [s for s in skills if request.query["query"] in s["display_title"]]
                skills = skills[: int(request.query["top_k"])]
            return 200, {"data": [{"id": s["id"], "display_title": s["display_title"]} for s in skills]}
        if path == "/v1/skills" and method == "POST":
            fields = parse_multipart(request)
            digest, source = repack_digest(fields["files[]"])
            self.minted += 1
            skill = {
                "id": f"skill_{self.minted}",
                "display_title": fields["display_title"].decode(),
                "description": fields["description"].decode(),
                "digest": digest,
                "source": source.decode(),
            }
            self.skills[skill["id"]] = skill
            return 200, {"id": skill["id"]}
        if path.startswith("/v1/skills/") and method == "DELETE":
            return (200, {"deleted": True}) if self.skills.pop(path.rsplit("/", 1)[1], None) else (404, None)
        if path == "/claude-code/plugins" and method == "POST":
            entry = request.json()
            if entry["name"] in self.plugins:
                return 409, {"error": "exists"}
            self.plugins[entry["name"]] = entry
            return 200, entry
        if path.startswith("/claude-code/plugins/"):
            name = path.rsplit("/", 1)[1]
            if name not in self.plugins:
                return 404, {"error": "not found"}
            if method == "PUT":
                major, minor, patch = self.plugins[name]["version"].split(".")
                self.plugins[name] = {"name": name, **request.json(), "version": f"{major}.{minor}.{int(patch) + 1}"}
            return 200, self.plugins[name]
        return 404, {"detail": f"no route for {method} {path}"}

    def titles(self) -> list[str]:
        return sorted(skill["display_title"] for skill in self.skills.values())

    def patch_model(self, model_id: str, body: dict[str, Any]) -> tuple[int, Any]:
        """PATCH /model/{id}/update as LiteLLM 1.110 merges it (update_db_model).

        Written from the proxy's source: litellm_params and model_info merge
        key by key, a None value is ignored, and a litellm_params key goes
        only when litellm_params_to_remove names it.
        """
        # A wildcard deployment shows as several /model/info rows, all one
        # deployment in the database: the change reaches every row.
        entries = [m for m in self.models if (m.get("model_info") or {}).get("id") == model_id]
        if not entries:
            return 404, {"error": f"Model {model_id} not found on proxy."}
        if not self.apply_patches:
            return 200, entries[0]
        for entry in entries:
            if body.get("model_name"):
                entry["model_name"] = body["model_name"]
            params = entry.setdefault("litellm_params", {})
            params.update(
                {key: value for key, value in (body.get("litellm_params") or {}).items() if value is not None}
            )
            for key in body.get("litellm_params_to_remove") or ():
                params.pop(key, None)
            info = entry.setdefault("model_info", {})
            info.update({key: value for key, value in (body.get("model_info") or {}).items() if value is not None})
        return 200, entries[0]

    def add_model(self, body: dict[str, Any]) -> tuple[int, Any]:
        """POST /model/new: store the deployment, listed (expanded) unless list_new_models is off."""
        self.deployments_created += 1
        model_id = f"deployment-{self.deployments_created}"
        info = {**(body.get("model_info") or {}), "id": model_id}
        if self.list_new_models:
            for name in self.wildcard_names.get(body["model_name"], [body["model_name"]]):
                params = {**(body.get("litellm_params") or {}), "model": name}
                self.models.append({"model_name": name, "litellm_params": params, "model_info": dict(info)})
        return 200, {"model_id": model_id, "model_name": body["model_name"], "model_info": info}

    def delete_model(self, model_id: object) -> tuple[int, Any]:
        """POST /model/delete: drop every row of that deployment."""
        kept = [m for m in self.models if (m.get("model_info") or {}).get("id") != model_id]
        if len(kept) == len(self.models):
            return 400, {"error": f"model {model_id} not found"}
        self.models = kept
        return 200, {"message": f"Model: {model_id} deleted successfully"}

    # The Skill Hub as LiteLLM 1.110 serves it to the master key, written
    # from the proxy's source (proxy/discovery_endpoints/skill_hub_endpoints.py,
    # skill_hub_mutations.py, repositories/plugin_publication.py): one row
    # per name in the table the Claude Code marketplace also reads. A proxy
    # admin's upload is published at once: a new row is public at 1.0.0, a
    # replacement keeps the row's other manifest fields (domain, keywords)
    # and its visibility and bumps the patch version, and each publication
    # is kept as a version. Uploads and replacements share a rate limit (10
    # a minute per user). A row holds its own archive once it carries
    # agent_skill metadata; the rows registered through /claude-code/plugins
    # (add_marketplace_row, or the routes in handle) point elsewhere, such as
    # the legacy store. A PUT there rebuilds the manifest from its body, so
    # the row loses its archive; LiteLLM keeps it only for an unchanged
    # source, which the publisher never sends. GET and PUT there on a hub
    # row are answered by plugin_route_on_hub_row.

    def hub_stamp(self) -> str:
        self.hub_clock += 1
        minutes, seconds = divmod(self.hub_clock, 60)
        return f"2026-11-12T12:{minutes:02d}:{seconds:02d}.000000Z"

    def hub_row(self, name: str) -> dict[str, Any] | None:
        """The table row for a name, taking over an entry the /claude-code/plugins routes registered."""
        if name not in self.hub and name in self.plugins:
            entry = self.plugins.pop(name)
            stamp = self.hub_stamp()
            self.hub[name] = {
                "id": f"hub-{self.hub_clock}",
                "name": name,
                "version": entry.get("version"),
                "description": entry.get("description"),
                "enabled": True,
                "manifest": dict(entry),
                "content": None,
                "skill_md": None,
                "created_at": stamp,
                "updated_at": stamp,
                "versions": [{"version": entry.get("version"), "manifest": dict(entry)}],
            }
        return self.hub.get(name)

    def table(self) -> list[dict[str, Any]]:
        """Every row, by name."""
        for name in list(self.plugins):
            self.hub_row(name)
        return sorted(self.hub.values(), key=lambda row: row["name"])

    def plugin_route_on_hub_row(self, method: str, name: str | None, request: Request) -> tuple[Any, ...] | None:
        """/claude-code/plugins for a name the hub holds; None leaves the request to those routes in handle."""
        if name is None:
            if method == "POST" and (request.json() or {}).get("name") in self.hub:
                return 409, {"error": "exists"}
            return None
        row = self.hub_row(name)
        if row is None:
            return None
        if method == "GET":
            return 200, {
                "id": row["id"],
                "name": name,
                "version": row["version"],
                "description": row["description"],
                "source": row["manifest"].get("source"),
                "keywords": row["manifest"].get("keywords"),
                "enabled": row["enabled"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
        if method == "PUT":
            # A full replace: the manifest is rebuilt from the body, the patch
            # version bumped, and the row's own archive dropped.
            body = request.json()
            major, minor, patch = row["version"].split(".")
            version = f"{major}.{minor}.{int(patch) + 1}"
            manifest = {"name": name, **{key: value for key, value in body.items() if value}, "version": version}
            row.update(
                version=version,
                description=body.get("description"),
                manifest=manifest,
                content=None,
                skill_md=None,
                updated_at=self.hub_stamp(),
            )
            row["versions"].append({"version": version, "manifest": manifest})
            plugin = {"id": row["id"], "name": name, "version": version, "source": body.get("source")}
            return 200, {"status": "success", "action": "updated", "plugin": {**plugin, "enabled": row["enabled"]}}
        return None

    def hub_view(self, row: dict[str, Any]) -> dict[str, Any]:
        """A row as GET /skill-hub/skills shows it (SkillHubSkill)."""
        manifest = row["manifest"]
        managed = "agent_skill" in manifest
        route = f"{self.proxy_base_url}/skill-hub/skills/{row['name']}"
        return {
            "id": row["id"],
            "name": row["name"],
            "description": row["description"],
            "version": row["version"],
            "updated_at": row["updated_at"],
            "created_at": row["created_at"],
            "created_by": "default_user_id",
            "approved_by": None,
            "published_by": "default_user_id",
            "security_scan_passed": False,
            "visibility": "public" if row["enabled"] else "private",
            "source": {"source": "archive", "url": f"{route}/archive"} if managed else manifest["source"],
            "download_url": f"{route}/archive" if managed else None,
            "content_url": f"{route}/content" if managed else None,
            "domain": manifest.get("domain"),
            "namespace": manifest.get("namespace"),
            "keywords": manifest.get("keywords"),
            "download_count": None,
        }

    def handle_hub(self, method: str, path: str, request: Request) -> tuple[Any, ...]:
        if not self.hub_enabled:
            return 404, {"detail": "Not Found"}
        if path == "" and method == "GET":
            return self.hub_list(request.query)
        if path == "/upload" and method == "POST":
            return self.hub_upload(request, None)
        name, _, action = path.strip("/").partition("/")
        if action == "upload" and method == "PUT":
            return self.hub_upload(request, name)
        row = self.hub_row(name)
        if row is None:
            return 404, {"detail": "Skill not found"}
        if action == "" and method == "GET":
            return 200, self.hub_view(row)
        if action in ("publish", "unpublish") and method == "POST":
            row["enabled"] = action == "publish"
            row["updated_at"] = self.hub_stamp()
            return 200, self.hub_view(row)
        return 404, {"detail": f"no route for {method} /skill-hub/skills{path}"}

    def hub_list(self, query: dict[str, str]) -> tuple[int, Any]:
        """GET /skill-hub/skills: rows by name, a substring query, offset/limit pages (limit 1-100)."""
        limit, offset = int(query.get("limit", 50)), int(query.get("offset", 0))
        if not 1 <= limit <= 100:
            return 422, {"detail": "limit must be between 1 and 100"}
        needle = query.get("query", "").casefold()
        table = self.table()
        rows = [
            row
            for row in table
            if needle
            in " ".join((row["name"], row["description"] or "", *(row["manifest"].get("keywords") or ()))).casefold()
        ]
        size = min(limit, self.hub_page_cap)
        return 200, {
            "skills": [self.hub_view(row) for row in rows[offset : offset + size]],
            "count": len(rows),
            "next_offset": offset + size if offset + size < len(rows) else None,
            "tags": sorted({word for row in table for word in row["manifest"].get("keywords") or ()}),
        }

    def hub_upload(self, request: Request, name: str | None) -> tuple[Any, ...]:
        """POST /skill-hub/skills/upload (name None) and PUT /skill-hub/skills/{name}/upload."""
        if self.hub_uploads_in_window >= self.hub_upload_limit:
            limited = {"detail": "Skills Hub allows 10 uploads or imports per minute per user"}
            return 429, limited, {"Retry-After": "60"}
        self.hub_uploads_in_window += 1
        fields = parse_multipart(request)
        files = skill_files(fields.get("file", b""))
        declared = skill_md_fields(files["SKILL.md"]) if files else {}
        if not declared.get("name") or not declared.get("description"):
            return 422, {"detail": "Upload a valid Agent Skill with name and description in SKILL.md"}
        row = self.hub_row(name or declared["name"])
        if name is None and row is not None:
            return 409, {"detail": f"A skill named '{row['name']}' already exists. Update the existing skill instead."}
        if name is not None:
            if row is None:
                return 404, {"detail": "Skill not found"}
            expected = fields.get("expected_updated_at")
            if expected is not None and expected.decode() != row["updated_at"]:
                return 409, {"detail": "The published skill changed. Reload it before submitting."}
            if declared["name"] != name:
                return 422, {"detail": "Replacement SKILL.md must declare the existing skill name and a description"}
        if self.hub_review_uploads:
            return 200, {"status": "pending_review", "action": "submitted", "submission_id": "submission-1"}
        skill = declared["name"]
        plugin = packed(
            {
                ".claude-plugin/plugin.json": json.dumps({"name": skill}).encode(),
                **{f"skills/{skill}/{path}": data for path, data in files.items()},
            }
        )
        sha256 = hashlib.sha256(plugin).hexdigest()
        published = {
            "name": skill,
            "description": declared["description"],
            "source": {
                "source": "archive",
                "url": f"{self.proxy_base_url}/claude-code/plugins/{skill}/archive/{sha256}.zip",
                "sha256": sha256,
            },
            "agent_skill": {
                "name": skill,
                "description": declared["description"],
                "digest": "sha256:" + hashlib.sha256(packed(files)).hexdigest(),
                "origin": None,
            },
        }
        stamp = self.hub_stamp()
        if row is None:
            row = self.hub[skill] = {
                "id": f"hub-{self.hub_clock}",
                "name": skill,
                "enabled": not self.hub_new_private,
                "created_at": stamp,
                "versions": [],
            }
            version, manifest = "1.0.0", published
        else:
            major, minor, patch = row["version"].split(".")
            version, manifest = f"{major}.{minor}.{int(patch) + 1}", {**row["manifest"], **published}
        manifest = {**manifest, "version": version}
        row.update(
            version=version,
            description=declared["description"],
            manifest=manifest,
            content=plugin,
            skill_md=files["SKILL.md"],
            updated_at=stamp,
        )
        row["versions"].append({"version": version, "manifest": manifest})
        return 200, self.hub_view(row)

    def hub_content(self, request: Request, name: str) -> tuple[Any, ...]:
        """GET /skill-hub/skills/{name}/content: the stored SKILL.md, for a key or, with the index on, anyone."""
        keyed = "Authorization" in request.headers
        if (keyed and not self.authorized(request)) or (
            not keyed and (not self.index_enabled or self.marketplace_auth_required)
        ):
            return 401, {"error": "invalid proxy key"}
        row = self.hub_row(name)
        if row is None or (not keyed and not row["enabled"]):
            return 404, {"detail": "Skill not found"}
        if "agent_skill" not in row["manifest"]:
            return 404, {"detail": "This skill no longer refers to a managed archive"}
        return 200, row["skill_md"]

    def marketplace(self, request: Request) -> tuple[Any, ...]:
        """GET /claude-code/marketplace.json: the public rows, to anyone unless a key is required."""
        if self.marketplace_auth_required and not self.authorized(request):
            return 401, {"error": "Authentication required"}, {"WWW-Authenticate": "Bearer"}
        plugins = []
        for row in self.table():
            if row["enabled"] and "source" in row["manifest"]:
                entry = {"name": row["name"], "source": row["manifest"]["source"], "version": row["version"]}
                if row["description"]:
                    entry["description"] = row["description"]
                if "keywords" in row["manifest"]:
                    entry["keywords"] = row["manifest"]["keywords"]
                plugins.append(entry)
        return 200, {"name": "litellm", "owner": {"name": "LiteLLM", "email": "support@litellm.ai"}, "plugins": plugins}

    def discovery_index(self) -> tuple[int, Any]:
        """The Agent Skills index as 1.110 builds it (agent_skills_endpoints.py).

        Legacy-store skills first, oldest first, named after their SKILL.md;
        then the public hub rows holding their own archive, unless the
        marketplace requires a key. A hub row whose name a legacy skill
        holds is listed as name-2.
        """
        if not self.index_enabled:
            return 404, {"detail": "Not Found"}
        legacy = list(self.skills.values())
        legacy_names = deduplicated(
            [slug(skill_md_fields(s["source"].encode()).get("name") or s["display_title"]) for s in legacy]
        )
        entries = [
            {
                "name": name,
                "type": "archive",
                "description": skill["description"],
                "url": f"{self.proxy_base_url}/v1/skills/{skill['id']}/archive",
                "digest": "sha256:" + skill["digest"],
            }
            for skill, name in zip(legacy, legacy_names, strict=True)
        ]
        hub = [
            row
            for row in self.table()
            if row["enabled"] and "agent_skill" in row["manifest"] and not self.marketplace_auth_required
        ]
        for row, name in zip(hub, deduplicated([row["name"] for row in hub], frozenset(legacy_names)), strict=True):
            digest = row["manifest"]["agent_skill"]["digest"]
            entries.append(
                {
                    "name": name,
                    "type": "archive",
                    "description": row["description"],
                    "url": f"{self.proxy_base_url}/skill-hub/skills/{row['name']}/archive?digest={digest}",
                    "digest": digest,
                }
            )
        # Key order as the live proxy serializes it: "$schema" comes first,
        # which a prefix test for '{"skills"' would miss.
        return 200, {"$schema": "https://schemas.agentskills.io/discovery/0.2.0/schema.json", "skills": entries}

    def reset_rate_window(self) -> None:
        """Open a new hub rate window, as a minute passing would."""
        self.hub_uploads_in_window = 0

    def add_legacy_skill(self, title: str, skill_md: bytes) -> str:
        """Store a skill in the legacy store the way POST /v1/skills does; return its id."""
        upload = io.BytesIO()
        with zipfile.ZipFile(upload, "w") as archive:
            archive.writestr(f"{title}/SKILL.md", skill_md)
        digest, source = repack_digest(upload.getvalue())
        self.minted += 1
        skill_id = f"skill_{self.minted}"
        self.skills[skill_id] = {
            "id": skill_id,
            "display_title": title,
            "description": skill_md_fields(skill_md).get("description", ""),
            "digest": digest,
            "source": source.decode(),
        }
        return skill_id

    def add_marketplace_row(self, name: str, legacy_id: str) -> None:
        """A hub row as the earlier publisher left it, read from the live proxy on 2026-11-12.

        Registered through POST /claude-code/plugins with the legacy store's
        archive as its source: no agent_skill metadata, no stored archive.
        """
        legacy = self.skills[legacy_id]
        stamp = self.hub_stamp()
        manifest = {
            "name": name,
            "domain": "Engineering",
            "source": {
                "url": f"{self.proxy_base_url}/v1/skills/{legacy_id}/archive",
                "sha256": legacy["digest"],
                "source": "archive",
            },
            "version": "1.0.0",
            "keywords": [name],
            "description": legacy["description"],
            "_litellm_published_by": "default_user_id",
        }
        self.hub[name] = {
            "id": f"hub-{self.hub_clock}",
            "name": name,
            "version": "1.0.0",
            "description": legacy["description"],
            "enabled": True,
            "manifest": manifest,
            "content": None,
            "skill_md": None,
            "created_at": stamp,
            "updated_at": stamp,
            "versions": [{"version": "1.0.0", "manifest": manifest}],
        }


# Open WebUI 0.13.3 MfaConfigForm fields; written from its source, not
# imported from the script under test.
MFA_FIELDS = ("ENABLE_MFA", "MFA_ALLOW_OAUTH_BYPASS", "MFA_ALLOW_TRUSTED_HEADER_BYPASS")


class FakeOpenWebUI(FakeService):
    def __init__(self, port: int = 0) -> None:
        super().__init__(port)
        self.served: list[dict[str, Any]] = []
        self.config: dict[str, Any] = {"DEFAULT_MODELS": "", "MODEL_ORDER_LIST": ["kept"]}
        self.ignore_config_writes = False
        # GET /api/v1/auths/admin/config as 0.13.3 returns it (a subset).
        self.admin_config: dict[str, Any] = {
            "ENABLE_MFA": False,
            "MFA_ALLOW_OAUTH_BYPASS": False,
            "MFA_ALLOW_TRUSTED_HEADER_BYPASS": False,
            "WEBUI_URL": "https://chat.example.test",
            "ENABLE_SIGNUP": False,
            "DEFAULT_USER_ROLE": "user",
            "DEFAULT_GROUP_ID": "",
            "JWT_EXPIRES_IN": "4w",
            "I18N": {},
            "SKILL_CONTENT_MAX_CHARS": None,
            "DEFAULT_INTERFACE_SETTINGS": {"chatBubble": False},
        }
        self.sessions_revoked_on_save = False
        self.skills: dict[str, dict[str, Any]] = {}
        self.skill_lookup_by_id = True
        self.functions: dict[str, dict[str, Any]] = {}
        self.model_rows: dict[str, dict[str, Any]] = {}

    def handle(self, request: Request) -> tuple[int, Any]:
        if not self.authorized(request):
            return 401, {"detail": "Unauthorized"}
        method, path = request.method, request.path
        if path == "/api/models":
            return 200, {"data": self.served}
        if path == "/api/v1/configs/models":
            if method == "POST" and not self.ignore_config_writes:
                self.config = request.json()
            return 200, self.config
        if path == "/api/v1/auths/admin/config":
            return self.handle_admin_config(method, request)
        if path.startswith("/api/v1/skills"):
            return self.handle_skills(method, path.removeprefix("/api/v1/skills"), request)
        if path.startswith("/api/v1/functions"):
            return self.handle_functions(method, path.removeprefix("/api/v1/functions"), request)
        if path == "/api/v1/models/all":
            return 200, list(self.model_rows.values())
        if path == "/api/v1/models/create":
            row = request.json()
            self.model_rows[row["id"]] = row
            return 200, row
        if path == "/api/v1/models/model/update":
            form = request.json()
            row = self.model_rows[form["id"]]
            row.update(name=form["name"], meta=form["meta"], params=form["params"])
            # 0.13.3 keeps the stored active state unless the form sets it.
            if "is_active" in form:
                row["is_active"] = form["is_active"]
            return 200, row
        if path == "/api/v1/models/model/delete":
            if self.model_rows.pop(request.json()["id"], None) is None:
                return 401, {"detail": "NOT_FOUND"}
            return 200, True
        return 404, {"detail": f"no route for {method} {path}"}

    def handle_admin_config(self, method: str, request: Request) -> tuple[int, Any]:
        """The admin config as 0.13.3 saves it: a form, with its MFA guard and I18N rule."""
        if method != "POST":
            return 200, self.admin_config
        form = request.json()
        if any(field in form for field in MFA_FIELDS):
            # An API key is not a human admin session; Open WebUI refuses
            # the MFA fields from it even when they are unchanged.
            return 403, {"detail": "A human administrator session is required to change MFA settings."}
        if "I18N" in form and form["I18N"] is None:
            return 422, {"detail": "I18N must be a dictionary"}
        if not self.ignore_config_writes:
            self.admin_config.update(form)
        return 200, {**self.admin_config, "sessions_revoked": self.sessions_revoked_on_save}

    def handle_skills(self, method: str, path: str, request: Request) -> tuple[int, Any]:
        if path == "/":
            return 200, list(self.skills.values())
        if path == "/create":
            form = request.json()
            if form["id"] in self.skills:
                return 400, {"detail": "id taken"}
            self.skills[form["id"]] = {**form, "version_id": "v1"}
            return 200, self.skills[form["id"]]
        skill_id = path.split("/")[2]
        if skill_id not in self.skills:
            return 404, {"detail": "not found"}
        if path.endswith("/update"):
            form = request.json()
            skill = self.skills[skill_id]
            if form["expected_version_id"] != skill["version_id"]:
                return 409, {"detail": "version conflict"}
            skill.update(
                name=form["name"],
                description=form["description"],
                content=form["content"],
                version_id=f"v{int(skill['version_id'][1:]) + 1}",
            )
            return 200, skill
        return (200, self.skills[skill_id]) if self.skill_lookup_by_id else (404, {"detail": "not found"})

    def handle_functions(self, method: str, path: str, request: Request) -> tuple[int, Any]:
        if path == "/create":
            form = request.json()
            self.functions[form["id"]] = {**form, "is_active": False, "is_global": False}
            return 200, self.functions[form["id"]]
        function_id = path.split("/")[2]
        if function_id not in self.functions:
            return 401, {"detail": "NOT_FOUND"}
        function = self.functions[function_id]
        if path.endswith("/update"):
            function.update(request.json())
        elif path.endswith("/toggle/global"):
            function["is_global"] = not function["is_global"]
        elif path.endswith("/toggle"):
            function["is_active"] = not function["is_active"]
        return 200, function


def write_skill(root: pathlib.Path, package: str, frontmatter: str, *, name: str | None = None, body: str = "") -> None:
    """Write root/<package>/SKILL.md; the frontmatter name defaults to the package name."""
    (root / package).mkdir(parents=True, exist_ok=True)
    text = f"---\nname: {name or package}\n{frontmatter}\n---\n\n# {package}\n{body}"
    (root / package / "SKILL.md").write_text(text, encoding="utf-8", newline="\n")
