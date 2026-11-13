"""skills_mcp_server.py: the published skills as MCP tools (search, list, get) for any caller.

The server runs for real on a local port, reading a temporary skills folder;
search embeds through the fake LiteLLM's /v1/embeddings (bag-of-words vectors).
"""

from __future__ import annotations

import json
import pathlib
import threading
import urllib.error
import urllib.request
from typing import Any

import pytest

import skills_mcp_server as skills_mcp
from fakes import TOKEN, FakeLiteLLM, write_skill


class Client:
    def __init__(self, url: str) -> None:
        self.url = url
        self.next_id = 0

    def send(self, method: str, path: str, body: Any = None):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            self.url + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                raw = response.read()
                return response.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as error:
            raw = error.read()
            return error.code, json.loads(raw) if raw else None

    def rpc(self, method: str, params: dict[str, Any] | None = None):
        self.next_id += 1
        status, body = self.send(
            "POST", "/mcp", {"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params or {}}
        )
        assert status == 200, body
        return body

    def tool(self, name: str, arguments: dict[str, Any] | None = None) -> tuple[str, bool]:
        result = self.rpc("tools/call", {"name": name, "arguments": arguments or {}})["result"]
        return result["content"][0]["text"], result["isError"]


@pytest.fixture
def skills_root(tmp_path: pathlib.Path) -> pathlib.Path:
    root = tmp_path / "skills"
    write_skill(
        root,
        "docx",
        "description: Create and edit Word documents (.docx) with reports, letters and contracts.",
        body="Use python-docx and save the file.",
    )
    write_skill(
        root,
        "dataviz",
        "description: Design charts and graphs with a colorblind-safe palette.",
        body="Bars start at zero.",
    )
    write_skill(
        root,
        "sso-troubleshooting",
        "description: Diagnose Keycloak single sign-on failures in Open WebUI and LiteLLM.",
        body="Check the groups claim.",
    )
    return root


def start(root: pathlib.Path, litellm: FakeLiteLLM | None = None, **settings: str) -> tuple[Client, Any]:
    environ = {"SKILLS_DIR": str(root), **settings}
    if litellm is not None:
        environ.update(
            {"LITELLM_URL": litellm.url, "LITELLM_MASTER_KEY": TOKEN, "SKILLS_EMBEDDING_MODEL": "local-embedding"}
        )
    httpd = skills_mcp.make_server(skills_mcp.build_app(environ), "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return Client(f"http://127.0.0.1:{httpd.server_port}"), httpd


@pytest.fixture
def client(skills_root: pathlib.Path, litellm: FakeLiteLLM):
    client, httpd = start(skills_root, litellm)
    yield client
    httpd.shutdown()
    httpd.server_close()


def test_initialize_answers_the_clients_version_or_the_newest(client: Client) -> None:
    answer = client.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {}})["result"]
    assert answer["protocolVersion"] == "2025-06-18"
    assert answer["capabilities"] == {"tools": {"listChanged": False}}
    assert "search for it" in answer["instructions"]
    newest = client.rpc("initialize", {"protocolVersion": "1999-01-01"})["result"]["protocolVersion"]
    assert newest == skills_mcp.PROTOCOL_VERSIONS[0]


def test_notifications_get_202_and_get_mcp_405(client: Client) -> None:
    assert client.send("POST", "/mcp", {"jsonrpc": "2.0", "method": "notifications/initialized"}) == (202, None)
    assert client.send("GET", "/mcp")[0] == 405
    assert client.send("GET", "/healthz") == (200, {"ok": True})


def test_lists_three_read_only_tools(client: Client) -> None:
    tools = client.rpc("tools/list")["result"]["tools"]
    assert [tool["name"] for tool in tools] == ["search", "list", "get"]
    assert all(tool["annotations"]["readOnlyHint"] for tool in tools)


def test_unknown_tool_is_an_rpc_error(client: Client) -> None:
    body = client.rpc("tools/call", {"name": "delete", "arguments": {}})
    assert body["error"]["code"] == -32602


