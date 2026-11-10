"""
title: Tool Result File Download
description: Makes chat-created 3D models downloadable - injects FreeCAD/Blender export rules into 3D modelling chats and uploads the /models artifacts (STL/3MF/STEP) their tool calls export as real Open WebUI files with a rendered STL preview image
required_open_webui_version: 0.13.0
version: 1.3.0
"""

import asyncio
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
    # Two detection paths, both limited to known CAD/print extensions under
    # /models, and both requiring the file to already exist (nothing is
    # created here):
    #
    # 1. Path mentions: absolute /models/... paths in the tool-call
    #    arguments, the tool output text, or the model's own reply.
    # 2. New files: any /models file written since the turn's first tool
    #    call started. Models routinely export without echoing the path
    #    (a bare "Code executed successfully"), so mentions alone miss real
    #    exports. Only runs for turns that made at least one tool call. The
    #    volume is shared, so a file another user exports in the same window
    #    would be attached too; acceptable for a small trusted deployment.

    MODELS_DIR = "/models"
    EXTENSIONS = (".stl", ".3mf", ".step", ".stp")
    PATH_RE = re.compile(r"/models/[\w.\- /]+?\.(?:stl|3mf|step|stp)\b", re.IGNORECASE)
    MAX_FILE_SIZE = 500 * 1024 * 1024
    MAX_FILES_PER_TURN = 10
    # Clock slack (seconds) between the tool-call start stamp taken in the
    # open-webui process and the file mtime set by the writer container.
    MTIME_SLACK = 2.0

    # Inlet: 3D-modelling guidance. Small local models write FreeCAD/Blender
    # code from memory of older or different APIs (Part.Vector, two-argument
    # rotate, bpy.ops.boolean.*), fail once, and give up or invent a download
    # link. Skills only load when $-mentioned or bound to a model, so the
    # rules that make an export succeed are injected into the system prompt
    # whenever the recent user messages are about 3D modelling. Verified
    # against FreeCAD 1.1 headless and gemma-4-26b-a4b: with this text the
    # model exported a correct STL on its first execute_code call.
    CAD_TOPIC_RE = re.compile(
        r"\b(stl|3mf|step file|freecad|blender|cad|3d[ -]?(?:print\w*|model\w*)|bambu)\b",
        re.IGNORECASE,
    )
    CAD_TOPIC_LOOKBACK = 6
    CAD_GUIDE_MARKER = "3D model export rules (FreeCAD / Blender tools):"
    CAD_GUIDE = (
        CAD_GUIDE_MARKER
        + """
- Save every model to /models/<name>.stl (absolute path). Open WebUI attaches new /models files to your reply automatically as a download. Never write a link to the file (no sandbox: or markdown links); just name it.
- End the code with print("EXPORTED", path, os.path.getsize(path)). Only say the file exists if the tool output shows EXPORTED.
- If the tool output starts with "Failed to execute code" or "Error", nothing was saved: read the error, fix that line, and call the tool again. Keep retrying (up to 6 times) before giving up; do not hand the user a script to run instead.
FreeCAD execute_code (headless FreeCAD 1.1, units mm):
  import FreeCAD, Part, MeshPart, os; V = FreeCAD.Vector   # vectors are FreeCAD.Vector, never Part.Vector
  Part.makeBox(l, w, h) | Part.makeCylinder(r, h, V(base), V(direction)) | Part.makeCone(r_bottom, r_top, h)
  a.cut(b) | a.fuse(b) | a.common(b)                        # booleans return a new shape
  s.translate(V(dx, dy, dz)); s.rotate(V(cx, cy, cz), V(ax, ay, az), degrees)   # rotate takes exactly 3 arguments
  A hole through both sides of a round part is ONE long cylinder across it, e.g. Part.makeCylinder(r, 500, V(-250, 0, z), V(1, 0, 0)); no rotation or matrix needed.
  Hollow cone/tube: outer solid .cut(inner solid). Do not use FreeCAD.Matrix or FreeCADGui.
  Export: MeshPart.meshFromShape(Shape=s, LinearDeflection=0.05, AngularDeflection=0.3).write("/models/name.stl")
Blender execute_blender_code (Blender 5.0): there is no bpy.ops.boolean.*; add a BOOLEAN modifier (mod.operation = 'DIFFERENCE', mod.object = cutter) and apply it with bpy.ops.object.modifier_apply(modifier=mod.name). Export with bpy.ops.wm.stl_export(filepath="/models/name.stl"); bpy.ops.export_mesh.stl does not exist."""
    )

    def __init__(self):
        pass

    def _is_cad_conversation(self, messages):
        recent = [m for m in messages if isinstance(m, dict) and m.get("role") == "user"]
        for message in recent[-self.CAD_TOPIC_LOOKBACK :]:
            for text in self._item_texts(message.get("content")):
                if self.CAD_TOPIC_RE.search(text):
                    return True
        return False

    async def inlet(self, body, __user__=None):
        messages = body.get("messages")
        if not isinstance(messages, list) or not self._is_cad_conversation(messages):
            return body
        for message in messages:
            if not isinstance(message, dict) or message.get("role") != "system":
                continue
            content = message.get("content")
            if isinstance(content, str):
                if self.CAD_GUIDE_MARKER not in content:
                    message["content"] = content.rstrip() + "\n\n" + self.CAD_GUIDE
                return body
            if isinstance(content, list):
                if not any(self.CAD_GUIDE_MARKER in t for t in self._item_texts(content)):
                    content.append({"type": "text", "text": self.CAD_GUIDE})
                return body
        messages.insert(0, {"role": "system", "content": self.CAD_GUIDE})
        return body

    @staticmethod
    def _item_texts(value):
        # Tool output arrives as a plain string or as a list of parts whose
        # type is "input_text" (Open WebUI 0.13 tool results), "output_text"
        # or "text" depending on the code path; take the text of all of them.
        if isinstance(value, str):
            return [value]
        texts = []
        if isinstance(value, list):
            for part in value:
                if isinstance(part, str):
                    texts.append(part)
                elif isinstance(part, dict) and isinstance(part.get("text"), str):
                    texts.append(part["text"])
        return texts

    def _mentioned_paths(self, message):
        texts = []
        output = message.get("output")
        if isinstance(output, list):
            for item in output:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "function_call_output":
                    texts.extend(self._item_texts(item.get("output")))
                elif item.get("type") == "function_call":
                    texts.extend(self._item_texts(item.get("arguments")))
        # Also scan the message content (the model's own report of the export)
        texts.extend(self._item_texts(message.get("content")))
        found = []
        for text in texts:
            if "/models/" in text:
                found.extend(m.group(0) for m in self.PATH_RE.finditer(text))
        return found

    def _turn_started_at(self, message):
        # Earliest tool-call start in this turn, or None when the turn made no
        # tool calls (then nothing can have been exported by it).
        output = message.get("output")
        if not isinstance(output, list):
            return None
        starts = []
        has_call = False
        for item in output:
            if isinstance(item, dict) and item.get("type") == "function_call":
                has_call = True
                if isinstance(item.get("started_at"), (int, float)):
                    starts.append(float(item["started_at"]))
        if starts:
            return min(starts)
        if has_call and isinstance(message.get("timestamp"), (int, float)):
            return float(message["timestamp"])
        return None

    def _new_files_since(self, started_at):
        found = []
        for root, dirs, files in os.walk(self.MODELS_DIR):
            # One level of subdirectories is enough for /models/<project>/x.stl
            if root != self.MODELS_DIR:
                dirs[:] = []
            for name in files:
                if not name.lower().endswith(self.EXTENSIONS):
                    continue
                path = os.path.join(root, name)
                try:
                    mtime = os.path.getmtime(path)
                except OSError:
                    continue
                if mtime >= started_at - self.MTIME_SLACK:
                    found.append((mtime, path))
        return [path for _, path in sorted(found)]

    def _detect_model_paths(self, message):
        candidates = self._mentioned_paths(message)
        started_at = self._turn_started_at(message)
        if started_at is not None:
            candidates.extend(self._new_files_since(started_at))
        models_root = os.path.realpath(self.MODELS_DIR) + os.sep
        seen = set()
        unique = []
        for path in candidates:
            # realpath collapses ".." and symlinks so a crafted mention cannot
            # read outside the shared volume.
            real = os.path.realpath(path.rstrip("."))
            if not real.startswith(models_root) or real in seen:
                continue
            seen.add(real)
            unique.append(real)
        return unique[: self.MAX_FILES_PER_TURN]

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

    # STL preview. Blender's `look` tool returns a viewport screenshot, but
    # FreeCAD runs headless (freecadcmd, no GUI) and has no view to capture,
    # so FreeCAD exports arrived as a bare file card. Every exported STL gets
    # a rendered preview instead, drawn here with numpy + Pillow (both ship
    # in the open-webui image; no GPU or display needed): isometric view,
    # flat shading, painter's-algorithm depth sort, with the bounding-box
    # size in the caption so a wrong-unit export is visible at a glance.
    PREVIEW_SIZE = (960, 720)
    PREVIEW_MAX_TRIANGLES = 150000
    PREVIEW_MAX_BYTES = 100 * 1024 * 1024

    @staticmethod
    def _read_stl_triangles(path):
        import numpy as np

        with open(path, "rb") as fh:
            data = fh.read()
        if len(data) >= 84:
            count = int(np.frombuffer(data, dtype="<u4", count=1, offset=80)[0])
            if len(data) == 84 + 50 * count and count > 0:
                record = np.dtype([("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")])
                return np.frombuffer(data, dtype=record, count=count, offset=84)["v"].astype(np.float64)
        # ASCII STL: every facet lists exactly three "vertex x y z" lines.
        values = re.findall(rb"vertex\s+(\S+)\s+(\S+)\s+(\S+)", data)
        if not values or len(values) % 3:
            return None
        return np.array(values, dtype=np.float64).reshape(-1, 3, 3)

    def _render_stl_preview(self, path):
        import numpy as np
        from PIL import Image, ImageDraw

        if os.path.getsize(path) > self.PREVIEW_MAX_BYTES:
            return None
        tris = self._read_stl_triangles(path)
        if tris is None or len(tris) == 0 or not np.isfinite(tris).all():
            return None
        lo, hi = tris.reshape(-1, 3).min(axis=0), tris.reshape(-1, 3).max(axis=0)
        extents = hi - lo
        total = len(tris)
        if total > self.PREVIEW_MAX_TRIANGLES:
            tris = tris[:: int(np.ceil(total / self.PREVIEW_MAX_TRIANGLES))]

        # Camera looks along `forward` with Z up (the slicer's build direction).
        forward = np.array([-1.0, 1.0, -0.75])
        forward /= np.linalg.norm(forward)
        right = np.cross(forward, [0.0, 0.0, 1.0])
        right /= np.linalg.norm(right)
        up = np.cross(right, forward)
        light = np.array([0.35, -0.55, 0.75])
        light /= np.linalg.norm(light)

        normals = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
        lengths = np.linalg.norm(normals, axis=1)
        keep = lengths > 0
        tris, normals = tris[keep], normals[keep] / lengths[keep][:, None]
        if len(tris) == 0:
            return None
        facing = normals @ -forward
        # Inside faces (seen through an opening) are lit from their back side
        # and darkened so hollow parts and through-holes read as such.
        shade = 0.30 + 0.70 * np.clip(np.where(facing >= 0, normals @ light, -(normals @ light)), 0, 1)
        shade = np.where(facing >= 0, shade, shade * 0.55)

        scale_up = 2  # supersample, then downscale for anti-aliased edges
        width, height = self.PREVIEW_SIZE[0] * scale_up, self.PREVIEW_SIZE[1] * scale_up
        caption_h = 56 * scale_up
        margin = 40 * scale_up
        xs, ys = tris @ right, tris @ up
        span = max(xs.max() - xs.min(), ys.max() - ys.min(), 1e-9)
        scale = min((width - 2 * margin) / (xs.max() - xs.min() or span),
                    (height - caption_h - 2 * margin) / (ys.max() - ys.min() or span))
        px = (xs - (xs.max() + xs.min()) / 2) * scale + width / 2
        py = (height - caption_h) / 2 - (ys - (ys.max() + ys.min()) / 2) * scale

        image = Image.new("RGB", (width, height), (245, 246, 248))
        draw = ImageDraw.Draw(image)
        base = np.array([70.0, 130.0, 200.0])
        # Painter's algorithm on triangle centroids mis-sorts long thin facets,
        # which showed as dark inside-face streaks over the outer wall. Drawing
        # every inside (back-facing) triangle before any outside one removes
        # that class of error; within each pass, farthest first.
        order = np.lexsort((-(tris @ forward).mean(axis=1), facing >= 0))
        for idx in order:
            colour = tuple(int(c) for c in base * shade[idx])
            draw.polygon(
                [(px[idx, 0], py[idx, 0]), (px[idx, 1], py[idx, 1]), (px[idx, 2], py[idx, 2])],
                fill=colour,
            )
        image = image.resize(self.PREVIEW_SIZE, Image.LANCZOS)
        draw = ImageDraw.Draw(image)
        caption = "%s   %.1f x %.1f x %.1f mm   %s triangles" % (
            os.path.basename(path), extents[0], extents[1], extents[2], format(total, ","),
        )
        try:
            from PIL import ImageFont

            font = ImageFont.load_default(size=20)
        except Exception:
            font = None
        draw.text((24, self.PREVIEW_SIZE[1] - 40), caption, fill=(40, 44, 52), font=font)
        buffer = BytesIO()
        image.save(buffer, format="PNG", optimize=True)
        return buffer.getvalue()

    async def _upload(self, request, path, user, metadata, data=None, filename=None):
        from starlette.datastructures import UploadFile

        from open_webui.routers.files import upload_file_handler

        if data is None:
            size = os.path.getsize(path)
            if size > self.MAX_FILE_SIZE:
                log.warning("tool_result_file_download: skipping %s (%d bytes over limit)", path, size)
                return None
            with open(path, "rb") as fh:
                data = fh.read()
        filename = filename or os.path.basename(path)
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
                if file_item is None:
                    continue
                if path.lower().endswith(".stl"):
                    # A failed preview must never cost the user the download.
                    try:
                        png = await asyncio.to_thread(self._render_stl_preview, path)
                        if png:
                            preview_name = os.path.splitext(os.path.basename(path))[0] + "-preview.png"
                            preview = await self._upload(
                                __request__, path, user, __metadata__ or {}, data=png, filename=preview_name
                            )
                            if preview is not None:
                                uploaded.append({"type": "image", "url": f"/api/v1/files/{preview.id}/content"})
                    except Exception as e:
                        log.warning("tool_result_file_download: preview failed for %s: %s", path, e)
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
