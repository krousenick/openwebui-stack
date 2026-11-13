"""Name the person behind every saved-memory call, and show models their memory index.

Saved memories (memory-mcp, container-scripts/memory_mcp_server.py) belong to
one owner each. This LiteLLM pre-call hook decides the owner of a request
from what LiteLLM has verified, and does two things with it:

1. MCP tool calls to the `memory` server. LiteLLM runs the hook before it
   forwards a call_mcp_tool. The hook adds three headers, which LiteLLM sends
   upstream with the call (a pre-call hook's "extra_headers",
   ProxyLogging.convert_mcp_hook_response_to_kwargs): the owner, the time and
   an HMAC-SHA256 over both keyed with MEMORY_MCP_SIGNING_KEY. memory-mcp
   refuses a call without a valid one. When there is no owner the call is
   refused here with a 403 that says why.
2. Chat completions whose tools include the memory tools. The hook appends
   the owner's memory index (names and one-line descriptions, from
   memory-mcp's /v1/index, cached per owner) to the system prompt, the way
   Claude Code loads its MEMORY.md index into every session; the model reads
   full entries with the tools. A request without the tools is left alone,
   so Open WebUI's title and tag requests carry no index. Opt out per
   request with metadata {"memory_index": false}. Claude Code's /v1/messages
   (the subscription passthrough) is not touched: it has its own memory.

The owner:
  * The master key, which Open WebUI calls LiteLLM with server-side: the
    X-OpenWebUI-User-Email header, lowercased. Open WebUI sets it from the
    signed-in user (ENABLE_FORWARD_USER_INFO_HEADERS); users cannot change
    their email there, while they can rename themselves, so the name header
    is not used. A master-key request without the header has no owner.
  * Any other key: the user_id stored on the key, read from /key/info by the
    key's hash and cached. Not user_api_key_dict.user_id: with
    general_settings.user_header_mappings, LiteLLM overwrites that field from
    the request header for every caller (add_internal_user_from_user_mapping),
    so a key holder could otherwise name another user by sending the header.

Loaded through LITELLM_WORKER_STARTUP_HOOKS (docker-compose.yml) when
memory_mcp_enabled, before the context compactor, so the index counts toward
the window the compactor fits a history to.

Environment: MEMORY_MCP_SIGNING_KEY, LITELLM_MASTER_KEY; optional
MEMORY_MCP_URL (http://memory-mcp:8080), MEMORY_MCP_SERVER_NAME (memory),
MEMORY_INDEX_ENABLED (true), MEMORY_INDEX_TTL (seconds, 30) and
MEMORY_PROXY_URL (http://127.0.0.1:4000, where /key/info is asked).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import time
import urllib.parse
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import litellm
from fastapi import HTTPException
from litellm.integrations.custom_logger import CustomLogger

try:
    from litellm.constants import LITELLM_PROXY_MASTER_KEY_ALIAS
except ImportError:  # older LiteLLM: the master key's auth object carries the key or its hash
    LITELLM_PROXY_MASTER_KEY_ALIAS = None

logger = logging.getLogger("LiteLLM Proxy")

# memory_mcp_server.py checks the same context string and headers.
SIGNATURE_CONTEXT = "stack-memory/v1"
OWNER_HEADER = "x-stack-memory-owner"
TIMESTAMP_HEADER = "x-stack-memory-ts"
SIGNATURE_HEADER = "x-stack-memory-sig"

OPENWEBUI_EMAIL_HEADER = "x-openwebui-user-email"
INDEX_MARKER = "<saved_memories>"
CHAT_CALL_TYPES = frozenset({"completion", "acompletion"})
MCP_CALL_TYPE = "call_mcp_tool"
TOOL_NAMES = ("save", "get", "search", "list", "delete")
WRITE_TOOLS = frozenset({"save", "delete"})
KEY_OWNER_TTL = 300.0
FAILED_INDEX_TTL = 15.0
HTTP_TIMEOUT = 3.0
CACHE_LIMIT = 10000

HttpGet = Callable[[str, Mapping[str, str]], Awaitable[tuple[int, Any]]]


def sign(key: str, owner: str, timestamp: int) -> str:
    message = f"{SIGNATURE_CONTEXT}\n{owner}\n{timestamp}".encode()
    return hmac.new(key.encode(), message, hashlib.sha256).hexdigest()


def signed_headers(key: str, owner: str, now: float) -> dict[str, str]:
    timestamp = int(now)
    return {
        OWNER_HEADER: urllib.parse.quote(owner, safe="@._+-"),
        TIMESTAMP_HEADER: str(timestamp),
        SIGNATURE_HEADER: sign(key, owner, timestamp),
    }


def _header(headers: Any, name: str) -> str:
    if not isinstance(headers, Mapping):
        return ""
    for key, value in headers.items():
        if str(key).lower() == name:
            return str(value)
    return ""


def request_headers(data: Mapping[str, Any]) -> Mapping[str, Any]:
    """The client's request headers: where LiteLLM keeps them for a chat request, then for an MCP call."""
    for source in (data.get("proxy_server_request"), data.get("metadata"), data.get("litellm_metadata")):
        headers = source.get("headers") if isinstance(source, Mapping) else None
        if isinstance(headers, Mapping) and headers:
            return headers
    return {}


