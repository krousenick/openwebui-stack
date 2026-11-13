---
name: pptx
description: >-
  Create, read, and edit PowerPoint decks (.pptx) with python-pptx: 16:9
  slides on one grid, titles that state each point, bullets, native
  charts, tables, images, speaker notes, and slide numbers; reorder or
  delete slides in an existing deck. Use whenever the user wants a
  presentation or slides as the result, or an existing deck summarized or
  changed. Runs in Open WebUI's code interpreter (the file comes back as a
  download) and in local Python.
license: MIT
---

# Presentations (.pptx)

Plan the story first, then build each slide on one grid with
python-pptx, check the result, and hand over the file.

## Where the code runs

| You are in | Run Python with | Save to | The user gets |
|---|---|---|---|
| Open WebUI, Code Interpreter on (you have `execute_code`) | `execute_code`: the stack's sandbox, Python 3.13 with python-pptx, matplotlib, pandas, python-docx, openpyxl, pypdf, reportlab; no internet, no `pip` | The download directory your instructions name (under `/outputs/`) | A download card under your reply |
| Open WebUI, Code Interpreter off | Nothing | | Ask the user to turn on Code Interpreter in the chat input; meanwhile you can draft the outline |
| Claude Code or opencode | Local Python. If `import pptx` fails, ask before installing `python-pptx` (in a virtual environment) | The working directory, or the path the user gave | The file path |

Rules everywhere:

- End every script with `print("SAVED", path, os.path.getsize(path))`
  and say the file exists only after the output shows that line.
- Name files for their content (`q3-business-review.pptx`). Never
  overwrite the user's file; save the result under a new name.
- In Open WebUI the user's uploaded files are not in the sandbox: you
  see the extracted text of a deck, not the deck. Rebuild from that text
  (the original design is not carried over), or suggest a coding agent
  for changes that must keep the user's template.

## Plan the deck before writing code

