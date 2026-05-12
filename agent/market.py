"""
Market data collection.
Pulls index/sector context, then scans for unusual movers
in the discovery universe (mid-cap sweet spot).
"""
import yfinance as yf
from typing import Any
import time

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


# ============================================================
# Candidate ticker sources
# ============================================================
# Discovery universe: hand-picked subset of S&P 400 (mid-cap) and
# S&P 600 (small-cap) names.
#
# History:
#   - Phase 1.5-lite (Apr 22, 2026): launched with this ~80-ticker
#     hand-picked sample. Generated the EXTR +17.9% trade, plus
#     all 5 OVERDONE flags that have ever been recorded.
#   - Universe expansion (May 5, 2026): switched to live SP400+SP600
#     fetch (~1003 tickers), in pursuit of a wider candidate pool.
#   - Rollback (May 12, 2026): the wider universe produced almost no
#     edge — May 11's run produced 51 SKIP decisions, zero OVERDONE /
#     UNDERDONE flags, all entries either RATIONAL or UNCLEAR. The
#     selection-analysis from May 9 already showed every OVERDONE flag
#     lived in the 4-8% bucket; in a 1003-ticker universe that bucket
#     is overwhelmed by movers that don't carry the same information
#     content as the curated names did. Rolled back to the original 80.
#
# This list is deliberately small, hand-curated, and biased toward the
# sectors where the strategy's mispricing thesis applies: biotech,
# growth tech, defense, networking, gene editing, restaurants, and
# the original "interesting names that move" set. No dividend-heavy
# names (Michael is a non-US resident — 30% US dividend withholding
# would shred returns). No REITs.
#
# To curate: edit DISCOVERY_UNIVERSE below directly. No re-deploy or
# refresh needed — every cron tick reads the list at import time.
#
# The names below are the original Phase 1.5-lite sample, preserved
# verbatim from market.py prior to the May 5 expansion.

# Original ~45 mid-cap names (S&P 400 segment).
_MIDCAP_SEED = [
    "ALGN", "DECK", "PSTG", "WSM", "RGEN", "ENTG", "FIVE", "CHRW",
    "MEDP", "EXEL", "GTLS", "SAIA", "MANH", "JBL", "CASY", "BLDR",
    "RPM", "WST", "POOL", "FFIV", "TXRH", "INSM", "WSO", "AIT",
    "JLL", "WEX", "GGG", "AOS", "WAT", "MASI", "WWD", "AYI",
    "GME", "BBY", "DKS", "FOXA", "LSCC", "QRVO", "MKSI", "ONTO",
    "FLEX", "JBLU", "ALK", "SAVE", "SKYW",
]

# Original ~35 small-cap names (S&P 600 segment), grouped by theme.
_SMALLCAP_SEED = [
    # Industrial / energy / education
    "MGY", "MMSI", "UFPI", "AMR", "PRDO", "ATGE", "ENV", "BMI",
    # Networking / semiconductors
    "CALX", "AEIS", "PLAB", "VECO", "PRGS", "EXTR", "CAMP",
    # Defense / aerospace
    "AVAV", "KTOS", "MRCY", "DCO", "HEI",
    # Biotech / gene editing
    "CRSP", "BEAM", "EDIT", "VCYT", "CDNA", "NTLA",
    "BBIO", "ITCI", "AXSM", "PTCT", "MYGN",
    # Consumer staples / food
    "FIZZ", "CENT", "JJSF", "LANC",
]

# The curated discovery universe — 80 hand-picked names.
DISCOVERY_UNIVERSE: list[str] = _MIDCAP_SEED + _SMALLCAP_SEED


def get_discovery_candidates() -> list[str]:
    """
    Return the curated 80-ticker discovery universe.

    Returns:
      Deduplicated list of ticker symbols in their original curated
      order — mid-cap segment first, then small-cap segment.
    """
    # Dedup defensively while preserving order, in case the seed lists
    # ever drift to overlap. Cheap and protects downstream callers.
    return list(dict.fromkeys(DISCOVERY_UNIVERSE))


if __name__ == "__main__":
    # Smoke test
    print("Indices:", fetch_context_quotes(config.INDICES[:2]))
    candidates = get_discovery_candidates()
    print(f"Discovery candidate count: {len(candidates)}")
    print(f"First 10: {candidates[:10]}")