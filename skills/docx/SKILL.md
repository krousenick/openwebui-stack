---
name: docx
description: >-
  Create, read, and edit Word documents (.docx) with python-docx:
  headings, lists, tables, images and charts, headers and footers, page
  numbers, a table of contents, hyperlinks, comments, and tracked changes.
  Use whenever the user wants a Word file as the result, or an existing
  .docx summarized, converted, or changed. Runs in Open WebUI's code
  interpreter (the file comes back as a download) and in local Python.
  For PDF use pdf.
license: MIT
---

# Word Documents (.docx)

Build the document with python-docx, save it, prove it opens, and hand
the user the file.

## Where the code runs

| You are in | Run Python with | Save to | The user gets |
|---|---|---|---|
| Open WebUI, Code Interpreter on (you have `execute_code`) | `execute_code`: the stack's sandbox, Python 3.13 with python-docx, openpyxl, python-pptx, pypdf, reportlab, pandas, and matplotlib; no internet, no `pip` | The download directory your instructions name (under `/outputs/`) | A download card under your reply |
| Open WebUI, Code Interpreter off | Nothing | | Ask the user to turn on Code Interpreter in the chat input, then continue; until then you can only draft the text |
| Claude Code or opencode | Local Python. If `import docx` fails, ask before installing `python-docx` (in a virtual environment) | The working directory, or the path the user gave | The file path |

Rules everywhere:

- End every script with `print("SAVED", path, os.path.getsize(path))`
  and say the file exists only after the output shows that line.
- Name files for their content (`q3-board-report.docx`), never
  `output.docx`. Never overwrite a file the user gave you; save the
  result next to it under a new name.
- In Open WebUI the user's uploaded files are not in the sandbox: you see
  their extracted text, not the .docx. To change an uploaded document,
  rebuild it from that text (its original formatting is lost) and say so,
  or suggest a coding agent for edits that must keep the formatting.

## Create a document

```python
import os
from io import BytesIO

import matplotlib.pyplot as plt
from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.opc.constants import RELATIONSHIP_TYPE
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt

OUT_DIR = "."  # Open WebUI: the download directory from your instructions
path = os.path.join(OUT_DIR, "project-atlas-status-report.docx")


def add_field(paragraph, instruction):
    """Insert a Word field such as PAGE, NUMPAGES or TOC; Word computes it on open."""
    run = paragraph.add_run()
    for tag, attrs, text in (
        ("w:fldChar", {"w:fldCharType": "begin"}, None),
        ("w:instrText", {"xml:space": "preserve"}, instruction),
        ("w:fldChar", {"w:fldCharType": "end"}, None),
    ):
        element = OxmlElement(tag)
        for key, value in attrs.items():
            element.set(qn(key), value)
        if text:
            element.text = text
        run._r.append(element)
    return run


def add_hyperlink(paragraph, url, text):
    """Append a clickable, blue, underlined link to a paragraph."""
    rel_id = paragraph.part.relate_to(url, RELATIONSHIP_TYPE.HYPERLINK, is_external=True)
    link = OxmlElement("w:hyperlink")
    link.set(qn("r:id"), rel_id)
    run = OxmlElement("w:r")
    props = OxmlElement("w:rPr")
    color = OxmlElement("w:color")
    color.set(qn("w:val"), "0563C1")
    underline = OxmlElement("w:u")
    underline.set(qn("w:val"), "single")
    props.extend([color, underline])
    label = OxmlElement("w:t")
    label.text = text
    run.extend([props, label])
    link.append(run)
    paragraph._p.append(link)


doc = Document()
normal = doc.styles["Normal"]
normal.font.name = "Calibri"
normal.font.size = Pt(11)

section = doc.sections[0]
section.left_margin = section.right_margin = Inches(1)
section.header.paragraphs[0].text = "Project Atlas · Status report"
footer = section.footer.paragraphs[0]
footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
footer.add_run("Page ")
add_field(footer, "PAGE")
footer.add_run(" of ")
add_field(footer, "NUMPAGES")

doc.add_heading("Project Atlas status report", level=0)
meta = doc.add_paragraph()
meta.add_run("Reporting period: ").bold = True
meta.add_run("October 2026")

doc.add_heading("Summary", level=1)
doc.add_paragraph("The migration finished two weeks early. The remaining risk is the vendor sign-off.")
for point in ["Cut-over completed on 14 October", "Error rate down 40% since cut-over", "Spend 6% under plan"]:
    doc.add_paragraph(point, style="List Bullet")

doc.add_heading("Error rate", level=1)
fig, ax = plt.subplots(figsize=(6.5, 2.6), dpi=200)
ax.plot(["Jul", "Aug", "Sep", "Oct"], [2.1, 2.0, 1.9, 1.2], color="#0072B2", linewidth=2)
ax.set_ylim(0, 2.5)
ax.set_ylabel("Errors per 1,000 requests")
for side in ("top", "right"):
    ax.spines[side].set_visible(False)
image = BytesIO()
fig.savefig(image, format="png", bbox_inches="tight")
plt.close(fig)
image.seek(0)
doc.add_picture(image, width=Inches(6))

doc.add_heading("Milestones", level=1)
rows = [
    ("Milestone", "Owner", "Due", "Status"),
    ("Data migration", "Priya", "2026-10-14", "Done"),
    ("Vendor sign-off", "Marco", "2026-11-20", "At risk"),
    ("Decommission old cluster", "Lena", "2026-12-05", "Planned"),
]
table = doc.add_table(rows=len(rows), cols=len(rows[0]))
table.style = "Light Grid Accent 1"
table.alignment = WD_TABLE_ALIGNMENT.CENTER
for r, values in enumerate(rows):
    for c, value in enumerate(values):
        table.cell(r, c).text = value

doc.add_heading("Next steps", level=1)
for step in ["Book the vendor sign-off review", "Schedule the decommissioning window"]:
    doc.add_paragraph(step, style="List Number")
more = doc.add_paragraph("Details are on the ")
add_hyperlink(more, "https://example.com/atlas", "project page")
more.add_run(".")

doc.save(path)
print("SAVED", path, os.path.getsize(path))
```

