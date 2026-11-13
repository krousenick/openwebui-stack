"""Hand the stack's skills to every model, on /v1/chat/completions and /v1/messages.

A skill (skills/<name>/SKILL.md) reaches a model only when its client loads
it: Open WebUI lists them itself, Claude Code installs them from the
marketplace, opencode copies the folder, and an MCP client can add LiteLLM's
`skills` server (skills-mcp, container-scripts/skills_mcp_server.py). This
LiteLLM pre-call hook covers every other caller, so a plain API request
names nothing:

1. A request that carries no tools of its own gets the `skills` server as a
   gateway tool ({"type": "mcp", "server_url": "litellm_proxy/mcp/skills",
   "require_approval": "never"}), which LiteLLM runs itself, and a short
   index of the skills (each name with what it is for) in its system
   prompt, the way Claude Code lists its skills; the model reads one with
   skills-get. On /v1/messages (call type anthropic_messages) streaming or
   not: LiteLLM's /v1/messages gateway runs every tool round itself and
   answers a streaming request with one fake stream. On
   /v1/chat/completions only when not streaming: LiteLLM's chat gateway
   streams the first round's tool-call chunks to the client, and a client
   that runs its own tool loop then tries to run skills-get itself (the
   Brave injector's rule).
2. A request whose tools already include the skills tools (its client added
   the `skills` server: LiteLLM `skills-get`, opencode `skills_skills-get`,
   Claude Code `mcp__skills__skills-get`) gets the index only.
3. Everything else is left alone: requests with other tools (their client
   owns the tool loop, and one gateway tool would make LiteLLM run every
   tool call, the client's too, and fail on them), Open WebUI's requests
   (any X-OpenWebUI-* header: Open WebUI lists its own copy of the skills
   and offers view_skill), Claude models (on this stack the user's own
   Claude subscription, SKILLS_SKIP_MODELS), requests that opt out with
   metadata {"skills_index": false}, and every request while the `skills`
   server is not registered with LiteLLM.

LiteLLM's chat gateway runs one tool round. When the model answers the
skill it read with another call to the gateway's tools (a second skill, or a
web search the skill asks for), the agentic-loop hooks below run that round
too, up to LiteLLM's max_agentic_loops (3), and the last round they may run
has to end in text. brave_mcp_callback.py does the same for Brave-only
rounds and declines mixed ones, which then reach this hook.

The index comes from SKILLS_DIR (/app/skills, the read-only mount the
Skills Gateway task uses), read with the frontmatter rules of
container-scripts/skill_packages.py, checked for changes every 30 seconds.

Loaded through LITELLM_WORKER_STARTUP_HOOKS (docker-compose.yml) when
skills_mcp_hook_enabled: after the Brave injector, whose gateway tool it
joins, and before the context compactor, so the index counts toward the
window the compactor fits a history to.

Environment, all optional: SKILLS_DIR (/app/skills), SKILLS_MCP_SERVER_NAME
(skills), SKILLS_INDEX_DESCRIPTION_CHARS (200), SKILLS_INDEX_MAX_CHARS
(6000), SKILLS_SKIP_MODELS (claude*,anthropic/*: shell patterns, matched
against the requested model name in lower case) and SKILLS_MAX_EXTRA_ROUNDS
(LiteLLM's ceiling; 0 turns the later chat rounds off).
"""

from __future__ import annotations

import fnmatch
import logging
import math
import os
import re
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Any

import litellm
from litellm.integrations.custom_logger import CustomLogger

logger = logging.getLogger("LiteLLM Proxy")

