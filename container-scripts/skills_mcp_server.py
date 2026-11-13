"""Every skill in ./skills as MCP tools (search, list, get) for any client of the gateway.

Runs as the skills-mcp service (docker-compose.yml, skills_mcp_enabled) on the
litellm image's Python. LiteLLM serves it to every key as the MCP server
`skills`: https://litellm.<domain>/mcp/skills, with the tools skills-search,
skills-list and skills-get. Open WebUI and opencode get it as a tool server;
Claude Code, Codex and other agents add it like any remote MCP server. It
reads the same skills/<name>/SKILL.md packages the playbook publishes to
LiteLLM's skill store, Open WebUI and opencode (mounted read-only at
SKILLS_DIR) and picks up a changed folder on the next call.

Why not LiteLLM's own skill_search tool: LiteLLM 1.110 shows a skill only to
the key that created it and to admin keys, and the playbook publishes every
skill as the master key's user; it also offers skill_search only to keys in
tool-search mode, which replaces that key's whole MCP tool list. The skills
are the stack's shared know-how, so every caller here sees all of them, and
nothing is ever written.

Endpoints:
  POST /mcp      MCP over streamable HTTP: initialize, ping, tools/list,
                 tools/call. JSON responses, no sessions.
  GET /healthz   liveness.

search ranks by meaning with SKILLS_EMBEDDING_MODEL through LiteLLM's
/v1/embeddings (vectors cached in process per skill and content digest),
blended with keyword overlap; by keywords alone when no model or key is set
or the embedding call fails. The ranking follows memory_mcp_server.py.

--healthcheck exits 0 while the server answers /healthz (the compose
healthcheck).

Environment, all optional: SKILLS_DIR (/app/skills), LITELLM_URL
(http://litellm:4000), LITELLM_MASTER_KEY (used for /v1/embeddings only),
SKILLS_EMBEDDING_MODEL, SKILLS_MCP_PORT (8080).

Standard library only, like the other container scripts.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import math
import os
import re
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import skill_packages
from stack_api import Api, ScriptError

SERVER_INFO = {"name": "stack-skills", "version": "1.0.0"}
# Newest first. initialize answers with the client's version when it is one
# of these, else with the newest, and the client decides whether to go on.
PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
INSTRUCTIONS = (
    "Skills are packaged instructions for specific jobs (reviewing code; Word, Excel, PowerPoint and PDF files; "
    "charts, diagrams and web pages; research; operating this stack), shared by every client of this gateway. "
    "When a task might have one, search for it, then get it and follow its instructions."
)

DEFAULT_SKILLS_DIR = "/app/skills"
FRONTMATTER = re.compile(r"(?s)\A---[ \t]*\r?\n.*?\r?\n---[ \t]*\r?\n")
MAX_BODY_BYTES = 1_000_000
EMBED_BATCH = 32
EMBED_TEXT_CHARS = 2000
DESCRIPTION_CHARS = 300
MAX_SKILL_CHARS = 200_000
DEFAULT_RESULTS = 3
MAX_RESULTS = 10


def log(message: str) -> None:
    print(f"skills-mcp: {message}", flush=True)


class ToolError(Exception):
    """A tool call failed; the message is what the model reads."""


class RpcError(Exception):
    """A JSON-RPC error answer."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Skill:
    name: str
    description: str
    text: str  # SKILL.md as written
    digest: str

    @property
    def body(self) -> str:
        """SKILL.md without its frontmatter."""
        match = FRONTMATTER.match(self.text)
        return self.text[match.end() :] if match else self.text


