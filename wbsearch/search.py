"""The search pipeline.

1. For every search term, query the Documents & Reports API for PIDs and PADs
   (and PAD equivalents) that mention it.
2. Download each candidate document's text and check the term really is in it
   (whole-word / exact-phrase match). Documents without text keep the API match
   and are flagged "not verified".
3. Look up the projects behind the matching documents in the Projects API.
4. Apply the status rules:
     Pipeline       -> keyword in the PID (or in a PAD, if one is public)
     Active/Closed  -> keyword in the PAD or PAD equivalent;
                       keyword in the PID counts only if no PAD is disclosed yet
     + Active/Closed must be approved in or after the minimum fiscal year.
5. Optionally check a reference list of P-codes and explain each one.
"""

from __future__ import annotations

import datetime as dt
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable

import pandas as pd

from .api import (WBClient, _as_str, fetch_projects, project_ids_of, search_documents)
from .matching import compile_term, find, normalise_text

ProgressFn = Callable[[str, float], None]

# Queries sent to the API's partial document-type filter. Results are then
# classified precisely by classify_doctype(), so over-broad queries are harmless.
DOCTYPE_QUERIES = [
    "Information Document",     # Project / Program Information Document (PID)
    "Integrated Safeguards",    # older combined "Project Information and Integrated Safeguards Data Sheet"
    "Appraisal Document",       # PAD and Program(-for-Results) Appraisal Document
    "Program Document",         # Development Policy Financing
    "Project Paper",            # Additional financing / restructuring project papers
]

ROLE_PID = "PID"
ROLE_PAD = "PAD"


def classify_doctype(docty: str) -> tuple[str, str] | None:
    """Map a raw document type to (role, label). None = not a PID / PAD type."""
    t = " ".join(docty.lower().split())
    if not t:
        return None
    if "project information and integrated safeguards" in t:
        return ROLE_PID, "PID (combined PID/ISDS)"
    if "information document" in t:
        if "program" in t and "project" not in t:
            return ROLE_PID, "Program Information Document (PID equivalent)"
        return ROLE_PID, "PID"
    if "project appraisal document" in t:
        return ROLE_PAD, "PAD"
    if "appraisal document" in t and ("program" in t or "results" in t):
        return ROLE_PAD, "Program Appraisal Document – PforR (PAD equivalent)"
    if "program document" in t:
        return ROLE_PAD, "Program Document – DPF (PAD equivalent)"
    if "project paper" in t:
        return ROLE_PAD, "Project Paper – AF/restructuring (PAD equivalent)"
    return None


@dataclass
class SearchSettings:
    terms: list[str]
    reference_pcodes: list[str] = field(default_factory=list)
    min_fy: int = 2012
    include_dropped: bool = False
    match_plurals: bool = False
    max_docs_per_query: int = 2000
    workers: int = 4


@dataclass
class SearchResult:
    projects: pd.DataFrame
    doc_matches: pd.DataFrame
    excluded: pd.DataFrame
    reference: pd.DataFrame
    log: pd.DataFrame
    settings: SearchSettings
    run_at: str
    warnings: list[str]


# ---------------------------------------------------------------------------------
def fiscal_year(d: dt.date | None) -> int | None:
    """World Bank fiscal year: 1 July – 30 June (FY2012 = 1 Jul 2011 – 30 Jun 2012)."""
    if d is None:
        return None
    return d.year + 1 if d.month >= 7 else d.year