CHAT_CALL_TYPES = frozenset({"completion", "acompletion"})
MESSAGES_CALL_TYPE = "anthropic_messages"
# LiteLLM marks the /v1/chat/completions agentic-loop dispatch with this
# value; /v1/messages and /v1/responses call the same hook names.
CHAT_COMPLETIONS_SURFACE = "chat_completions"
GATEWAY_PREFIX = "litellm_proxy"
# Request metadata the pre-call hook sets on the chat requests it gave the
# tool to: the agentic-loop gate continues only those, through the gateway
# servers the request carried (the Brave injector's too).
AUTO_EXECUTE_KEY = "skills_mcp_auto_execute"
GATEWAY_SERVERS_KEY = "skills_mcp_gateway_servers"
# Per-request opt-out: metadata {"skills_index": false} leaves the request alone.
OPT_OUT_KEY = "skills_index"
INDEX_TAG = "gateway_skills"
INDEX_MARKER = f"<{INDEX_TAG}>"
TOOL_NAMES = ("get", "list", "search")
OPENWEBUI_HEADER_PREFIX = "x-openwebui-"
DEFAULT_SKIP_MODELS = "claude*,anthropic/*"
# LiteLLM's ceiling on follow-up rounds per request when the deployment sets
# no litellm_params.max_agentic_loops (litellm_core_utils/agentic_loop_settings).
DEFAULT_MAX_AGENTIC_LOOPS = 3
SCAN_INTERVAL = 30.0
# The same rules as brave_mcp_callback.py for the last round the hook may
# run: Claude's API enforces tool_choice "none"; every other provider gets
# the tool rounds as plain text and no tools.
ENFORCED_TOOL_CHOICE_MARKERS = ("anthropic", "claude")
FINAL_ANSWER_NOTE = (
    "\n\n[Tool limit reached for this request. No further tool calls will run. Answer now from the results above.]"
)

# container-scripts/skill_packages.py reads SKILL.md frontmatter with these
# expressions; the publisher enforces its rules, so the index shows exactly
# what LiteLLM, Open WebUI and opencode were given.
_FRONTMATTER = re.compile(r"(?ms)^---\s*\n(.*?)\n---\s*\n")
_NAME = re.compile(r"^name:[ \t]*(.*?)[ \t\r]*$", re.M)
_DESCRIPTION = re.compile(r"^description:\s*(.+)$", re.M)
_FOLDED_DESCRIPTION = re.compile(r"^description:\s*>-\s*\n((?:\s+.+\n?)+)", re.M)


def parse_frontmatter(text: str) -> tuple[str, str]:
    """(name, description) of a SKILL.md, read as skill_packages.parse_frontmatter reads it; "" when missing."""
    frontmatter = _FRONTMATTER.search(text)
    if not frontmatter:
        return "", ""
    block = frontmatter.group(1)
    name = _NAME.search(block)
    description = ""
    single_line = _DESCRIPTION.search(block)
    if single_line:
        description = " ".join(single_line.group(1).split())
    if description.startswith(">-"):
        folded = _FOLDED_DESCRIPTION.search(block)
        description = " ".join(line.strip() for line in folded.group(1).splitlines()) if folded else ""
    return (name.group(1).strip('"').strip("'") if name else ""), description


