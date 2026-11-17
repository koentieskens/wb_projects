# World Bank project keyword search (PIDs & PADs)

A small app that finds World Bank projects whose **PID** (pipeline projects) or **PAD / PAD equivalent**
(active and closed projects) mentions one of your search terms. It shows the projects with their
financing, status, country and region in a viewer, and lets you download everything as an Excel file.

Data comes live from two free, public World Bank APIs (no key needed):
the Documents & Reports API and the Projects API.

## Start it

You need Python 3.10 or newer (https://www.python.org/downloads/).

- **Windows:** double-click `start_windows.bat`
- **Mac / Linux:** run `./start_mac_linux.sh` in a terminal

The first start installs what's needed into a `.venv` folder next to the app (one to two minutes). Your browser
then opens the app at http://localhost:8501.

Manual alternative:

```
python -m venv .venv
.venv\Scripts\activate          (Windows)   |   source .venv/bin/activate   (Mac/Linux)
pip install -r requirements.txt
streamlit run app.py
```

No browser needed? `python run_search.py` runs the same search and writes the Excel file
(`python run_search.py --help` for options).

## Using it

1. **Search terms**: loaded from `data/search_terms.xlsx` (column A). Upload another file in the sidebar or edit
   the list directly.
2. **Reference list (optional)**: `data/reference_pcodes.xlsx` (the FY24 P-codes). Each one is reported in a
   "Reference list check" tab, saying whether it is in the results and, if not, why not. This is a good way to
   check that the search isn't missing projects you know about.
3. Set the filters and press **Run search**.

The first run is the slow one. For every term the app pulls the candidate PIDs/PADs and downloads each
document's text to check the term is really there. Expect anything from several minutes to an hour, depending
on how common the terms are. Everything is cached in the `cache` folder, so later runs (and runs with edited
terms) are much faster. Tick *Advanced → Ignore cached data* to re-download.

## The rules

| Project status | Included when a search term appears in… |
|---|---|
| Pipeline | the PID (or a PAD / PAD equivalent, if one is already public) |
| Active / Closed | the PAD or PAD equivalent. If **no PAD is disclosed yet**, the PID counts and the row says *PID (PAD not yet disclosed)* |
| Dropped | excluded (tick the option to include) |

- **PAD equivalents** are included and labelled as such in *Match basis (document type)*:
  Program Appraisal Document (Program-for-Results), Program Document (Development Policy Financing), and
  Project Paper (additional financing / restructuring).
- **Date range:** active/closed projects approved in or after **FY2012** (World Bank fiscal year: 1 July – 30 June,
  so from 1 July 2011). *Approval FY* is shown for active/closed projects. Pipeline projects have no approval date
  yet and are not date-filtered.
- **Matching** uses the exact words/phrases from the list:
  - whole words only: *reforestation* does not match *deforestation*
  - not case-sensitive, except all-capital terms such as **NBS**, which only match in capitals
  - spaces, hyphens and line breaks between words count as the same, so *nature-based* also matches
    *nature based*. This protects against PDF line breaks.
  - plural endings are off by default; there is an option to turn them on.
- **Text verified:** "Yes" means the app found the term in the document's full text itself. For the few documents
  without a text version, it falls back to the World Bank search engine's match and marks the row "No".
- Projects with a keyword hit that don't meet the rules are listed in the **Excluded** tab with the reason
  (e.g. approved before FY2012, or keyword only in the PID while the PAD is public and doesn't contain it).

## The Excel file

| Sheet | Contents |
|---|---|
| Projects | One row per qualifying project: project data, matched keywords, hit counts, match basis, document link and the best text snippet |
| Document matches | One row per document × search term, with snippet and links. Use it to check a match |
| Excluded | Projects with hits that did not qualify, and why |
| Reference list check | Your reference P-codes: in the results or not, and why |
| Search log | How many documents the API returned per term and document type |
| About | The settings and rules used for the run, plus any warnings |

Amounts are in US$ millions as reported by the Projects API.

## Good to know

- **Short acronyms** like *NBS* can have other meanings in some documents (e.g. "National Bureau of Statistics").
  Check the snippet column for those rows.
- Only English text is matched. Documents published only in French, Spanish, etc. won't match English terms.
- If the World Bank renames a document type, the app may stop recognising it. The *Search log* sheet shows what
  each query returned. Document types are mapped in `classify_doctype()` in `wbsearch/search.py`.
- `python tests/test_search.py` runs the built-in tests against a simulated API.