def parse_date(v) -> dt.date | None:
    s = _as_str(v)
    if not s:
        return None
    s = s.split(";")[0].strip()
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%m/%d/%Y %I:%M:%S %p",
                "%m/%d/%Y %H:%M:%S", "%m/%d/%Y", "%d-%b-%Y", "%B %d, %Y"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        try:
            return dt.date(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            return None
    return None


def _num(v) -> float | None:
    s = _as_str(v).replace(",", "").replace("$", "").strip()
    if not s:
        return None
    try:
        return float(s.split(";")[0])
    except ValueError:
        return None


def _musd(rec: dict, full_usd_keys: list[str], curr_keys: list[str]) -> float | None:
    """Commitment in US$ millions. Full-dollar fields are preferred; the curr_* fields
    are sometimes already in millions, so small values there are taken as millions."""
    for k in full_usd_keys:
        n = _num(rec.get(k))
        if n is not None:
            return round(n / 1e6, 3)
    for k in curr_keys:
        n = _num(rec.get(k))
        if n is not None:
            return round(n / 1e6, 3) if abs(n) >= 1e5 else round(n, 3)
    return None


def _sectors(rec: dict) -> str:
    names: list[str] = []
    for key in ("sector", "sectors"):
        v = rec.get(key)
        if isinstance(v, list):
            names += [_as_str(x.get("Name") if isinstance(x, dict) else x) for x in v]
    if not names:
        for i in range(1, 6):
            v = rec.get(f"sector{i}")
            if v:
                names.append(_as_str(v.get("Name") if isinstance(v, dict) else v))
    if not names and rec.get("mjsector_namecode"):
        names = [_as_str(x.get("name") if isinstance(x, dict) else x) for x in rec["mjsector_namecode"]]
    seen, out = set(), []
    for n in names:
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return "; ".join(out)


def project_info(pid: str, rec: dict | None) -> dict:
    rec = rec or {}
    appr = parse_date(rec.get("boardapprovaldate"))
    fy = fiscal_year(appr)
    if fy is None:
        n = _num(rec.get("approvalfy"))
        fy = int(n) if n else None
    status = _as_str(rec.get("status")) or _as_str(rec.get("projectstatusdisplay"))
    return {
        "Project ID": pid,
        "Project name": _as_str(rec.get("project_name")),
        "Country": _as_str(rec.get("countryshortname")) or _as_str(rec.get("countryname")),
        "Region": _as_str(rec.get("regionname")),
        "Status": status.title() if status else "",
        "Approval date": appr,
        "Approval FY": fy,
        "Closing date": parse_date(rec.get("closingdate")),
        "Total commitment (US$ m)": _musd(rec, ["totalcommamt", "totalamt"], ["curr_total_commitment"]),
        "IBRD commitment (US$ m)": _musd(rec, ["ibrdcommamt"], ["curr_ibrd_commitment"]),
        "IDA commitment (US$ m)": _musd(rec, ["idacommamt"], ["curr_ida_commitment"]),
        "Grant amount (US$ m)": _musd(rec, ["grantamt"], []),
        "Lending instrument": _as_str(rec.get("lendinginstr")),
        "Product line": _as_str(rec.get("prodlinetext") or rec.get("prodline")),
        "Sectors": _sectors(rec),
        "Borrower": _as_str(rec.get("borrower")),
        "Implementing agency": _as_str(rec.get("impagency")),
        "Project development objective": _as_str(rec.get("pdo")),
        "Project page": _as_str(rec.get("url")) or
            f"https://projects.worldbank.org/en/projects-operations/project-detail/{pid}",
    }


# ---------------------------------------------------------------------------------
def run_search(settings: SearchSettings, client: WBClient | None = None,
               progress: ProgressFn | None = None) -> SearchResult:
    client = client or WBClient()
    say = progress or (lambda msg, frac: None)
    warnings: list[str] = []
    terms = settings.terms
    patterns = {t: compile_term(t, settings.match_plurals) for t in terms}
    strdate = f"{settings.min_fy - 2}-07-01"  # one FY of slack: PADs often pre-date board approval

    # docs[doc_id] = record; api_terms[doc_id] = terms the API matched; via[doc_id] = how found
    docs: dict[str, dict] = {}
    api_terms: dict[str, set[str]] = defaultdict(set)
    via: dict[str, set[str]] = defaultdict(set)
    log_rows: list[dict] = []

    def doc_key(d: dict) -> str:
        return _as_str(d.get("id")) or _as_str(d.get("url")) or _as_str(d.get("pdfurl"))

    # ---- 1. keyword searches ---------------------------------------------------
    jobs = [(t, q) for t in terms for q in DOCTYPE_QUERIES]
    say(f"Searching documents for {len(terms)} terms…", 0.0)

    def run_job(job):
        term, dq = job
        try:
            found, total = search_documents(client, qterm=f'"{term}"', docty=dq, strdate=strdate,
                                            max_docs=settings.max_docs_per_query)
            return term, dq, found, total, None
        except Exception as e:  # keep going; report in the log
            return term, dq, [], 0, str(e)

    done = 0
    with ThreadPoolExecutor(max_workers=settings.workers) as ex:
        for term, dq, found, total, err in ex.map(run_job, jobs):
            done += 1
            kept = 0
            for d in found:
                cls = classify_doctype(_as_str(d.get("docty")))
                k = doc_key(d)
                if not cls or not k or not project_ids_of(d):
                    continue
                kept += 1
                docs.setdefault(k, d)
                api_terms[k].add(term)
                via[k].add("Keyword search")
            log_rows.append({"Search term": term, "Document-type query": dq, "API total": total,
                             "Retrieved": len(found), "PID/PAD-type with P-code": kept,
                             "Capped": "Yes" if total > settings.max_docs_per_query else "",
                             "Error": err or ""})
            if total > settings.max_docs_per_query:
                warnings.append(f'"{term}" / {dq}: API reported {total} documents, only the first '
                                f'{settings.max_docs_per_query} were checked.')
            if err:
                warnings.append(f'Search failed for "{term}" / {dq}: {err}')
            say(f"Searched {done}/{len(jobs)} term × document-type combinations", 0.30 * done / len(jobs))

    # ---- 1b. reference list: pull every PID/PAD of those projects ---------------
    all_docs_by_project: dict[str, list[dict]] = {}

    def project_docs(pid: str) -> list[dict]:
        if pid not in all_docs_by_project:
            try:
                found, _ = search_documents(client, proid=pid, max_docs=500)
            except Exception as e:
                warnings.append(f"Could not list documents for {pid}: {e}")
                found = []
            all_docs_by_project[pid] = [d for d in found if pid in project_ids_of(d)]
        return all_docs_by_project[pid]

    refs = list(dict.fromkeys(settings.reference_pcodes))
    if refs:
        say(f"Checking {len(refs)} reference projects…", 0.31)
        with ThreadPoolExecutor(max_workers=settings.workers) as ex:
            list(ex.map(project_docs, refs))
        for pid in refs:
            for d in all_docs_by_project.get(pid, []):
                if classify_doctype(_as_str(d.get("docty"))):
                    k = doc_key(d)
                    if k:
                        docs.setdefault(k, d)
                        via[k].add("Reference list")

    # ---- 2. verify in full text --------------------------------------------------
    keys = list(docs)
    say(f"Downloading and checking text of {len(keys)} documents…", 0.35)
    text_hits: dict[str, dict[str, tuple[int, str]] | None] = {}

    def check(k):
        d = docs[k]
        text = client.get_text(_as_str(d.get("txturl")), k)
        if text is None:
            return k, None
        text = normalise_text(text)
        res = {}
        for t, tp in patterns.items():
            n, snip = find(text, tp)
            if n:
                res[t] = (n, snip)
        return k, res

    done = 0
    with ThreadPoolExecutor(max_workers=settings.workers) as ex:
        for k, res in ex.map(check, keys):
            text_hits[k] = res
            done += 1
            if done % 10 == 0 or done == len(keys):
                say(f"Checked text of {done}/{len(keys)} documents", 0.35 + 0.45 * done / max(1, len(keys)))

    no_text = sum(1 for v in text_hits.values() if v is None)
    if no_text:
        warnings.append(f"{no_text} document(s) had no downloadable text; for those the World Bank "
                        f"search engine's match is used and flagged 'Text verified = No'.")

    # ---- document × term matches ---------------------------------------------------
    match_rows: list[dict] = []
    for k in keys:
        d = docs[k]
        role, label = classify_doctype(_as_str(d.get("docty")))
        res = text_hits.get(k)
        if res is None:
            hits = {t: (None, "") for t in api_terms.get(k, set())}
            verified = "No"
        else:
            hits = res
            verified = "Yes"
        for t, (n, snip) in hits.items():
            for pid in project_ids_of(d):
                match_rows.append({
                    "Project ID": pid, "Search term": t, "Hits": n, "Snippet": snip,
                    "Document role": role, "Document type": label,
                    "Document type (as published)": _as_str(d.get("docty")),
                    "Document date": parse_date(d.get("docdt")),
                    "Document title": _as_str(d.get("display_title")),
                    "Text verified": verified,
                    "Found via": " + ".join(sorted(via.get(k, {"Keyword search"}))),
                    "Document ID": k,
                    "PDF link": _as_str(d.get("pdfurl")) or _as_str(d.get("url")),
                    "Text link": _as_str(d.get("txturl")),
                })
    dm = pd.DataFrame(match_rows)

    # ---- 3. project data -----------------------------------------------------------
    candidate_pids = sorted(set(dm["Project ID"]) if not dm.empty else set())
    say(f"Fetching project data for {len(candidate_pids) + len(refs)} projects…", 0.82)
    meta = fetch_projects(client, set(candidate_pids) | set(refs))
    info = {pid: project_info(pid, meta.get(pid)) for pid in set(candidate_pids) | set(refs)}

    # Is a PAD (or equivalent) disclosed? Needed for Active/Closed projects whose
    # only matches are in a PID.
    def pad_disclosed(pid: str) -> bool:
        return any(classify_doctype(_as_str(d.get("docty"))) and
                   classify_doctype(_as_str(d.get("docty")))[0] == ROLE_PAD for d in project_docs(pid))

    need_check = []
    for pid in candidate_pids:
        st = info[pid]["Status"].lower()
        sub = dm[dm["Project ID"] == pid]
        if st in ("active", "closed") and not (sub["Document role"] == ROLE_PAD).any():
            need_check.append(pid)
    if need_check:
        say(f"Checking whether a PAD is disclosed for {len(need_check)} projects…", 0.88)
        with ThreadPoolExecutor(max_workers=settings.workers) as ex:
            list(ex.map(project_docs, need_check))

    # ---- 4. apply rules --------------------------------------------------------------
    included: list[dict] = []
    excluded: list[dict] = []
    decision: dict[str, tuple[bool, str]] = {}

    for pid in candidate_pids:
        p = info[pid]
        sub = dm[dm["Project ID"] == pid]
        st = p["Status"].lower()
        pad_m = sub[sub["Document role"] == ROLE_PAD]
        pid_m = sub[sub["Document role"] == ROLE_PID]
        basis_df = None
        reason = ""
        basis = ""

        if not meta.get(pid):
            reason = "Project not returned by the Projects API (status unknown)"
        elif st == "dropped" and not settings.include_dropped:
            reason = "Dropped project"
        elif st in ("pipeline", "dropped"):
            basis_df = pd.concat([pid_m, pad_m])
            basis = "PID (pipeline)" if pad_m.empty else ("PAD (pipeline)" if pid_m.empty else "PID + PAD (pipeline)")
        elif st in ("active", "closed"):
            fy = p["Approval FY"]
            if fy is None:
                reason = "Approval date unknown"
            elif fy < settings.min_fy:
                reason = f"Approved FY{fy}, before FY{settings.min_fy}"
            elif not pad_m.empty:
                basis_df = pad_m
                labels = sorted(set(pad_m["Document type"]))
                basis = " / ".join(labels)
            elif not pid_m.empty and not pad_disclosed(pid):
                basis_df = pid_m
                basis = "PID (PAD not yet disclosed)"
            else:
                reason = "Keyword only in PID; the PAD (or equivalent) is disclosed and does not contain it"
        else:
            reason = f"Unrecognised status '{p['Status']}'"

        all_terms = sorted(set(sub["Search term"]), key=str.lower)
        if basis_df is None or basis_df.empty:
            excluded.append({**{k: p[k] for k in ("Project ID", "Project name", "Country", "Region",
                                                   "Status", "Approval FY")},
                             "Reason excluded": reason or "No qualifying document match",
                             "Keywords found (any document)": "; ".join(all_terms),
                             "Documents with matches": "; ".join(sorted(set(sub["Document type"]))),
                             "Project page": p["Project page"]})
            decision[pid] = (False, reason)
            continue

        b = basis_df.copy()
        b["_h"] = b["Hits"].fillna(0)
        best = b.sort_values("_h", ascending=False).iloc[0]
        kw = sorted(set(b["Search term"]), key=str.lower)
        doc_titles = list(dict.fromkeys(b["Document title"]))
        other_kw = sorted(set(all_terms) - set(kw), key=str.lower)
        row = dict(p)
        if st not in ("active", "closed"):
            row["Approval FY"] = None  # only reported for approved (active/closed) projects
        row.update({
            "Matched keywords": "; ".join(kw),
            "No. of keywords matched": len(kw),
            "Total hits": int(b["_h"].sum()) if b["Hits"].notna().any() else None,
            "Match basis (document type)": basis,
            "Matched document(s)": " | ".join(doc_titles),
            "Document date": best["Document date"],
            "Document link": best["PDF link"],
            "Best snippet": best["Snippet"],
            "Text verified": "Yes" if (b["Text verified"] == "Yes").all() else
                             ("Partly" if (b["Text verified"] == "Yes").any() else "No"),
            "Other keywords (in other docs)": "; ".join(other_kw),
            "Found via": " + ".join(sorted(set(" + ".join(b["Found via"]).split(" + ")))),
            "In reference list": "Yes" if pid in refs else "",
        })
        included.append(row)
        decision[pid] = (True, basis)

    proj_cols = ["Project ID", "Project name", "Country", "Region", "Status", "Approval FY",
                 "Approval date", "Closing date", "Total commitment (US$ m)", "IBRD commitment (US$ m)",
                 "IDA commitment (US$ m)", "Grant amount (US$ m)", "Lending instrument", "Product line",
                 "Sectors", "Borrower", "Implementing agency", "Matched keywords", "No. of keywords matched",
                 "Total hits", "Match basis (document type)", "Matched document(s)", "Document date",
                 "Document link", "Best snippet", "Text verified", "Other keywords (in other docs)",
                 "Found via", "In reference list", "Project development objective", "Project page"]
    projects_df = pd.DataFrame(included, columns=proj_cols)
    for col in ("Approval FY", "Total hits", "No. of keywords matched"):
        projects_df[col] = pd.array(projects_df[col].tolist(), dtype="Int64") if len(projects_df) else projects_df[col]
    if not projects_df.empty:
        order = {"Pipeline": 0, "Active": 1, "Closed": 2}
        projects_df = projects_df.sort_values(
            by=["Status", "Approval FY", "Project ID"],
            key=lambda s: s.map(order).fillna(9) if s.name == "Status" else s,
            ascending=[True, False, True], na_position="first").reset_index(drop=True)
    excluded_df = pd.DataFrame(excluded, columns=["Project ID", "Project name", "Country", "Region", "Status",
                                                  "Approval FY", "Reason excluded", "Keywords found (any document)",
                                                  "Documents with matches", "Project page"])

    # ---- 5. reference list check -------------------------------------------------------
    ref_rows = []
    for pid in refs:
        p = info.get(pid) or project_info(pid, None)
        sub = dm[dm["Project ID"] == pid] if not dm.empty else dm
        pdocs = project_docs(pid)
        n_pidpad = sum(1 for d in pdocs if classify_doctype(_as_str(d.get("docty"))))
        if pid in decision and decision[pid][0]:
            res, note = "Yes", f"Included – match basis: {decision[pid][1]}"
        elif pid in decision:
            res, note = "No", decision[pid][1]
        elif not meta.get(pid):
            res, note = "No", "Project not returned by the Projects API"
        elif n_pidpad == 0:
            res, note = "No", "No PID or PAD (or equivalent) found in Documents & Reports"
        else:
            res, note = "No", f"{n_pidpad} PID/PAD-type document(s) disclosed; none contains a search term"
        by_search = (not sub.empty) and sub["Found via"].str.contains("Keyword search").any()
        ref_rows.append({"Project ID": pid, "In results": res, "Explanation": note,
                         "Found by keyword search": "Yes" if by_search else "No",
                         "Keywords found": "; ".join(sorted(set(sub["Search term"]), key=str.lower)) if not sub.empty else "",
                         "Project name": p["Project name"], "Status": p["Status"], "Country": p["Country"],
                         "Approval FY": p["Approval FY"], "PID/PAD-type documents disclosed": n_pidpad,
                         "Project page": p["Project page"]})
    reference_df = pd.DataFrame(ref_rows)
    for df_ in (excluded_df, reference_df):
        if len(df_) and "Approval FY" in df_:
            df_["Approval FY"] = pd.array(df_["Approval FY"].tolist(), dtype="Int64")
    if refs:
        missed = [r["Project ID"] for r in ref_rows if r["In results"] == "Yes" and r["Found by keyword search"] == "No"]
        if missed:
            warnings.append(f"{len(missed)} reference project(s) qualify but were NOT returned by the World Bank "
                            f"keyword search (found only via the reference check): {', '.join(missed)}.")

    if not dm.empty:
        dm = dm.sort_values(["Project ID", "Document role", "Search term"]).reset_index(drop=True)
    say("Done", 1.0)
    return SearchResult(projects=projects_df, doc_matches=dm, excluded=excluded_df, reference=reference_df,
                        log=pd.DataFrame(log_rows), settings=settings,
                        run_at=dt.datetime.now().strftime("%Y-%m-%d %H:%M"), warnings=warnings)