def clip(text: str, limit: int) -> str:
    """Text cut to at most limit characters at a word boundary, marked with '...' when cut."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[: max(0, limit - 3)]
    if text[len(cut)] != " " and " " in cut:
        cut = cut.rsplit(" ", 1)[0]  # drop the word the cut went through
    return cut.rstrip(" ,;:.") + "..."


class SkillIndex:
    """Names and descriptions of the skill packages under one folder, read again when a SKILL.md changes."""

    def __init__(self, root: Path, clock: Callable[[], float] = time.monotonic) -> None:
        self.root = root
        self._clock = clock
        self._lock = threading.Lock()
        self._checked = -math.inf
        self._stamp: tuple[tuple[str, int, int], ...] | None = None
        self._entries: tuple[tuple[str, str], ...] = ()
        self._warned: set[tuple[str, str]] = set()

    def entries(self) -> tuple[tuple[str, str], ...]:
        """(name, description) of every valid package, sorted by name."""
        now = self._clock()
        with self._lock:
            if now - self._checked < SCAN_INTERVAL:
                return self._entries
            self._checked = now
            stamp = self._scan()
            if stamp != self._stamp:
                self._entries = self._load(stamp)
                self._stamp = stamp
            return self._entries

    def _scan(self) -> tuple[tuple[str, int, int], ...]:
        """(package, mtime, size) of every SKILL.md, the cheap check for a change."""
        try:
            packages = sorted(entry for entry in self.root.iterdir() if entry.is_dir() and entry.name[:1] != ".")
        except OSError as error:
            self._warn(str(self.root), f"the skills folder cannot be read ({error.strerror or error})")
            return ()
        stamp = []
        for package in packages:
            try:
                stat = (package / "SKILL.md").stat()
            except OSError:
                continue
            stamp.append((package.name, stat.st_mtime_ns, stat.st_size))
        return tuple(stamp)

    def _load(self, stamp: tuple[tuple[str, int, int], ...]) -> tuple[tuple[str, str], ...]:
        entries = []
        for package, _, _ in stamp:
            try:
                name, description = parse_frontmatter((self.root / package / "SKILL.md").read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError) as error:
                self._warn(package, f"cannot be read ({error})")
                continue
            if name != package or not description:
                self._warn(package, "its frontmatter name must equal the folder name, with a description")
                continue
            entries.append((name, description))
        return tuple(entries)

    def _warn(self, package: str, reason: str) -> None:
        if (package, reason) not in self._warned:
            self._warned.add((package, reason))
            logger.warning("skills: left out of the index: skills/%s/SKILL.md %s", package, reason)


def index_text(entries: Sequence[tuple[str, str]], tool: str, description_chars: int, max_chars: int) -> str:
    """The index a model reads: one line per skill, and the tool that reads a skill."""
    head = (
        f"{INDEX_MARKER}\n"
        "Skills are tested instructions for specific jobs, kept on this gateway. When the task at hand matches "
        f"one below, call {tool} with its name before you start and follow what it says; skip them otherwise.\n"
    )
    lines: list[str] = []
    left_out: list[str] = []
    size = len(head)
    for name, description in entries:
        line = f"- {name}: {clip(description, description_chars)}\n"
        if left_out or size + len(line) > max_chars:
            left_out.append(name)
            continue
        lines.append(line)
        size += len(line)
    if left_out:
        lines.append(f"- Also available: {', '.join(left_out)}.\n")
    return f"{head}{''.join(lines)}</{INDEX_TAG}>"


def skills_tool_pattern(server: str) -> re.Pattern[str]:
    """Tool names of the skills server as clients present them.

    LiteLLM prefixes them "<server>-" (skills-get), Open WebUI and opencode
    add "<connection>_" (skills_skills-get), Claude Code "mcp__<server>__".
    """
    return re.compile(rf"(?:^|_){re.escape(server)}-(?:{'|'.join(TOOL_NAMES)})$")


def tool_name(tool: Mapping[str, Any]) -> str:
    """A chat completions function tool's name, or an Anthropic tool's."""
    function = tool.get("function")
    name = function.get("name") if isinstance(function, Mapping) else tool.get("name")
    return name if isinstance(name, str) else ""


def is_gateway_reference(tool: Any) -> bool:
    """An MCP tool that LiteLLM's own gateway serves (server_url litellm_proxy/...)."""
    return (
        isinstance(tool, Mapping)
        and tool.get("type") == "mcp"
        and str(tool.get("server_url") or "").startswith(GATEWAY_PREFIX)
    )


def request_headers(data: Mapping[str, Any]) -> Mapping[str, Any]:
    """The client's request headers: where LiteLLM keeps them for a model request."""
    for source in (data.get("proxy_server_request"), data.get("metadata"), data.get("litellm_metadata")):
        headers = source.get("headers") if isinstance(source, Mapping) else None
        if isinstance(headers, Mapping) and headers:
            return headers
    return {}


