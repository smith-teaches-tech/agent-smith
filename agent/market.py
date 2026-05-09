"""
Market data collection.
Pulls index/sector context, then scans for unusual movers
in the discovery universe (mid-cap sweet spot).
"""
import yfinance as yf
import pandas as pd
import requests
from datetime import datetime, timedelta
from typing import Any
import time
import sys
import io
import json
from pathlib import Path

from . import config


def fetch_context_quotes(tickers: list[str]) -> dict[str, dict[str, Any]]:
    """
    Pull current quote and intraday change for context tickers
    (indices, sector ETFs, mega-caps).
    Returns dict: {ticker: {price, change_pct, volume, ...}}
    """
    out: dict[str, dict[str, Any]] = {}
    for tkr in tickers:
        try:
            t = yf.Ticker(tkr)
            hist = t.history(period="2d", interval="1d")
            if len(hist) < 1:
                continue
            last = hist.iloc[-1]
            prev_close = hist.iloc[-2]["Close"] if len(hist) >= 2 else last["Open"]
            change_pct = ((last["Close"] - prev_close) / prev_close) * 100
            out[tkr] = {
                "price": round(float(last["Close"]), 2),
                "change_pct": round(float(change_pct), 2),
                "volume": int(last["Volume"]),
                "high": round(float(last["High"]), 2),
                "low": round(float(last["Low"]), 2),
            }
        except Exception as e:
            out[tkr] = {"error": str(e)}
        time.sleep(0.05)  # be polite to Yahoo
    return out


def fetch_movers_universe(
    candidate_tickers: list[str],
    filters: dict[str, Any] = None,
    apply_filters: bool = True,
) -> list[dict[str, Any]]:
    """
    Given a candidate ticker list, fetch each one's recent price+volume
    and filter for the discovery universe (mid-cap, liquid, not penny).

    Returns sorted list of mover candidates with full context.

    Note: yfinance does not expose a 'top movers' endpoint, so the
    candidate_tickers list should come from a separate source (Finnhub
    top gainers/losers, an SP400/SP600 constituent list, etc.).
    For v0 we accept whatever is passed in.

    apply_filters: when False, skip the market_cap / price / dollar-volume
    gates and return a mover dict for every ticker that yfinance returns
    valid data for. Used by the --tickers override path in main.py for
    cheap targeted testing on hand-picked names.
    """
    if filters is None:
        filters = config.DISCOVERY_FILTERS

    movers: list[dict[str, Any]] = []
    for tkr in candidate_tickers:
        try:
            t = yf.Ticker(tkr)
            info = t.info or {}
            hist = t.history(period="22d", interval="1d")
            if len(hist) < 5:
                continue

            mcap = info.get("marketCap")
            price = float(hist.iloc[-1]["Close"])
            volume = int(hist.iloc[-1]["Volume"])
            avg_volume = float(hist["Volume"].mean())
            avg_dollar_volume = avg_volume * float(hist["Close"].mean())

            # Apply filters (skipped under --tickers override path)
            if apply_filters:
                if mcap is None or mcap < filters["min_market_cap"]:
                    continue
                if mcap > filters["max_market_cap"]:
                    continue
                if price < filters["min_price"]:
                    continue
                if avg_dollar_volume < filters["min_avg_dollar_volume"]:
                    continue

            prev_close = float(hist.iloc[-2]["Close"]) if len(hist) >= 2 else float(hist.iloc[-1]["Open"])
            change_pct = ((price - prev_close) / prev_close) * 100
            volume_multiple = volume / avg_volume if avg_volume > 0 else 0

            # Five-day context
            five_day_change = ((price - float(hist.iloc[-6]["Close"])) / float(hist.iloc[-6]["Close"])) * 100 if len(hist) >= 6 else None

            movers.append({
                "ticker": tkr,
                "name": info.get("shortName") or info.get("longName") or tkr,
                "sector": info.get("sector"),
                "industry": info.get("industry"),
                "market_cap": mcap,
                "price": round(price, 2),
                "change_pct": round(change_pct, 2),
                "volume": volume,
                "avg_volume": int(avg_volume),
                "volume_multiple": round(volume_multiple, 2),
                "five_day_change_pct": round(five_day_change, 2) if five_day_change is not None else None,
            })
        except Exception:
            continue
        time.sleep(0.1)

    return movers