class Library:
    """The skill packages under one folder, read again whenever a SKILL.md changes."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._lock = threading.Lock()
        self._stamp: tuple[tuple[str, int, int], ...] | None = None
        self._skills: dict[str, Skill] = {}
        self._warned: set[tuple[str, str]] = set()

    def _scan(self) -> tuple[tuple[str, int, int], ...]:
        """(package, mtime, size) of every SKILL.md, the cheap check for a change."""
        try:
            packages = sorted(entry for entry in self.root.iterdir() if entry.is_dir() and entry.name[:1] != ".")
        except OSError as error:
            raise ToolError(f"the skills folder cannot be read ({error.strerror or error})") from error
        stamp = []
        for package in packages:
            try:
                stat = (package / "SKILL.md").stat()
            except OSError:
                continue
            stamp.append((package.name, stat.st_mtime_ns, stat.st_size))
        return tuple(stamp)

    def skills(self) -> dict[str, Skill]:
        stamp = self._scan()
        with self._lock:
            if stamp != self._stamp:
                self._skills = self._load(stamp)
                self._stamp = stamp
            return self._skills

    def _load(self, stamp: tuple[tuple[str, int, int], ...]) -> dict[str, Skill]:
        loaded: dict[str, Skill] = {}
        for package, _, _ in stamp:
            try:
                text = (self.root / package / "SKILL.md").read_text(encoding="utf-8")
                name, description = skill_packages.parse_frontmatter(text)
            except (OSError, UnicodeDecodeError, ScriptError) as error:
                self._skip(package, str(error))
                continue
            if name != package or not description:
                self._skip(package, "the frontmatter name must equal the folder name, with a description")
                continue
            loaded[name] = Skill(name, description, text, hashlib.sha256(text.encode()).hexdigest()[:16])
        return loaded

    def _skip(self, package: str, reason: str) -> None:
        if (package, reason) not in self._warned:
            self._warned.add((package, reason))
            log(f"skipped skills/{package}/SKILL.md: {reason}")


def skill_name(value: Any) -> str:
    """The skill name a caller meant: 'docx', 'Docx', 'skills/docx' and 'docx/SKILL.md' all give 'docx'."""
    text = str(value or "").strip().lower()
    text = re.sub(r"^skills/|/skill\.md$", "", text)
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


def tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def query_terms(query: str) -> set[str]:
    """The words of a query worth matching: longer than two characters, unless nothing else is left."""
    words = set(tokens(query))
    return {word for word in words if len(word) > 2} or words


def _matches(term: str, words: set[str]) -> bool:
    """A term matches a word, or the start of a longer word when it has 4+ letters."""
    return term in words or (len(term) >= 4 and any(word.startswith(term) for word in words))


def keyword_score(terms: set[str], skill: Skill) -> float:
    """Share of the query terms in the skill: a full hit in its name or description, half a hit in its body."""
    if not terms:
        return 0.0
    head = set(tokens(f"{skill.name.replace('-', ' ')} {skill.description}"))
    body = set(tokens(skill.body))
    hits = sum(1.0 if _matches(term, head) else 0.5 if _matches(term, body) else 0.0 for term in terms)
    return hits / len(terms)


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right, strict=False))
    norm = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    return dot / norm if norm else 0.0


def embed_text(skill: Skill) -> str:
    return f"{skill.name.replace('-', ' ')}: {skill.description}\n{skill.body}"[:EMBED_TEXT_CHARS]


class Embedder:
    """Vectors from an embedding model the proxy serves (POST /v1/embeddings)."""

    def __init__(self, api: Api, model: str) -> None:
        self.api = api
        self.model = model

    def __call__(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), EMBED_BATCH):
            batch = texts[start : start + EMBED_BATCH]
            status, payload = self.api.request(
                "POST", "/v1/embeddings", json_body={"model": self.model, "input": batch}
            )
            if status != 200 or not isinstance(payload, dict):
                raise RuntimeError(f"/v1/embeddings answered {status}")
            data = sorted(
                (item for item in payload.get("data") or [] if isinstance(item, dict)),
                key=lambda item: item.get("index", 0),
            )
            if len(data) != len(batch):
                raise RuntimeError(f"/v1/embeddings returned {len(data)} vectors for {len(batch)} texts")
            vectors.extend([float(value) for value in item.get("embedding") or []] for item in data)
        return vectors


class Ranker:
    """Orders skills for a query: by meaning and keywords, or by keywords alone."""

    def __init__(self, embed: Callable[[list[str]], list[list[float]]] | None) -> None:
        self.embed = embed
        self._vectors: dict[tuple[str, str], list[float]] = {}
        self._lock = threading.Lock()

    def _skill_vectors(self, skills: Sequence[Skill]) -> list[list[float] | None]:
        keys = [(skill.name, skill.digest) for skill in skills]
        with self._lock:
            missing = [skill for skill, key in zip(skills, keys, strict=True) if key not in self._vectors]
        if missing and self.embed is not None:
            fresh = self.embed([embed_text(skill) for skill in missing])
            with self._lock:
                for skill, vector in zip(missing, fresh, strict=True):
                    self._vectors[(skill.name, skill.digest)] = vector
                current = set(keys)
                for stale in [key for key in self._vectors if key not in current]:
                    del self._vectors[stale]
        with self._lock:
            return [self._vectors.get(key) for key in keys]

    def rank(self, query: str, skills: Sequence[Skill], limit: int) -> tuple[list[tuple[float, Skill]], str]:
        """Return the best matches with their scores, and how they were ranked."""
        terms = query_terms(query)
        keyword = [keyword_score(terms, skill) for skill in skills]
        semantic: list[float] | None = None
        if self.embed is not None and skills:
            try:
                vectors = self._skill_vectors(skills)
                (query_vector,) = self.embed([query])
                semantic = [cosine(query_vector, vector) if vector else 0.0 for vector in vectors]
            except Exception as error:
                log(f"search: embeddings unavailable, ranking by keywords only: {error}")
        if semantic is None:
            scored = [(score, skill) for score, skill in zip(keyword, skills, strict=True) if score > 0]
            method = "keywords"
        else:
            scored = [
                (0.75 * meaning + 0.25 * words, skill)
                for meaning, words, skill in zip(semantic, keyword, skills, strict=True)
            ]
            method = "meaning and keywords"
        scored.sort(key=lambda item: (-item[0], item[1].name))
        return scored[:limit], method


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

TOOLS: list[dict[str, Any]] = [
    {
        "name": "search",
        "title": "Search skills",
        "description": (
            "Find the skills for a task by describing the task in plain words. Returns the best matches, ranked "
            "by meaning, with what each one is for. Read a match with get before following it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The task, in plain words."},
                "limit": {"type": "integer", "description": "How many matches to return (1-10, default 3)."},
            },
            "required": ["query"],
        },
        "annotations": {"title": "Search skills", "readOnlyHint": True},
    },
    {
        "name": "list",
        "title": "List skills",
        "description": "List every available skill with what it is for.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"title": "List skills", "readOnlyHint": True},
    },
    {
        "name": "get",
        "title": "Read a skill",
        "description": (
            "Read a skill's full instructions by name (from search or list), then follow them for the task at hand."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "The skill's name, for example 'docx'."}},
            "required": ["name"],
        },
        "annotations": {"title": "Read a skill", "readOnlyHint": True},
    },
]
TOOL_NAMES = frozenset(tool["name"] for tool in TOOLS)


class Skills:
    """The three tools over one library."""

    def __init__(self, library: Library, ranker: Ranker) -> None:
        self.library = library
        self.ranker = ranker
        self.handlers: dict[str, Callable[[Mapping[str, Any]], str]] = {
            "search": self.search,
            "list": self.list,
            "get": self.get,
        }

    def call(self, tool: str, arguments: Mapping[str, Any]) -> str:
        return self.handlers[tool](arguments)

    def _installed(self) -> dict[str, Skill]:
        skills = self.library.skills()
        if not skills:
            raise ToolError("No skills are installed on this gateway.")
        return skills

    def search(self, arguments: Mapping[str, Any]) -> str:
        query = str(arguments.get("query") or "").strip()
        if not query:
            raise ToolError("query is empty: describe the task in plain words")
        try:
            limit = min(MAX_RESULTS, max(1, int(arguments.get("limit") or DEFAULT_RESULTS)))
        except (TypeError, ValueError):
            limit = DEFAULT_RESULTS
        skills = sorted(self._installed().values(), key=lambda skill: skill.name)
        ranked, method = self.ranker.rank(query, skills, limit)
        if not ranked:
            names = ", ".join(skill.name for skill in skills)
            return f"No skill matches '{clip(query, 80)}' (ranked by {method}). Available: {names}."
        lines = [f"{len(ranked)} of {len(skills)} skills, best match first (ranked by {method}):"]
        lines.extend(
            f"- {skill.name} (relevance {score:.2f}): {clip(skill.description, DESCRIPTION_CHARS)}"
            for score, skill in ranked
        )
        lines.append("Read the one that fits with get, then follow its instructions.")
        return "\n".join(lines)

    def list(self, arguments: Mapping[str, Any]) -> str:
        skills = self._installed()
        lines = [f"{len(skills)} skills (name: what it is for):"]
        lines.extend(f"- {name}: {clip(skills[name].description, DESCRIPTION_CHARS)}" for name in sorted(skills))
        return "\n".join(lines)

    def get(self, arguments: Mapping[str, Any]) -> str:
        name = skill_name(arguments.get("name"))
        skills = self._installed()
        skill = skills.get(name)
        if skill is None:
            available = ", ".join(sorted(skills))
            raise ToolError(f"No skill is named '{name or arguments.get('name')}'. Available: {available}.")
        text = skill.text
        if len(text) > MAX_SKILL_CHARS:
            text = text[:MAX_SKILL_CHARS] + "\n\n[The rest of this skill was cut off.]"
        return f"Skill '{skill.name}'. Follow these instructions for the task at hand.\n\n{text}"


# ---------------------------------------------------------------------------
# MCP over HTTP
# ---------------------------------------------------------------------------


def rpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def tool_result(text: str, *, error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": error}


class McpApp:
    """The JSON-RPC methods; one message in, one answer (or None for a notification) out."""

    def __init__(self, skills: Skills) -> None:
        self.skills = skills

    def handle(self, message: Any) -> dict[str, Any] | None:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return rpc_error(None, -32600, "Invalid Request")
        method = message.get("method")
        if "id" not in message:
            return None  # a notification: nothing to answer
        request_id = message.get("id")
        if not isinstance(method, str):
            if "result" in message or "error" in message:
                return None  # a client's answer to a server request; this server sends none
            return rpc_error(request_id, -32600, "Invalid Request")
        params = message.get("params") or {}
        if not isinstance(params, dict):
            return rpc_error(request_id, -32602, "params must be an object")
        try:
            if method == "initialize":
                result = self.initialize(params)
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                result = self.call_tool(params)
            else:
                raise RpcError(-32601, f"Method not found: {method}")
        except RpcError as error:
            return rpc_error(request_id, error.code, error.message)
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def initialize(params: Mapping[str, Any]) -> dict[str, Any]:
        requested = params.get("protocolVersion")
        version = requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
            "instructions": INSTRUCTIONS,
        }

    def call_tool(self, params: Mapping[str, Any]) -> dict[str, Any]:
        tool = params.get("name")
        arguments = params.get("arguments") or {}
        if tool not in TOOL_NAMES:
            raise RpcError(-32602, f"Unknown tool: {tool}")
        if not isinstance(arguments, dict):
            raise RpcError(-32602, "arguments must be an object")
        try:
            text = self.skills.call(str(tool), arguments)
        except ToolError as error:
            log(f"{tool}: {str(error)[:120]}")
            return tool_result(str(error), error=True)
        except Exception as error:
            log(f"{tool}: failed: {error!r}")
            return tool_result("The skills tool failed unexpectedly; try again.", error=True)
        log(f"{tool}: ok")
        return tool_result(text)


def make_server(app: McpApp, host: str, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "stack-skills/1"

        def reply(self, status: int, payload: Any = None, extra: Mapping[str, str] | None = None) -> None:
            raw = b"" if payload is None else json.dumps(payload).encode()
            self.send_response(status)
            if payload is not None:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(raw)

        def route(self) -> str:
            return urllib.parse.urlsplit(self.path).path.rstrip("/") or "/"

        def do_POST(self) -> None:
            if self.route() != "/mcp":
                self.reply(404, {"detail": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length < 0 or length > MAX_BODY_BYTES:
                self.reply(413, rpc_error(None, -32600, "request body too large or without a length"))
                return
            try:
                message = json.loads(self.rfile.read(length) or b"null")
            except ValueError:
                self.reply(400, rpc_error(None, -32700, "Parse error"))
                return
            if isinstance(message, list):
                answer: Any = [reply for item in message if (reply := app.handle(item)) is not None]
            else:
                answer = app.handle(message)
            if answer:
                self.reply(200, answer)
            else:
                self.reply(202)

        def do_GET(self) -> None:
            route = self.route()
            if route == "/healthz":
                self.reply(200, {"ok": True})
            elif route == "/mcp":
                # No server-to-client stream: the spec allows 405 here.
                self.reply(405, {"detail": "use POST"}, {"Allow": "POST"})
            else:
                self.reply(404, {"detail": "not found"})

        def do_DELETE(self) -> None:
            self.reply(405, {"detail": "no sessions to end"}, {"Allow": "POST"})

        def log_message(self, *args: Any) -> None:
            pass  # tool calls are logged by McpApp, without the queries

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


def healthcheck(port: int) -> int:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5) as response:
            return 0 if response.status == 200 else 1
    except (OSError, urllib.error.URLError):
        return 1


def _int_env(environ: Mapping[str, str], name: str, default: int) -> int:
    try:
        return int(environ.get(name) or default)
    except ValueError:
        return default


def build_app(environ: Mapping[str, str]) -> McpApp:
    """Wire the app from the environment. Search falls back to keywords without a model or key."""
    model = (environ.get("SKILLS_EMBEDDING_MODEL") or "").strip()
    key = environ.get("LITELLM_MASTER_KEY", "")
    embed = None
    if model and key:
        embed = Embedder(Api(environ.get("LITELLM_URL") or "http://litellm:4000", key, timeout=30.0), model)
    library = Library(Path(environ.get("SKILLS_DIR") or DEFAULT_SKILLS_DIR))
    return McpApp(Skills(library, Ranker(embed)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="0.0.0.0", help="address to listen on (default: %(default)s)")
    parser.add_argument(
        "--port",
        type=int,
        default=_int_env(os.environ, "SKILLS_MCP_PORT", 8080),
        help="port to listen on (default: %(default)s)",
    )
    parser.add_argument("--healthcheck", action="store_true", help="exit 0 while the server answers /healthz")
    args = parser.parse_args(argv)
    if args.healthcheck:
        return healthcheck(args.port)
    app = build_app(os.environ)
    server = make_server(app, args.host, args.port)
    library = app.skills.library
    try:
        count = len(library.skills())
    except ToolError as error:
        count = 0
        log(str(error))
    embed = app.skills.ranker.embed
    model = embed.model if isinstance(embed, Embedder) else ""
    log(
        f"listening on {args.host}:{args.port}; {count} skills in {library.root}; "
        f"search by {'meaning with ' + model + ' and keywords' if model else 'keywords'}"
    )
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
