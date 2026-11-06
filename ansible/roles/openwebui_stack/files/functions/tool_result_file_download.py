"""
title: Tool Result File Download
description: Uploads /models artifacts (STL/3MF/STEP) that MCP tool results report as exported to real Open WebUI files, so users can download them from the chat
required_open_webui_version: 0.13.0
version: 1.0.0
"""

import logging
import mimetypes
import os
import re
from io import BytesIO

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)


class Filter:
    # Open WebUI's process_tool_result only persists image/* and audio/wav
    # tool-result content as real files; binary resources become
    # "[Resource: uri] (binary data...)" text placeholders, and text results
    # stay text. Blender/FreeCAD/printer-mcp tool results are text that
    # mentions exported files under /models (the shared volume), which the
    # open-webui container mounts read-only. This outlet filter detects those
    # mentions after the turn completes, reads each file, uploads it through
    # Open WebUI's own file handler (same code path as manual uploads, so
    # ownership, storage, and RAG limits all apply), and emits a files event
    # so the file renders as a downloadable card in the chat window.
    #
    # Detection is deliberately narrow: absolute paths under /models with a
    # known CAD/print extension, matched against tool output text only. The
    # file must already exist at that path; nothing is created here.

    MODELS_DIR = "/models"
    EXTENSIONS = (".stl", ".3mf", ".step", ".stp")
    PATH_RE = re.compile(r"/models/[\w.\- ]+?\.(?:stl|3mf|step|stp)\b", re.IGNORECASE)
    MAX_FILE_SIZE = 500 * 1024 * 1024

    def __init__(self):
        pass

    def _detect_model_paths(self, message):
        found = []
        output = message.get("output") or []
        if not isinstance(output, list):
            return found
        for item in output:
            if not isinstance(item, dict) or item.get("type") != "function_call_output":
                continue
            for part in item.get("output") or []:
                if not isinstance(part, dict) or part.get("type") != "output_text":
                    continue
                text = part.get("text") or ""
                if not isinstance(text, str) or "/models/" not in text:
                    continue
                found.extend(m.group(0) for m in self.PATH_RE.finditer(text))
        # Also scan the message content (the model's own report of the export)
        content = message.get("content")
        if isinstance(content, str) and "/models/" in content:
            found.extend(m.group(0) for m in self.PATH_RE.finditer(content))
        seen = set()
        unique = []
        for path in found:
            normalized = path.rstrip(".")
            if normalized not in seen:
                seen.add(normalized)
                unique.append(path)
        return unique

    def _file_record(self, file_item):
        meta = file_item.meta or {}
        return {
            "type": "file",
            "id": file_item.id,
            "url": f"/api/v1/files/{file_item.id}/content",
            "name": meta.get("name") or file_item.filename,
            "size": meta.get("size"),
            "content_type": meta.get("content_type"),
        }

    async def _upload(self, request, path, user, metadata):
        from starlette.datastructures import UploadFile

        from open_webui.routers.files import upload_file_handler

        size = os.path.getsize(path)
        if size > self.MAX_FILE_SIZE:
            log.warning("tool_result_file_download: skipping %s (%d bytes over limit)", path, size)
            return None
        filename = os.path.basename(path)
        with open(path, "rb") as fh:
            data = fh.read()
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        upload = UploadFile(
            file=BytesIO(data),
            filename=filename,
            headers={"content-type": content_type},
        )
        file_item = await upload_file_handler(
            request,
            file=upload,
            metadata=None,
            process=False,
            process_in_background=False,
            user=user,
            background_tasks=None,
            db=None,
        )
        return file_item

    async def outlet(self, body, __event_emitter__, __user__=None, __request__=None, __metadata__=None):
        chat_id = body.get("chat_id") or ""
        message_id = body.get("id") or ""
        if not chat_id or not message_id or not __event_emitter__:
            return body
        if not isinstance(__user__, dict) or not __user__.get("id") or __request__ is None:
            return body

        # The models volume is only mounted when a 3D-pipeline service is
        # enabled; a missing directory means there is nothing to read.
        if not os.path.isdir(self.MODELS_DIR):
            return body

        from open_webui.models.users import UserModel

        user = UserModel(**__user__)

        uploaded = []
        for message in body.get("messages", []):
            if message.get("id") != message_id or message.get("role") != "assistant":
                continue
            for path in self._detect_model_paths(message):
                if not os.path.isfile(path):
                    continue
                try:
                    file_item = await self._upload(__request__, path, user, __metadata__ or {})
                except Exception as e:
                    log.warning("tool_result_file_download: upload failed for %s: %s", path, e)
                    continue
                if file_item is not None:
                    uploaded.append(self._file_record(file_item))

        if uploaded:
            await __event_emitter__(
                {
                    "type": "files",
                    "data": {
                        "files": uploaded,
                    },
                }
            )

        return body
