"""litellm/memory_callback.py: who owns a saved-memory call, and the memory index in chat requests.

The hook runs inside LiteLLM; these tests load it with small stand-ins for
the litellm and fastapi modules it imports, and check its signatures with
memory_mcp_server.py's own verify_owner.
"""

from __future__ import annotations

import asyncio
import importlib.util
import pathlib
import sys
import types
from typing import Any

import pytest

import memory_mcp_server

HOOK_FILE = pathlib.Path(__file__).resolve().parents[2] / "litellm" / "memory_callback.py"
KEY = "k" * 40
MASTER = "sk-master"
ALIAS = "litellm-proxy-master-key"
NOW = 1_800_000_000.0
ALICE_INDEX = "<saved_memories>\n- editor: Neovim\n</saved_memories>"


class StubHTTPException(Exception):
    def __init__(self, status_code: int, detail: Any = None) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@pytest.fixture
def hook_module(monkeypatch: pytest.MonkeyPatch):
    litellm = types.ModuleType("litellm")
    litellm.callbacks = []
    custom_logger = types.ModuleType("litellm.integrations.custom_logger")
    custom_logger.CustomLogger = type("CustomLogger", (), {})
    constants = types.ModuleType("litellm.constants")
    constants.LITELLM_PROXY_MASTER_KEY_ALIAS = ALIAS
    fastapi = types.ModuleType("fastapi")
    fastapi.HTTPException = StubHTTPException
    stubs = {
        "litellm": litellm,
        "litellm.integrations": types.ModuleType("litellm.integrations"),
        "litellm.integrations.custom_logger": custom_logger,
        "litellm.constants": constants,
        "fastapi": fastapi,
    }
    for name, module in stubs.items():
        monkeypatch.setitem(sys.modules, name, module)
    spec = importlib.util.spec_from_file_location("memory_callback", HOOK_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert litellm.callbacks == [module.proxy_handler_instance]
    return module


class FakeHttp:
    """GET /key/info and memory-mcp's /v1/index, as the hook calls them."""

    def __init__(self) -> None:
        self.key_owners: dict[str, str | None] = {}
        self.indexes: dict[str, str] = {}
        self.calls: list[str] = []
        self.index_fails = False

    async def __call__(self, url: str, headers: dict[str, str]) -> tuple[int, Any]:
        self.calls.append(url)
        if "/key/info?key=" in url:
            assert headers["Authorization"] == f"Bearer {MASTER}"
            token = url.rsplit("=", 1)[1]
            if token not in self.key_owners:
                return 404, {"detail": "not found"}
            return 200, {"key": token, "info": {"user_id": self.key_owners[token]}}
        if url.endswith("/v1/index"):
            if self.index_fails:
                raise OSError("connection refused")
            owner = memory_mcp_server.verify_owner(headers, KEY, now=NOW)
            return 200, {"count": 1, "text": self.indexes.get(owner, "")}
        raise AssertionError(f"unexpected GET {url}")

    def count(self, part: str) -> int:
        return sum(part in call for call in self.calls)


@pytest.fixture
def http() -> FakeHttp:
    return FakeHttp()


@pytest.fixture
def hook(hook_module, http: FakeHttp):
    return hook_module.SavedMemories(signing_key=KEY, master_key=MASTER, http_get=http, clock=lambda: NOW)


def master_auth() -> Any:
    return types.SimpleNamespace(api_key=ALIAS, token=None, user_id="default_user_id", user_role="proxy_admin")


def key_auth(token: str, user_id: str | None = None) -> Any:
    return types.SimpleNamespace(api_key=token, token=token, user_id=user_id, user_role="internal_user")


def openwebui(email: str = "alice@example.com") -> dict[str, str]:
    return {"x-openwebui-user-email": email, "x-openwebui-user-name": "whatever they renamed themselves to"}


def tool_call(server: str = "memory", tool: str = "list", headers: dict[str, str] | None = None) -> dict[str, Any]:
    """The synthetic request LiteLLM hands a pre-call hook for an MCP tool call."""
    return {
        "mcp_server_name": server,
        "mcp_tool_name": tool,
        "mcp_arguments": {},
        "metadata": {"headers": headers or {}},
    }


def chat(tools: list[str] | None, headers: dict[str, str] | None = None, system: Any = "You are helpful.") -> dict:
    messages: list[dict[str, Any]] = [{"role": "user", "content": "hi"}]
    if system is not None:
        messages.insert(0, {"role": "system", "content": system})
    data: dict[str, Any] = {"model": "gemma", "messages": messages, "proxy_server_request": {"headers": headers or {}}}
    if tools is not None:
        data["tools"] = [{"type": "function", "function": {"name": name, "parameters": {}}} for name in tools]
    return data


def run(hook, auth, data, call_type: str = "call_mcp_tool"):
    return asyncio.run(hook.async_pre_call_hook(user_api_key_dict=auth, cache=None, data=data, call_type=call_type))


def owner_of(data: dict[str, Any]) -> str:
    return memory_mcp_server.verify_owner(data["extra_headers"], KEY, now=NOW)


def test_master_key_calls_belong_to_the_open_webui_email(hook) -> None:
    data = run(hook, master_auth(), tool_call(headers=openwebui(" Alice@Example.com ")))
    assert owner_of(data) == "alice@example.com"


def test_master_key_without_the_email_header_is_refused(hook) -> None:
    with pytest.raises(StubHTTPException) as refused:
        run(hook, master_auth(), tool_call(headers={"x-openwebui-user-name": "alice"}))
    assert refused.value.status_code == 403
    assert "X-OpenWebUI-User-Email" in refused.value.detail


def test_a_key_belongs_to_its_stored_user_id_not_the_header_mapped_one(hook, http: FakeHttp) -> None:
    # LiteLLM's user_header_mappings has already overwritten user_id from a
    # header this caller sent; the hook asks /key/info what the key says.
    http.key_owners["hash-bob"] = "Bob@Example.com"
    auth = key_auth("hash-bob", user_id="victim@example.com")
    data = run(hook, auth, tool_call(headers=openwebui("victim@example.com")))
    assert owner_of(data) == "bob@example.com"
    run(hook, auth, tool_call())
    assert http.count("/key/info") == 1, "the key's owner is cached"


@pytest.mark.parametrize("stored", [None, ""])
def test_a_key_without_a_user_id_is_refused(hook, http: FakeHttp, stored) -> None:
    http.key_owners["hash-service"] = stored
    with pytest.raises(StubHTTPException) as refused:
        run(hook, key_auth("hash-service", user_id="someone@example.com"), tool_call())
    assert refused.value.status_code == 403 and "no user_id" in refused.value.detail


def test_an_unknown_key_is_refused(hook) -> None:
    with pytest.raises(StubHTTPException, match="could not be looked up"):
        run(hook, key_auth("hash-gone"), tool_call())


def test_other_mcp_servers_are_left_alone(hook, http: FakeHttp) -> None:
    assert "extra_headers" not in run(hook, master_auth(), tool_call(server="brave_search", headers=openwebui()))
    assert http.calls == []


def test_no_signing_key_means_no_memory_calls(hook_module, http: FakeHttp) -> None:
    hook = hook_module.SavedMemories(signing_key="", master_key=MASTER, http_get=http, clock=lambda: NOW)
    with pytest.raises(StubHTTPException) as refused:
        run(hook, master_auth(), tool_call(headers=openwebui()))
    assert refused.value.status_code == 503


def test_existing_extra_headers_are_kept(hook) -> None:
    data = tool_call(headers=openwebui())
    data["extra_headers"] = {"traceparent": "00-abc"}
    headers = run(hook, master_auth(), data)["extra_headers"]
    assert headers["traceparent"] == "00-abc" and "x-stack-memory-sig" in headers


def test_the_index_goes_into_requests_that_carry_the_memory_tools(hook, http: FakeHttp) -> None:
    http.indexes["alice@example.com"] = ALICE_INDEX
    data = run(hook, master_auth(), chat(["memory_memory-get", "brave_search_web"], openwebui()), "acompletion")
    assert data["messages"] == [
        {"role": "system", "content": f"You are helpful.\n\n{ALICE_INDEX}"},
        {"role": "user", "content": "hi"},
    ]
    run(hook, master_auth(), chat(["memory_memory-get"], openwebui()), "acompletion")
    assert http.count("/v1/index") == 1, "the index is cached"


def test_no_index_without_the_tools_an_owner_or_consent(hook, http: FakeHttp) -> None:
    http.indexes["alice@example.com"] = ALICE_INDEX
    untouched = [
        chat(None, openwebui()),
        chat(["brave_search_web"], openwebui()),
        chat(["memory_memory-get"], {"x-openwebui-user-name": "alice"}),
        {**chat(["memory_memory-get"], openwebui()), "metadata": {"memory_index": False}},
    ]
    for data in untouched:
        before = [dict(message) for message in data["messages"]]
        assert run(hook, master_auth(), data, "acompletion")["messages"] == before
    claude_code = run(hook, master_auth(), chat(["mcp__memory__memory-get"], openwebui()), "anthropic_messages")
    assert claude_code["messages"][0]["content"] == "You are helpful."


def test_the_index_opens_a_conversation_without_a_system_message(hook, http: FakeHttp) -> None:
    http.key_owners["hash-alice"] = "alice@example.com"
    http.indexes["alice@example.com"] = ALICE_INDEX
    data = run(hook, key_auth("hash-alice"), chat(["memory_memory-search"], system=None), "acompletion")
    assert data["messages"][0] == {"role": "system", "content": ALICE_INDEX}


def test_a_system_message_made_of_parts_gets_a_part(hook, http: FakeHttp) -> None:
    http.key_owners["hash-alice"] = "alice@example.com"
    http.indexes["alice@example.com"] = ALICE_INDEX
    system = [{"type": "text", "text": "Hi"}]
    data = run(hook, key_auth("hash-alice"), chat(["memory_memory-search"], system=system), "acompletion")
    assert data["messages"][0]["content"] == [*system, {"type": "text", "text": ALICE_INDEX}]
    again = run(hook, key_auth("hash-alice"), data, "acompletion")
    assert len(again["messages"][0]["content"]) == 2, "an index already present is not added twice"


def test_a_write_drops_the_cached_index(hook, http: FakeHttp) -> None:
    http.indexes["alice@example.com"] = "<saved_memories>\n- old: one\n</saved_memories>"
    run(hook, master_auth(), chat(["memory_memory-save"], openwebui()), "acompletion")
    run(hook, master_auth(), tool_call(tool="save", headers=openwebui()))
    http.indexes["alice@example.com"] = "<saved_memories>\n- new: two\n</saved_memories>"
    data = run(hook, master_auth(), chat(["memory_memory-save"], openwebui()), "acompletion")
    assert "- new: two" in data["messages"][0]["content"]


def test_an_unreachable_memory_server_never_fails_the_chat(hook, http: FakeHttp) -> None:
    http.index_fails = True
    data = run(hook, master_auth(), chat(["memory_memory-get"], openwebui()), "acompletion")
    assert data["messages"][0]["content"] == "You are helpful."


def test_tool_name_shapes(hook_module) -> None:
    pattern = hook_module.memory_tool_pattern("memory")
    for name in ("memory-save", "memory_memory-get", "mcp__memory__memory-search", "memory_memory-delete"):
        assert pattern.search(name), name
    for name in ("memorysave", "my-memory-save", "memory-save-all", "memory-forget", "brave_search-memory"):
        assert not pattern.search(name), name


def test_signatures_match_the_servers(hook_module) -> None:
    for owner in ("alice@example.com", "odd owner:with/chars", "ü@example.com"):
        headers = hook_module.signed_headers(KEY, owner, NOW)
        assert memory_mcp_server.verify_owner(headers, KEY, now=NOW + 10) == owner
