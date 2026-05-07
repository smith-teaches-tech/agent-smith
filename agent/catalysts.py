"""
Per-mover catalyst enrichment.

Wraps edgar.py and earnings.py to attach a `catalyst_signals` field to each
mover dict before it's sent to the discovery prompt.

The discovery pass currently sees mover price/volume but has no information
about what triggered the move. RSS news catches some catalysts but missed
~90% of May 2026 movers (the catalyst-blindness problem). EDGAR 8-Ks are
filed for every material event; earnings calendar surfaces forward-looking
positioning context. Together they convert most "UNCLEAR conf 2" calls into
directional reads with evidence the LLM can actually point to.

Design notes:
- Per-ticker failures don't abort the batch (one bad yfinance call should
  not nuke the whole run). Errors are logged, the field gets {} for that
  mover, and the caller continues.
- 8-Ks and recent_earnings overlap on earnings days (an earnings 8-K shows
  up in both). We dedup by suppressing recent_earnings when an 8-K with
  item 2.02 was filed on the same date — keeps the 8-K (which has a URL)
  and drops the duplicate signal.
- This module is sequential by design. Concurrent fetching would speed it
  up, but the SEC's 0.15s polite-sleep policy applies per-IP, not
  per-connection, and we don't want to be the project that gets the SEC
  blocking the GitHub Actions runner IP range.
"""
from typing import Any

from . import edgar
from . import earnings


# 8-K item code → human-readable label. Surfaced into the prompt so the LLM
# doesn't have to know SEC item taxonomy from training data — we tell it.
# Keep this list short; we only label codes that are signal-rich for trade
# direction. Less common codes (3.x, 6.x, 7.x) are passed through as raw
# codes; the LLM can guess from context.
ITEM_CODE_LABELS = {
    "1.01": "material definitive agreement (e.g. M&A, big contract)",
    "1.02": "termination of material agreement",
    "1.03": "bankruptcy or receivership",
    "2.01": "completion of acquisition or disposition",
    "2.02": "results of operations (earnings)",
    "2.03": "creation of direct financial obligation",
    "2.04": "triggering events accelerating financial obligation",
    "2.05": "costs associated with exit or disposal (impairment / restructuring)",
    "2.06": "material impairment",
    "3.01": "delisting / failure to satisfy listing rule",
    "3.03": "material modification to security holder rights",
    "4.01": "change in registrant's certifying accountant (auditor change)",
    "4.02": "non-reliance on previously issued financials (RESTATEMENT — usually severe)",
    "5.02": "departure / appointment of officers or directors",
    "5.03": "amendments to articles of incorporation",
    "7.01": "regulation FD disclosure",
    "8.01": "other events (catch-all)",
    "9.01": "financial statements and exhibits (boilerplate, ignore in isolation)",
}


def _label_items(items: list[str]) -> list[dict[str, str]]:
    """Annotate raw item codes with labels where we have them."""
    return [
        {"code": code, "label": ITEM_CODE_LABELS.get(code, "(no label)")}
        for code in items
    ]


