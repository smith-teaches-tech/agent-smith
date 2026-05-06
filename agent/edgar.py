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
        out.append({
            "ticker": ticker,
            "date": dates[i],
            "form": forms[i],
            "accession_number": accessions[i],
            "primary_document": primary_docs[i],
            "url": filing_url,
        })

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
                print(f"  {f['date']} - {f['form']} - {f['url']}")
        else:
            print(f"{ticker}: no recent 8-Ks")
        print()