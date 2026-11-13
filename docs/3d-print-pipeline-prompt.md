# 3D print pipeline — model skill

**The canonical skill text lives in `skills/3d-print-pipeline/SKILL.md`**
(Claude/Anthropic Agent Skills format). The playbook registers that package
into the LiteLLM Skills Gateway: the zip is uploaded to the proxy-hosted
skill store (`/v1/skills`, `custom_llm_provider=litellm_proxy`) and the
registry entry (`/claude-code/plugins/3d-print-pipeline`) is served from the
proxy's own archive, so Claude Code (`3d-print-pipeline@litellm`), `npx
skills add`, and any Skills-API agent can fetch it. Semantic search
(`skill_search_embedding_model`, by default the local `local-embedding`
model) lets agents find it by description. The copy below is kept for human reading; if you edit one,
edit both.

---

You have three MCP tool servers for 3D work, all reached through LiteLLM:

- **blender** — mesh modeling (organic shapes, figurines, cases, artistic
  models). Author geometry with bpy code, inspect the scene, export STL.
- **freecad** — parametric CAD (brackets, enclosures, adapters, threads,
  dimensioned mechanical parts). Model with FreeCAD Python via execute_code,
  export STL or STEP.
- **printer_3d** — slicing and printing (Bambu H2C): `slice_stl`,
  `print_3mf`, `process_and_print_stl`, `get_printer_status`,
  `resolve_3mf_ams_slots` (dry-run filament plan), `camera_snapshot`.

## File conventions

1. **All generated geometry lands in `/models`.** Export STLs there with a
   descriptive name: `/models/phone-stand-v2.stl`. The printer server reads
   STLs from that shared volume; paths elsewhere on disk will not slice.
2. Blender: use its STL export tool (verified export with dimension report).
   FreeCAD: export via `Mesh`/`Part` export in execute_code to `/models`.
3. **Always report the exported dimensions in millimeters and compare them
   with the user's intent before slicing.** STL units are raw numbers; the
   slicer assumes millimeters. Catch a 1000× error before it becomes filament.

## Tool selection

- Organic/artistic mesh → **blender**
- Dimensioned/parametric/mechanical → **freecad**
- Repair, decimate, hollow, measure, or reorient a finished mesh →
  **code-sandbox** with trimesh (read/write `/models`)
- Then hand the STL to **printer_3d** for slicing.

## Print guardrail (mandatory)

- **Never call `print_3mf` or `process_and_print_stl` without asking the user
  first and receiving an explicit yes.** This is a hard rule; the printer
  gate only checks hardware state, not human intent.
- Before asking, slice with `slice_stl`, then show the user: estimated time,
  filament weight, material, and the AMS tray plan (run
  `resolve_3mf_ams_slots` dry-run; prefer `auto_match_ams: true` over
  hand-picking trays).
- Frame the question concretely: "Start this print? ~2h 10m, 34 g PLA, AMS
  tray 2 (black)."
- `get_printer_status` is always safe (read-only). Use it before proposing a
  print and to report progress afterward.
