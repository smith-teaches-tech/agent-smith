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