def test_list_shows_every_valid_skill_and_skips_broken_ones(skills_root: pathlib.Path, client: Client) -> None:
    write_skill(skills_root, "broken", "description: Named after another folder.", name="other")
    (skills_root / ".hidden").mkdir()
    (skills_root / ".hidden" / "SKILL.md").write_text("---\nname: .hidden\ndescription: x\n---\n", encoding="utf-8")
    text, error = client.tool("list")
    assert not error
    lines = text.splitlines()
    assert lines[0] == "3 skills (name: what it is for):"
    assert [line.split(":")[0] for line in lines[1:]] == ["- dataviz", "- docx", "- sso-troubleshooting"]


def test_get_returns_the_whole_skill_and_accepts_loose_names(skills_root: pathlib.Path, client: Client) -> None:
    whole = (skills_root / "docx" / "SKILL.md").read_text(encoding="utf-8")
    for name in ("docx", "Docx", "skills/docx", "docx/SKILL.md"):
        text, error = client.tool("get", {"name": name})
        assert not error
        assert text.startswith("Skill 'docx'.")
        assert text.endswith(whole)


def test_get_names_the_available_skills_when_the_name_is_unknown(client: Client) -> None:
    text, error = client.tool("get", {"name": "../../etc/passwd"})
    assert error
    assert "Available: dataviz, docx, sso-troubleshooting." in text


def test_search_ranks_by_meaning_and_caches_skill_vectors(client: Client, litellm: FakeLiteLLM) -> None:
    text, error = client.tool("search", {"query": "edit a Word report"})
    assert not error
    lines = text.splitlines()
    assert lines[0] == "3 of 3 skills, best match first (ranked by meaning and keywords):"
    assert lines[1].startswith("- docx (relevance ")
    assert len(litellm.embedded) == 4  # three skills, one query
    client.tool("search", {"query": "draw a bar chart", "limit": 1})
    assert len(litellm.embedded) == 5  # only the new query
    text, _ = client.tool("search", {"query": "draw a bar chart", "limit": 1})
    assert text.splitlines()[1].startswith("- dataviz ")


def test_search_falls_back_to_keywords_when_embeddings_fail(client: Client, litellm: FakeLiteLLM) -> None:
    litellm.embeddings_enabled = False
    text, error = client.tool("search", {"query": "keycloak sign-on"})
    assert not error
    lines = text.splitlines()
    assert lines[0] == "1 of 3 skills, best match first (ranked by keywords):"
    assert lines[1].startswith("- sso-troubleshooting ")


def test_search_without_a_model_uses_keywords_and_says_when_nothing_matches(skills_root: pathlib.Path) -> None:
    client, httpd = start(skills_root)
    try:
        text, error = client.tool("search", {"query": "palette"})
        assert not error
        assert "ranked by keywords" in text
        assert "- dataviz " in text
        text, error = client.tool("search", {"query": "quantum spreadsheets"})
        assert not error
        assert text.startswith("No skill matches 'quantum spreadsheets' (ranked by keywords).")
        _, error = client.tool("search", {"query": "  "})
        assert error
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_a_skill_added_or_changed_later_is_picked_up(skills_root: pathlib.Path, client: Client) -> None:
    write_skill(skills_root, "pptx", "description: Build PowerPoint slide decks with python-pptx.")
    text, _ = client.tool("search", {"query": "slide deck", "limit": 1})
    assert text.splitlines()[1].startswith("- pptx ")
    write_skill(skills_root, "pptx", "description: Build PowerPoint slide decks, now with speaker notes.")
    text, _ = client.tool("get", {"name": "pptx"})
    assert "speaker notes" in text


def test_an_empty_or_missing_folder_is_a_tool_error(tmp_path: pathlib.Path) -> None:
    client, httpd = start(tmp_path / "nowhere")
    try:
        text, error = client.tool("list")
        assert error
        assert "cannot be read" in text
        (tmp_path / "empty").mkdir()
        httpd.shutdown()
        httpd.server_close()
        client, httpd = start(tmp_path / "empty")
        text, error = client.tool("search", {"query": "anything"})
        assert error
        assert text == "No skills are installed on this gateway."
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_skill_name_keeps_only_slug_characters() -> None:
    assert skills_mcp.skill_name(" Skills/Deep-Research/SKILL.md ") == "deep-research"
    assert skills_mcp.skill_name("../../etc/passwd") == "etc-passwd"
    assert skills_mcp.skill_name(None) == ""
