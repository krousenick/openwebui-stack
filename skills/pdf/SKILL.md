---
name: pdf
description: >-
  Create and process PDF files: reports and letters with reportlab
  (tables, charts, page numbers, Unicode text), and with pypdf text
  extraction, merge, split, rotate, watermark, form filling, encryption,
  and image extraction. Use whenever the user wants a PDF as the result,
  or an existing PDF read, combined, split, filled in, or changed. Runs in
  Open WebUI's code interpreter (the file comes back as a download) and in
  local Python. No OCR in the sandbox.
license: MIT
---

# PDF Files

Create PDFs with reportlab and work on existing ones with pypdf. Save
the result, check it opens with the pages you expect, and hand it over.

## Where the code runs

| You are in | Run Python with | Save to | The user gets |
|---|---|---|---|
| Open WebUI, Code Interpreter on (you have `execute_code`) | `execute_code`: the stack's sandbox, Python 3.13 with reportlab, pypdf, matplotlib, pandas, python-docx, openpyxl, python-pptx; no internet, no `pip`, no OCR | The download directory your instructions name (under `/outputs/`) | A download card under your reply |
| Open WebUI, Code Interpreter off | Nothing | | Ask the user to turn on Code Interpreter in the chat input; meanwhile you can draft the content |
| Claude Code or opencode | Local Python. If `import reportlab` or `import pypdf` fails, ask before installing it (in a virtual environment) | The working directory, or the path the user gave | The file path |

Rules everywhere:

- End every script with `print("SAVED", path, os.path.getsize(path))`
  and say the file exists only after the output shows that line.
- Name files for their content (`november-operations-report.pdf`). Never
  overwrite the user's file; write a new one.
- In Open WebUI the user's uploaded PDFs are not in the sandbox: you see
  their extracted text, not the file. You can build a new PDF from that
  text; merging, splitting, filling, or stamping the user's own PDF needs
  a coding agent.

## Create a PDF

