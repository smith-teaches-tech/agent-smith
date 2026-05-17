"""
SEC EDGAR 8-K filing fetcher.

Fetches recent material-event filings (8-Ks) for given tickers.
Free, no API key, but requires a User-Agent header per SEC policy.
"""
import json
import time
from datetime import datetime, timezone, timedelta
from typing import Optional
import urllib.request
import urllib.error

# SEC requires a User-Agent identifying the requester.
# Format: "Sample Company Name AdminContact@samplecompany.com"
USER_AGENT = "Smith Labs agent-smith research@smith-labs.dev"

# Cache for ticker -> CIK lookup (CIK = SEC's internal company ID)
_CIK_CACHE: dict[str, str] = {}


def _fetch_json(url: str) -> Optional[dict]:
    """Fetch a JSON URL with required SEC headers. Returns None on failure."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print(f"[edgar] HTTP {e.code} for {url}")
        return None
    except Exception as e:
        print(f"[edgar] error fetching {url}: {e}")
        return None


def _load_ticker_to_cik() -> dict[str, str]:
    """Load the SEC's ticker->CIK mapping. Cached after first call."""
    global _CIK_CACHE
    if _CIK_CACHE:
        return _CIK_CACHE

    data = _fetch_json("https://www.sec.gov/files/company_tickers.json")
    if not data:
        return {}

    # Format: {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}, ...}
    for entry in data.values():
        ticker = entry["ticker"].upper()
        cik = str(entry["cik_str"]).zfill(10)  # CIK must be 10 digits, zero-padded
        _CIK_CACHE[ticker] = cik

    print(f"[edgar] loaded {len(_CIK_CACHE)} ticker->CIK mappings")
    return _CIK_CACHE


def get_recent_filings(ticker: str, days: int = 7, form_types: tuple = ("8-K",)) -> list[dict]:
    """
    Fetch recent filings for a ticker.

    Args:
        ticker: stock ticker (e.g., "DOCN")
        days: how many days back to look
        form_types: which form types to include (default: 8-K only)

    Returns:
        list of dicts with keys: date, form, accession_number, primary_document, url
        Empty list if ticker unknown or no recent filings.
    """
    ticker = ticker.upper()
    cik_map = _load_ticker_to_cik()
    cik = cik_map.get(ticker)
    if not cik:
        return []

    # SEC submissions endpoint
    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    time.sleep(0.15)  # be polite to SEC
    data = _fetch_json(url)
    if not data:
        return []

    recent = data.get("filings", {}).get("recent", {})
    if not recent:
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out = []
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accessions = recent.get("accessionNumber", [])
    primary_docs = recent.get("primaryDocument", [])
    # 8-K item codes (e.g. "2.02,9.01"). Parallel array with the others.
    # Empty string for non-8-K filings. Codes map to material-event types:
    # 1.01=material agreement, 2.02=earnings, 2.05=impairment,
    # 4.02=restatement (non-reliance), 5.02=officer departure, 8.01=other.
    items_arr = recent.get("items", [])

    for i in range(len(forms)):
        if forms[i] not in form_types:
            continue
        try:
            filing_date = datetime.fromisoformat(dates[i]).replace(tzinfo=timezone.utc)
        except (ValueError, IndexError):
            continue
        if filing_date < cutoff:
            continue

        accession_clean = accessions[i].replace("-", "")
        filing_url = (
            f"https://www.sec.gov/Archives/edgar/data/"
            f"{int(cik)}/{accession_clean}/{primary_docs[i]}"
        )
        # Parse items: SEC stores them as a comma-separated string. Split,
        # strip, drop empties. For non-8-K forms the field is "" → [].
        raw_items = items_arr[i] if i < len(items_arr) else ""
        item_codes = [s.strip() for s in (raw_items or "").split(",") if s.strip()]
        out.append({
            "ticker": ticker,
            "date": dates[i],
            "form": forms[i],
            "items": item_codes,
            "accession_number": accessions[i],
            "primary_document": primary_docs[i],
            "url": filing_url,
        })

    return out