def filter_unusual_movers(
    movers: list[dict[str, Any]],
    thresholds: dict[str, Any] = None,
) -> list[dict[str, Any]]:
    """
    From the discovery universe, keep only those moving unusually
    (big % move OR unusual volume), then take a representative slice
    of `max_candidates_per_run`.

    Two selection modes:

    - Stratified (default, since May 9 2026): bucket the filtered
      movers by abs(change_pct) and take K from each bucket per
      `stratified_buckets`. Within a bucket, rank by score and take
      the top K. Under-filled buckets cascade their leftover capacity
      to the next-smaller bucket so we never under-fill the prompt
      and the small-mover bias is preserved on quiet days. See
      selection_analysis.md for the data motivation.

    - Top-N (legacy): score = |move%| + 2 × volume_multiple, sort
      desc, take top max_candidates_per_run. Toggle via
      `MOVEMENT_THRESHOLDS["stratified_sampling"] = False`.
    """
    if thresholds is None:
        thresholds = config.MOVEMENT_THRESHOLDS

    pct_min = thresholds["intraday_pct_min"]
    vol_mult_min = thresholds["volume_multiple_min"]
    cap = thresholds["max_candidates_per_run"]

    # Score for ranking: combination of move size and volume anomaly.
    # Used both as the within-bucket sort key (stratified mode) and as
    # the global sort key (legacy top-N mode).
    def score(m):
        return abs(m.get("change_pct", 0)) + (m.get("volume_multiple", 0) * 2)

    interesting = [
        m for m in movers
        if abs(m.get("change_pct", 0)) >= pct_min
        or m.get("volume_multiple", 0) >= vol_mult_min
    ]

    if not thresholds.get("stratified_sampling", False):
        # Legacy top-N mode
        interesting.sort(key=score, reverse=True)
        return interesting[:cap]

    # ---- Stratified mode --------------------------------------
    buckets_def = thresholds["stratified_buckets"]

    # Place each mover into exactly one bucket by abs(change_pct).
    # Iteration is in defined bucket order; first-match wins, which
    # is correct because buckets are non-overlapping by construction.
    bucketed: dict[str, list[dict[str, Any]]] = {b[0]: [] for b in buckets_def}
    for m in interesting:
        a = abs(m.get("change_pct", 0))
        for label, lo, hi, _cap in buckets_def:
            if lo <= a < hi:
                bucketed[label].append(m)
                break

    # Sort within each bucket by score, descending.
    for lst in bucketed.values():
        lst.sort(key=score, reverse=True)

    # Take K from each bucket. Track underfill so we can cascade it
    # toward smaller buckets — preserves the small-mover bias on quiet
    # days when the big-mover buckets are sparse.
    chosen: list[dict[str, Any]] = []
    spillover = 0  # unused capacity from previously processed buckets
    # Process buckets from largest to smallest move so spillover flows
    # naturally into the small-mover bucket where we want it.
    for label, lo, hi, bucket_cap in reversed(buckets_def):
        slots = bucket_cap + spillover
        available = bucketed[label]
        take = min(slots, len(available))
        chosen.extend(available[:take])
        spillover = slots - take  # what's left over goes to the next-smaller bucket

    # If after all buckets we still have spillover (very quiet day where
    # everything underfills), there's nothing to do — we just return
    # fewer than `cap` movers. The discovery pass handles a smaller list
    # gracefully.

    # Final sort by score for downstream consumers that expect
    # "interestingness order" (e.g. the retry path in main.py uses
    # movers[:N//2] expecting the strongest signals are first).
    chosen.sort(key=score, reverse=True)

    # Diagnostic print so cron logs show the bucket distribution.
    # Cheap to keep on permanently; aids the 2-week observation window.
    counts = {b[0]: 0 for b in buckets_def}
    for m in chosen:
        a = abs(m.get("change_pct", 0))
        for label, lo, hi, _ in buckets_def:
            if lo <= a < hi:
                counts[label] += 1
                break
    summary = ", ".join(f"{lbl}:{n}" for lbl, n in counts.items())
    print(f"[market] stratified selection: {len(chosen)} movers ({summary})")

    return chosen

