"""
title: Tool Result Image Display
description: Promotes MCP tool-result images (Playwright screenshots, etc.) to chat message files so they render in the chat window
required_open_webui_version: 0.13.0
version: 1.1.0
"""

import logging

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)


class Filter:
    # Open WebUI 0.13.0 stores MCP image content (Playwright screenshots and
    # the like) on the function_call_output output item's files array. The
    # frontend renders that only inside the collapsed "Tool Executed" box,
    # and the message node's own files array stays empty, so nothing shows
    # in the chat window itself (the upstream open-webui#33320 class of
    # bug). The outlet handler re-emits the stored file URLs through the
    # socket emitter, which persists them onto the assistant message node
    # and pushes them to the frontend, where they render as chat images.
    def __init__(self):
        pass

    def _collect_tool_output_images(self, output):
        collected = []
        if not isinstance(output, list):
            return collected
        for item in output:
            if not isinstance(item, dict) or item.get("type") != "function_call_output":
                continue
            for file_item in item.get("files") or []:
                if not isinstance(file_item, dict):
                    continue
                if file_item.get("type") == "image" and file_item.get("url"):
                    collected.append({"type": "image", "url": file_item["url"]})
        return collected

    async def outlet(self, body, __event_emitter__):
        chat_id = body.get("chat_id") or ""
        message_id = body.get("id") or ""
        if not chat_id or not message_id or not __event_emitter__:
            return body

        for message in body.get("messages", []):
            if message.get("id") != message_id or message.get("role") != "assistant":
                continue
            image_files = self._collect_tool_output_images(message.get("output"))
            if image_files:
                await __event_emitter__(
                    {
                        "type": "files",
                        "data": {
                            "files": image_files,
                        },
                    }
                )

        return body