# ============================================================
# 10-K / 10-Q Risk Factors fetcher (Screen 1 — AI sympathy fade)
# ============================================================
#
# Public surface added by this extension:
#     get_latest_10k_risk_factors(ticker) -> dict | None
#     get_latest_10q_risk_factors(ticker) -> dict | None
#     get_filings_for_ai_threat_assessment(ticker) -> dict
#
# All three are additive — existing get_recent_filings(),
# _load_ticker_to_cik(), and the 8-K standalone test are untouched.
#
# Design notes:
# - Caches extracted Risk Factors to .cache/edgar/{ticker}_{accession}.json.
#   Cache invalidation is automatic: when a new 10-K is filed, accession
#   changes, cache miss, fresh fetch. No TTL needed.
# - Cap each section at 40K chars (~10K tokens). Most issuers fit;
#   outliers get truncated with truncated:True so Screen 1's prompt
#   knows to qualify its reasoning.
# - Per-ticker failures don't raise. Returns None on any failure path.
# - Polite to SEC: 0.3s sleep between document fetches (heavier than
#   JSON metadata calls, which already use 0.15s).
#
# .gitignore: add `.cache/` (the cache lives in `.cache/edgar/` at repo root).

import re
from pathlib import Path
from html import unescape

# ------------------------------------------------------------
# Cache infrastructure
# ------------------------------------------------------------

# Top-level .cache/ directory (gitignored — caller must add to .gitignore).
_CACHE_DIR = Path(".cache/edgar")

# Cap per section. ~10K tokens at 4 chars/token. Beyond this is rare.
_RISK_FACTORS_MAX_CHARS = 40_000


def _cache_path(ticker: str, accession: str) -> Path:
    """Cache file path for a (ticker, accession) pair."""
    safe_acc = accession.replace("-", "")
    return _CACHE_DIR / f"{ticker.upper()}_{safe_acc}.json"


def _cache_read(ticker: str, accession: str) -> dict | None:
    """Read cached extraction if present. Returns None on miss or read error."""
    path = _cache_path(ticker, accession)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"[edgar] cache read failed for {path}: {e}; will refetch")
        return None


def _cache_write(ticker: str, accession: str, payload: dict) -> None:
    """Write cache entry. Best-effort — failure to cache is not fatal."""
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _cache_path(ticker, accession).write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as e:
        print(f"[edgar] cache write failed: {e}; continuing without cache")


# ------------------------------------------------------------
# HTML fetching + text extraction
# ------------------------------------------------------------