# SP400/SP600 constituent cache (local-dev accelerator)
# - 30-day TTL: Wikipedia constituent changes happen ~quarterly with 0-3
#   swaps each rebalance, so 30 days is comfortably below the cadence.
# - GitHub Actions runs fresh containers each cron tick, so production
#   always re-fetches from Wikipedia regardless of this cache.
# - File location: .cache/market/constituents.json (gitignored via
#   parent .cache/ entry from the Screen 1 work).
_CONSTITUENT_CACHE_PATH = Path(".cache/market/constituents.json")
_CONSTITUENT_CACHE_TTL_SECONDS = 30 * 24 * 60 * 60  # 30 days
 
 
def _read_constituent_cache() -> list[str] | None:
    """
    Return the cached ticker list if present and fresh (< TTL old).
    Returns None on miss, expired cache, or read/parse error.
    """
    path = _CONSTITUENT_CACHE_PATH
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"[market] constituent cache unreadable ({e}); will refetch")
        return None
 
    fetched_at = payload.get("fetched_at_unix", 0)
    age_s = time.time() - fetched_at
    if age_s > _CONSTITUENT_CACHE_TTL_SECONDS:
        print(
            f"[market] constituent cache expired "
            f"(age {age_s/86400:.1f} days > 30); will refetch"
        )
        return None
 
    tickers = payload.get("tickers")
    if not isinstance(tickers, list) or not tickers:
        return None
 
    print(
        f"[market] constituent cache hit "
        f"({len(tickers)} tickers, age {age_s/86400:.1f} days)"
    )
    return tickers
 
 
def _write_constituent_cache(tickers: list[str]) -> None:
    """Persist the freshly-fetched list. Best-effort — failure is not fatal."""
    try:
        _CONSTITUENT_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "fetched_at_unix": time.time(),
            "fetched_at_iso": __import__("datetime").datetime.utcnow().isoformat() + "Z",
            "ttl_seconds": _CONSTITUENT_CACHE_TTL_SECONDS,
            "tickers": tickers,
        }
        _CONSTITUENT_CACHE_PATH.write_text(
            json.dumps(payload, indent=2),
            encoding="utf-8",
        )
        print(f"[market] wrote constituent cache ({len(tickers)} tickers)")
    except OSError as e:
        print(f"[market] constituent cache write failed ({e}); continuing")

# ============================================================
# Candidate ticker sources
# ============================================================
# Discovery universe: S&P 400 (mid-cap) + S&P 600 (small-cap) constituents.
#
# Phase 1.5-lite update (2026-05-03): switched from a small hardcoded
# sample (~80 tickers, ~8% coverage) to fetching the live constituent
# lists from Wikipedia each run. Hardcoded fallback remains in place
# in case the fetch fails — falls back loudly with a printed warning
# visible in GitHub Actions output.

# Wikipedia constituent table URLs.
SP400_WIKIPEDIA_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_400_companies"
SP600_WIKIPEDIA_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_600_companies"

# Fallback samples — used only if the live fetch fails.
# These are the original Phase 1.5-lite samples; proven to work.
SP400_FALLBACK = [
    "ALGN", "DECK", "PSTG", "WSM", "RGEN", "ENTG", "FIVE", "CHRW",
    "MEDP", "EXEL", "GTLS", "SAIA", "MANH", "JBL", "CASY", "BLDR",
    "RPM", "WST", "POOL", "FFIV", "TXRH", "INSM", "WSO", "AIT",
    "JLL", "WEX", "GGG", "AOS", "WAT", "MASI", "WWD", "AYI",
    "GME", "BBY", "DKS", "FOXA", "LSCC", "QRVO", "MKSI", "ONTO",
    "FLEX", "JBLU", "ALK", "SAVE", "SKYW",
]

SP600_FALLBACK = [
    "MGY", "MMSI", "UFPI", "AMR", "PRDO", "ATGE", "ENV", "BMI",
    "CALX", "AEIS", "PLAB", "VECO", "PRGS", "EXTR", "CAMP",
    "AVAV", "KTOS", "MRCY", "DCO", "HEI",
    "CRSP", "BEAM", "EDIT", "VCYT", "CDNA", "NTLA",
    "BBIO", "ITCI", "AXSM", "PTCT", "MYGN",
    "FIZZ", "CENT", "JJSF", "LANC",
]

# Back-compat aliases (some external code may still reference these names)
SP400_SAMPLE = SP400_FALLBACK
SP600_SAMPLE = SP600_FALLBACK


def _normalize_ticker(symbol: str) -> str:
    """
    Convert Wikipedia-style share-class tickers (BRK.B) to yfinance-style (BRK-B).
    Wikipedia uses '.' as the share-class separator; yfinance uses '-'.
    Also strips whitespace and uppercases.
    """
    if not isinstance(symbol, str):
        return ""
    return symbol.strip().upper().replace(".", "-")


