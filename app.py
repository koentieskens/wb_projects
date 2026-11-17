"""World Bank NBS project finder — Streamlit app.

Run with:   streamlit run app.py
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st

from wbsearch import SearchSettings, read_first_column, run_search, to_excel_bytes
from wbsearch.api import WBClient
from wbsearch.inputs import clean_pcodes, clean_terms

HERE = Path(__file__).parent
DEFAULT_TERMS = HERE / "data" / "search_terms.xlsx"
DEFAULT_REFS = HERE / "data" / "reference_pcodes.xlsx"
CACHE = HERE / "cache"

st.set_page_config(page_title="World Bank project keyword search", page_icon="🌿", layout="wide")


def _lines(text: str) -> list[str]:
    return [l.strip() for l in text.splitlines() if l.strip()]


# ---------------------------------------------------------------- sidebar --------
with st.sidebar:
    st.header("Search setup")

    up_terms = st.file_uploader("Search terms (.xlsx, terms in column A)", type=["xlsx"], key="up_terms")
    if up_terms is not None:
        terms0 = clean_terms(read_first_column(up_terms.getvalue()))
        tkey = f"terms_{up_terms.name}_{up_terms.size}"
    else:
        terms0 = clean_terms(read_first_column(DEFAULT_TERMS)) if DEFAULT_TERMS.exists() else []
        tkey = "terms_default"
    terms_text = st.text_area(f"Terms (one per line) — {len(terms0)} loaded", "\n".join(terms0),
                              height=260, key=tkey)

    use_refs = st.checkbox("Check a reference list of P-codes", value=DEFAULT_REFS.exists(),
                           help="Each listed project is reported in a 'Reference list check' tab: "
                                "is it in the results, and if not, why not.")
    refs_text = ""
    if use_refs:
        up_refs = st.file_uploader("Reference P-codes (.xlsx, column A)", type=["xlsx"], key="up_refs")
        if up_refs is not None:
            refs0 = clean_pcodes(read_first_column(up_refs.getvalue()))
            rkey = f"refs_{up_refs.name}_{up_refs.size}"
        else:
            refs0 = clean_pcodes(read_first_column(DEFAULT_REFS)) if DEFAULT_REFS.exists() else []
            rkey = "refs_default"
        refs_text = st.text_area(f"P-codes — {len(refs0)} loaded", "\n".join(refs0), height=120, key=rkey)

    st.subheader("Filters")
    min_fy = st.number_input("Active/Closed: approved in or after FY", min_value=1950, max_value=2100,
                             value=2012, step=1,
                             help="World Bank fiscal year runs 1 July – 30 June; FY2012 starts 1 July 2011.")
    include_dropped = st.checkbox("Include dropped projects", value=False)
    plurals = st.checkbox("Also match plural endings (-s / -es)", value=False,
                          help="Off = exact phrases only, e.g. 'green space' does not match 'green spaces'.")
    with st.expander("Advanced"):
        max_docs = st.number_input("Max documents per term & document type", 100, 20000, 2000, step=100)
        refresh = st.checkbox("Ignore cached data (re-download everything)", value=False)

    run = st.button("Run search", type="primary", use_container_width=True)

# ---------------------------------------------------------------- header ---------
st.title("World Bank projects — keyword search in PIDs and PADs")
st.caption("Pipeline projects are matched on their PID; Active and Closed projects on their PAD or PAD "
           "equivalent (a PID counts only when no PAD has been disclosed yet). Data: World Bank Documents "
           "& Reports API and Projects API.")

# ---------------------------------------------------------------- run ------------
if run:
    terms = clean_terms(_lines(terms_text))
    refs = clean_pcodes(_lines(refs_text)) if use_refs else []
    if not terms:
        st.error("Add at least one search term.")
    else:
        settings = SearchSettings(terms=terms, reference_pcodes=refs, min_fy=int(min_fy),
                                  include_dropped=include_dropped, match_plurals=plurals,
                                  max_docs_per_query=int(max_docs))
        bar = st.progress(0.0, text="Starting…")

        def progress(msg: str, frac: float) -> None:
            bar.progress(min(1.0, max(0.0, frac)), text=msg)

        try:
            client = WBClient(cache_dir=CACHE, refresh=refresh)
            st.session_state["result"] = run_search(settings, client=client, progress=progress)
            bar.empty()
        except Exception as e:  # pragma: no cover - shown to the user
            bar.empty()
            st.error(f"The search failed: {e}. Check your internet connection and try again "
                     f"(results downloaded so far are cached, so a re-run resumes quickly).")

res = st.session_state.get("result")
if res is None:
    st.info("Set up the search in the sidebar and press **Run search**. The first run downloads every "
            "candidate document's text, which can take a while; later runs reuse the local cache.")
    st.stop()

# ---------------------------------------------------------------- results --------
for w in res.warnings:
    st.warning(w)

p = res.projects
c = st.columns(5)
c[0].metric("Projects found", len(p))
for i, s in enumerate(["Pipeline", "Active", "Closed"], start=1):
    c[i].metric(s, int((p["Status"] == s).sum()) if not p.empty else 0)
c[4].metric("Total commitment (US$ m)",
            f"{p['Total commitment (US$ m)'].sum():,.0f}" if not p.empty else "0")

tab_names = ["Projects", "Document matches", "Excluded"]
if not res.reference.empty:
    tab_names.append("Reference list check")
tab_names.append("Search log")
tabs = dict(zip(tab_names, st.tabs(tab_names)))

LINKS = {
    "Document link": st.column_config.LinkColumn("Document link", display_text="Open PDF"),
    "Project page": st.column_config.LinkColumn("Project page", display_text="Open"),
    "PDF link": st.column_config.LinkColumn("PDF link", display_text="Open PDF"),
    "Text link": st.column_config.LinkColumn("Text link", display_text="Text"),
    "Approval FY": st.column_config.NumberColumn("Approval FY", format="%d"),
    "Total commitment (US$ m)": st.column_config.NumberColumn(format="%.2f"),
    "IBRD commitment (US$ m)": st.column_config.NumberColumn(format="%.2f"),
    "IDA commitment (US$ m)": st.column_config.NumberColumn(format="%.2f"),
    "Grant amount (US$ m)": st.column_config.NumberColumn(format="%.2f"),
}


def _opts(df: pd.DataFrame, col: str, split: bool = False) -> list[str]:
    if df.empty:
        return []
    vals = df[col].dropna().astype(str)
    if split:
        vals = vals.str.split("; ").explode()
    return sorted(v for v in set(vals) if v)


with tabs["Projects"]:
    f = st.columns([1, 1, 1, 1.4, 1.2])
    f_status = f[0].multiselect("Status", _opts(p, "Status"))
    f_region = f[1].multiselect("Region", _opts(p, "Region"))
    f_country = f[2].multiselect("Country", _opts(p, "Country", split=True))
    f_kw = f[3].multiselect("Matched keyword", _opts(p, "Matched keywords", split=True))
    f_basis = f[4].multiselect("Match basis", _opts(p, "Match basis (document type)"))
    q = st.text_input("Search in name, objective, snippet…", "")

    view = p.copy()
    if f_status:
        view = view[view["Status"].isin(f_status)]
    if f_region:
        view = view[view["Region"].isin(f_region)]
    if f_country:
        view = view[view["Country"].fillna("").apply(lambda s: any(c in s.split("; ") for c in f_country))]
    if f_kw:
        view = view[view["Matched keywords"].fillna("").apply(lambda s: any(k in s.split("; ") for k in f_kw))]
    if f_basis:
        view = view[view["Match basis (document type)"].isin(f_basis)]
    if q.strip():
        hay = view[["Project name", "Project development objective", "Best snippet", "Project ID",
                    "Borrower", "Implementing agency"]].fillna("").agg(" ".join, axis=1)
        view = view[hay.str.contains(q.strip(), case=False, regex=False)]

    st.caption(f"Showing {len(view)} of {len(p)} projects. Click a column header to sort.")
    st.dataframe(view, use_container_width=True, hide_index=True, column_config=LINKS, height=560)

    d1, d2 = st.columns(2)
    stamp = res.run_at.replace(":", "").replace(" ", "_")
    d1.download_button("⬇ Download all results (Excel)", to_excel_bytes(res),
                       file_name=f"wb_project_search_{stamp}.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                       use_container_width=True)
    d2.download_button("⬇ Download filtered view (Excel)", to_excel_bytes(res, projects=view),
                       file_name=f"wb_project_search_{stamp}_filtered.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                       use_container_width=True, disabled=len(view) == len(p))

with tabs["Document matches"]:
    st.caption("One row per document and search term, including documents that did not qualify a project.")
    dm = res.doc_matches
    pick = st.selectbox("Show matches for project", ["All"] + (sorted(dm["Project ID"].unique()) if not dm.empty else []))
    st.dataframe(dm if pick == "All" else dm[dm["Project ID"] == pick], use_container_width=True,
                 hide_index=True, column_config=LINKS, height=560)

with tabs["Excluded"]:
    st.caption("Projects with a keyword match that did not meet the rules, and why.")
    st.dataframe(res.excluded, use_container_width=True, hide_index=True, column_config=LINKS, height=560)

if "Reference list check" in tabs:
    with tabs["Reference list check"]:
        r = res.reference
        st.caption(f"{int((r['In results'] == 'Yes').sum())} of {len(r)} reference projects are in the results; "
                   f"{int((r['Found by keyword search'] == 'Yes').sum())} were returned by the keyword search itself.")
        st.dataframe(r, use_container_width=True, hide_index=True, column_config=LINKS, height=560)

with tabs["Search log"]:
    st.caption("API results per search term and document-type query.")
    st.dataframe(res.log, use_container_width=True, hide_index=True, height=560)