def is_master_key(auth: Any, master_key: str) -> bool:
    api_key = getattr(auth, "api_key", None)
    if not api_key:
        return False
    if LITELLM_PROXY_MASTER_KEY_ALIAS and api_key == LITELLM_PROXY_MASTER_KEY_ALIAS:
        return True
    return bool(master_key) and api_key in (master_key, hashlib.sha256(master_key.encode()).hexdigest())


def normalize_owner(value: Any) -> str:
    owner = str(value or "").strip()
    return owner.lower() if "@" in owner else owner


def memory_tool_pattern(server: str) -> re.Pattern[str]:
    """Tool names of the memory server as clients present them.

    LiteLLM prefixes them "<server>-" (memory-save), Open WebUI and opencode
    add "<connection>_" (memory_memory-save), Claude Code "mcp__<server>__".
    """
    return re.compile(rf"(?:^|_){re.escape(server)}-(?:{'|'.join(TOOL_NAMES)})$")


def carries_memory_tools(tools: Any, pattern: re.Pattern[str]) -> bool:
    if not isinstance(tools, list):
        return False
    for tool in tools:
        if not isinstance(tool, Mapping):
            continue
        function = tool.get("function")
        name = function.get("name") if isinstance(function, Mapping) else tool.get("name")
        if isinstance(name, str) and pattern.search(name):
            return True
    return False


def with_index(messages: list[Any], text: str) -> list[Any]:
    """Append the index to the first system message, or open the conversation with one."""
    for message in messages:
        if isinstance(message, Mapping) and message.get("role") in ("system", "developer"):
            content = message.get("content")
            if INDEX_MARKER in (content if isinstance(content, str) else str(content)):
                return messages
    updated = list(messages)
    for position, message in enumerate(updated):
        if not (isinstance(message, Mapping) and message.get("role") in ("system", "developer")):
            continue
        content = message.get("content")
        if isinstance(content, str):
            joined = f"{content.rstrip()}\n\n{text}" if content.strip() else text
            updated[position] = {**message, "content": joined}
            return updated
        if isinstance(content, list):
            updated[position] = {**message, "content": [*content, {"type": "text", "text": text}]}
            return updated
    return [{"role": "system", "content": text}, *updated]


def _flag(value: str) -> bool:
    return value.strip().lower() not in ("0", "false", "no", "off")


