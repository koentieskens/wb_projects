"""Thin clients for the two public World Bank APIs, with retries and an on-disk cache.

* Documents & Reports API: https://search.worldbank.org/api/v3/wds
* Projects API:            https://search.worldbank.org/api/v2/projects

No API key is needed. Responses are cached under ./cache so re-runs are fast and
polite to the World Bank servers; document texts are cached permanently.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Iterable

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

WDS_URL = "https://search.worldbank.org/api/v3/wds"
PROJECTS_URL = "https://search.worldbank.org/api/v2/projects"
USER_AGENT = "wb-nbs-project-search/1.0 (research tool; python-requests)"

WDS_FIELDS = "id,docty,docdt,projectid,display_title,pdfurl,txturl,url,count,repnb,lang"


class WBClient:
    """HTTP access to the APIs. Tests replace this with a fake that serves fixtures."""

    def __init__(self, cache_dir: str | Path = "cache", api_cache_days: float = 7.0,
                 refresh: bool = False, min_interval: float = 0.15, timeout: float = 90):
        self.cache_dir = Path(cache_dir)
        (self.cache_dir / "api").mkdir(parents=True, exist_ok=True)
        (self.cache_dir / "text").mkdir(parents=True, exist_ok=True)
        self.api_cache_seconds = api_cache_days * 86400
        self.refresh = refresh
        self.timeout = timeout
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._last = 0.0
        self.session = requests.Session()
        retry = Retry(total=5, backoff_factor=1.5, status_forcelist=(429, 500, 502, 503, 504),
                      allowed_methods=("GET",), respect_retry_after_header=True)
        adapter = HTTPAdapter(max_retries=retry, pool_maxsize=8)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.session.headers["User-Agent"] = USER_AGENT

    # -- low level -------------------------------------------------------
    def _throttle(self) -> None:
        with self._lock:
            wait = self.min_interval - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()

    def get_json(self, url: str, params: dict[str, Any]) -> dict:
        key = hashlib.sha1((url + "?" + json.dumps(params, sort_keys=True)).encode()).hexdigest()
        path = self.cache_dir / "api" / f"{key}.json"
        if not self.refresh and path.exists() and time.time() - path.stat().st_mtime < self.api_cache_seconds:
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                pass
        self._throttle()
        r = self.session.get(url, params=params, timeout=self.timeout)
        r.raise_for_status()
        data = r.json()
        path.write_text(json.dumps(data), encoding="utf-8")
        return data

    def get_text(self, url: str, doc_id: str) -> str | None:
        """Download a document's plain-text version. Returns None if unavailable."""
        if not url:
            return None
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", doc_id)[:120] or hashlib.sha1(url.encode()).hexdigest()
        path = self.cache_dir / "text" / f"{safe}.txt"
        miss = self.cache_dir / "text" / f"{safe}.missing"
        if path.exists() and not self.refresh:
            return path.read_text(encoding="utf-8", errors="replace")
        if miss.exists() and not self.refresh:
            return None
        url = re.sub(r"^http://", "https://", url.strip())
        try:
            self._throttle()
            r = self.session.get(url, timeout=self.timeout)
            if r.status_code != 200:
                miss.write_text(str(r.status_code))
                return None
            text = r.content.decode("utf-8", errors="replace")
        except requests.RequestException:
            return None  # network problem: don't cache, try again next run
        if len(text.strip()) < 300 or text.lstrip().lower().startswith(("<!doctype", "<html")):
            miss.write_text("empty-or-html")
            return None
        path.write_text(text, encoding="utf-8")
        return text


# -- response parsing helpers (defensive: the APIs are loosely typed) -----------

def _as_str(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, dict):
        for k in ("cdata", "Name", "name", "value", "#text"):
            if k in v:
                return _as_str(v[k])
        return "; ".join(_as_str(x) for x in v.values() if _as_str(x))
    if isinstance(v, (list, tuple)):
        return "; ".join(s for s in (_as_str(x) for x in v) if s)
    return str(v)


def _records(container: Any) -> list[dict]:
    """The APIs return records either as {id: record} or as [record, ...]."""
    if isinstance(container, dict):
        return [v for k, v in container.items() if isinstance(v, dict) and k != "facets"]
    if isinstance(container, list):
        return [v for v in container if isinstance(v, dict)]
    return []


def _total(data: dict) -> int:
    try:
        return int(str(data.get("total", 0)).replace(",", ""))
    except ValueError:
        return 0


PCODE_FIND = re.compile(r"P\d{6}", re.IGNORECASE)


def project_ids_of(doc: dict) -> list[str]:
    raw = _as_str(doc.get("projectid") or doc.get("proid") or "")
    return sorted({m.upper() for m in PCODE_FIND.findall(raw)})


# -- Documents & Reports API -------------------------------------------------------

def search_documents(client: WBClient, *, qterm: str | None = None, docty: str | None = None,
                     proid: str | None = None, strdate: str | None = None,
                     rows: int = 200, max_docs: int = 3000) -> tuple[list[dict], int]:
    """Page through the Documents & Reports API. Returns (documents, total reported by the API)."""
    params: dict[str, Any] = {"format": "json", "fl": WDS_FIELDS, "rows": rows}
    if qterm:
        params["qterm"] = qterm
    if docty:
        params["docty"] = docty
    if proid:
        params["proid"] = proid
    if strdate:
        params["strdate"] = strdate
    out: list[dict] = []
    os_ = 0
    total = 0
    while True:
        params["os"] = os_
        data = client.get_json(WDS_URL, dict(params))
        total = _total(data)
        recs = _records(data.get("documents", {}))
        out.extend(recs)
        os_ += rows
        if not recs or os_ >= total or len(out) >= max_docs:
            break
    return out[:max_docs], total


# -- Projects API -----------------------------------------------------------------

def fetch_projects(client: WBClient, ids: Iterable[str], batch: int = 40) -> dict[str, dict]:
    ids = sorted(set(ids))
    found: dict[str, dict] = {}
    for i in range(0, len(ids), batch):
        chunk = ids[i:i + batch]
        try:
            data = client.get_json(PROJECTS_URL, {"format": "json", "id": "^".join(chunk), "rows": len(chunk) + 5})
            for rec in _records(data.get("projects", {})):
                pid = _as_str(rec.get("id")).upper()
                if pid:
                    found[pid] = rec
        except requests.RequestException:
            pass
    # anything the batch call didn't return: ask one by one
    for pid in ids:
        if pid in found:
            continue
        try:
            data = client.get_json(PROJECTS_URL, {"format": "json", "id": pid, "rows": 5})
            for rec in _records(data.get("projects", {})):
                if _as_str(rec.get("id")).upper() == pid:
                    found[pid] = rec
        except requests.RequestException:
            pass
    return found