def _fetch_text(url: str) -> str | None:
    """
    Fetch an HTML/text URL with required SEC headers. Returns decoded
    string on success, None on failure. Heavier endpoint than _fetch_json
    so we sleep slightly longer.
    """
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,*/*",
        },
    )
    time.sleep(0.3)  # be polite — document fetches are heavier than JSON
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
            for enc in ("utf-8", "latin-1"):
                try:
                    return raw.decode(enc)
                except UnicodeDecodeError:
                    continue
            return raw.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        print(f"[edgar] HTTP {e.code} for {url}")
        return None
    except Exception as e:
        print(f"[edgar] fetch error for {url}: {e}")
        return None


# Strip HTML tags + collapse whitespace. Preserves block-level structure
# by emitting a newline at <p>, <div>, <br>, <li>, <tr>, and heading tags
# *before* tag removal — otherwise text comes out as one unreadable line.
_BLOCK_TAG_RE = re.compile(
    r"</?(?:p|div|br|li|tr|h[1-6])[^>]*>",
    flags=re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]+")
_MULTI_NL_RE = re.compile(r"\n{3,}")


def _html_to_text(html: str) -> str:
    """Best-effort plaintext from filing HTML."""
    s = _BLOCK_TAG_RE.sub("\n", html)
    s = _TAG_RE.sub("", s)
    s = unescape(s)
    s = _WS_RE.sub(" ", s)
    s = _MULTI_NL_RE.sub("\n\n", s)
    return s.strip()


# Risk Factors section starts at "Item 1A" and ends at the next "Item N"
# heading. Filings are inconsistent in formatting — capitalization,
# punctuation, whitespace, "Item 1A" vs "ITEM 1A." vs "Item&nbsp;1A".
# The regex below is forgiving on all of those.
#
# 10-Qs: "Part II, Item 1A" — same regex still matches because we don't
# anchor on "Part" — we just find the Item 1A header.
_RISK_FACTORS_START_RE = re.compile(
    r"(?:^|\n)\s*item\s*1\s*a\.?\s*[:\-\u2013\u2014]?\s*risk\s*factors?\b",
    flags=re.IGNORECASE,
)
_RISK_FACTORS_END_RE = re.compile(
    r"(?:^|\n)\s*item\s*(?:1\s*b|2|3)\b",
    flags=re.IGNORECASE,
)


def _extract_risk_factors(text: str) -> tuple[str, bool]:
    """
    Extract the Risk Factors section from filing plaintext.

    Returns (extracted_text, was_truncated). On extraction failure
    (no Item 1A header found) returns ("", False).

    Filings often have a TOC reference to "Item 1A. Risk Factors" near
    the top, then the *real* section later. We grab all matches and
    pick the one with the most content following — TOC matches are
    short, real sections are multi-thousand chars.
    """
    starts = list(_RISK_FACTORS_START_RE.finditer(text))
    if not starts:
        return "", False

    best_section = ""
    truncated = False
    for start_match in starts:
        section_start = start_match.end()
        end_match = _RISK_FACTORS_END_RE.search(text, pos=section_start)
        section_end = end_match.start() if end_match else len(text)
        candidate = text[section_start:section_end].strip()
        if len(candidate) > len(best_section):
            best_section = candidate

    if not best_section:
        return "", False

    if len(best_section) > _RISK_FACTORS_MAX_CHARS:
        best_section = best_section[:_RISK_FACTORS_MAX_CHARS]
        truncated = True

    return best_section, truncated


# ------------------------------------------------------------
# Per-form fetchers
# ------------------------------------------------------------

def _get_latest_filing_meta(ticker: str, form_type: str) -> dict | None:
    """
    Find the most recent filing of `form_type` for `ticker`. Returns the
    same dict shape as get_recent_filings entries, or None.

    Unlike get_recent_filings, no `days` cutoff — 10-Ks are filed
    annually, and we want the latest regardless of age.
    """
    ticker = ticker.upper()
    cik_map = _load_ticker_to_cik()
    cik = cik_map.get(ticker)
    if not cik:
        return None

    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    time.sleep(0.15)
    data = _fetch_json(url)
    if not data:
        return None

    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accessions = recent.get("accessionNumber", [])
    primary_docs = recent.get("primaryDocument", [])

    # Iterate newest-first (SEC returns recent.* in newest-first order).
    for i in range(len(forms)):
        if forms[i] != form_type:
            continue
        accession_clean = accessions[i].replace("-", "")
        filing_url = (
            f"https://www.sec.gov/Archives/edgar/data/"
            f"{int(cik)}/{accession_clean}/{primary_docs[i]}"
        )
        return {
            "ticker": ticker,
            "form": form_type,
            "filing_date": dates[i],
            "accession_number": accessions[i],
            "primary_document": primary_docs[i],
            "url": filing_url,
        }

    return None


def _fetch_and_extract_risk_factors(meta: dict) -> dict | None:
    """
    Fetch the primary document for a filing and extract Risk Factors.
    Cache-aware: returns cached payload on hit, fetches+caches on miss.

    Returns a dict with: ticker, form, filing_date, accession,
    risk_factors, truncated, char_count, source_url. Or None on
    fetch/extraction failure.
    """
    ticker = meta["ticker"]
    accession = meta["accession_number"]

    cached = _cache_read(ticker, accession)
    if cached is not None:
        return cached

    html = _fetch_text(meta["url"])
    if not html:
        return None

    text = _html_to_text(html)
    risk_factors, truncated = _extract_risk_factors(text)

    if not risk_factors:
        # Extraction failed. We could fall back to "first 30K chars of
        # the whole filing" but that's mostly noise (cover page, TOC,
        # boilerplate). Better to return None and let Screen 1's prompt
        # explicitly note "could not extract Risk Factors for $TICKER."
        print(f"[edgar] {ticker} {meta['form']}: Risk Factors extraction failed")
        return None

    payload = {
        "ticker": ticker,
        "form": meta["form"],
        "filing_date": meta["filing_date"],
        "accession": accession,
        "risk_factors": risk_factors,
        "truncated": truncated,
        "char_count": len(risk_factors),
        "source_url": meta["url"],
    }
    _cache_write(ticker, accession, payload)
    return payload


# ------------------------------------------------------------
# Public API
# ------------------------------------------------------------

def get_latest_10k_risk_factors(ticker: str) -> dict | None:
    """
    Fetch and extract the Risk Factors section from `ticker`'s most
    recent 10-K. Returns None if the filing is unavailable or the
    section can't be extracted.
    """
    meta = _get_latest_filing_meta(ticker, "10-K")
    if not meta:
        return None
    return _fetch_and_extract_risk_factors(meta)


def get_latest_10q_risk_factors(ticker: str) -> dict | None:
    """
    Fetch and extract the Risk Factors section from `ticker`'s most
    recent 10-Q. Returns None if the filing is unavailable or the
    section can't be extracted.

    Note: 10-Q Risk Factors are often "no material changes from the
    10-K" — that's itself signal. Caller should pass both 10-K and 10-Q
    risk_factors to Claude even when 10-Q is short.
    """
    meta = _get_latest_filing_meta(ticker, "10-Q")
    if not meta:
        return None
    return _fetch_and_extract_risk_factors(meta)


def get_filings_for_ai_threat_assessment(ticker: str) -> dict:
    """
    One-call helper for Screen 1's per-name pass. Returns a dict with:
        {
          "ticker": str,
          "k10": dict | None,   # latest 10-K Risk Factors
          "q10": dict | None,   # latest 10-Q Risk Factors
          "errors": [str, ...]  # human-readable notes if either fetch missed
        }

    Always returns a dict — Screen 1 can render even when both fetches
    fail (the prompt would say "no filing data available, downgrading
    confidence").
    """
    ticker = ticker.upper()
    out: dict = {"ticker": ticker, "k10": None, "q10": None, "errors": []}

    out["k10"] = get_latest_10k_risk_factors(ticker)
    if out["k10"] is None:
        out["errors"].append("10-K Risk Factors unavailable")

    out["q10"] = get_latest_10q_risk_factors(ticker)
    if out["q10"] is None:
        out["errors"].append("10-Q Risk Factors unavailable")

    return out


# ============================================================
# Screen 2 — Pre-earnings filings read
# ============================================================
#
# Public surface added by this extension:
#     get_8k_earnings_exhibit_text(ticker, quarters=4) -> list[dict]
#     get_latest_10k_business_section(ticker)          -> dict | None
#     get_filings_for_pre_earnings_read(ticker)        -> dict
#
# All three are additive — the 8-K fetcher, the Screen 1 Risk Factors
# fetchers, and the standalone test are untouched.
#
# What Screen 2 needs that Screen 1 did not:
# - The TEXT of an 8-K's earnings press release exhibit (Exhibit 99.1),
#   not just the 8-K's primary cover document. The press release is a
#   SEPARATE file inside the same accession folder; finding it requires
#   reading the accession's index.json.
# - The 10-K BUSINESS section (Item 1) — "what the company actually
#   does" — which Screen 1 never needed (it only read Risk Factors).
#
# Design notes:
# - Reuses the Screen 1 cache infrastructure (_cache_read/_cache_write,
#   _CACHE_DIR) and the HTML→text machinery (_html_to_text, _fetch_text).
# - Cache keys for 8-K exhibits use accession (immutable once filed) so
#   invalidation is automatic — a new earnings 8-K is a new accession,
#   cache miss, fresh fetch. Same model as the Risk Factors cache.
# - Per-ticker / per-filing failures never raise. Missing data returns
#   None or an empty list; Screen 2's prompt is told to qualify its
#   confidence when filing data is thin.
# - Polite to SEC: index.json is a metadata call (0.15s, like other
#   JSON); exhibit document fetches go through _fetch_text (0.3s).

# Earnings press releases are long but not 10-K-long. 60K chars
# (~15K tokens) comfortably holds prepared remarks + financial tables
# + guidance for all but the most verbose issuers.
_EARNINGS_EXHIBIT_MAX_CHARS = 60_000

# 10-K Business section: capped like Risk Factors. Item 1 is usually
# the longest narrative section of a 10-K; 40K chars (~10K tokens)
# truncates the verbose outliers.
_BUSINESS_SECTION_MAX_CHARS = 40_000


def _fetch_accession_index(cik: str, accession_clean: str) -> Optional[dict]:
    """
    Fetch the index.json for one filing accession. The index lists every
    document in the accession folder with its `type` (EX-99.1, 10-K, ...)
    and `name` (the actual filename). Returns the parsed dict or None.
    """
    url = (
        f"https://www.sec.gov/Archives/edgar/data/"
        f"{int(cik)}/{accession_clean}/index.json"
    )
    time.sleep(0.15)
    return _fetch_json(url)


def _find_earnings_exhibit_doc(index_data: dict) -> Optional[str]:
    """
    Given an accession index.json, return the filename of the earnings
    press release exhibit, or None.

    Preference order:
      1. document `type` is exactly "EX-99.1"  (the canonical earnings PR)
      2. document `type` starts with "EX-99"   (EX-99.2 etc. — fallback)
    SEC's `type` tagging is mostly reliable but not universal; when no
    EX-99* type is present the caller falls back to the 8-K primary doc.
    """
    items = (index_data.get("directory", {}) or {}).get("item", [])
    if not items:
        return None

    ex_99_1: Optional[str] = None
    ex_99_any: Optional[str] = None
    for entry in items:
        doc_type = (entry.get("type") or "").upper().strip()
        name = entry.get("name") or ""
        if not name:
            continue
        if doc_type == "EX-99.1":
            ex_99_1 = name
        elif doc_type.startswith("EX-99") and ex_99_any is None:
            ex_99_any = name

    return ex_99_1 or ex_99_any


def _extract_section(
    text: str,
    start_re: "re.Pattern[str]",
    end_re: "re.Pattern[str]",
    max_chars: int,
) -> tuple[str, bool]:
    """
    Generic section extractor: same TOC-vs-real-section logic as
    _extract_risk_factors, parameterized on the start/end patterns so
    the 10-K Business section can reuse it. Returns (text, truncated);
    ("", False) when the start header is not found.
    """
    starts = list(start_re.finditer(text))
    if not starts:
        return "", False

    best_section = ""
    for start_match in starts:
        section_start = start_match.end()
        end_match = end_re.search(text, pos=section_start)
        section_end = end_match.start() if end_match else len(text)
        candidate = text[section_start:section_end].strip()
        if len(candidate) > len(best_section):
            best_section = candidate

    if not best_section:
        return "", False

    truncated = False
    if len(best_section) > max_chars:
        best_section = best_section[:max_chars]
        truncated = True
    return best_section, truncated


# 10-K Business section: "Item 1. Business", ending at "Item 1A" (Risk
# Factors) or "Item 2" (Properties). Same formatting-tolerance as the
# Risk Factors regex. The negative lookahead on "1a" keeps "Item 1A"
# from matching the Item 1 START pattern.
_BUSINESS_START_RE = re.compile(
    r"(?:^|\n)\s*item\s*1\.?\s*(?![a-z])\s*[:\-\u2013\u2014]?\s*business\b",
    flags=re.IGNORECASE,
)
_BUSINESS_END_RE = re.compile(
    r"(?:^|\n)\s*item\s*(?:1\s*a|1\s*b|2)\b",
    flags=re.IGNORECASE,
)


def get_8k_earnings_exhibit_text(ticker: str, quarters: int = 4) -> list[dict]:
    """
    Fetch the earnings press release text (Exhibit 99.1) from the most
    recent earnings 8-Ks for `ticker`.

    An "earnings 8-K" is an 8-K carrying item code 2.02 (Results of
    Operations and Financial Condition). For each, the press release is
    Exhibit 99.1 — a separate document in the accession folder, located
    via the accession index.json.

    Args:
        ticker:   stock ticker
        quarters: how many recent earnings 8-Ks to fetch (default 4 =
                  one fiscal year)

    Returns:
        list of dicts, newest first, each with: ticker, filing_date,
        accession, exhibit_text, truncated, char_count, source_url.
        Empty list if the ticker is unknown or no earnings 8-Ks found.
        Individual filings whose exhibit can't be fetched are skipped,
        not error-stubbed — a short list is the signal.
    """
    ticker = ticker.upper()
    cik_map = _load_ticker_to_cik()
    cik = cik_map.get(ticker)
    if not cik:
        return []

    # Earnings 8-Ks are filed quarterly; a 400-day window comfortably
    # captures 4 of them even with late filers and fiscal-year quirks.
    earnings_8ks = [
        f for f in get_recent_filings(ticker, days=400, form_types=("8-K",))
        if "2.02" in f.get("items", [])
    ]
    if not earnings_8ks:
        return []

    out: list[dict] = []
    for filing in earnings_8ks[:quarters]:
        accession = filing["accession_number"]
        accession_clean = accession.replace("-", "")

        # Cache check first — keyed on accession (immutable once filed).
        cache_key = f"{accession}_EX99"
        cached = _cache_read(ticker, cache_key)
        if cached is not None:
            out.append(cached)
            continue

        index_data = _fetch_accession_index(cik, accession_clean)
        exhibit_name = (
            _find_earnings_exhibit_doc(index_data) if index_data else None
        )

        # Fall back to the 8-K primary document if no EX-99* is tagged.
        # The primary doc is the 8-K cover — less ideal than the press
        # release, but it often references the same numbers and is
        # better than nothing. Flagged via `fell_back_to_primary`.
        fell_back = False
        if exhibit_name:
            doc_url = (
                f"https://www.sec.gov/Archives/edgar/data/"
                f"{int(cik)}/{accession_clean}/{exhibit_name}"
            )
        else:
            doc_url = filing["url"]
            fell_back = True

        html = _fetch_text(doc_url)
        if not html:
            print(f"[edgar] {ticker} 8-K {filing['date']}: exhibit fetch failed")
            continue

        text = _html_to_text(html)
        truncated = False
        if len(text) > _EARNINGS_EXHIBIT_MAX_CHARS:
            text = text[:_EARNINGS_EXHIBIT_MAX_CHARS]
            truncated = True

        payload = {
            "ticker": ticker,
            "filing_date": filing["date"],
            "accession": accession,
            "exhibit_text": text,
            "truncated": truncated,
            "char_count": len(text),
            "fell_back_to_primary": fell_back,
            "source_url": doc_url,
        }
        _cache_write(ticker, cache_key, payload)
        out.append(payload)

    return out


def get_latest_10k_business_section(ticker: str) -> Optional[dict]:
    """
    Fetch and extract the Business section (Item 1) from `ticker`'s most
    recent 10-K. Returns None if the filing is unavailable or the
    section can't be extracted.

    Cache-aware: keyed on accession + "_BIZ" suffix so it does not
    collide with the Risk Factors cache entry for the same 10-K.
    """
    meta = _get_latest_filing_meta(ticker, "10-K")
    if not meta:
        return None

    ticker_u = meta["ticker"]
    cache_key = f"{meta['accession_number']}_BIZ"
    cached = _cache_read(ticker_u, cache_key)
    if cached is not None:
        return cached

    html = _fetch_text(meta["url"])
    if not html:
        return None

    text = _html_to_text(html)
    business, truncated = _extract_section(
        text, _BUSINESS_START_RE, _BUSINESS_END_RE, _BUSINESS_SECTION_MAX_CHARS
    )
    if not business:
        print(f"[edgar] {ticker_u} 10-K: Business section extraction failed")
        return None

    payload = {
        "ticker": ticker_u,
        "form": meta["form"],
        "filing_date": meta["filing_date"],
        "accession": meta["accession_number"],
        "business": business,
        "truncated": truncated,
        "char_count": len(business),
        "source_url": meta["url"],
    }
    _cache_write(ticker_u, cache_key, payload)
    return payload


def get_filings_for_pre_earnings_read(ticker: str) -> dict:
    """
    One-call helper for Screen 2's per-name pass. Bundles the four
    filing inputs the pre-earnings read needs:

        {
          "ticker": str,
          "business": dict | None,       # latest 10-K Item 1 Business
          "k10_risk": dict | None,       # latest 10-K Risk Factors
          "q10_risk": dict | None,       # latest 10-Q Risk Factors
          "earnings_8ks": list[dict],    # last ~4 quarters of EX-99.1
          "errors": [str, ...],          # human-readable gaps
        }

    Always returns a dict — Screen 2 can render even when everything is
    missing (the prompt is told to downgrade confidence on thin data).

    Reuses get_latest_10k_risk_factors / get_latest_10q_risk_factors
    from the Screen 1 extension; their cache entries are shared, so a
    name read by both screens fetches Risk Factors only once.
    """
    ticker = ticker.upper()
    out: dict = {
        "ticker": ticker,
        "business": None,
        "k10_risk": None,
        "q10_risk": None,
        "earnings_8ks": [],
        "errors": [],
    }

    out["business"] = get_latest_10k_business_section(ticker)
    if out["business"] is None:
        out["errors"].append("10-K Business section unavailable")

    out["k10_risk"] = get_latest_10k_risk_factors(ticker)
    if out["k10_risk"] is None:
        out["errors"].append("10-K Risk Factors unavailable")

    out["q10_risk"] = get_latest_10q_risk_factors(ticker)
    if out["q10_risk"] is None:
        out["errors"].append("10-Q Risk Factors unavailable")

    out["earnings_8ks"] = get_8k_earnings_exhibit_text(ticker, quarters=4)
    if not out["earnings_8ks"]:
        out["errors"].append("no earnings 8-K exhibits found")

    return out


# --- standalone test entry point ---
if __name__ == "__main__":
    # Test against May 5 movers
    test_tickers = ["DOCN", "IPGP", "CYTK", "OSIS", "AEIS", "ADEA", "GXO", "ECG"]
    print(f"[edgar] testing against {len(test_tickers)} May 5 movers...")
    print()

    for ticker in test_tickers:
        filings = get_recent_filings(ticker, days=10)
        if filings:
            print(f"{ticker}: {len(filings)} recent 8-K(s)")
            for f in filings:
                items_str = ",".join(f["items"]) if f["items"] else "(no items)"
                print(f"  {f['date']} - {f['form']} - items={items_str} - {f['url']}")
        else:
            print(f"{ticker}: no recent 8-Ks")
        print()

    # ---- Screen 2 pre-earnings read smoke test ----
    # Two well-covered names; confirms 8-K EX-99.1 location, 10-K
    # Business extraction, and the bundling helper all work end to end.
    print("=" * 60)
    print("[edgar] Screen 2 — pre-earnings read smoke test")
    print("=" * 60)
    for ticker in ["WMT", "NVDA"]:
        print(f"\n--- {ticker} ---")
        bundle = get_filings_for_pre_earnings_read(ticker)

        biz = bundle["business"]
        if biz:
            trunc = " (truncated)" if biz["truncated"] else ""
            print(f"  10-K Business: {biz['char_count']} chars{trunc} "
                  f"(filed {biz['filing_date']})")
        else:
            print("  10-K Business: MISSING")

        for label, key in (("10-K Risk", "k10_risk"), ("10-Q Risk", "q10_risk")):
            rf = bundle[key]
            if rf:
                trunc = " (truncated)" if rf["truncated"] else ""
                print(f"  {label}: {rf['char_count']} chars{trunc}")
            else:
                print(f"  {label}: MISSING")

        ex = bundle["earnings_8ks"]
        print(f"  earnings 8-Ks: {len(ex)} found")
        for e in ex:
            fb = " [fell back to 8-K primary]" if e["fell_back_to_primary"] else ""
            trunc = " (truncated)" if e["truncated"] else ""
            print(f"    {e['filing_date']}: {e['char_count']} chars{trunc}{fb}")

        if bundle["errors"]:
            print(f"  errors: {bundle['errors']}")
    print()
    print("Eyeball: Business + Risk sections should be multi-thousand "
          "chars; earnings 8-Ks should be 4 with few/no fall-backs.")