"""Command-line version: run the search and write the Excel file, no browser needed.

    python run_search.py                       # uses data/search_terms.xlsx and data/reference_pcodes.xlsx
    python run_search.py --terms my_terms.xlsx --refs none --min-fy 2015 --out results.xlsx
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

from wbsearch import SearchSettings, read_first_column, run_search, to_excel_bytes
from wbsearch.api import WBClient
from wbsearch.inputs import clean_pcodes, clean_terms

HERE = Path(__file__).parent


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--terms", default=str(HERE / "data" / "search_terms.xlsx"))
    ap.add_argument("--refs", default=str(HERE / "data" / "reference_pcodes.xlsx"),
                    help="xlsx with P-codes in column A, or 'none'")
    ap.add_argument("--min-fy", type=int, default=2012)
    ap.add_argument("--include-dropped", action="store_true")
    ap.add_argument("--plurals", action="store_true")
    ap.add_argument("--max-docs", type=int, default=2000)
    ap.add_argument("--refresh", action="store_true", help="ignore the local cache")
    ap.add_argument("--out", default=f"wb_project_search_{dt.date.today():%Y%m%d}.xlsx")
    a = ap.parse_args()

    terms = clean_terms(read_first_column(a.terms))
    refs = [] if a.refs.lower() == "none" or not Path(a.refs).exists() else clean_pcodes(read_first_column(a.refs))
    print(f"{len(terms)} search terms, {len(refs)} reference P-codes, approved from FY{a.min_fy}")

    last = [""]

    def progress(msg, frac):
        if msg != last[0]:
            print(f"[{frac:5.0%}] {msg}", flush=True)
            last[0] = msg

    res = run_search(SearchSettings(terms=terms, reference_pcodes=refs, min_fy=a.min_fy,
                                    include_dropped=a.include_dropped, match_plurals=a.plurals,
                                    max_docs_per_query=a.max_docs),
                     client=WBClient(cache_dir=HERE / "cache", refresh=a.refresh), progress=progress)
    Path(a.out).write_bytes(to_excel_bytes(res))
    for w in res.warnings:
        print("WARNING:", w)
    print(f"\n{len(res.projects)} projects -> {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
