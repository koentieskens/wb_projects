"""Excel export with readable formatting (frozen header, filters, widths, clickable links)."""

from __future__ import annotations

import io

import pandas as pd
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

HEADER_FILL = PatternFill("solid", fgColor="1F3864")
HEADER_FONT = Font(bold=True, color="FFFFFF")
LINK_FONT = Font(color="0563C1", underline="single")

WIDTHS = {"Project name": 45, "Matched document(s)": 50, "Best snippet": 70, "Snippet": 70,
          "Document title": 50, "Project development objective": 70, "Sectors": 40, "Borrower": 30,
          "Implementing agency": 35, "Matched keywords": 35, "Explanation": 55, "Reason excluded": 50,
          "Keywords found (any document)": 35, "Other keywords (in other docs)": 30,
          "Match basis (document type)": 34, "Document type": 34}
WRAP = {"Best snippet", "Snippet", "Project development objective", "Matched document(s)"}
LINK_COLS = {"Document link", "Project page", "PDF link", "Text link"}
MONEY_COLS = {"Total commitment (US$ m)", "IBRD commitment (US$ m)", "IDA commitment (US$ m)",
              "Grant amount (US$ m)"}
DATE_COLS = {"Approval date", "Closing date", "Document date"}


def _format_sheet(ws, df: pd.DataFrame) -> None:
    ws.freeze_panes = "B2"
    if ws.max_row >= 1 and ws.max_column >= 1:
        ws.auto_filter.ref = ws.dimensions
    for j, col in enumerate(df.columns, start=1):
        c = ws.cell(row=1, column=j)
        c.fill, c.font = HEADER_FILL, HEADER_FONT
        c.alignment = Alignment(wrap_text=True, vertical="center")
        letter = get_column_letter(j)
        if col in WIDTHS:
            width = WIDTHS[col]
        else:
            sample = [len(str(col))] + [len(str(v)) for v in df[col].head(200) if v is not None]
            width = min(40, max(10, max(sample) + 2))
        ws.column_dimensions[letter].width = width
        for i in range(2, ws.max_row + 1):
            cell = ws.cell(row=i, column=j)
            if col in LINK_COLS and isinstance(cell.value, str) and cell.value.startswith("http"):
                cell.hyperlink = cell.value
                cell.font = LINK_FONT
            elif col in MONEY_COLS:
                cell.number_format = "#,##0.00"
            elif col in DATE_COLS:
                cell.number_format = "yyyy-mm-dd"
            elif col == "Approval FY":
                cell.number_format = "0"
            if col in WRAP:
                cell.alignment = Alignment(wrap_text=True, vertical="top")
            else:
                cell.alignment = Alignment(vertical="top")
    ws.row_dimensions[1].height = 32


def _about(result) -> pd.DataFrame:
    s = result.settings
    rows = [
        ("Run at", result.run_at),
        ("Search terms", "; ".join(s.terms)),
        ("Number of search terms", len(s.terms)),
        ("Approval date range", f"Active/Closed projects approved in or after FY{s.min_fy} "
                                f"(from 1 July {s.min_fy - 1}). Pipeline projects: no date limit."),
        ("Rule – Pipeline", "Included if a search term appears in the PID (or in a PAD / PAD equivalent, if public)."),
        ("Rule – Active / Closed", "Included if a search term appears in the PAD or a PAD equivalent "
                                   "(Program Appraisal Document – PforR; Program Document – DPF; Project Paper – "
                                   "additional financing/restructuring). If no PAD is disclosed yet, a match in the "
                                   "PID counts and is labelled 'PID (PAD not yet disclosed)'."),
        ("Dropped projects", "Included" if s.include_dropped else "Excluded"),
        ("Matching", "Whole words / exact phrases from the search-term list. Case-insensitive, except "
                     "all-capital terms (e.g. NBS) which are case-sensitive. Spaces, hyphens and line breaks "
                     "between the words of a phrase are treated as equivalent."
                     + (" Plural endings (-s/-es) also match." if s.match_plurals else "")),
        ("Text verified", "Yes = the term was found by this tool in the document's full text. "
                          "No = no text version was available, so the World Bank search engine's match is used."),
        ("Amounts", "US$ millions, as reported by the World Bank Projects API."),
        ("Sources", "World Bank Documents & Reports API (search.worldbank.org/api/v3/wds) and "
                    "Projects API (search.worldbank.org/api/v2/projects)."),
    ]
    rows += [("Warning", w) for w in result.warnings]
    return pd.DataFrame(rows, columns=["Item", "Details"])


def to_excel_bytes(result, projects: pd.DataFrame | None = None) -> bytes:
    """Write the result to an .xlsx workbook. `projects` lets the app export a filtered view."""
    projects = result.projects if projects is None else projects
    sheets = [("Projects", projects), ("Document matches", result.doc_matches),
              ("Excluded", result.excluded)]
    if result.reference is not None and not result.reference.empty:
        sheets.append(("Reference list check", result.reference))
    sheets += [("Search log", result.log), ("About", _about(result))]

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        for name, df in sheets:
            df = df if df is not None else pd.DataFrame()
            df.to_excel(xw, sheet_name=name, index=False)
            _format_sheet(xw.sheets[name], df)
        about = xw.sheets["About"]
        about.column_dimensions["A"].width = 24
        about.column_dimensions["B"].width = 120
        for row in about.iter_rows(min_row=2):
            row[1].alignment = Alignment(wrap_text=True, vertical="top")
    return buf.getvalue()
