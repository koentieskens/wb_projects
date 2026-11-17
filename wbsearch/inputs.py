"""Reading the search-term and reference P-code spreadsheets."""

from __future__ import annotations

import io
import re
from pathlib import Path
from typing import BinaryIO, Iterable

import openpyxl

PCODE_RE = re.compile(r"^P\d{6}$", re.IGNORECASE)


def read_first_column(source: str | Path | bytes | BinaryIO) -> list[str]:
    """Return the non-empty values of column A of the first sheet (trimmed, de-duplicated, order kept).

    A header row such as "Search term" / "P-code" is skipped automatically when it
    is clearly not data (handled by the callers via clean_terms / clean_pcodes).
    """
    if isinstance(source, (bytes, bytearray)):
        source = io.BytesIO(source)
    wb = openpyxl.load_workbook(source, read_only=True, data_only=True)
    ws = wb.worksheets[0]
    out: list[str] = []
    seen: set[str] = set()
    for row in ws.iter_rows(min_col=1, max_col=1, values_only=True):
        v = row[0]
        if v is None:
            continue
        s = " ".join(str(v).split())  # trim + collapse internal whitespace
        if not s:
            continue
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    wb.close()
    return out


def clean_terms(values: Iterable[str]) -> list[str]:
    out, seen = [], set()
    for v in values:
        s = " ".join(str(v).split())
        if not s or s.lower() in {"search term", "search terms", "keyword", "keywords", "term", "terms"}:
            continue
        if s.lower() in seen:
            continue
        seen.add(s.lower())
        out.append(s)
    return out


def clean_pcodes(values: Iterable[str]) -> list[str]:
    out, seen = [], set()
    for v in values:
        s = str(v).strip().upper()
        if PCODE_RE.match(s) and s not in seen:
            seen.add(s)
            out.append(s)
    return out
