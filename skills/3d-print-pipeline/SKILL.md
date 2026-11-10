---
name: 3d-print-pipeline
description: >-
  Model, slice, and print 3D parts end-to-end on the Bambu H2C. Covers the
  blender (mesh), freecad (parametric), and printer_3d (slice + print) MCP
  tools, the /models shared volume convention, dimension checks in
  millimeters, and the mandatory ask-before-print confirmation rule.
license: MIT
---

# 3D Print Pipeline

Prompt-to-print: author geometry, export a verified STL to the shared
volume, slice it, and start the print on the Bambu H2C — after asking.

## Tools (all through LiteLLM MCP)

| Tool server | Use for | Key tools |
|---|---|---|
| `printer_3d` | Slicing + printing (Bambu H2C) | `slice_stl`, `print_3mf`, `process_and_print_stl`, `get_printer_status`, `resolve_3mf_ams_slots` (dry-run filament plan), `camera_snapshot` |
| `blender` | Mesh / organic modeling | scene tools, bpy code execution, verified STL export |
| `freecad` | Parametric CAD (dimensioned, mechanical) | `execute_code`, documents, parts list |
| code sandbox | Mesh post-processing only | trimesh: repair, decimate, hollow, measure, reorient |

## Rules

1. **File convention.** All generated geometry lands in
   `/models/<descriptive-name>.stl` — the shared volume the slicer reads
   from. Paths elsewhere will not slice. (STEP from FreeCAD for archival is
   fine, but always also export the printable STL.) Blender 5.x exports with
   `bpy.ops.wm.stl_export(filepath=...)` — the old `export_mesh.stl`
   operator no longer exists. FreeCAD exports via Mesh in execute_code.
2. **Dimension check.** Before slicing, report the exported dimensions in
   **millimeters** and compare with the user's intent. STL units are raw
   numbers; the slicer assumes mm. Catch a 1000x error before it becomes
   wasted filament.
3. **Tool choice.** Organic/artistic → `blender`. Dimensioned/mechanical
   (brackets, enclosures, threads) → `freecad`. Repair/decimate/measure a
   finished mesh → code sandbox trimesh on `/models`. Then hand the STL to
   `printer_3d` for slicing.
4. **Ask before printing — mandatory.** Never call `print_3mf` or
   `process_and_print_stl` without asking the user first and receiving an
   explicit yes. The printer's own gate only checks hardware state, not
   human intent. Before asking:
   - slice with `slice_stl`,
   - run `resolve_3mf_ams_slots` (dry-run) and prefer
     `auto_match_ams: true` over hand-picking trays,
   - present: estimated print time, filament weight, material, and the AMS
     tray plan ("Start this print? ~2h 10m, 34 g PLA, AMS tray 2 (black).").
5. **Status is always safe.** `get_printer_status` is read-only. Use it
   before proposing a print and to report progress afterward.
   `camera_snapshot` (RTSP) works while printing.

## FreeCAD `execute_code` that works

Headless FreeCAD 1.1 (no GUI, no `FreeCADGui`). Vectors come from
`FreeCAD`, not `Part`. Print the result so the tool output proves the
export, and never report a file you did not see confirmed there.

```python
import FreeCAD, Part, Mesh, MeshPart, os
V = FreeCAD.Vector

doc = FreeCAD.newDocument("Part")
# Solids: makeBox(l, w, h), makeCylinder(r, h, base, direction),
# makeCone(r_bottom, r_top, h, base, direction)
outer = Part.makeCone(28.6, 41.3, 76.2)
inner = Part.makeCone(25.6, 38.3, 76.2)
hole = Part.makeCylinder(3.2, 200, V(-100, 0, 63.5), V(1, 0, 0))
shape = outer.cut(inner).cut(hole)          # booleans: cut / fuse / common

# Move/rotate with translate() and rotate(center, axis, degrees) on the
# shape itself; FreeCAD.Matrix has move(), not translate().
shape.translate(V(0, 0, 0))

path = "/models/snoot-cone.stl"
mesh = MeshPart.meshFromShape(Shape=shape, LinearDeflection=0.05, AngularDeflection=0.3)
mesh.write(path)
bb = shape.BoundBox
print("EXPORTED", path, os.path.getsize(path), "bytes",
      "valid=", shape.isValid(), "mm=", round(bb.XLength, 2), round(bb.YLength, 2), round(bb.ZLength, 2))
```

A tool result that starts with `Failed to execute code` means nothing was
exported: fix the code and call again. Do not tell the user a file exists,
and do not invent download links (`sandbox:/...`); Open WebUI attaches the
exported `/models` file to the reply automatically.

## Gotchas

- Print starts refused with HMS `0500-0500-0001-0007` → the printer's
  Developer Mode is off; ask the user to enable it (Settings → Network).
- Bed: `textured_plate` is the verified CLI bed; `supertack_plate` is not.
- Nozzle type/flow must match the printer's report; default is 0.4 mm.
- If a slicing profile error mentions an unknown machine, confirm the
  printer model is H2C — the slicer image's presets are H2C-specific.
