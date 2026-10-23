from typing import Literal, Optional

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
        call_type: Literal[
            "completion",
            "text_completion",
            "embeddings",
            "image_generation",
            "moderation",
            "audio_transcription",
        ],
    ) -> Optional[dict]:
        if call_type != "completion":
            return data
        tools = data.get("tools")
        if not isinstance(tools, list):
            tools = []
        if not any(
            isinstance(tool, dict)
            and tool.get("type") == "mcp"
            and tool.get("server_url") == self.tool["server_url"]
            for tool in tools
        ):
            tools.append(dict(self.tool))
        data["tools"] = tools
        return data


proxy_handler_instance = BraveMCPInjector()


def register_callback():
    import litellm

    if not any(isinstance(callback, BraveMCPInjector) for callback in litellm.callbacks):
        litellm.callbacks.append(proxy_handler_instance)


register_callback()
