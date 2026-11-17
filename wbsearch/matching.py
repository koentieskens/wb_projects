"""Keyword matching inside document text.

Rules (kept deliberately close to "exact phrase"):
* Whole words / whole phrases only: "NBS" does not match "NBSP", "reforestation"
  does not match inside "deforestation".
* Case-insensitive, EXCEPT terms written entirely in capitals (acronyms such as
  "NBS"), which are matched case-sensitively so "nbs" in running text is ignored.
* Spaces, hyphens and line breaks between the words of a phrase are treated as
  equivalent, because PDF-to-text conversion often breaks or re-hyphenates lines:
  "nature-based" matches "nature-based", "nature based" and "nature-\nbased".
* Optional: also match a plural "s"/"es" ending (off by default).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_SEP = r"(?:\s*[-‐‑‒–­]\s*|\s+)"  # hyphen variants or whitespace
_WORD_SPLIT = re.compile(r"[\s\-‐‑‒–]+")


@dataclass(frozen=True)
class TermPattern:
    term: str
    regex: re.Pattern
    case_sensitive: bool


def is_acronym(term: str) -> bool:
    letters = [c for c in term if c.isalpha()]
    return bool(letters) and all(c.isupper() for c in letters) and len(term.replace(" ", "")) <= 8


def compile_term(term: str, plurals: bool = False) -> TermPattern:
    term = " ".join(term.split())
    parts = [re.escape(p) for p in _WORD_SPLIT.split(term) if p]
    body = _SEP.join(parts)
    if plurals:
        body += r"(?:s|es)?"
    pattern = r"(?<![A-Za-z0-9])" + body + r"(?![A-Za-z0-9])"
    cs = is_acronym(term)
    flags = 0 if cs else re.IGNORECASE
    return TermPattern(term=term, regex=re.compile(pattern, flags), case_sensitive=cs)


def normalise_text(text: str) -> str:
    # remove soft hyphens and zero-width characters that break phrases
    return re.sub(r"[­​‌‍﻿]", "", text)


def find(text: str, tp: TermPattern, snippet_chars: int = 160) -> tuple[int, str]:
    """Return (number of hits, snippet around the first hit)."""
    matches = list(tp.regex.finditer(text))
    if not matches:
        return 0, ""
    m = matches[0]
    start = max(0, m.start() - snippet_chars)
    end = min(len(text), m.end() + snippet_chars)
    pre = " ".join(text[start:m.start()].split())
    hit = " ".join(text[m.start():m.end()].split())
    post = " ".join(text[m.end():end].split())
    snippet = ("…" if start > 0 else "") + pre + " [" + hit + "] " + post + ("…" if end < len(text) else "")
    return len(matches), snippet.strip()
