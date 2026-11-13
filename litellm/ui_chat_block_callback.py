"""Refuse model calls that come from the LiteLLM admin UI.

Chat on this stack goes through Open WebUI or an API client (opencode, an
IDE, a script with a virtual key). The admin UI stays for keys, models,
usage and settings, but its Playground and Chat pages send prompts to the
models straight from the browser. LiteLLM has a switch for the Chat page
(general_settings.enable_chat_ui, set in config.yaml) and none for the
Playground, so this hook is what turns both off: a model call is refused
with a 403 when either is true:

  1. It is authenticated with a UI session key. Signing in to the admin UI
     (SSO or password) mints a key in the reserved "litellm-dashboard"
     team; the Playground and Chat pages send it by default.
  2. Its Referer is the proxy's own /ui pages. This covers a virtual key
     pasted into the Playground's "Virtual Key" field. Best effort: only a
     browser sends it.

Left alone: every other caller (Open WebUI and API clients send neither
signal), and the admin UI's calls that are not a prompt to a model, such as
MCP tool tests, search tool tests, vector store searches, skill uploads and
the model health checks (those never reach this hook).

Loaded through LITELLM_WORKER_STARTUP_HOOKS (see docker-compose.yml) when
litellm_ui_chat_enabled is false. Registered first so a refused request
skips the Brave injector and the compactor.
"""

import os
from urllib.parse import urlsplit

import litellm
from fastapi import HTTPException
from litellm.integrations.custom_logger import CustomLogger

try:
    from litellm.constants import UI_SESSION_TOKEN_TEAM_ID
except ImportError:  # older LiteLLM: the literal the proxy has always used
    UI_SESSION_TOKEN_TEAM_ID = "litellm-dashboard"

# Every way the Playground and Chat pages can put a prompt to a model. The
# proxy passes the async name on its routes (LiteLLM's /v1/chat/completions
# is "acompletion", /v1/responses is "aresponses"); the sync names are
# listed so a route that reports one is covered too.
BLOCKED_CALL_TYPES = frozenset(
    {
        # conversation
        "completion",
        "acompletion",
        "text_completion",
        "atext_completion",
        "responses",
        "aresponses",
        "aresponses_websocket",
        "anthropic_messages",
        "aanthropic_messages",
        "generate_content",
        "agenerate_content",
        "generate_content_stream",
        "agenerate_content_stream",
        "arealtime",
        "send_message",
        "asend_message",
        "create_interaction",
        "acreate_interaction",
        # the Playground's other endpoint types
        "embedding",
        "aembedding",
        "image_generation",
        "aimage_generation",
        "image_edit",
        "aimage_edit",
        "speech",
        "aspeech",
        "transcription",
        "atranscription",
        "video_generation",
        "avideo_generation",
        "create_video",
        "acreate_video",
    }
)

# Where to send people instead, e.g. https://chatbot.example.com. Optional.
OPENWEBUI_URL_ENV = "UI_CHAT_BLOCK_OPENWEBUI_URL"


def _message() -> str:
    url = os.environ.get(OPENWEBUI_URL_ENV, "").strip()
    where = f"Open WebUI ({url})" if url else "Open WebUI"
    return (
        "Chat from the LiteLLM admin UI is turned off on this stack. "
        f"Use {where} or an API client such as opencode with a virtual key."
    )


def _header(headers, name: str) -> str:
    for key, value in (headers or {}).items():
        if str(key).lower() == name:
            return str(value)
    return ""


def _host(value: str) -> str:
    """Host name of a URL or Host header, lowercased, without the port."""
    value = value.strip()
    if "//" not in value:
        value = "//" + value
    try:
        return (urlsplit(value).hostname or "").lower()
    except ValueError:
        return ""


def _from_admin_ui_page(data: dict) -> bool:
    """True when the request's Referer is one of this proxy's own /ui pages."""
    request = data.get("proxy_server_request")
    headers = request.get("headers") if isinstance(request, dict) else None
    referer = _header(headers, "referer")
    if not referer:
        return False
    try:
        parts = urlsplit(referer)
    except ValueError:
        return False
    path = parts.path or ""
    if not (path.rstrip("/").endswith("/ui") or "/ui/" in path):
        return False
    referer_host = (parts.hostname or "").lower()
    own_hosts = {
        _host(os.environ.get("PROXY_BASE_URL", "")),
        _host(_header(headers, "host")),
    } - {""}
    return bool(referer_host) and referer_host in own_hosts


class UIChatBlock(CustomLogger):
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        if call_type not in BLOCKED_CALL_TYPES:
            return data
        ui_session = getattr(user_api_key_dict, "team_id", None) == UI_SESSION_TOKEN_TEAM_ID
        if ui_session or _from_admin_ui_page(data):
            # A plain string: LiteLLM returns it as the error's message.
            raise HTTPException(status_code=403, detail=_message())
        return data


proxy_handler_instance = UIChatBlock()


def register_callback():
    if not any(isinstance(callback, UIChatBlock) for callback in litellm.callbacks):
        # First in line: nothing else should work on a request that is refused.
        litellm.callbacks.insert(0, proxy_handler_instance)


register_callback()