1. One sentence: what should the audience believe or decide at the end?
2. Outline the slides as titles that are full statements ("Support costs
   fell 18% after self-service launched"), not topics ("Support costs").
   Reading only the titles should tell the story.
3. Choose one visual per slide: a chart, a table, an image, a diagram,
   or at most five short bullets.
4. Put details, sources, and talking points in the speaker notes, not on
   the slide.

A typical deck: title slide, the answer up front (summary), three to
eight evidence slides, recommendation or next steps. Add an agenda only
above ten slides.

## Design rules

- 16:9 (13.333 × 7.5 in), margins of 0.6 in, the same title position on
  every slide.
- Titles 28-36 pt, body 18-24 pt, nothing below 14 pt (notes and sources
  excepted). If the text does not fit at 18 pt, split the slide.
- One accent color plus dark text on light backgrounds (or the user's
  template colors), the same color for the same thing on every slide.
- Native charts (editable in PowerPoint) for data; label values on the
  chart instead of relying on gridlines.
- Slide numbers on every slide except the title slide.

## Build a deck

The default python-pptx template is 4:3; `widen` stretches its master
and layouts to 16:9 so slides the user adds later in PowerPoint line up
too.

```python
import os
from io import BytesIO

import matplotlib.pyplot as plt
from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE, XL_LEGEND_POSITION
from pptx.enum.text import PP_ALIGN
from pptx.util import Inches, Pt

OUT_DIR = "."  # Open WebUI: the download directory from your instructions
path = os.path.join(OUT_DIR, "q3-business-review.pptx")
INK, MUTED, ACCENT, SECOND = RGBColor(0x20, 0x21, 0x24), RGBColor(0x5F, 0x63, 0x68), RGBColor(0x00, 0x72, 0xB2), RGBColor(0xE6, 0x9F, 0x00)
TITLE_SLIDE, TITLE_AND_CONTENT, TITLE_ONLY = 0, 1, 5


def widen(prs, width=Inches(13.333)):
    """Stretch the template to a new width. Layouts go first: their placeholders
    inherit positions from the master, so scaling the master first would scale
    those inherited positions twice."""
    factor = width / prs.slide_width
    prs.slide_width = width
    for shapes in [*(layout.shapes for layout in prs.slide_layouts), prs.slide_master.shapes]:
        for shape in shapes:
            shape.left, shape.width = int(shape.left * factor), int(shape.width * factor)


def style_runs(paragraph, size, color=INK, bold=None):
    for run in paragraph.runs:
        run.font.size = Pt(size)
        run.font.color.rgb = color
        if bold is not None:
            run.font.bold = bold


def new_slide(prs, layout, title, number=None):
    slide = prs.slides.add_slide(prs.slide_layouts[layout])
    shape = slide.shapes.title
    shape.left, shape.top, shape.width, shape.height = Inches(0.6), Inches(0.4), Inches(12.13), Inches(1.1)
    shape.text = title
    for paragraph in shape.text_frame.paragraphs:
        paragraph.alignment = PP_ALIGN.LEFT
        style_runs(paragraph, 32, bold=True)
    if number is not None:
        box = slide.shapes.add_textbox(Inches(12.0), Inches(6.95), Inches(0.75), Inches(0.35))
        box.text_frame.text = str(number)
        box.text_frame.paragraphs[0].alignment = PP_ALIGN.RIGHT
        style_runs(box.text_frame.paragraphs[0], 12, MUTED)
    return slide


def fill_bullets(placeholder, items, size=22):
    """items: strings, or (text, level) pairs for sub-points."""
    placeholder.left, placeholder.top, placeholder.width, placeholder.height = Inches(0.6), Inches(1.7), Inches(12.13), Inches(5.0)
    frame = placeholder.text_frame
    frame.clear()
    for i, item in enumerate(items):
        text, level = item if isinstance(item, tuple) else (item, 0)
        paragraph = frame.paragraphs[0] if i == 0 else frame.add_paragraph()
        paragraph.text, paragraph.level = text, level
        paragraph.space_after = Pt(10)
        style_runs(paragraph, size - 4 * level, INK if level == 0 else MUTED)


prs = Presentation()
widen(prs)

slide = prs.slides.add_slide(prs.slide_layouts[TITLE_SLIDE])
slide.shapes.title.text = "Q3 business review"
slide.placeholders[1].text = "Operations · October 2026"
style_runs(slide.shapes.title.text_frame.paragraphs[0], 40, bold=True)
style_runs(slide.placeholders[1].text_frame.paragraphs[0], 20, MUTED)

slide = new_slide(prs, TITLE_AND_CONTENT, "Self-service cut support costs 18% this quarter", number=2)
fill_bullets(slide.placeholders[1], [
    "Ticket volume fell from 4,100 to 3,360 a month",
    ("Password resets moved fully to self-service", 1),
    "First response time improved from 9 h to 4 h",
    "Next: extend self-service to billing questions",
])
slide.notes_slide.notes_text_frame.text = "Source: helpdesk export, 1 Jul-30 Sep 2026. Costs exclude the one-off migration."

slide = new_slide(prs, TITLE_ONLY, "East leads revenue; the Q4 forecast holds every region's rank", number=3)
data = CategoryChartData()
data.categories = ["North", "South", "East", "West", "Central"]
data.add_series("Q3 revenue", (4.2, 3.1, 5.6, 2.4, 3.8))
data.add_series("Q4 forecast", (4.5, 3.3, 6.0, 2.6, 4.1))
chart = slide.shapes.add_chart(
    XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(0.6), Inches(1.6), Inches(12.13), Inches(5.2), data
).chart
chart.has_legend = True
chart.legend.position = XL_LEGEND_POSITION.BOTTOM
chart.legend.include_in_layout = False
plot = chart.plots[0]
plot.has_data_labels = True
plot.data_labels.number_format = '"$"0.0"M"'
plot.data_labels.number_format_is_linked = False
for series, color in zip(plot.series, (ACCENT, SECOND)):
    series.format.fill.solid()
    series.format.fill.fore_color.rgb = color
chart.value_axis.has_major_gridlines = False
chart.value_axis.visible = False

slide = new_slide(prs, TITLE_ONLY, "Two milestones remain before year end", number=4)
rows = [("Milestone", "Owner", "Due", "Status"), ("Vendor sign-off", "Marco", "20 Nov", "At risk"), ("Decommission", "Lena", "5 Dec", "Planned")]
table = slide.shapes.add_table(len(rows), 4, Inches(0.6), Inches(1.8), Inches(12.13), Inches(0.5) * len(rows)).table
for r, values in enumerate(rows):
    for c, value in enumerate(values):
        cell = table.cell(r, c)
        cell.text = value
        style_runs(cell.text_frame.paragraphs[0], 18, bold=(r == 0), color=INK if r else RGBColor(0xFF, 0xFF, 0xFF))

slide = new_slide(prs, TITLE_ONLY, "Error rate halved after the cut-over", number=5)
fig, ax = plt.subplots(figsize=(10, 4.2), dpi=200)
ax.plot(["Jul", "Aug", "Sep", "Oct"], [2.1, 2.0, 1.9, 1.0], color="#0072B2", linewidth=3)
ax.set_ylim(0, 2.5)
ax.set_ylabel("Errors per 1,000 requests")
for side in ("top", "right"):
    ax.spines[side].set_visible(False)
image = BytesIO()
fig.savefig(image, format="png", bbox_inches="tight")
plt.close(fig)
image.seek(0)
slide.shapes.add_picture(image, Inches(0.6), Inches(1.6), width=Inches(12.13))

prs.save(path)
print("SAVED", path, os.path.getsize(path))
```

| Need | python-pptx |
|---|---|
| A slide with nothing on it | `prs.slides.add_slide(prs.slide_layouts[6])` (Blank) |
| Text box | `box = slide.shapes.add_textbox(left, top, width, height); box.text_frame.word_wrap = True` |
| Shape | `from pptx.enum.shapes import MSO_SHAPE`; `slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, l, t, w, h)`, then `.fill.solid()`, `.fill.fore_color.rgb`, `.line.fill.background()` for no outline |
| Background color | `slide.background.fill.solid(); slide.background.fill.fore_color.rgb = RGBColor(...)` |
| Line or pie chart | `XL_CHART_TYPE.LINE_MARKERS`, `XL_CHART_TYPE.PIE` (pie: one series, at most five categories) |
| The user's template | `Presentation("template.pptx")`; list `[l.name for l in prs.slide_layouts]` and use its layouts and placeholders instead of `widen` and fixed positions |

Text does not shrink to fit in python-pptx. Keep bullets to one line
each (about 60 characters at 22 pt on the full width) and at most five
per slide; split the slide rather than shrinking the font.

## Read a deck

```python
from pptx import Presentation


def outline(source):
    lines = []
    for number, slide in enumerate(Presentation(source).slides, start=1):
        title = slide.shapes.title.text if slide.shapes.title is not None else "(no title)"
        lines.append(f"## {number}. {title}")
        for shape in slide.shapes:
            if shape.has_text_frame and shape != slide.shapes.title:
                lines += [f"- {p.text}" for p in shape.text_frame.paragraphs if p.text.strip()]
            if shape.has_table:
                lines += [" | ".join(cell.text for cell in row.cells) for row in shape.table.rows]
            if shape.has_chart:
                lines.append(f"[chart: {shape.chart.chart_type}]")
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame.text.strip():
            lines.append(f"Notes: {slide.notes_slide.notes_text_frame.text.strip()}")
    return "\n".join(lines)


print(outline(path))
```

## Edit a deck

Change text inside runs to keep the template's formatting; reorder or
delete slides through the slide list.

```python
import os

from pptx import Presentation

OUT_DIR = "."  # Open WebUI: the download directory from your instructions
source = os.path.join(OUT_DIR, "q3-business-review.pptx")  # the deck to edit
edited = os.path.join(OUT_DIR, "q3-business-review-v2.pptx")


def replace_text(prs, old, new):
    count = 0
    for slide in prs.slides:
        for shape in slide.shapes:
            frames = [shape.text_frame] if shape.has_text_frame else []
            if shape.has_table:
                frames += [cell.text_frame for row in shape.table.rows for cell in row.cells]
            for frame in frames:
                for paragraph in frame.paragraphs:
                    for run in paragraph.runs:
                        if old in run.text:
                            run.text = run.text.replace(old, new)
                            count += 1
    return count


def move_slide(prs, old_index, new_index):
    slide_ids = prs.slides._sldIdLst
    slide_id = slide_ids[old_index]
    slide_ids.remove(slide_id)
    slide_ids.insert(new_index, slide_id)


def delete_slide(prs, index):
    slide_ids = prs.slides._sldIdLst
    slide_id = slide_ids[index]
    prs.part.drop_rel(slide_id.rId)
    slide_ids.remove(slide_id)


prs = Presentation(source)
print("replaced:", replace_text(prs, "October 2026", "November 2026"))
move_slide(prs, 4, 2)    # the error-rate slide moves up to third place
delete_slide(prs, 3)     # drop what is now the fourth slide
prs.save(edited)
print("SAVED", edited, os.path.getsize(edited))
```

Slide numbers in the text boxes added by `new_slide` do not update when
slides move; renumber them, or use the template's slide-number
placeholder when there is one.

## Check before you deliver

Re-open the file, print the outline, and read it as the audience would:
does each title state a point, and does the order tell the story? Flag
any text frame with more than five paragraphs or 400 characters as
likely to overflow, and fix it. There is no PowerPoint or LibreOffice in
the sandbox, so nothing can render slide images there; describe the deck
(slide count, what each slide shows) in your reply. In a coding agent
with LibreOffice installed, `soffice --headless --convert-to pdf --outdir
<dir> <deck.pptx>` makes a PDF you can look at.
