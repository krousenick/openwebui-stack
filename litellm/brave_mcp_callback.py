import logging
import os
from types import MappingProxyType
from typing import Optional

from litellm.integrations.custom_logger import CustomLogger

logger = logging.getLogger("LiteLLM Proxy")

# Request metadata flag. The pre-call hook sets it on requests whose Brave
# tool calls LiteLLM executes itself (require_approval "never"). The
# agentic-loop gate below continues only requests that carry it, so a client
# that asked to approve tool calls still gets them back unexecuted.
AUTO_EXECUTE_KEY = "brave_mcp_auto_execute"
# LiteLLM marks the /v1/chat/completions agentic-loop dispatch with this
# value. /v1/messages and /v1/responses share the same hook names; this hook
# only acts on chat completions.
CHAT_COMPLETIONS_SURFACE = "chat_completions"
# LiteLLM's ceiling on follow-up rounds per request when the deployment sets
# no litellm_params.max_agentic_loops (litellm_core_utils/agentic_loop_settings).
DEFAULT_MAX_AGENTIC_LOOPS = 3
# Optional lower ceiling for the rounds this hook adds after the first one,
# read from the litellm container's environment. 0 turns them off (one
# search round per request, LiteLLM's own behaviour).
MAX_EXTRA_ROUNDS_ENV = "BRAVE_SEARCH_MAX_EXTRA_ROUNDS"
# Closes the results on the last round this hook may run, so the model knows
# it has to answer now.
FINAL_ANSWER_NOTE = (
    "\n\n[Search limit reached for this request. No further searches will run. "
    "Answer now from the results above.]"
)
# Providers whose API enforces tool_choice "none" (the model cannot return a
# tool call). Matched against "<provider>/<model>", so it covers Claude on
# the Anthropic API, Bedrock, and Vertex AI. Everywhere else the setting is
# advisory: on the live stack gemini-3.5-flash-lite returned another tool
# call under functionCallingConfig mode NONE, with the note in the tool
# result, and even with the tools removed from the request, as long as the
# history still showed tool calls (2026-11-10). Those models get the results
# as plain text instead; see _as_plain_final_request.
ENFORCED_TOOL_CHOICE_MARKERS = ("anthropic", "claude")


def _whole_number(value, default: int) -> int:
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


def _extra_rounds_allowed(kwargs: dict) -> int:
    """Follow-up rounds a request may get: LiteLLM's ceiling, lowered by the env var."""
    ceiling = _whole_number(kwargs.get("max_agentic_loops"), DEFAULT_MAX_AGENTIC_LOOPS)
    configured = _whole_number(os.environ.get(MAX_EXTRA_ROUNDS_ENV), ceiling)
    return max(0, min(ceiling, configured))


def _auto_execute_requested(kwargs: dict) -> bool:
    """True when the pre-call hook flagged this request for server-side execution."""
    buckets = [kwargs.get("metadata"), kwargs.get("litellm_metadata")]
    litellm_params = kwargs.get("litellm_params")
    if isinstance(litellm_params, dict):
        buckets.append(litellm_params.get("metadata"))
    return any(isinstance(bucket, dict) and bucket.get(AUTO_EXECUTE_KEY) is True for bucket in buckets)


def _function_tool_names(tools) -> set:
    names = set()
    for tool in tools or []:
        if isinstance(tool, dict) and tool.get("type") == "function":
            name = (tool.get("function") or {}).get("name")
            if isinstance(name, str):
                names.add(name)
    return names


def _response_tool_calls(response) -> list:
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


def _role_of(message) -> Optional[str]:
    if isinstance(message, dict):
        return message.get("role")
    return getattr(message, "role", None)


def _with_final_answer_note(messages: list) -> list:
    """Copy of messages with the note appended to the last tool result."""
    messages = list(messages)
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if not isinstance(message, dict) or message.get("role") != "tool":
            continue
        content = message.get("content")
        if isinstance(content, list):
            content = content + [{"type": "text", "text": FINAL_ANSWER_NOTE.strip()}]
        else:
            content = ("" if content is None else str(content)) + FINAL_ANSWER_NOTE
        messages[index] = {**message, "content": content}
        break
    return messages


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            part["text"] for part in content if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    return "" if content is None else str(content)


