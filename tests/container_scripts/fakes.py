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
    """An in-memory HTTP API on a local port (a free one by default). Subclasses route requests in handle()."""

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
                status, payload = service.handle(request)
                raw = b"" if payload is None else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
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

    def handle(self, request: Request) -> tuple[int, Any]:
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


class FakeLiteLLM(FakeService):
    def __init__(self, port: int = 0) -> None:
        super().__init__(port)
        self.index_enabled = True
        self.search_enabled = True
        self.skills: dict[str, dict[str, Any]] = {}
        self.plugins: dict[str, dict[str, Any]] = {}
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
        # /v1/memory rows by key, and the clock that stamps them.
        self.memories: dict[str, dict[str, Any]] = {}
        self.memory_clock = 0
        # /v1/embeddings: False answers 500, like a model that is down.
        self.embeddings_enabled = True
        self.embedded: list[str] = []

    def handle(self, request: Request) -> tuple[int, Any]:
        method, path = request.method, request.path
        if path == "/.well-known/agent-skills/index.json":
            if not self.index_enabled:
                return 404, {"detail": "Not Found"}
            entries = [
                {
                    "name": skill["display_title"],
                    "type": "skill-md",
                    "description": skill["description"],
                    "url": f"/v1/skills/{skill['id']}/archive",
                    "digest": "sha256:" + skill["digest"],
                }
                for skill in self.skills.values()
            ]
            # Key order as the live proxy serializes it: "$schema" comes
            # first, which a prefix test for '{"skills"' would miss.
            return 200, {"$schema": "https://schemas.agentskills.io/discovery/0.2.0/schema.json", "skills": entries}
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
        if path == "/v1/memory":
            return self.list_memories(method, request.query)
        if path.startswith("/v1/memory/"):
            return self.memory_row(method, path.removeprefix("/v1/memory/"), request.json())
        if path == "/v1/embeddings" and method == "POST":
            return self.embed(request.json())
        return 404, {"detail": f"no route for {method} {path}"}

    # /v1/memory as LiteLLM 1.110 serves it to the master key, written from
    # the proxy's source (proxy/memory/memory_endpoints.py): keys are unique
    # across all users, an admin sees and may write every row, a list is
    # ordered newest update first, and PUT updates value and metadata of an
    # existing row or creates one, taking user_id from the body (admins only).

    def tick(self) -> str:
        self.memory_clock += 1
        minutes, seconds = divmod(self.memory_clock, 60)
        return f"2026-11-12T10:{minutes:02d}:{seconds:02d}.000Z"

    def add_memory(self, key: str, value: str, *, user_id: str, metadata: Any = None) -> dict[str, Any]:
        """Store a row as any caller could through the API, e.g. another user's key."""
        stamp = self.tick()
        self.memories[key] = {
            "memory_id": f"mem-{self.memory_clock}",
            "key": key,
            "value": value,
            "metadata": metadata,
            "user_id": user_id,
            "team_id": None,
            "created_at": stamp,
            "created_by": user_id,
            "updated_at": stamp,
            "updated_by": user_id,
        }
        return self.memories[key]

    def list_memories(self, method: str, query: dict[str, str]) -> tuple[int, Any]:
        if method != "GET":
            return 405, {"detail": "Method Not Allowed"}
        rows = sorted(self.memories.values(), key=lambda row: row["updated_at"], reverse=True)
        if "key_prefix" in query:
            rows = [row for row in rows if row["key"].startswith(query["key_prefix"])]
        page, size = int(query.get("page", 1)), int(query.get("page_size", 50))
        return 200, {"memories": rows[(page - 1) * size : page * size], "total": len(rows)}

    def memory_row(self, method: str, key: str, body: Any) -> tuple[int, Any]:
        row = self.memories.get(key)
        if method == "GET":
            return (200, row) if row else (404, {"detail": f"Memory with key '{key}' not found"})
        if method == "DELETE":
            if row is None:
                return 404, {"detail": f"Memory with key '{key}' not found"}
            del self.memories[key]
            return 200, {"key": key, "deleted": True}
        if method != "PUT":
            return 405, {"detail": "Method Not Allowed"}
        body = body or {}
        if body.get("value") is None and "metadata" not in body:
            return 400, {"detail": "Request body must include at least one of: value, metadata."}
        if row is None:
            if body.get("value") is None:
                return 400, {"detail": "Cannot create a new memory via PUT without a 'value'."}
            user_id = body.get("user_id") or "default_user_id"
            return 200, self.add_memory(key, body["value"], user_id=user_id, metadata=body.get("metadata"))
        if body.get("value") is not None:
            row["value"] = body["value"]
        if "metadata" in body:
            row["metadata"] = body["metadata"]
        row["updated_at"] = self.tick()
        return 200, row

    def embed(self, body: dict[str, Any]) -> tuple[int, Any]:
        """POST /v1/embeddings: a bag-of-words vector per text, so related texts score higher."""
        if not self.embeddings_enabled:
            return 500, {"error": {"message": "embedding model unavailable"}}
        texts = body["input"] if isinstance(body["input"], list) else [body["input"]]
        self.embedded.extend(texts)
        return 200, {
            "model": body["model"],
            "data": [{"index": index, "embedding": bag_of_words(text)} for index, text in enumerate(texts)],
        }

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
            "ENABLE_MEMORIES": True,
            "ENABLE_MEMORY_SYSTEM_CONTEXT": True,
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


def bag_of_words(text: str, dimensions: int = 64) -> list[float]:
    """A stand-in embedding: word counts hashed into a fixed number of buckets."""
    vector = [0.0] * dimensions
    for word in re.findall(r"[a-z0-9]+", text.lower()):
        vector[int(hashlib.sha256(word.encode()).hexdigest(), 16) % dimensions] += 1.0
    return vector


def write_skill(root: pathlib.Path, package: str, frontmatter: str, *, name: str | None = None, body: str = "") -> None:
    """Write root/<package>/SKILL.md; the frontmatter name defaults to the package name."""
    (root / package).mkdir(parents=True, exist_ok=True)
    text = f"---\nname: {name or package}\n{frontmatter}\n---\n\n# {package}\n{body}"
    (root / package / "SKILL.md").write_text(text, encoding="utf-8", newline="\n")
