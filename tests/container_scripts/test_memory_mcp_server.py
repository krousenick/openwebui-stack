"""memory_mcp_server.py: per-user saved memories as MCP tools over LiteLLM's /v1/memory.

The server runs for real on a local port against the fake LiteLLM; every
request is plain HTTP, the way LiteLLM's MCP gateway sends it.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import pytest

import memory_mcp_server as memory
from fakes import TOKEN, FakeLiteLLM

KEY = "k" * 40
ALICE = "alice@example.com"
BOB = "bob@example.com"


def signed(owner: str, *, key: str = KEY, at: float | None = None) -> dict[str, str]:
    """The headers litellm/memory_callback.py adds to a tool call."""
    stamp = int(time.time() if at is None else at)
    return {
        memory.OWNER_HEADER: urllib.parse.quote(owner, safe="@._+-"),
        memory.TIMESTAMP_HEADER: str(stamp),
        memory.SIGNATURE_HEADER: memory.signature(key, owner, stamp),
    }


class Client:
    def __init__(self, url: str) -> None:
        self.url = url
        self.next_id = 0

    def send(self, method: str, path: str, body: Any = None, headers: dict[str, str] | None = None):
        data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
        request = urllib.request.Request(
            self.url + path,
            data=data,
            method=method,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                **(headers or {}),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                raw = response.read()
                return response.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as error:
            raw = error.read()
            return error.code, json.loads(raw) if raw else None

    def rpc(self, method: str, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None):
        self.next_id += 1
        status, body = self.send(
            "POST", "/mcp", {"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params or {}}, headers
        )
        assert status == 200, body
        return body

    def tool(self, name: str, arguments: dict[str, Any] | None = None, *, owner: str | None = ALICE, headers=None):
        """Call a tool; returns (text, is_error)."""
        if headers is None and owner is not None:
            headers = signed(owner)
        body = self.rpc("tools/call", {"name": name, "arguments": arguments or {}}, headers)
        result = body["result"]
        return result["content"][0]["text"], result["isError"]


def start(litellm: FakeLiteLLM, **settings: str) -> tuple[Client, Any]:
    environ = {
        "MEMORY_MCP_SIGNING_KEY": KEY,
        "LITELLM_MASTER_KEY": TOKEN,
        "LITELLM_URL": litellm.url,
        "MEMORY_EMBEDDING_MODEL": "local-embedding",
        **settings,
    }
    httpd = memory.make_server(memory.build_app(environ), "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return Client(f"http://127.0.0.1:{httpd.server_port}"), httpd


@pytest.fixture
def client(litellm: FakeLiteLLM):
    client, httpd = start(litellm)
    yield client
    httpd.shutdown()
    httpd.server_close()


def test_initialize_answers_the_clients_version_or_the_newest(client: Client) -> None:
    answer = client.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {}})
    assert answer["result"]["protocolVersion"] == "2025-06-18"
    assert answer["result"]["capabilities"] == {"tools": {"listChanged": False}}
    newest = client.rpc("initialize", {"protocolVersion": "1999-01-01"})["result"]["protocolVersion"]
    assert newest == memory.PROTOCOL_VERSIONS[0]


def test_notifications_and_client_answers_get_202(client: Client) -> None:
    assert client.send("POST", "/mcp", {"jsonrpc": "2.0", "method": "notifications/initialized"}) == (202, None)
    assert client.send("POST", "/mcp", {"jsonrpc": "2.0", "id": 9, "result": {}}) == (202, None)


def test_tools_list_names_the_five_tools(client: Client) -> None:
    tools = client.rpc("tools/list")["result"]["tools"]
    assert [tool["name"] for tool in tools] == ["save", "get", "search", "list", "delete"]
    save = tools[0]["inputSchema"]
    assert save["required"] == ["name", "content"]
    assert "owner" not in json.dumps(tools), "the owner never comes from the model"


def test_unsigned_calls_are_refused_and_never_reach_the_store(client: Client, litellm: FakeLiteLLM) -> None:
    litellm.forget_requests()
    text, error = client.tool("list", owner=None)
    assert error and "no owner signature" in text
    assert litellm.requests == []


@pytest.mark.parametrize(
    "headers",
    [
        signed(ALICE, key="o" * 40),
        signed(ALICE, at=time.time() - 600),
        {**signed(BOB), memory.OWNER_HEADER: ALICE},
    ],
    ids=["wrong key", "expired", "owner swapped after signing"],
)
def test_bad_signatures_are_refused(client: Client, litellm: FakeLiteLLM, headers: dict[str, str]) -> None:
    litellm.add_memory(f"user:{ALICE}:secret", "alice's note", user_id=ALICE)
    litellm.forget_requests()
    text, error = client.tool("list", headers=headers)
    assert error and "Saved memories are unavailable" in text
    assert litellm.requests == []


def test_save_creates_a_row_owned_by_the_caller(client: Client, litellm: FakeLiteLLM) -> None:
    text, error = client.tool("save", {"name": "Editor Preference", "content": "Uses Neovim with LazyVim.\nTabs: 4."})
    assert not error
    assert text == "Saved new memory 'editor-preference': Uses Neovim with LazyVim."
    row = litellm.memories[f"user:{ALICE}:editor-preference"]
    assert row["user_id"] == ALICE
    assert row["value"] == "Uses Neovim with LazyVim.\nTabs: 4."
    assert row["metadata"] == {"description": "Uses Neovim with LazyVim.", "source": "memory-mcp"}


def test_save_under_the_same_name_replaces_it(client: Client, litellm: FakeLiteLLM) -> None:
    client.tool("save", {"name": "editor-preference", "content": "Uses Vim."})
    text, error = client.tool(
        "save", {"name": "editor-preference", "content": "Uses Helix now.", "description": "Editor: Helix"}
    )
    assert (text, error) == ("Updated memory 'editor-preference': Editor: Helix", False)
    assert [row["value"] for row in litellm.memories.values()] == ["Uses Helix now."]


def test_owners_only_see_their_own_memories(client: Client) -> None:
    client.tool("save", {"name": "project", "content": "Alice works on the stack."})
    assert client.tool("list", owner=BOB) == ("Nothing is saved yet.", False)
    text, error = client.tool("get", {"name": "project"}, owner=BOB)
    assert error and "No memory is named 'project'" in text
    text, error = client.tool("delete", {"name": "project"}, owner=BOB)
    assert error
    assert "project" in client.tool("list")[0]


def test_a_row_another_user_put_under_the_owners_prefix_is_ignored(client: Client, litellm: FakeLiteLLM) -> None:
    # Any key can create any key through /v1/memory; this one names Alice's
    # prefix but belongs to mallory.
    squatted = litellm.add_memory(f"user:{ALICE}:project", "planted", user_id="mallory@example.com")
    assert client.tool("list") == ("Nothing is saved yet.", False)
    text, error = client.tool("get", {"name": "project"})
    assert error and "held by another account" in text
    text, error = client.tool("save", {"name": "project", "content": "Alice's real note"})
    assert error and "held by another account" in text
    assert squatted["value"] == "planted", "the planted row must not receive Alice's text"


def test_get_and_delete(client: Client, litellm: FakeLiteLLM) -> None:
    client.tool("save", {"name": "pets", "content": "Two cats, Mochi and Tofu."})
    text, error = client.tool("get", {"name": "PETS"})
    assert not error and text.startswith("Memory 'pets' (updated 2026-11-12):\nTwo cats")
    assert client.tool("delete", {"name": "pets"}) == ("Deleted memory 'pets'.", False)
    assert litellm.memories == {}
    text, error = client.tool("get", {"name": "pets"})
    assert error and "Nothing is saved yet" in text


def test_search_ranks_by_meaning_and_keywords_and_reuses_vectors(client: Client, litellm: FakeLiteLLM) -> None:
    client.tool("save", {"name": "editor", "content": "Prefers Neovim for editing code."})
    client.tool("save", {"name": "pets", "content": "Has two cats called Mochi and Tofu."})
    text, error = client.tool("search", {"query": "which cats does the user have", "limit": 1})
    assert not error
    assert text.startswith("1 of 2 saved memories, best match first (ranked by meaning and keywords):")
    assert "## pets" in text and "## editor" not in text
    embedded = len(litellm.embedded)
    client.tool("search", {"query": "code editor"})
    assert litellm.embedded[embedded:] == ["code editor"], "unchanged memories keep their cached vectors"


def test_search_falls_back_to_keywords_when_embeddings_fail(client: Client, litellm: FakeLiteLLM) -> None:
    client.tool("save", {"name": "pets", "content": "Has two cats called Mochi and Tofu."})
    client.tool("save", {"name": "editor", "content": "Prefers Neovim."})
    litellm.embeddings_enabled = False
    text, error = client.tool("search", {"query": "Mochi"})
    assert not error and "(ranked by keywords)" in text and "## pets" in text and "## editor" not in text
    text, error = client.tool("search", {"query": "spaceship"})
    assert not error and text.startswith("No saved memory matches 'spaceship'")


def test_index_is_signed_newest_first_and_capped(litellm: FakeLiteLLM) -> None:
    client, httpd = start(litellm, MEMORY_INDEX_MAX_CHARS="520")
    try:
        assert client.send("GET", "/v1/index")[0] == 401
        assert client.send("GET", "/v1/index", headers=signed(ALICE)) == (200, {"count": 0, "text": ""})
        for number in range(8):
            client.tool("save", {"name": f"note-{number}", "content": f"Note number {number} about something."})
        status, index = client.send("GET", "/v1/index", headers=signed(ALICE))
        assert status == 200 and index["count"] == 8
        text = index["text"]
        assert text.startswith("<saved_memories>\n") and text.endswith("</saved_memories>")
        assert text.index("- note-7:") < text.index("- note-6:"), "newest first"
        assert "more not listed here" in text and "- note-0:" not in text
        assert len(text) <= 520 + 80
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_limits_on_size_and_count(litellm: FakeLiteLLM) -> None:
    app = memory.build_app(
        {
            "MEMORY_MCP_SIGNING_KEY": KEY,
            "LITELLM_MASTER_KEY": TOKEN,
            "LITELLM_URL": litellm.url,
            "MEMORY_MAX_CONTENT_CHARS": "40",
            "MEMORY_MAX_PER_USER": "2",
        }
    )
    with pytest.raises(memory.ToolError, match="keep one memory under 40"):
        app.memories.call("save", ALICE, {"name": "long", "content": "x" * 41})
    app.memories.call("save", ALICE, {"name": "one", "content": "first"})
    app.memories.call("save", ALICE, {"name": "two", "content": "second"})
    with pytest.raises(memory.ToolError, match="the limit"):
        app.memories.call("save", ALICE, {"name": "three", "content": "third"})
    assert app.memories.call("save", ALICE, {"name": "two", "content": "second, edited"}).startswith("Updated")
    with pytest.raises(memory.ToolError, match="name must contain"):
        app.memories.call("save", ALICE, {"name": "!!!", "content": "x"})


def test_an_owner_with_a_colon_cannot_reach_into_another_prefix(client: Client, litellm: FakeLiteLLM) -> None:
    odd = "alice@example.com:project"
    client.tool("save", {"name": "notes", "content": "odd owner"}, owner=odd)
    assert list(litellm.memories) == ["user:alice@example.com%3Aproject:notes"]
    assert client.tool("list") == ("Nothing is saved yet.", False)


def test_protocol_errors(client: Client) -> None:
    assert client.rpc("resources/list")["error"]["code"] == -32601
    unknown = client.rpc("tools/call", {"name": "drop_all", "arguments": {}}, signed(ALICE))
    assert unknown["error"]["code"] == -32602
    status, body = client.send("POST", "/mcp", b"{not json")
    assert status == 400 and body["error"]["code"] == -32700
    assert client.send("GET", "/mcp")[0] == 405
    assert client.send("GET", "/healthz") == (200, {"ok": True})


def test_build_app_requires_its_secrets() -> None:
    with pytest.raises(ValueError, match="MEMORY_MCP_SIGNING_KEY"):
        memory.build_app({"MEMORY_MCP_SIGNING_KEY": "short", "LITELLM_MASTER_KEY": TOKEN})
    with pytest.raises(ValueError, match="LITELLM_MASTER_KEY"):
        memory.build_app({"MEMORY_MCP_SIGNING_KEY": KEY})