def _as_plain_final_request(messages: list) -> list:
    """Rewrite the tool rounds as text, for a last call that carries no tools.

    Assistant tool-call turns and tool results are removed and their content
    is appended, as numbered text sections, to the user turn they answered.
    With no tool calls left in the history and no tools on the request, a
    model has nothing to call and answers in text.
    """
    kept, labels, sections = [], {}, []
    for message in messages:
        if not isinstance(message, dict):
            kept.append(message)
            continue
        role = message.get("role")
        if role == "assistant" and message.get("tool_calls"):
            for tool_call in message["tool_calls"]:
                function = (tool_call.get("function") if isinstance(tool_call, dict) else None) or {}
                labels[tool_call.get("id")] = f"{function.get('name')} {function.get('arguments')}"
            continue
        if role == "tool":
            label = labels.get(message.get("tool_call_id")) or message.get("name") or "search"
            sections.append(f"[{len(sections) + 1}] {label}\n{_text_of(message.get('content'))}")
            continue
        kept.append(message)
    block = "Search results gathered for this request:\n\n" + "\n\n".join(sections) + FINAL_ANSWER_NOTE
    # Merged into the last user turn rather than added as a new one: some
    # chat templates (Gemma, Mistral) reject two user turns in a row.
    if kept and isinstance(kept[-1], dict) and kept[-1].get("role") == "user":
        content = kept[-1].get("content")
        if isinstance(content, list):
            content = content + [{"type": "text", "text": block}]
        else:
            content = _text_of(content) + "\n\n" + block
        kept[-1] = {**kept[-1], "content": content}
    else:
        kept.append({"role": "user", "content": block})
    return kept


def _final_round_request(messages: list, model, provider) -> tuple:
    """Messages and request overrides for the follow-up call that must answer in text."""
    target = f"{provider or ''}/{model or ''}".lower()
    if any(marker in target for marker in ENFORCED_TOOL_CHOICE_MARKERS):
        # Claude: the API enforces tool_choice "none". The tools and the
        # history stay as they are, which its thinking blocks require.
        return _with_final_answer_note(messages), {"tool_choice": "none"}
    # Everyone else: plain text, no tools. A None value removes the key from
    # LiteLLM's follow-up call. web_search_options goes too: with no function
    # tools left, a Gemini deployment that sets it would run a Google search
    # of its own on this call.
    return _as_plain_final_request(messages), {"tools": None, "web_search_options": None}


