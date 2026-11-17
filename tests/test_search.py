"""End-to-end test of the pipeline against a fake World Bank API.

Run:  python -m pytest -q tests   (or: python tests/test_search.py)
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import openpyxl  # noqa: E402

from wbsearch import SearchSettings, run_search, to_excel_bytes  # noqa: E402
from wbsearch.api import PROJECTS_URL, WDS_URL  # noqa: E402
from wbsearch.matching import compile_term, find  # noqa: E402

FILLER = "This project supports the government programme. " * 20

# doc id -> (project, docty, docdt, text or None, extra words the search engine indexes)
DOCS = {
    "D1": ("P100001", "Project Information Document", "2025-02-01T00:00:00Z",
           FILLER + "The project will scale up nature-based solutions in coastal cities." + FILLER, ""),
    "D2": ("P100002", "Project Appraisal Document", "2015-06-01T00:00:00Z",
           FILLER + "Reforestation of 10,000 ha. Further reforestation and reforestation monitoring." + FILLER, ""),
    "D3": ("P100003", "Project Appraisal Document", "2010-09-15T00:00:00Z",
           FILLER + "Afforestation of degraded slopes." + FILLER, ""),
    "D4a": ("P100004", "Project Information Document", "2019-03-01T00:00:00Z",
            FILLER + "Investments in green infrastructure for drainage." + FILLER, ""),
    "D4b": ("P100004", "Project Appraisal Document", "2019-11-01T00:00:00Z",
            FILLER + "Grey drainage works only." + FILLER, ""),
    "D5": ("P100005", "Project Information Document", "2023-10-01T00:00:00Z",
           FILLER + "Nature-based Solutions (NBS) will be piloted." + FILLER, ""),
    "D6": ("P100006", "Program Document", "2013-09-01T00:00:00Z",
           FILLER + "Prior action 3 supports ecosystem-\nbased adaptation planning." + FILLER, ""),
    "D7": ("P100007", "Project Appraisal Document", "2018-12-01T00:00:00Z",
           FILLER + "Measures against deforestation; nbsp formatting artefacts." + FILLER, "NBS reforestation"),
    "D8": ("P100008", "Project Information Document", "2025-05-01T00:00:00Z", None, "aquifer recharge"),
    "D9": ("P100009", "Project Information Document", "2022-01-01T00:00:00Z",
           FILLER + "Catchment rehabilitation works." + FILLER, ""),
    "D10": ("P100002", "Procurement Plan", "2016-01-01T00:00:00Z", FILLER + "reforestation" + FILLER, ""),
    # reference-only project: search engine does not index the term, but the text has it
    "D11": ("P200001", "Project Appraisal Document", "2022-12-01T00:00:00Z",
            FILLER + "Large-scale afforestation along the river." + FILLER, "__noindex__"),
}

PROJECTS = {
    "P100001": {"status": "Pipeline", "project_name": "Coastal Cities Resilience", "countryshortname": "Viet Nam",
                "regionname": "East Asia and Pacific", "totalcommamt": "150,000,000"},
    "P100002": {"status": "Active", "boardapprovaldate": "2015-09-10T00:00:00Z", "closingdate": "6/30/2025 12:00:00 AM",
                "project_name": "Forest Landscapes", "countryshortname": "Ethiopia", "regionname": "Eastern and Southern Africa",
                "totalcommamt": "80,000,000", "idacommamt": "80,000,000", "ibrdcommamt": "0",
                "lendinginstr": "Investment Project Financing", "sector": [{"Name": "Forestry"}],
                "pdo": {"cdata": "To restore forests."}},
    "P100003": {"status": "Closed", "boardapprovaldate": "2011-03-01T00:00:00Z", "project_name": "Old Forestry",
                "countryshortname": "Kenya"},
    "P100004": {"status": "Active", "boardapprovaldate": "2020-01-20T00:00:00Z", "project_name": "Urban Drainage",
                "countryshortname": "India", "curr_total_commitment": 200},
    "P100005": {"status": "Active", "boardapprovaldate": "2024-05-30T00:00:00Z", "project_name": "NBS Pilot",
                "countryshortname": "Peru"},
    "P100006": {"status": "Closed", "boardapprovaldate": "2013-12-12T00:00:00Z", "project_name": "Climate DPF",
                "countryshortname": "Morocco", "totalcommamt": "300,000,000"},
    "P100007": {"status": "Active", "boardapprovaldate": "2019-02-01T00:00:00Z", "project_name": "False positive",
                "countryshortname": "Brazil"},
    "P100008": {"status": "Pipeline", "project_name": "Groundwater", "countryshortname": "Jordan"},
    "P100009": {"status": "Dropped", "project_name": "Dropped one", "countryshortname": "Nepal"},
    "P200001": {"status": "Active", "boardapprovaldate": "2023-03-01T00:00:00Z", "project_name": "River Forests",
                "countryshortname": "Ghana"},
    "P200002": {"status": "Active", "boardapprovaldate": "2023-03-01T00:00:00Z", "project_name": "No docs",
                "countryshortname": "Chad"},
}

TERMS = ["nature-based solutions", "Nature-based", "reforestation", "Afforestation", "green infrastructure",
         "NBS", "Ecosystem-based adaptation", "aquifer recharge", "Catchment rehabilitation"]


class FakeClient:
    def __init__(self):
        self.calls = 0

    def _doc(self, k):
        pid, docty, docdt, text, _ = DOCS[k]
        return {"id": k, "projectid": pid, "docty": docty, "docdt": docdt, "display_title": f"{docty} {pid}",
                "pdfurl": f"https://documents.example/{k}.pdf", "txturl": f"https://documents.example/{k}.txt" if text else ""}

    def get_json(self, url, params):
        self.calls += 1
        if url == WDS_URL:
            hits = []
            for k, (pid, docty, docdt, text, extra) in DOCS.items():
                if params.get("proid") and params["proid"] != pid:
                    continue
                if params.get("docty") and params["docty"].lower() not in docty.lower():
                    continue
                if params.get("strdate") and docdt[:10] < params["strdate"]:
                    continue
                if params.get("qterm"):
                    q = params["qterm"].strip('"').lower()
                    if extra == "__noindex__":
                        continue
                    idx = ((text or "") + " " + extra).lower().replace("-\n", "-")
                    if q not in idx:
                        continue
                hits.append(k)
            os_, rows = params["os"], params["rows"]
            page = hits[os_:os_ + rows]
            return {"total": len(hits), "rows": rows, "os": os_,
                    "documents": {**{k: self._doc(k) for k in page}, "facets": {}}}
        if url == PROJECTS_URL:
            ids = params["id"].split("^")
            return {"total": len(ids), "projects": {i: {"id": i, **PROJECTS[i]} for i in ids if i in PROJECTS}}
        raise AssertionError(url)

    def get_text(self, url, doc_id):
        return DOCS[doc_id][3] if url else None


def test_matching_rules():
    assert find("we use NBS here", compile_term("NBS"))[0] == 1
    assert find("we use nbs here", compile_term("NBS"))[0] == 0          # acronym: case-sensitive
    assert find("deforestation", compile_term("reforestation"))[0] == 0
    assert find("Nature based solutions", compile_term("nature-based solutions"))[0] == 1
    assert find("nature-\nbased solutions", compile_term("nature-based solutions"))[0] == 1
    assert find("green spaces", compile_term("Green space"))[0] == 0
    assert find("green spaces", compile_term("Green space", plurals=True))[0] == 1


def run():
    return run_search(SearchSettings(terms=TERMS, reference_pcodes=["P100002", "P200001", "P200002"], min_fy=2012),
                      client=FakeClient())


def test_pipeline_rules():
    r = run()
    got = r.projects.set_index("Project ID")
    exc = r.excluded.set_index("Project ID")
    print(r.projects[["Project ID", "Status", "Approval FY", "Match basis (document type)", "Matched keywords",
                      "Total hits", "Text verified", "Found via"]].to_string())
    print(r.excluded[["Project ID", "Status", "Reason excluded"]].to_string())
    print(r.reference[["Project ID", "In results", "Explanation", "Found by keyword search"]].to_string())

    assert got.loc["P100001", "Match basis (document type)"] == "PID (pipeline)"
    assert "nature-based solutions" in got.loc["P100001", "Matched keywords"]
    assert "Nature-based" in got.loc["P100001", "Matched keywords"]
    assert got.loc["P100002", "Match basis (document type)"] == "PAD"
    assert got.loc["P100002", "Approval FY"] == 2016 and got.loc["P100002", "Total hits"] == 3
    assert got.loc["P100002", "Total commitment (US$ m)"] == 80
    assert "before FY2012" in exc.loc["P100003", "Reason excluded"]
    assert "PAD (or equivalent) is disclosed" in exc.loc["P100004", "Reason excluded"]
    assert got.loc["P100005", "Match basis (document type)"] == "PID (PAD not yet disclosed)"
    assert "NBS" in got.loc["P100005", "Matched keywords"]
    assert got.loc["P100006", "Match basis (document type)"].startswith("Program Document – DPF")
    assert got.loc["P100006", "Approval FY"] == 2014
    assert "P100007" not in got.index and "P100007" not in exc.index   # search-engine false positive removed
    assert got.loc["P100008", "Text verified"] == "No"
    assert exc.loc["P100009", "Reason excluded"] == "Dropped project"
    assert got.loc["P200001", "Found via"] == "Reference list"
    assert str(got.loc["P100001", "Approval FY"]) in ("<NA>", "nan", "None")
    ref = r.reference.set_index("Project ID")
    assert ref.loc["P200002", "In results"] == "No" and "No PID or PAD" in ref.loc["P200002", "Explanation"]
    assert ref.loc["P100002", "Found by keyword search"] == "Yes"
    assert any("NOT returned by the World Bank keyword search" in w for w in r.warnings)
    # Procurement plan never counted
    assert not (r.doc_matches["Document type (as published)"] == "Procurement Plan").any()

    xb = to_excel_bytes(r)
    wb = openpyxl.load_workbook(io.BytesIO(xb))
    assert wb.sheetnames == ["Projects", "Document matches", "Excluded", "Reference list check", "Search log", "About"]
    ws = wb["Projects"]
    hdr = [c.value for c in ws[1]]
    link_col = hdr.index("Document link") + 1
    assert ws.cell(row=2, column=link_col).hyperlink is not None
    Path("/tmp/sample_output.xlsx").write_bytes(xb) if Path("/tmp").exists() else None
    return r


if __name__ == "__main__":
    test_matching_rules()
    test_pipeline_rules()
    print("ALL TESTS PASSED")
