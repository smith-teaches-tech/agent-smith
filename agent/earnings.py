"""
Earnings calendar fetcher.

For a given ticker, returns upcoming earnings dates within a lookhead window.
Uses yfinance, which is already a project dependency.

Note: yfinance's earnings calendar data quality varies. Smaller/less-covered
names may have stale or missing data. We treat absence as "no known upcoming
earnings" rather than failing the whole pipeline.
"""
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

import yfinance as yf
import pandas as pd


def get_upcoming_earnings(ticker: str, lookahead_days: int = 14) -> Optional[dict]:
    """
    Check if a ticker has upcoming earnings within the lookahead window.

    Args:
        ticker: stock ticker (e.g., "DOCN")
        lookahead_days: how far forward to look

    Returns:
        dict with keys: ticker, next_earnings_date, days_until, is_pre_market, is_after_hours
        OR None if no upcoming earnings in window or data unavailable.
    """
    ticker = ticker.upper()
    try:
        t = yf.Ticker(ticker)
        # earnings_dates returns a DataFrame indexed by date, both past and future
        df = t.earnings_dates
        if df is None or df.empty:
            return None
    except Exception as e:
        print(f"[earnings] error fetching {ticker}: {e}")
        return None

    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(days=lookahead_days)

    # Filter to future dates within lookahead
    future_dates = []
    for idx in df.index:
        # Index is timezone-aware in yfinance, but normalize defensively
        if hasattr(idx, 'to_pydatetime'):
            dt = idx.to_pydatetime()
        else:
            dt = idx
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if now <= dt <= cutoff:
            future_dates.append(dt)

    if not future_dates:
        return None

    next_dt = min(future_dates)
    days_until = (next_dt - now).total_seconds() / 86400  # seconds in a day

    # Heuristic: pre-market = before 9:30 ET, after-hours = after 16:00 ET
    # yfinance times are typically in market timezone (Eastern)
    hour_et = next_dt.hour  # may already be in ET depending on yfinance
    is_pre_market = hour_et < 9 or (hour_et == 9 and next_dt.minute < 30)
    is_after_hours = hour_et >= 16

    return {
        "ticker": ticker,
        "next_earnings_date": next_dt.isoformat(),
        "days_until": round(days_until, 1),
        "is_pre_market": is_pre_market,
        "is_after_hours": is_after_hours,
    }


def get_recent_earnings(ticker: str, lookback_days: int = 5) -> Optional[dict]:
    """
    Check if a ticker reported earnings in the recent past.
    Useful for confirming "this stock moved because of earnings."

    Returns dict similar to get_upcoming_earnings, or None.
    """
    ticker = ticker.upper()
    try:
        t = yf.Ticker(ticker)
        df = t.earnings_dates
        if df is None or df.empty:
            return None
    except Exception as e:
        print(f"[earnings] error fetching {ticker}: {e}")
        return None

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=lookback_days)

    past_dates = []
    for idx in df.index:
        if hasattr(idx, 'to_pydatetime'):
            dt = idx.to_pydatetime()
        else:
            dt = idx
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if cutoff <= dt <= now:
            past_dates.append(dt)

    if not past_dates:
        return None

    most_recent = max(past_dates)
    days_ago = (now - most_recent).total_seconds() / 86400

    return {
        "ticker": ticker,
        "last_earnings_date": most_recent.isoformat(),
        "days_ago": round(days_ago, 1),
    }


# --- standalone test entry point ---
if __name__ == "__main__":
    test_tickers = ["DOCN", "IPGP", "CYTK", "OSIS", "AEIS", "ADEA", "GXO", "ECG"]
    print(f"[earnings] testing against {len(test_tickers)} May 5 movers...")
    print()

    print("=== UPCOMING (next 14 days) ===")
    for ticker in test_tickers:
        result = get_upcoming_earnings(ticker, lookahead_days=14)
        if result:
            timing = "pre-market" if result["is_pre_market"] else ("after-hours" if result["is_after_hours"] else "during session")
            print(f"{ticker}: reports in {result['days_until']}d ({timing}) — {result['next_earnings_date'][:10]}")
        else:
            print(f"{ticker}: no upcoming earnings in window")
        time.sleep(0.1)  # be polite to Yahoo

    print()
    print("=== RECENT (last 5 days) ===")
    for ticker in test_tickers:
        result = get_recent_earnings(ticker, lookback_days=5)
        if result:
            print(f"{ticker}: reported {result['days_ago']}d ago — {result['last_earnings_date'][:10]}")
        else:
            print(f"{ticker}: no recent earnings")
        time.sleep(0.1)