def _fetch_constituents_from_wikipedia(url: str, label: str) -> list[str]:
    """
    Fetch S&P index constituent tickers from a Wikipedia page.

    Wikipedia constituent pages include a sortable HTML table where one
    column is named "Symbol" (or sometimes "Ticker symbol"). We fetch
    the page with requests (using a real browser User-Agent — Wikipedia
    returns 403 on the default urllib UA used by pandas.read_html), then
    hand the HTML string to pandas to parse all tables, then pick the
    first one that has a recognizable ticker column.

    Returns a list of normalized, deduplicated tickers. Raises on any
    failure — caller is responsible for fallback handling.
    """
    # Use a real browser User-Agent — Wikipedia 403s urllib's default UA.
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
    }
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()

    # pandas.read_html accepts an HTML string. Requires lxml or html5lib
    # for parsing — both are in requirements.
    tables = pd.read_html(io.StringIO(resp.text))
    if not tables:
        raise ValueError(f"{label}: no tables found on page")

    # Find the constituent table by looking for a ticker-like column header.
    ticker_col_candidates = ("Symbol", "Ticker symbol", "Ticker")
    for tbl in tables:
        cols = [str(c) for c in tbl.columns]
        for candidate in ticker_col_candidates:
            if candidate in cols:
                series = tbl[candidate].dropna().astype(str)
                tickers = [_normalize_ticker(s) for s in series]
                # Filter out anything that doesn't look like a real ticker.
                # Real tickers are 1-6 chars, A-Z plus optional '-' for share class.
                tickers = [
                    t for t in tickers
                    if t and 1 <= len(t) <= 6 and all(
                        c.isalpha() or c == "-" for c in t
                    )
                ]
                if len(tickers) >= 50:  # sanity check — real index has hundreds
                    # Deduplicate while preserving order
                    return list(dict.fromkeys(tickers))

    raise ValueError(
        f"{label}: no table with a recognizable ticker column "
        f"(looked for {ticker_col_candidates!r})"
    )


# The signature gains a force_refresh kwarg (default False) so existing
# callers (`market.get_discovery_candidates()` with no args) work unchanged.
 
def get_discovery_candidates(force_refresh: bool = False) -> list[str]:
    """
    Return the candidate ticker list for discovery scanning.
 
    Tries cache first (30-day TTL), falls back to live Wikipedia fetch
    of SP400 + SP600 constituent lists. Cache is at
    .cache/market/constituents.json — gitignored, local-dev only.
 
    Args:
      force_refresh: if True, bypass the cache and refetch from Wikipedia.
                     Use after a known index rebalance or for debugging.
                     Production cron runs (in fresh GitHub Actions
                     containers) always have a cache miss naturally and
                     don't need this flag.
 
    Returns:
      Deduplicated list of normalized tickers. Empty list on total
      failure (Wikipedia 403 with no cache available).
    """
    if not force_refresh:
        cached = _read_constituent_cache()
        if cached:
            return cached
 
    print("[market] fetching SP400 + SP600 constituents from Wikipedia...")
    tickers: list[str] = []
    try:
        sp400 = _fetch_constituents_from_wikipedia(
            "https://en.wikipedia.org/wiki/List_of_S%26P_400_companies",
            "SP400",
        )
        print(f"[market] fetched {len(sp400)} SP400 constituents from Wikipedia")
        tickers.extend(sp400)
    except Exception as e:
        print(f"[market] SP400 fetch failed: {e}")
 
    try:
        sp600 = _fetch_constituents_from_wikipedia(
            "https://en.wikipedia.org/wiki/List_of_S%26P_600_companies",
            "SP600",
        )
        print(f"[market] fetched {len(sp600)} SP600 constituents from Wikipedia")
        tickers.extend(sp600)
    except Exception as e:
        print(f"[market] SP600 fetch failed: {e}")
 
    # Deduplicate while preserving order (a ticker rarely appears in both,
    # but defensive cheap dedup costs nothing).
    deduped = list(dict.fromkeys(tickers))
    print(f"[market] total discovery universe: {len(deduped)} tickers")
 
    if deduped:
        # Only cache successful (non-empty) fetches. A partial-failure
        # write would otherwise lock us into a broken cache for 30 days.
        _write_constituent_cache(deduped)
 
    return deduped


if __name__ == "__main__":
    # Smoke test
    print("Indices:", fetch_context_quotes(config.INDICES[:2]))
    print("Discovery candidate count:", len(get_discovery_candidates()))