class BraveMCPInjector(CustomLogger):
    def __init__(self):
        super().__init__()
        self.tool = {
            "type": "mcp",
            "server_label": "brave",
            "server_url": "litellm_proxy/mcp/brave_search",
            "require_approval": "never",
        }

    async def async_pre_call_hook(
        self,
        user_api_key_dict,
        cache,
        data: dict,
        call_type: str,
    ) -> Optional[dict]:
        # The proxy passes the route type here: /v1/chat/completions arrives as
        # "acompletion", direct SDK calls as "completion".
        if call_type not in ("completion", "acompletion"):
            return data
        # Streaming clients that implement their own function-calling loop
        # (Open WebUI) record the streamed tool-call deltas as their own tool
        # calls, fail to find the MCP namespaced tool, and write error tool
        # responses that corrupt the history ("Missing corresponding tool call
        # for tool response message" on Gemini). Only inject for
        # non-streaming requests, where LiteLLM executes the tools and returns
        # just the final answer.
        if data.get("stream"):
            return data
        tools = data.get("tools")
        if not isinstance(tools, list):
            tools = []
        # Injecting an mcp-type tool flips LiteLLM into gateway execution
        # mode (acompletion_with_mcp) for the WHOLE request: with
        # require_approval "never" it auto-executes every tool call the
        # model returns, not just MCP ones. When the caller already sends
        # its own function tools (Open WebUI's client-side MCP servers name
        # them "<server_id>_<tool>", e.g. blender_blender-execute_blender_code),
        # LiteLLM cannot resolve those names against its own registry
        # (KeyError) and the call fails. Any function-type tool means the
        # caller owns the tool-calling loop; do not inject then.
        has_function_tools = any(
            isinstance(tool, dict) and tool.get("type") == "function" for tool in tools
        )
        if has_function_tools:
            return data
        # Open WebUI's direct MCP tool server already exposes Brave tools as
        # functions named "<server_id>_<tool>" (e.g. brave_search_...); skip
        # injection then so the model does not see the tool set twice.
        already_has_brave = any(
            isinstance(tool, dict)
            and (
                (
                    tool.get("type") == "mcp"
                    and tool.get("server_url") == self.tool["server_url"]
                )
                or str((tool.get("function") or {}).get("name") or "").startswith(
                    "brave_search"
                )
            )
            for tool in tools
        )
        if not already_has_brave:
            tools.append(dict(self.tool))
        data["tools"] = tools
        if self._gateway_auto_executes(tools):
            metadata = data.get("metadata")
            if not isinstance(metadata, dict):
                metadata = {}
                data["metadata"] = metadata
            metadata[AUTO_EXECUTE_KEY] = True
        return data

    def _gateway_auto_executes(self, tools: list) -> bool:
        # Same rule as LiteLLM's gateway: it runs tool calls itself only when
        # every mcp-type tool on the request says require_approval "never".
        mcp_tools = [tool for tool in tools if isinstance(tool, dict) and tool.get("type") == "mcp"]
        return (
            any(tool.get("server_url") == self.tool["server_url"] for tool in mcp_tools)
            and all(tool.get("require_approval") == "never" for tool in mcp_tools)
        )

    # ------------------------------------------------------------------
    # Further search rounds
    # ------------------------------------------------------------------
    # LiteLLM's gateway (acompletion_with_mcp) runs ONE round: model call,
    # tool calls executed, follow-up model call. When the model answers the
    # first results with another search, the gateway returns those tool
    # calls to the client, which never sent the tools and cannot run them.
    # The two methods below are LiteLLM's agentic-loop hooks. LiteLLM calls
    # the gate after every chat completion; when it returns True, LiteLLM
    # calls the plan builder and then makes the follow-up model call itself,
    # counting rounds against max_agentic_loops (default 3). The follow-up
    # goes through the same dispatch, so the loop repeats until the model
    # answers in text. The gateway keeps the first round; this hook adds the
    # rest, so a request gets up to 1 + 3 search rounds.

    async def async_should_run_agentic_loop(
        self,
        response,
        model,
        messages,
        tools,
        stream,
        custom_llm_provider,
        kwargs,
        **extra,
    ):
        try:
            calls = self._continuable_tool_calls(response, messages, tools, stream, kwargs)
        except Exception:
            logger.exception("Brave MCP: could not inspect the response for further tool calls")
            return False, {}
        if not calls:
            return False, {}
        return True, {"tool_calls": calls, "tool_type": "brave_mcp"}

    def _continuable_tool_calls(self, response, messages, tools, stream, kwargs) -> list:
        if stream or not isinstance(kwargs, dict):
            return []
        if kwargs.get("_agentic_loop_api_surface") != CHAT_COMPLETIONS_SURFACE:
            return []
        if not _auto_execute_requested(kwargs) or _extra_rounds_allowed(kwargs) < 1:
            return []
        # The gateway executes the model's first tool calls itself. Only a
        # reply to tool results (a second or later round) is continued here.
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
        # whole request. Every failure (a LiteLLM internal that moved, a tool
        # this hook cannot resolve) falls back to LiteLLM's default plan,
        # which runs nothing and returns the response as it is, the same
        # outcome as before this hook existed.
        plan = None
        try:
            plan = await self._plan_next_round(model, messages, response, kwargs or {})
        except Exception:
            logger.exception("Brave MCP: could not run a further tool round; returning the tool calls unexecuted")
        if plan is not None:
            return plan
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
            **extra,
        )

    async def _plan_next_round(self, model, messages, response, kwargs: dict):
        """Execute the pending Brave tool calls and describe the follow-up model call."""
        # Imported here, not at module load: these are LiteLLM internals, and
        # a failed import must cost one tool round, not the proxy's startup.
        from litellm.responses.mcp.litellm_proxy_mcp_handler import LiteLLM_Proxy_MCP_Handler
        from litellm.responses.mcp.request_context import MCPRequestContext
        from litellm.types.integrations.custom_logger import AgenticLoopPlan, AgenticLoopRequestPatch

        depth = _whole_number(kwargs.get("_agentic_loop_depth"), 0)
        allowed = _extra_rounds_allowed(kwargs)
        if depth >= allowed:
            return None

        # Same steps as LiteLLM's acompletion_with_mcp: resolve the caller's
        # MCP context, list the gateway's tools, execute, append the results.
        gateway_tool = dict(self.tool)
        gateway_tools, _ = await LiteLLM_Proxy_MCP_Handler.split_mcp_tools([gateway_tool])
        context = MCPRequestContext.resolve(
            kwargs=MappingProxyType({**kwargs, "model": model}),
            tools=[gateway_tool],
        )
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
        # execute_tool_calls raises KeyError on a name the gateway does not
        # serve. Leave such a response alone.
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

        # Appends the model's full assistant message (thinking blocks
        # included, which Claude needs back unchanged) and one tool message
        # per result. Tools and system prompt stay as they were.
        follow_up_messages = LiteLLM_Proxy_MCP_Handler.create_follow_up_messages_for_chat(
            original_messages=messages,
            response=response,
            tool_results=tool_results,
        )

        # LiteLLM refuses a further round once depth reaches the ceiling and
        # then only strips the tool calls, which leaves an empty answer (seen
        # live with Gemini). On the last round this hook may run, the
        # follow-up call is shaped so the model cannot call a tool and
        # answers from the results it has.
        follow_up_messages = list(follow_up_messages)
        optional_params = {}
        last_round = depth + 1 >= allowed
        if last_round:
            follow_up_messages, optional_params = _final_round_request(
                follow_up_messages, model, kwargs.get("custom_llm_provider")
            )
        logger.info(
            "Brave MCP: tool round %s of %s after the first (%s call(s))%s",
            depth + 1,
            allowed,
            len(pending),
            "; the follow-up must answer in text" if last_round else "",
        )
        return AgenticLoopPlan(
            run_agentic_loop=True,
            request_patch=AgenticLoopRequestPatch(
                messages=follow_up_messages,
                optional_params=optional_params,
            ),
            metadata={"tool_type": "brave_mcp", "round": depth + 1},
        )


proxy_handler_instance = BraveMCPInjector()


def register_callback():
    import litellm

    if not any(isinstance(callback, BraveMCPInjector) for callback in litellm.callbacks):
        litellm.callbacks.append(proxy_handler_instance)


register_callback()