class SavedMemories(CustomLogger):
    def __init__(
        self,
        *,
        signing_key: str,
        master_key: str,
        memory_url: str = "http://memory-mcp:8080",
        proxy_url: str = "http://127.0.0.1:4000",
        server_name: str = "memory",
        index_enabled: bool = True,
        index_ttl: float = 30.0,
        http_get: HttpGet | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        super().__init__()
        self.signing_key = signing_key
        self.master_key = master_key
        self.memory_url = memory_url.rstrip("/")
        self.proxy_url = proxy_url.rstrip("/")
        self.server_name = server_name
        self.index_enabled = index_enabled
        self.index_ttl = index_ttl
        self.tool_pattern = memory_tool_pattern(server_name)
        self._http_get = http_get
        self._client: Any = None
        self._clock = clock
        self._key_owners: dict[str, tuple[float, str]] = {}
        self._indexes: dict[str, tuple[float, str]] = {}

    @classmethod
    def from_env(cls) -> SavedMemories:
        return cls(
            signing_key=os.environ.get("MEMORY_MCP_SIGNING_KEY", ""),
            master_key=os.environ.get("LITELLM_MASTER_KEY", ""),
            memory_url=os.environ.get("MEMORY_MCP_URL", "http://memory-mcp:8080"),
            proxy_url=os.environ.get("MEMORY_PROXY_URL", "http://127.0.0.1:4000"),
            server_name=os.environ.get("MEMORY_MCP_SERVER_NAME", "memory"),
            index_enabled=_flag(os.environ.get("MEMORY_INDEX_ENABLED", "true")),
            index_ttl=float(os.environ.get("MEMORY_INDEX_TTL", "30") or 30),
        )

    async def _get(self, url: str, headers: Mapping[str, str]) -> tuple[int, Any]:
        if self._http_get is not None:
            return await self._http_get(url, headers)
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(timeout=HTTP_TIMEOUT)
        response = await self._client.get(url, headers=dict(headers))
        try:
            body = response.json()
        except ValueError:
            body = None
        return response.status_code, body

    @staticmethod
    def _remember(cache: dict[str, tuple[float, str]], key: str, value: tuple[float, str], now: float) -> None:
        if len(cache) >= CACHE_LIMIT:
            for stale in [name for name, (expires, _) in cache.items() if expires <= now]:
                del cache[stale]
            if len(cache) >= CACHE_LIMIT:
                cache.clear()
        cache[key] = value

    async def key_owner(self, token: str) -> str:
        """The user_id stored on a virtual key, by its hash ("" when it has none)."""
        now = self._clock()
        cached = self._key_owners.get(token)
        if cached and cached[0] > now:
            return cached[1]
        status, body = await self._get(
            f"{self.proxy_url}/key/info?key={urllib.parse.quote(token, safe='')}",
            {"Authorization": f"Bearer {self.master_key}"},
        )
        if status != 200 or not isinstance(body, Mapping):
            raise LookupError(f"/key/info answered {status}")
        info = body.get("info")
        owner = normalize_owner(info.get("user_id") if isinstance(info, Mapping) else None)
        self._remember(self._key_owners, token, (now + KEY_OWNER_TTL, owner), now)
        return owner

    async def resolve_owner(self, auth: Any, data: Mapping[str, Any]) -> tuple[str, str]:
        """Return (owner, "") or ("", why the request has none)."""
        if is_master_key(auth, self.master_key):
            email = _header(request_headers(data), OPENWEBUI_EMAIL_HEADER).strip()
            if "@" in email:
                return email.lower(), ""
            return "", "the request uses the master key without Open WebUI's X-OpenWebUI-User-Email header"
        token = str(getattr(auth, "token", None) or getattr(auth, "api_key", None) or "")
        if not token:
            return "", "the request carries no LiteLLM key"
        try:
            owner = await self.key_owner(token)
        except Exception as error:
            logger.warning("saved memories: could not look up a key's owner: %s", error)
            return "", "the key's owner could not be looked up; try again"
        if not owner:
            return "", "the LiteLLM key has no user_id; use a key minted for a user (user_id set to their email)"
        return owner, ""

    async def async_pre_call_hook(self, user_api_key_dict, cache, data: dict, call_type: str):
        if call_type == MCP_CALL_TYPE:
            return await self._sign_tool_call(user_api_key_dict, data)
        if call_type in CHAT_CALL_TYPES and self.index_enabled:
            try:
                return await self._add_index(user_api_key_dict, data)
            except Exception as error:
                logger.warning("saved memories: index skipped: %s", error)
        return data

    async def _sign_tool_call(self, auth: Any, data: dict) -> dict:
        if str(data.get("mcp_server_name") or "") != self.server_name:
            return data
        if not self.signing_key:
            raise HTTPException(
                status_code=503, detail="Saved memories are not configured: MEMORY_MCP_SIGNING_KEY is unset."
            )
        owner, reason = await self.resolve_owner(auth, data)
        if not owner:
            # A plain string: LiteLLM returns it as the error's message.
            raise HTTPException(status_code=403, detail=f"Saved memories are not available for this request: {reason}.")
        extra = data.get("extra_headers")
        data["extra_headers"] = {
            **(extra if isinstance(extra, Mapping) else {}),
            **signed_headers(self.signing_key, owner, self._clock()),
        }
        if str(data.get("mcp_tool_name") or "").rsplit("-", 1)[-1] in WRITE_TOOLS:
            self._indexes.pop(owner, None)
        return data

    async def _add_index(self, auth: Any, data: dict) -> dict:
        metadata = data.get("metadata")
        if isinstance(metadata, Mapping) and metadata.get("memory_index") is False:
            return data
        messages = data.get("messages")
        if not self.signing_key or not isinstance(messages, list) or not messages:
            return data
        if not carries_memory_tools(data.get("tools"), self.tool_pattern):
            return data
        owner, _ = await self.resolve_owner(auth, data)
        if not owner:
            return data
        text = await self.index_text(owner)
        if text:
            data["messages"] = with_index(messages, text)
        return data

    async def index_text(self, owner: str) -> str:
        """The owner's memory index, from memory-mcp, cached for index_ttl seconds ("" when there is none)."""
        now = self._clock()
        cached = self._indexes.get(owner)
        if cached and cached[0] > now:
            return cached[1]
        text, ttl = "", FAILED_INDEX_TTL
        try:
            status, body = await self._get(f"{self.memory_url}/v1/index", signed_headers(self.signing_key, owner, now))
            if status == 200 and isinstance(body, Mapping):
                text, ttl = str(body.get("text") or ""), self.index_ttl
            else:
                logger.warning("saved memories: memory-mcp /v1/index answered %s", status)
        except Exception as error:
            logger.warning("saved memories: memory-mcp is not reachable: %s", error)
        self._remember(self._indexes, owner, (now + ttl, text), now)
        return text


proxy_handler_instance = SavedMemories.from_env()


def register_callback():
    if not any(isinstance(callback, SavedMemories) for callback in litellm.callbacks):
        litellm.callbacks.append(proxy_handler_instance)


register_callback()
