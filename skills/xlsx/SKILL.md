---
name: xlsx
description: >-
  Create, read, clean, and edit Excel workbooks (.xlsx) and CSV files
  with openpyxl and pandas: live formulas, number formats, frozen headers,
  tables and filters, conditional formatting, drop-downs, native charts,
  and formula checks. Use whenever the user wants a spreadsheet or CSV as
  the result, or an existing one analyzed, cleaned, or changed. Runs in
  Open WebUI's code interpreter (the file comes back as a download) and in
  local Python.
license: MIT
---

# Spreadsheets (.xlsx and CSV)

Build workbooks a person can keep using: inputs they can change,
formulas that recalculate, formats that make numbers readable. Then
check the formulas and hand over the file.

## Where the code runs

| You are in | Run Python with | Save to | The user gets |
|---|---|---|---|
| Open WebUI, Code Interpreter on (you have `execute_code`) | `execute_code`: the stack's sandbox, Python 3.13 with openpyxl, pandas, matplotlib, python-docx, python-pptx, pypdf, reportlab; no internet, no `pip` | The download directory your instructions name (under `/outputs/`) | A download card under your reply |
| Open WebUI, Code Interpreter off | Nothing | | Ask the user to turn on Code Interpreter in the chat input; meanwhile a small table can go in your reply as Markdown or CSV text |
| Claude Code or opencode | Local Python. If `import openpyxl` fails, ask before installing `openpyxl` and `pandas` (in a virtual environment) | The working directory, or the path the user gave | The file path |

Rules everywhere:

- End every script with `print("SAVED", path, os.path.getsize(path))`
  and say the file exists only after the output shows that line.
- Name files for their content (`2026-regional-sales.xlsx`). Never
  overwrite the user's file; save the result under a new name.
- In Open WebUI the user's uploaded files are not in the sandbox: you
  see their extracted contents, not the workbook. Rebuild the data from
  what you see (say that formulas and formatting are not carried over),
  or suggest a coding agent for changes to the original file.

## Build a workbook

```python
import os
from datetime import date

from openpyxl import Workbook
from openpyxl.chart import BarChart, Reference
from openpyxl.formatting.rule import CellIsRule
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.worksheet.table import Table, TableStyleInfo

OUT_DIR = "."  # Open WebUI: the download directory from your instructions
path = os.path.join(OUT_DIR, "2026-regional-sales.xlsx")
MONEY, PERCENT, DAY = '"$"#,##0', "0.0%", "yyyy-mm-dd"
INPUT = Font(color="0000FF")  # blue: a value the user is meant to change

wb = Workbook()
assumptions = wb.active
assumptions.title = "Assumptions"
assumptions.append(["Assumption", "Value", "Note"])
assumptions.append(["Q4 growth", 0.08, "Applied to every region"])
assumptions.append(["Margin target", 0.22, "Used for the status flag"])
for row in (2, 3):
    assumptions.cell(row, 2).number_format = PERCENT
    assumptions.cell(row, 2).font = INPUT
assumptions["A1"].font = assumptions["B1"].font = assumptions["C1"].font = Font(bold=True)
assumptions.column_dimensions["A"].width = 18
assumptions.column_dimensions["C"].width = 32

sales = wb.create_sheet("Sales")
sales.append(["Region", "Q3 revenue", "Q4 forecast", "Status", "Updated"])
records = [
    ("North", 420_000, "Open", date(2026, 10, 1)),
    ("South", 310_000, "Closed", date(2026, 10, 3)),
    ("East", 560_000, "Open", date(2026, 10, 2)),
    ("West", 240_000, "At risk", date(2026, 10, 6)),
    ("Central", 380_000, "Open", date(2026, 10, 4)),
]
for row, (region, revenue, status, updated) in enumerate(records, start=2):
    sales.append([region, revenue, f"=B{row}*(1+Assumptions!$B$2)", status, updated])
last = len(records) + 1
sales.append([])
sales.append(["Total", f"=SUM(B2:B{last})", f"=SUM(C2:C{last})"])
total = last + 2

for row in range(2, total + 1):
    sales.cell(row, 2).number_format = MONEY
    sales.cell(row, 3).number_format = MONEY
    sales.cell(row, 5).number_format = DAY
for column in "BC":
    sales[f"{column}{total}"].font = Font(bold=True)
sales[f"A{total}"].font = Font(bold=True)
for column, width in zip("ABCDE", (14, 14, 14, 11, 12)):
    sales.column_dimensions[column].width = width
for cell in sales[1]:
    cell.alignment = Alignment(horizontal="center")
sales.freeze_panes = "A2"

table = Table(displayName="SalesTable", ref=f"A1:E{last}")
table.tableStyleInfo = TableStyleInfo(name="TableStyleMedium2", showRowStripes=True)
sales.add_table(table)  # also gives the header row filter buttons

status_list = DataValidation(type="list", formula1='"Open,Closed,At risk"', allow_blank=True)
sales.add_data_validation(status_list)
status_list.add(f"D2:D{last}")

low = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
sales.conditional_formatting.add(
    f"B2:B{last}", CellIsRule(operator="lessThan", formula=["300000"], fill=low, font=Font(color="9C0006"))
)

chart = BarChart()
chart.title = "Q3 revenue and Q4 forecast"
chart.y_axis.title = "USD"
chart.y_axis.numFmt = MONEY
chart.x_axis.delete = False  # without these two lines some Excel versions hide the axes
chart.y_axis.delete = False
chart.add_data(Reference(sales, min_col=2, max_col=3, min_row=1, max_row=last), titles_from_data=True)
chart.set_categories(Reference(sales, min_col=1, min_row=2, max_row=last))
chart.width, chart.height = 18, 9
sales.add_chart(chart, "G2")

wb.save(path)
print("SAVED", path, os.path.getsize(path))
```

