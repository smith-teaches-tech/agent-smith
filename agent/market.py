"""
Market data collection.
Pulls index/sector context, then scans for unusual movers
in the discovery universe (mid-cap sweet spot).
"""
import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta
from typing import Any
import time
import sys

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
) -> list[dict[str, Any]]:
    """
    Given a candidate ticker list, fetch each one's recent price+volume
    and filter for the discovery universe (mid-cap, liquid, not penny).

    Returns sorted list of mover candidates with full context.

    Note: yfinance does not expose a 'top movers' endpoint, so the
    candidate_tickers list should come from a separate source (Finnhub
    top gainers/losers, an SP400/SP600 constituent list, etc.).
    For v0 we accept whatever is passed in.
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

            # Apply filters
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
    (big % move OR unusual volume). Sort by interestingness.
    """
    if thresholds is None:
        thresholds = config.MOVEMENT_THRESHOLDS

    pct_min = thresholds["intraday_pct_min"]
    vol_mult_min = thresholds["volume_multiple_min"]
    cap = thresholds["max_candidates_per_run"]

    interesting = [
        m for m in movers
        if abs(m.get("change_pct", 0)) >= pct_min
        or m.get("volume_multiple", 0) >= vol_mult_min
    ]

    # Score for ranking: combination of move size and volume anomaly
    def score(m):
        return abs(m.get("change_pct", 0)) + (m.get("volume_multiple", 0) * 2)

    interesting.sort(key=score, reverse=True)
    return interesting[:cap]


def fetch_taiwan_quotes() -> dict[str, dict[str, Any]]:
    """Fetch Taiwan context quotes."""
    return fetch_context_quotes(config.TAIWAN_CONTEXT)


def fetch_adr_arb_opportunities() -> list[dict[str, Any]]:
    """
    Compare ADR vs local Taiwan listing for divergence.
    A meaningful divergence (after FX) often signals overnight news
    one market hasn't priced in yet.
    """
    out = []
    for adr, local in config.TAIWAN_ADR_PAIRS:
        try:
            adr_data = fetch_context_quotes([adr]).get(adr, {})
            local_data = fetch_context_quotes([local]).get(local, {})
            if "error" in adr_data or "error" in local_data:
                continue
            out.append({
                "adr": adr,
                "local": local,
                "adr_change_pct": adr_data.get("change_pct"),
                "local_change_pct": local_data.get("change_pct"),
                "divergence_pct": round(
                    (adr_data.get("change_pct") or 0) - (local_data.get("change_pct") or 0),
                    2,
                ),
            })
        except Exception:
            continue
    return out


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
    column is named "Symbol" (or sometimes "Ticker symbol"). We use
    pandas.read_html() to parse all tables on the page, then pick the
    first one that has a recognizable ticker column.

    Returns a list of normalized, deduplicated tickers. Raises on any
    failure — caller is responsible for fallback handling.
    """
    # pandas.read_html requires lxml or html5lib. Both are in requirements.
    tables = pd.read_html(url)
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


def get_discovery_candidates() -> list[str]:
    """
    Return the candidate ticker list for discovery scanning.

    Tries to fetch fresh SP400 + SP600 constituent lists from Wikipedia
    each run. On any failure, falls back to the hardcoded sample lists
    and prints a warning visible in GitHub Actions output.

    Returns deduplicated list of tickers.
    """
    sp400: list[str] = []
    sp600: list[str] = []

    # Fetch SP400
    try:
        sp400 = _fetch_constituents_from_wikipedia(SP400_WIKIPEDIA_URL, "SP400")
        print(f"[market] fetched {len(sp400)} SP400 constituents from Wikipedia")
    except Exception as e:
        print(
            f"[market] WARNING: SP400 fetch failed ({e!r}); "
            f"falling back to hardcoded sample of {len(SP400_FALLBACK)} tickers",
            file=sys.stderr,
        )
        sp400 = list(SP400_FALLBACK)

    # Fetch SP600
    try:
        sp600 = _fetch_constituents_from_wikipedia(SP600_WIKIPEDIA_URL, "SP600")
        print(f"[market] fetched {len(sp600)} SP600 constituents from Wikipedia")
    except Exception as e:
        print(
            f"[market] WARNING: SP600 fetch failed ({e!r}); "
            f"falling back to hardcoded sample of {len(SP600_FALLBACK)} tickers",
            file=sys.stderr,
        )
        sp600 = list(SP600_FALLBACK)

    # Combine and deduplicate
    combined = list(dict.fromkeys(sp400 + sp600))
    print(f"[market] total discovery universe: {len(combined)} tickers")
    return combined


if __name__ == "__main__":
    # Smoke test
    print("Indices:", fetch_context_quotes(config.INDICES[:2]))
    print("Discovery candidate count:", len(get_discovery_candidates()))