| Need | python-docx |
|---|---|
| Page break | `doc.add_page_break()` |
| Landscape section | `s = doc.add_section(); s.orientation = WD_ORIENT.LANDSCAPE; s.page_width, s.page_height = s.page_height, s.page_width` (`from docx.enum.section import WD_ORIENT`) |
| Table of contents | `add_field(doc.add_paragraph(), 'TOC \\o "1-3" \\h \\z \\u')`. Word fills it when the user updates fields (it asks on open if you also add `<w:updateFields w:val="true"/>` to `doc.settings.element`); other viewers may show it empty. |
| Column widths | Set `cell.width = Inches(x)` on every cell in the column |
| Repeat a table's header row on each page | `trPr = table.rows[0]._tr.get_or_add_trPr(); el = OxmlElement("w:tblHeader"); el.set(qn("w:val"), "true"); trPr.append(el)` |
| Font for one run | `run.font.size = Pt(12); run.font.bold = True; run.font.color.rgb = RGBColor(0x1F, 0x4E, 0x79)` |
| Start from the user's template | `Document("template.docx")`: its styles, headers, and margins carry over; remove its sample paragraphs first |

Writing rules: one Title (`level=0`), then Heading 1 and Heading 2 in
order (never skip levels; the navigation pane and a TOC depend on them).
Use the built-in styles (`List Bullet`, `List Number`, table styles)
rather than manual formatting, so the user can restyle the document in
Word.

## Read a document

Text and tables in reading order, as Markdown:

```python
from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph


def docx_to_markdown(source):
    lines = []
    for block in Document(source).iter_inner_content():
        if isinstance(block, Table):
            for i, row in enumerate(block.rows):
                lines.append("| " + " | ".join(cell.text.strip() for cell in row.cells) + " |")
                if i == 0:
                    lines.append("|" + " --- |" * len(row.cells))
            lines.append("")
            continue
        text = block.text.strip()
        if not text:
            continue
        style = block.style.name if block.style is not None else ""
        if style == "Title":
            lines.append(f"# {text}")
        elif style.startswith("Heading") and style.split()[-1].isdigit():
            lines.append("#" * (int(style.split()[-1]) + 1) + " " + text)
        elif style.startswith("List"):
            lines.append(f"- {text}")
        else:
            lines.append(text)
        lines.append("")
    return "\n".join(lines)


print(docx_to_markdown(path))
```