def metadata_buckets(data: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Where a request's metadata may sit, as a hook or an agentic-loop gate receives it."""
    buckets = [data.get("metadata"), data.get("litellm_metadata")]
    litellm_params = data.get("litellm_params")
    if isinstance(litellm_params, Mapping):
        buckets.append(litellm_params.get("metadata"))
    return [bucket for bucket in buckets if isinstance(bucket, Mapping)]


def with_chat_index(messages: list[Any], text: str) -> list[Any]:
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


def with_system_index(system: Any, text: str) -> Any:
    """An Anthropic request's top-level system prompt with the index appended."""
    if isinstance(system, str):
        if INDEX_MARKER in system:
            return system
        return f"{system.rstrip()}\n\n{text}" if system.strip() else text
    if isinstance(system, list):
        if any(isinstance(block, Mapping) and INDEX_MARKER in str(block.get("text")) for block in system):
            return system
        return [*system, {"type": "text", "text": text}]
    return text


# ---------------------------------------------------------------------------
# Later tool rounds on /v1/chat/completions (shapes as brave_mcp_callback.py)
# ---------------------------------------------------------------------------


def _whole_number(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return default
    return default


def _role_of(message: Any) -> str | None:
    if isinstance(message, Mapping):
        return message.get("role")
    return getattr(message, "role", None)


def _function_tool_names(tools: Any) -> set[str]:
    return {
        name
        for tool in tools or []
        if isinstance(tool, Mapping) and tool.get("type") == "function"
        for name in (tool_name(tool),)
        if name
    }


def _response_tool_calls(response: Any) -> list[dict[str, Any]]:
    """Tool calls of the first choice that made any, as plain dicts."""
    for choice in getattr(response, "choices", None) or []:
        tool_calls = getattr(getattr(choice, "message", None), "tool_calls", None)
        if not tool_calls:
            continue
        calls = []
        for tool_call in tool_calls:
            function = getattr(tool_call, "function", None)
            calls.append(
                {
                    "id": getattr(tool_call, "id", None),
                    "name": getattr(function, "name", None),
                    "arguments": getattr(function, "arguments", None),
                }
            )
        return calls
    return []


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            part["text"] for part in content if isinstance(part, Mapping) and isinstance(part.get("text"), str)
        )
    return "" if content is None else str(content)


def _with_final_answer_note(messages: list[Any]) -> list[Any]:
    """Copy of messages with the note appended to the last tool result."""
    messages = list(messages)
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if not isinstance(message, Mapping) or message.get("role") != "tool":
            continue
        content = message.get("content")
        if isinstance(content, list):
            content = [*content, {"type": "text", "text": FINAL_ANSWER_NOTE.strip()}]
        else:
            content = ("" if content is None else str(content)) + FINAL_ANSWER_NOTE
        messages[index] = {**message, "content": content}
        break
    return messages


def _as_plain_final_request(messages: list[Any]) -> list[Any]:
    """The tool rounds as text in the user turn they answered, for a last call that carries no tools."""
    kept: list[Any] = []
    labels: dict[Any, str] = {}
    sections: list[str] = []
    for message in messages:
        if not isinstance(message, Mapping):
            kept.append(message)
            continue
        role = message.get("role")
        if role == "assistant" and message.get("tool_calls"):
            for tool_call in message["tool_calls"]:
                function = (tool_call.get("function") if isinstance(tool_call, Mapping) else None) or {}
                labels[tool_call.get("id")] = f"{function.get('name')} {function.get('arguments')}"
            continue
        if role == "tool":
            label = labels.get(message.get("tool_call_id")) or message.get("name") or "tool"
            sections.append(f"[{len(sections) + 1}] {label}\n{_text_of(message.get('content'))}")
            continue
        kept.append(message)
    block = "Tool results gathered for this request:\n\n" + "\n\n".join(sections) + FINAL_ANSWER_NOTE
    # Merged into the last user turn rather than added as a new one: some
    # chat templates (Gemma, Mistral) reject two user turns in a row.
    if kept and isinstance(kept[-1], Mapping) and kept[-1].get("role") == "user":
        content = kept[-1].get("content")
        if isinstance(content, list):
            content = [*content, {"type": "text", "text": block}]
        else:
            content = _text_of(content) + "\n\n" + block
        kept[-1] = {**kept[-1], "content": content}
    else:
        kept.append({"role": "user", "content": block})
    return kept


def final_round_request(messages: list[Any], model: Any, provider: Any) -> tuple[list[Any], dict[str, Any]]:
    """Messages and request overrides for the follow-up call that must answer in text."""
    target = f"{provider or ''}/{model or ''}".lower()
    if any(marker in target for marker in ENFORCED_TOOL_CHOICE_MARKERS):
        # Claude: the API enforces tool_choice "none"; the tools and the
        # history stay as they are, which its thinking blocks require.
        return _with_final_answer_note(messages), {"tool_choice": "none"}
    # Everyone else: plain text, no tools. A None value removes the key from
    # LiteLLM's follow-up call; web_search_options goes too, so a provider
    # cannot run a search of its own on this call.
    return _as_plain_final_request(messages), {"tools": None, "web_search_options": None}


def _gateway_server_registered(name: str) -> bool:
    # Imported here, not at module load: a LiteLLM internal that moved must
    # cost the skills, not the proxy's startup.
    from litellm.proxy.mcp_server.mcp_server_manager import global_mcp_server_manager

    return global_mcp_server_manager.get_mcp_server_by_name(name) is not None


class SkillsForEveryModel(CustomLogger):
    def __init__(
        self,
        *,
        skills_dir: str | Path = "/app/skills",
        server_name: str = "skills",
        description_chars: int = 200,
        index_max_chars: int = 6000,
        skip_models: str = DEFAULT_SKIP_MODELS,
        max_extra_rounds: int | None = None,
        server_registered: Callable[[str], bool] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__()
        self.server_name = server_name
        self.server_url = f"{GATEWAY_PREFIX}/mcp/{server_name}"
        self.tool = {
            "type": "mcp",
            "server_label": server_name,
            "server_url": self.server_url,
            "require_approval": "never",
        }
        self.get_tool = f"{server_name}-get"
        self.tool_pattern = skills_tool_pattern(server_name)
        self.index = SkillIndex(Path(skills_dir), clock=clock)
        self.description_chars = description_chars
        self.index_max_chars = index_max_chars
        self.skip_models = tuple(pattern.strip().lower() for pattern in skip_models.split(",") if pattern.strip())
        self.max_extra_rounds = max_extra_rounds
        self._server_registered = server_registered or _gateway_server_registered
        self._warned: set[str] = set()

    @classmethod
    def from_env(cls) -> SkillsForEveryModel:
        rounds = os.environ.get("SKILLS_MAX_EXTRA_ROUNDS", "").strip()
        return cls(
            skills_dir=os.environ.get("SKILLS_DIR") or "/app/skills",
            server_name=os.environ.get("SKILLS_MCP_SERVER_NAME") or "skills",
            description_chars=_whole_number(os.environ.get("SKILLS_INDEX_DESCRIPTION_CHARS"), 200),
            index_max_chars=_whole_number(os.environ.get("SKILLS_INDEX_MAX_CHARS"), 6000),
            skip_models=os.environ.get("SKILLS_SKIP_MODELS", DEFAULT_SKIP_MODELS),
            max_extra_rounds=_whole_number(rounds, 0) if rounds else None,
        )

    def _warn_once(self, key: str, message: str, *args: Any) -> None:
        if key not in self._warned:
            self._warned.add(key)
            logger.warning(message, *args)

    # ------------------------------------------------------------------
    # The tool and the index
    # ------------------------------------------------------------------

    async def async_pre_call_hook(self, user_api_key_dict, cache, data: dict, call_type: str):
        if call_type not in CHAT_CALL_TYPES and call_type != MESSAGES_CALL_TYPE:
            return data
        try:
            return self.apply(data, call_type)
        except Exception as error:
            # The skills are an extra: a failure here never fails the request.
            logger.warning("skills: request left unchanged: %s", error)
            return data

    def plan(self, tools: Sequence[Any]) -> tuple[str, str] | None:
        """("tool", name) to add the gateway tool and the index, ("index", name) for the index only, or None.

        name is the tool the index tells the model to read a skill with.
        """
        references = [tool for tool in tools if is_gateway_reference(tool)]
        own = [tool for tool in tools if not is_gateway_reference(tool)]
        skills_tools = [
            name
            for tool in own
            if isinstance(tool, Mapping)
            for name in (tool_name(tool),)
            if name and self.tool_pattern.search(name)
        ]
        if skills_tools:
            getter = next((name for name in skills_tools if name.endswith(self.get_tool)), skills_tools[0])
            return "index", getter
        if any(str(ref.get("server_url") or "").rstrip("/") in (self.server_url, GATEWAY_PREFIX) for ref in references):
            # The caller named the skills server (or the whole gateway) itself.
            return "index", self.get_tool
        if own:
            return None
        # LiteLLM runs gateway tools itself only when every reference says
        # require_approval "never"; with one that does not, the model's
        # skills-get call would go back to a caller that cannot run it.
        if any(ref.get("require_approval") != "never" for ref in references):
            return None
        return "tool", self.get_tool

    def skipped(self, data: Mapping[str, Any]) -> bool:
        """Opted out, from Open WebUI, or for a model the hook must not touch."""
        if any(bucket.get(OPT_OUT_KEY) is False for bucket in metadata_buckets(data)):
            return True
        if any(str(key).lower().startswith(OPENWEBUI_HEADER_PREFIX) for key in request_headers(data)):
            return True
        model = str(data.get("model") or "").lower()
        return any(fnmatch.fnmatchcase(model, pattern) for pattern in self.skip_models)

    def server_registered(self) -> bool:
        try:
            registered = bool(self._server_registered(self.server_name))
        except Exception as error:
            self._warn_once("lookup", "skills: cannot look up MCP server '%s': %s", self.server_name, error)
            return False
        if not registered:
            self._warn_once(
                "missing",
                "skills: MCP server '%s' is not registered with LiteLLM; requests get no skills tool",
                self.server_name,
            )
        return registered

    def apply(self, data: dict, call_type: str) -> dict:
        if self.skipped(data):
            return data
        tools = data.get("tools")
        if tools is None:
            tools = []
        if not isinstance(tools, list):
            return data
        plan = self.plan(tools)
        if plan is None:
            return data
        action, getter = plan
        chat = call_type in CHAT_CALL_TYPES
        if action == "tool" and ((chat and data.get("stream")) or not self.server_registered()):
            return data
        entries = self.index.entries()
        if not entries:
            return data
        text = index_text(entries, getter, self.description_chars, self.index_max_chars)
        if chat:
            messages = data.get("messages")
            if not isinstance(messages, list) or not messages:
                return data
            data["messages"] = with_chat_index(messages, text)
        else:
            data["system"] = with_system_index(data.get("system"), text)
        if action == "tool":
            data["tools"] = [*tools, dict(self.tool)]
            if chat:
                metadata = data.get("metadata")
                if not isinstance(metadata, dict):
                    metadata = {}
                    data["metadata"] = metadata
                metadata[AUTO_EXECUTE_KEY] = True
                metadata[GATEWAY_SERVERS_KEY] = sorted(
                    {str(tool["server_url"]) for tool in data["tools"] if is_gateway_reference(tool)}
                )
        return data

    # ------------------------------------------------------------------
    # Later tool rounds on /v1/chat/completions
    # ------------------------------------------------------------------
    # LiteLLM's chat gateway (acompletion_with_mcp) runs ONE round: model
    # call, tool calls executed, follow-up model call. When the follow-up
    # answers with more calls to the gateway's tools, LiteLLM asks the
    # agentic-loop gate below after that completion; when it claims the
    # calls, the plan builder runs them with the gateway's own helpers and
    # LiteLLM makes the next call, counting rounds against max_agentic_loops.

    def extra_rounds_allowed(self, kwargs: Mapping[str, Any]) -> int:
        ceiling = _whole_number(kwargs.get("max_agentic_loops"), DEFAULT_MAX_AGENTIC_LOOPS)
        configured = ceiling if self.max_extra_rounds is None else self.max_extra_rounds
        return max(0, min(ceiling, configured))

    async def async_should_run_agentic_loop(
        self, response, model, messages, tools, stream, custom_llm_provider, kwargs, **extra
    ):
        try:
            calls = self._continuable_tool_calls(response, messages, tools, stream, kwargs)
        except Exception:
            logger.exception("skills: could not inspect the response for further tool calls")
            return False, {}
        if not calls:
            return False, {}
        return True, {"tool_calls": calls, "tool_type": "skills_mcp"}

    def _continuable_tool_calls(self, response, messages, tools, stream, kwargs) -> list[dict[str, Any]]:
        if stream or not isinstance(kwargs, Mapping):
            return []
        if kwargs.get("_agentic_loop_api_surface") != CHAT_COMPLETIONS_SURFACE:
            return []
        if not any(bucket.get(AUTO_EXECUTE_KEY) is True for bucket in metadata_buckets(kwargs)):
            return []
        if self.extra_rounds_allowed(kwargs) < 1:
            return []
        # The gateway runs the model's first tool calls itself; only a reply
        # to tool results (a second or later round) is continued here.
        if not messages or _role_of(messages[-1]) != "tool":
            return []
        calls = _response_tool_calls(response)
        offered = _function_tool_names(tools)
        if not calls or any(call["name"] not in offered for call in calls):
            return []
        return calls

    async def async_build_agentic_loop_plan(
        self,
        tools,
        model,
        messages,
        response,
        anthropic_messages_provider_config=None,
        anthropic_messages_optional_request_params=None,
        logging_obj=None,
        stream=False,
        kwargs=None,
        **extra,
    ):
        # LiteLLM does not catch an exception raised here: it would fail the
        # whole request. Every failure falls back to LiteLLM's default plan,
        # which runs nothing and returns the response as it is.
        plan = None
        try:
            plan = await self._plan_next_round(model, messages, response, kwargs or {})
        except Exception:
            logger.exception("skills: could not run a further tool round; returning the tool calls unexecuted")
        if plan is not None:
            return plan
        # Without extra: LiteLLM's default takes exactly these arguments.
        return await super().async_build_agentic_loop_plan(
            tools=tools,
            model=model,
            messages=messages,
            response=response,
            anthropic_messages_provider_config=anthropic_messages_provider_config,
            anthropic_messages_optional_request_params=anthropic_messages_optional_request_params,
            logging_obj=logging_obj,
            stream=stream,
            kwargs=kwargs,
        )

    def _gateway_tools(self, kwargs: Mapping[str, Any]) -> list[dict[str, Any]]:
        """The gateway references the request carried when the pre-call hook saw it (ours at least)."""
        servers: set[str] = {self.server_url}
        for bucket in metadata_buckets(kwargs):
            recorded = bucket.get(GATEWAY_SERVERS_KEY)
            if isinstance(recorded, list):
                servers.update(str(url) for url in recorded if str(url).startswith(GATEWAY_PREFIX))
        return [
            {"type": "mcp", "server_label": url.rsplit("/", 1)[-1], "server_url": url, "require_approval": "never"}
            for url in sorted(servers)
        ]

    async def _plan_next_round(self, model, messages, response, kwargs: Mapping[str, Any]):
        """Execute the pending gateway tool calls and describe the follow-up model call."""
        # LiteLLM internals, imported here so that a moved one costs a tool
        # round, not the proxy's startup.
        from litellm.responses.mcp.litellm_proxy_mcp_handler import LiteLLM_Proxy_MCP_Handler
        from litellm.responses.mcp.request_context import MCPRequestContext
        from litellm.types.integrations.custom_logger import AgenticLoopPlan, AgenticLoopRequestPatch

        depth = _whole_number(kwargs.get("_agentic_loop_depth"), 0)
        allowed = self.extra_rounds_allowed(kwargs)
        if depth >= allowed:
            return None

        # Same steps as LiteLLM's acompletion_with_mcp: resolve the caller's
        # MCP context, list the gateway's tools, execute, append the results.
        gateway_tools, _ = await LiteLLM_Proxy_MCP_Handler.split_mcp_tools(self._gateway_tools(kwargs))
        context = MCPRequestContext.resolve(kwargs=MappingProxyType({**kwargs, "model": model}), tools=gateway_tools)
        request_tags = list(context.request_tags) if context.request_tags else None
        served_tools, tool_server_map = await LiteLLM_Proxy_MCP_Handler.process_mcp_tools_without_openai_transform(
            user_api_key_auth=context.user_api_key_auth,
            mcp_tools_with_litellm_proxy=gateway_tools,
            litellm_trace_id=context.litellm_trace_id,
            mcp_auth_header=context.mcp_auth_header,
            mcp_server_auth_headers=context.mcp_server_auth_headers,
            request_tags=request_tags,
            raw_headers=context.raw_headers,
        )

        pending = LiteLLM_Proxy_MCP_Handler.extract_tool_calls_from_chat_response(response=response)
        names = [LiteLLM_Proxy_MCP_Handler.extract_tool_call_details(call)[0] for call in pending]
        # execute_tool_calls fails on a name the gateway does not serve:
        # leave such a response to the client.
        if not pending or any(name not in tool_server_map for name in names):
            return None

        tool_results = await LiteLLM_Proxy_MCP_Handler.execute_tool_calls(
            tool_server_map=tool_server_map,
            tool_calls=pending,
            served_tools=served_tools,
            user_api_key_auth=context.user_api_key_auth,
            mcp_auth_header=context.mcp_auth_header,
            mcp_server_auth_headers=context.mcp_server_auth_headers,
            oauth2_headers=context.oauth2_headers,
            raw_headers=context.raw_headers,
            litellm_call_id=context.litellm_call_id,
            litellm_trace_id=context.litellm_trace_id,
            request_tags=request_tags,
            guardrail_context=context.guardrail_context,
        )
        # Every tool call needs its result in the next turn (Claude rejects
        # the history otherwise).
        answered = {result.get("tool_call_id") for result in tool_results}
        call_ids = [LiteLLM_Proxy_MCP_Handler.extract_tool_call_details(call)[2] for call in pending]
        if any(call_id not in answered for call_id in call_ids):
            return None

        follow_up_messages = list(
            LiteLLM_Proxy_MCP_Handler.create_follow_up_messages_for_chat(
                original_messages=messages,
                response=response,
                tool_results=tool_results,
            )
        )
        optional_params: dict[str, Any] = {}
        last_round = depth + 1 >= allowed
        if last_round:
            follow_up_messages, optional_params = final_round_request(
                follow_up_messages, model, kwargs.get("custom_llm_provider")
            )
        logger.info(
            "skills: tool round %s of %s after the first (%s call(s))%s",
            depth + 1,
            allowed,
            len(pending),
            "; the follow-up must answer in text" if last_round else "",
        )
        return AgenticLoopPlan(
            run_agentic_loop=True,
            request_patch=AgenticLoopRequestPatch(messages=follow_up_messages, optional_params=optional_params),
            metadata={"tool_type": "skills_mcp", "round": depth + 1},
        )


proxy_handler_instance = SkillsForEveryModel.from_env()


def register_callback():
    if not any(isinstance(callback, SkillsForEveryModel) for callback in litellm.callbacks):
        litellm.callbacks.append(proxy_handler_instance)


register_callback()
