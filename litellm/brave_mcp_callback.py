from typing import Optional

from litellm.integrations.custom_logger import CustomLogger


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
        return data


proxy_handler_instance = BraveMCPInjector()


def register_callback():
    import litellm

    if not any(isinstance(callback, BraveMCPInjector) for callback in litellm.callbacks):
        litellm.callbacks.append(proxy_handler_instance)


register_callback()