Comments live in `Document(source).comments` (each has `.author` and
`.text`).

## Edit a document

Open the file, change it, and save under a new name. Text in Word is
split into runs (pieces with the same formatting); a phrase may span
several runs, so change text inside a run to keep its formatting.

```python
import copy
import datetime
import os

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

OUT_DIR = "."  # Open WebUI: the download directory from your instructions
source = os.path.join(OUT_DIR, "project-atlas-status-report.docx")  # the document to edit
edited = os.path.join(OUT_DIR, "project-atlas-status-report-reviewed.docx")


def replace_in_runs(paragraph, old, new):
    """Replace text that lies inside one run, keeping that run's formatting."""
    for run in paragraph.runs:
        if old in run.text:
            run.text = run.text.replace(old, new)
            return True
    return False


def next_revision_id(paragraph):
    ids = [int(v) for el in paragraph.part.element.iter() if (v := el.get(qn("w:id"))) and v.isdigit()]
    return max(ids, default=0) + 1


def tracked_replace(paragraph, old, new, author="Assistant"):
    """Replace `old` with `new` as a tracked change that Word shows as a deletion plus an insertion.

    Handles text inside one plain run (text and formatting only); returns
    False when `old` is not found in such a run.
    """
    plain = {qn("w:rPr"), qn("w:t")}
    for run in paragraph.runs:
        if old not in run.text or any(child.tag not in plain for child in run._r):
            continue
        before, _, after = run.text.partition(old)
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        def piece(text, deleted=False):
            element = copy.deepcopy(run._r)
            for child in element.findall(qn("w:t")):
                element.remove(child)
            node = OxmlElement("w:delText" if deleted else "w:t")
            node.set(qn("xml:space"), "preserve")
            node.text = text
            element.append(node)
            return element

        def revision(tag, content):
            element = OxmlElement(tag)
            element.set(qn("w:id"), str(next_revision_id(paragraph)))
            element.set(qn("w:author"), author)
            element.set(qn("w:date"), stamp)
            element.append(content)
            return element

        parts = ([piece(before)] if before else []) + [
            revision("w:del", piece(old, deleted=True)),
            revision("w:ins", piece(new)),
        ] + ([piece(after)] if after else [])
        for part in parts:
            run._r.addprevious(part)
        run._r.getparent().remove(run._r)
        return True
    return False


doc = Document(source)
for paragraph in doc.paragraphs:
    if tracked_replace(paragraph, "two weeks early", "ten days early", author="Reviewer"):
        anchor = paragraph.runs[0] if paragraph.runs else paragraph.add_run("")
        doc.add_comment(anchor, text="Checked against the cut-over log.", author="Reviewer", initials="RV")
    replace_in_runs(paragraph, "October 2026", "October 2026 (final)")
doc.save(edited)
print("SAVED", edited, os.path.getsize(edited))
```

Redlining rules: use tracked changes when the user will accept or
reject edits (contracts, policies, anything a second person signs off);
plain edits when they just want the new version. Keep each tracked
change small, on whole words or phrases, so a reviewer can judge it
alone. When a phrase spans several runs, `tracked_replace` returns False:
edit those paragraphs as plain changes and list them for the user.

## Check before you deliver

Re-open the saved file and confirm the structure you meant to build:

```python
from docx import Document
from docx.oxml.ns import qn

check = Document(edited)  # the file you just saved
headings = [p.text for p in check.paragraphs if p.style.name.startswith(("Heading", "Title"))]
body = check.element.body
print("headings:", headings)
print("tables:", len(check.tables), "| images:", len(check.inline_shapes))
print("tracked insertions:", len(body.findall(".//" + qn("w:ins"))), "| deletions:", len(body.findall(".//" + qn("w:del"))))
print("comments:", len(list(check.comments)))
```

There is no Word or LibreOffice in the sandbox, so nothing can render a
preview there. Describe what the file contains (sections, tables, page
setup) in your reply. In a coding agent with LibreOffice installed,
`soffice --headless --convert-to pdf --outdir <dir> <file.docx>` makes a
PDF you can check.