```python
import os
from io import BytesIO
from xml.sax.saxutils import escape

import matplotlib
import matplotlib.pyplot as plt
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Image, ListFlowable, ListItem, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

OUT_DIR = "."  # Open WebUI: the download directory from your instructions
path = os.path.join(OUT_DIR, "november-operations-report.pdf")

# The built-in Helvetica covers Latin-1 only. matplotlib ships DejaVu Sans,
# which covers most scripts, so register it for every style.
fonts = os.path.join(matplotlib.get_data_path(), "fonts", "ttf")
for name, filename in (("Body", "DejaVuSans.ttf"), ("Body-Bold", "DejaVuSans-Bold.ttf"),
                       ("Body-Italic", "DejaVuSans-Oblique.ttf"), ("Body-BoldItalic", "DejaVuSans-BoldOblique.ttf")):
    pdfmetrics.registerFont(TTFont(name, os.path.join(fonts, filename)))
pdfmetrics.registerFontFamily("Body", normal="Body", bold="Body-Bold", italic="Body-Italic", boldItalic="Body-BoldItalic")

base = getSampleStyleSheet()
INK, MUTED, ACCENT = colors.HexColor("#202124"), colors.HexColor("#5F6368"), colors.HexColor("#0072B2")
title = ParagraphStyle("title", parent=base["Title"], fontName="Body-Bold", fontSize=22, leading=27, alignment=0, textColor=INK)
heading = ParagraphStyle("heading", parent=base["Heading2"], fontName="Body-Bold", fontSize=14, leading=18, spaceBefore=12, spaceAfter=4, textColor=INK)
body = ParagraphStyle("body", parent=base["BodyText"], fontName="Body", fontSize=10.5, leading=15, textColor=INK)
small = ParagraphStyle("small", parent=body, fontSize=8.5, leading=11, textColor=MUTED)


def decorate(canvas, doc):
    """Running footer on every page."""
    canvas.saveState()
    canvas.setFont("Body", 8.5)
    canvas.setFillColor(MUTED)
    canvas.drawString(doc.leftMargin, 12 * mm, "Operations report · November 2026")
    canvas.drawRightString(doc.pagesize[0] - doc.rightMargin, 12 * mm, f"Page {doc.page}")
    canvas.restoreState()


fig, ax = plt.subplots(figsize=(6.4, 2.6), dpi=200)
ax.bar(["Jul", "Aug", "Sep", "Oct"], [4100, 3900, 3600, 3360], color="#0072B2")
ax.set_ylabel("Tickets per month")
for side in ("top", "right"):
    ax.spines[side].set_visible(False)
chart = BytesIO()
fig.savefig(chart, format="png", bbox_inches="tight")
plt.close(fig)
chart.seek(0)

rows = [["Service", "Uptime", "Incidents", "Mean time to restore"],
        ["Chat", "99.97%", "1", "18 min"], ["API gateway", "99.95%", "2", "11 min"], ["Search", "99.90%", "3", "26 min"]]
table = Table(rows, colWidths=[55 * mm, 30 * mm, 30 * mm, 50 * mm], repeatRows=1)
table.setStyle(TableStyle([
    ("FONT", (0, 0), (-1, -1), "Body", 9.5),
    ("FONT", (0, 0), (-1, 0), "Body-Bold", 9.5),
    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
    ("BACKGROUND", (0, 0), (-1, 0), ACCENT),
    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F1F3F4")]),
    ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
    ("LINEBELOW", (0, -1), (-1, -1), 0.5, MUTED),
    ("TOPPADDING", (0, 0), (-1, -1), 5),
    ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
]))

story = [
    Paragraph("Operations report", title),
    Paragraph("November 2026 · prepared for the leadership team", small),
    Spacer(1, 8 * mm),
    Paragraph("Summary", heading),
    Paragraph(escape("Uptime held above 99.9% for every service while ticket volume fell 18% & response times halved."), body),
    ListFlowable(
        [ListItem(Paragraph(text, body), leftIndent=12) for text in [
            "<b>Self-service</b> now handles all password resets.",
            "Two incidents on the API gateway, both restored within 15 minutes.",
            "Next quarter: self-service for billing questions.",
        ]],
        bulletType="bullet", start="•", bulletFontName="Body", leftIndent=12,
    ),
    Paragraph("Service levels", heading),
    table,
    Paragraph("Ticket volume", heading),
    Image(chart, width=160 * mm, height=65 * mm),
    Paragraph("Source: helpdesk export and status page, 1 July-31 October 2026.", small),
]

doc = SimpleDocTemplate(path, pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm, topMargin=18 * mm,
                        bottomMargin=22 * mm, title="Operations report", author="Operations team")
doc.build(story, onFirstPage=decorate, onLaterPages=decorate)
print("SAVED", path, os.path.getsize(path))
```

- `Paragraph` text is a small markup language (`<b>`, `<i>`, `<br/>`,
  `<font color="#D55E00">`, `<a href="https://...">`). Pass any text you
  did not write yourself through `escape()` first, or a stray `&` or `<`
  breaks the build.
- Keep an image's aspect ratio: height = width × (figure height ÷ figure
  width).
- `from reportlab.platypus import PageBreak` forces a new page;
  `KeepTogether([...])` keeps a heading with what follows it.
- US paper: `from reportlab.lib.pagesizes import letter`.
- Letters and invoices follow the same pattern: a `Table` without grid
  lines for the address blocks, a bordered `Table` for line items, and
  totals computed in Python and shown right-aligned.

## Read a PDF

```python
from pypdf import PdfReader

reader = PdfReader(path)
print(len(reader.pages), "pages | title:", reader.metadata.title if reader.metadata else None)
for number, page in enumerate(reader.pages, start=1):
    text = page.extract_text(extraction_mode="layout")  # keeps columns roughly aligned, which helps with tables
    print(f"--- page {number} ---")
    print(text[:2000])
```