| Need | openpyxl |
|---|---|
| A cell's formula vs its value | `cell.value` holds the formula string (`"=SUM(B2:B6)"`). |
| Text that starts with `=` | Set `cell.data_type = "s"` after assigning, or the text becomes a formula. |
| Wrap and align | `cell.alignment = Alignment(wrap_text=True, vertical="top")` |
| Borders | `from openpyxl.styles import Border, Side`; `Border(bottom=Side(style="thin", color="999999"))` |
| Merge cells | `ws.merge_cells("A1:E1")`, for titles only; never inside a data table |
| Color scale | `from openpyxl.formatting.rule import ColorScaleRule`; `ColorScaleRule(start_type="min", start_color="F8696B", end_type="max", end_color="63BE7B")` |
| Hyperlink | `cell.hyperlink = "https://..."; cell.style = "Hyperlink"` |
| Macros in .xlsm | `load_workbook(path, keep_vba=True)` and save with the `.xlsm` extension |

## Workbook conventions

- **Formulas, not pasted results.** Totals, growth, ratios, and anything
  derived are Excel formulas, so the workbook still works when the user
  changes an input. Compute in Python only to check the formulas.
- **Inputs in one place.** Assumptions on their own sheet (or a clearly
  marked block), in blue font, with a note on what each one means.
  Formulas refer to them with absolute references (`$B$2`); no constants
  buried inside formulas.
- **One table per sheet**, one header row, one record per row, no blank
  rows or merged cells inside the data. Units in the headers ("Revenue
  ($)") or the number format, never typed into the cells.
- Real dates and numbers, not text that looks like them. Negative
  amounts as negative numbers, formatted, not written with parentheses.
- Freeze the header row, set widths so values are not cut off, and add a
  filter (an Excel table gives one).
- Name sheets for what they hold ("Assumptions", "Sales", "Summary").

## Read and clean data

```python
import pandas as pd

sheets = pd.read_excel(path, sheet_name=None)  # {sheet name: DataFrame}
for name, frame in sheets.items():
    print(name, frame.shape)
    print(frame.head(10).to_string())
```

pandas reads the values Excel cached when it last saved the file. A
workbook written by openpyxl has no cached values yet, so its formula
cells read as empty until Excel or LibreOffice opens and saves it; check
those formulas with the next block instead.

For messy sheets (titles above the header, notes below the data, merged
header cells), find the header row first and read from there:
`pd.read_excel(source, sheet_name="Data", header=3, usecols="A:F", nrows=120)`.
Then clean explicitly: `df.columns = df.columns.str.strip()`,
`pd.to_numeric(df[col], errors="coerce")`,
`pd.to_datetime(df[col], errors="coerce")`, and drop rows that are
entirely empty. Report what you changed and how many rows were dropped.

CSV: `pd.read_csv(source)` and `frame.to_csv(target, index=False)`.
Excel opens UTF-8 CSV correctly only with a byte-order mark:
`encoding="utf-8-sig"`.

## Check the formulas

openpyxl cannot calculate formulas, so verify them two ways: every
reference points somewhere real, and the logic matches the same
calculation done in pandas.

```python
import re

from openpyxl import load_workbook

wb = load_workbook(path)
problems = []
sheet_ref = re.compile(r"(?:'([^']+)'|([A-Za-z_][\w.]*))!")
for ws in wb.worksheets:
    for row in ws.iter_rows():
        for cell in row:
            formula = cell.value
            if not (isinstance(formula, str) and formula.startswith("=")):
                continue
            if "#REF!" in formula:
                problems.append(f"{ws.title}!{cell.coordinate}: broken reference {formula}")
            for quoted, bare in sheet_ref.findall(formula):
                if (quoted or bare) not in wb.sheetnames:
                    problems.append(f"{ws.title}!{cell.coordinate}: unknown sheet in {formula}")
print(problems or "every formula refers to existing sheets")

# Recompute the forecast in pandas and compare with what the formulas should give
growth = wb["Assumptions"]["B2"].value
revenue = [wb["Sales"][f"B{row}"].value for row in range(2, 7)]
expected_total = sum(value * (1 + growth) for value in revenue)
print("expected Q4 forecast total:", round(expected_total))
```

State the expected key results in your reply ("the Q4 total should show
$2,062,800"), so the user can spot a difference when Excel calculates.

## Edit an existing workbook

`load_workbook(source)` keeps values, formulas, styles, charts, images,
validation, and conditional formatting. It drops shapes, text boxes,
form controls, and slicers, and it drops macros unless the file is
opened with `keep_vba=True`. Save to a new name, and if the original had
any of those, tell the user what will be missing. Use
`load_workbook(source, data_only=True)` only for reading cached values,
and never save that copy: it replaces every formula with its last value.