def _fetch_one(ticker: str) -> dict[str, Any]:
    """
    Fetch all catalyst signals for one ticker.

    Returns a dict with at most three keys: filings_8k, recent_earnings,
    upcoming_earnings. Missing keys = no signal of that type. Empty dict
    = no signals at all.
    """
    out: dict[str, Any] = {}

    # 8-Ks: last 7 days. Each filing already has date, items, url from
    # the extended edgar.get_recent_filings.
    try:
        filings = edgar.get_recent_filings(ticker, days=7, form_types=("8-K",))
    except Exception as e:
        print(f"[catalysts] {ticker}: EDGAR error: {e}")
        filings = []

    if filings:
        out["filings_8k"] = [
            {
                "date": f["date"],
                "items": _label_items(f["items"]),
                "url": f["url"],
            }
            for f in filings
        ]

    # Recent earnings: last 5 days. Used to confirm "this stock moved on
    # earnings" when no 8-K is found (rare but happens).
    try:
        recent = earnings.get_recent_earnings(ticker, lookback_days=5)
    except Exception as e:
        print(f"[catalysts] {ticker}: recent earnings error: {e}")
        recent = None

    if recent:
        out["recent_earnings"] = recent

    # Upcoming earnings: next 14 days. UNIQUE forward-looking signal that
    # EDGAR can't provide. A stock moving 8% with no 8-K but reports
    # tomorrow is a positioning signal.
    try:
        upcoming = earnings.get_upcoming_earnings(ticker, lookahead_days=14)
    except Exception as e:
        print(f"[catalysts] {ticker}: upcoming earnings error: {e}")
        upcoming = None

    if upcoming:
        out["upcoming_earnings"] = upcoming

    # Dedup: if there's a 2.02 (earnings) 8-K on the same date as a
    # recent_earnings hit, drop recent_earnings. The 8-K is strictly more
    # informative (has URL, can be inspected) so we keep that one.
    if "filings_8k" in out and "recent_earnings" in out:
        recent_date = (out["recent_earnings"].get("last_earnings_date") or "")[:10]
        for f in out["filings_8k"]:
            has_2_02 = any(it["code"] == "2.02" for it in f["items"])
            if has_2_02 and f["date"] == recent_date:
                del out["recent_earnings"]
                break

    return out


def enrich_movers(movers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Attach a `catalyst_signals` field to each mover.

    Mutates a *copy* of each mover dict (does not modify the input list).
    Returns a new list of enriched dicts. Catalyst-less movers get
    `catalyst_signals: {}` so the LLM can see we looked and found nothing.

    Logging: prints one line per mover summarizing what was found, plus a
    rollup at the end. Useful for spot-checking cron logs.
    """
    print(f"[catalysts] enriching {len(movers)} movers...")
    enriched: list[dict[str, Any]] = []
    n_with_8k = 0
    n_with_recent_er = 0
    n_with_upcoming_er = 0
    n_silent = 0

    for m in movers:
        ticker = m.get("ticker")
        if not ticker:
            enriched.append(dict(m))
            continue
        signals = _fetch_one(ticker)

        # Build summary line for this ticker
        parts = []
        if "filings_8k" in signals:
            n_with_8k += 1
            n_filings = len(signals["filings_8k"])
            # Show item codes from the most recent filing as a quick read
            top_items = signals["filings_8k"][0]["items"]
            codes = ",".join(it["code"] for it in top_items) or "no-items"
            parts.append(f"{n_filings} 8-K (latest items: {codes})")
        if "recent_earnings" in signals:
            n_with_recent_er += 1
            parts.append("recent earnings")
        if "upcoming_earnings" in signals:
            n_with_upcoming_er += 1
            days = signals["upcoming_earnings"].get("days_until")
            parts.append(f"earnings in {days}d")
        if not parts:
            n_silent += 1
            parts.append("no signals")
        print(f"[catalysts] {ticker}: {'; '.join(parts)}")

        out = dict(m)
        out["catalyst_signals"] = signals
        enriched.append(out)

    print(
        f"[catalysts] done. {n_with_8k} with 8-K, "
        f"{n_with_recent_er} with recent earnings, "
        f"{n_with_upcoming_er} with upcoming earnings, "
        f"{n_silent} silent."
    )
    return enriched


# --- standalone test entry point ---
if __name__ == "__main__":
    # Test against today's actual movers from the May 7 run
    test_movers = [
        {"ticker": "PRIM", "name": "Primoris Services Corporation"},
        {"ticker": "TMDX", "name": "TransMedics Group"},
        {"ticker": "VECO", "name": "Veeco Instruments"},
        {"ticker": "VCYT", "name": "Veracyte"},
        {"ticker": "GEO",  "name": "Geo Group"},
    ]
    enriched = enrich_movers(test_movers)
    print()
    import json
    print(json.dumps(enriched, indent=2, default=str))