If a page returns little or no text, it is probably a scanned image.
The sandbox has no OCR engine: tell the user. In a coding agent,
`ocrmypdf` or `pytesseract` can add a text layer where they are
installed.

## Change a PDF

```python
import os
from io import BytesIO

from pypdf import PdfReader, PdfWriter
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

OUT_DIR = "."  # Open WebUI: the download directory from your instructions


def merge(sources, target):
    """Concatenate PDFs; a (path, (start, stop)) pair takes a page range (0-based, stop excluded)."""
    writer = PdfWriter()
    for source in sources:
        if isinstance(source, tuple):
            writer.append(source[0], pages=source[1])
        else:
            writer.append(source)
    writer.write(target)


def split(source, target_dir):
    """One file per page."""
    for number, page in enumerate(PdfReader(source).pages, start=1):
        writer = PdfWriter()
        writer.add_page(page)
        writer.write(os.path.join(target_dir, f"page-{number:03d}.pdf"))


def stamp(source, target, text="DRAFT", rotate_last=False):
    """Diagonal watermark on every page; optionally turn the last page 90 degrees."""
    layer = BytesIO()
    c = canvas.Canvas(layer, pagesize=A4)
    c.setFont("Helvetica-Bold", 72)
    c.setFillColorRGB(0.84, 0.37, 0.0, alpha=0.18)
    c.translate(A4[0] / 2, A4[1] / 2)
    c.rotate(45)
    c.drawCentredString(0, 0, text)
    c.save()
    mark = PdfReader(BytesIO(layer.getvalue())).pages[0]
    writer = PdfWriter(clone_from=source)
    for page in writer.pages:
        page.merge_page(mark)
    if rotate_last:
        writer.pages[-1].rotate(90)
    writer.write(target)


def protect(source, target, password):
    """AES-256 encryption; use the password the user gave you, never one you invent."""
    writer = PdfWriter(clone_from=source)
    writer.encrypt(user_password=password, algorithm="AES-256")
    writer.write(target)


def unlock(source, target, password):
    reader = PdfReader(source)
    if reader.is_encrypted:
        reader.decrypt(password)
    writer = PdfWriter(clone_from=reader)
    writer.write(target)


def form_fields(source):
    """{field name: current value} for a fillable PDF form."""
    return {name: field.get("/V") for name, field in (PdfReader(source).get_fields() or {}).items()}


def fill_form(source, target, values):
    """Fill AcroForm fields by name; list the names with form_fields() first."""
    writer = PdfWriter(clone_from=source)
    writer.update_page_form_field_values(None, values, auto_regenerate=False)
    writer.set_need_appearances_writer(True)
    writer.write(target)


def extract_images(source, target_dir):
    saved = []
    for number, page in enumerate(PdfReader(source).pages, start=1):
        for image in page.images:
            name = os.path.join(target_dir, f"page{number}-{image.name}")
            with open(name, "wb") as handle:
                handle.write(image.data)
            saved.append(name)
    return saved


report = os.path.join(OUT_DIR, "november-operations-report.pdf")  # a PDF to work on
draft = os.path.join(OUT_DIR, "november-operations-report-draft.pdf")
stamp(report, draft, "DRAFT")
print("SAVED", draft, os.path.getsize(draft))
```

Reorder pages: `writer = PdfWriter(); [writer.add_page(reader.pages[i])
for i in new_order]`. Delete pages the same way, by leaving them out.
Shrink a file: `writer.compress_identical_objects()` before writing, and
`page.compress_content_streams()` on each page.

## Check before you deliver

```python
from pypdf import PdfReader

for name in (path, draft):  # every file you produced
    check = PdfReader(name)
    first = check.pages[0].extract_text() or ""
    print(name, "|", len(check.pages), "pages | encrypted:", check.is_encrypted, "|", first[:80].replace("\n", " "))
```

Confirm the page count, that the first page starts with the right text,
and for forms that `form_fields()` shows the values you filled. There is
no PDF viewer in the sandbox; describe the file's pages in your reply.
