"""
Earnings calendar — Screen 2 trigger foundation.

Screen 2 (pre-earnings filings read) fires on watchlist names with an
earnings date 3-7 *trading* days out: far enough ahead that there is
time to read filings and open a T-2 position, close enough that the
print is the dominant near-term catalyst.

This module does ONE job: given a universe of tickers, fetch each
name's next earnings date and return the ones whose date lands inside
the T+3..T+7 trading-day window. It does not read filings, score
candidates, or decide trades — that is the screen module's job.

yfinance earnings-date data is messy. Two sources exist and disagree
often:
  - Ticker.calendar          — dict, usually the most current single date
  - Ticker.get_earnings_dates() — DataFrame, includes estimates, can be
                                  stale or carry past dates
Strategy: `calendar` is primary, `get_earnings_dates()` is fallback.
When both yield a date and they disagree, the name is flagged so the
"verify on 5 known names" step can catch a bad source. When yfinance
itself marks the date an estimate, the entry is tagged accordingly.

Trading-day counting uses a hardcoded US market holiday list (see
US_MARKET_HOLIDAYS). Hardcoding keeps this module fully testable
offline (no network call to count days) at the cost of a one-line
yearly update. Update the list each January.

Standalone check (the roadmap's "verify on 5 known names" gate):
    python -m agent.earnings_calendar
"""
import datetime as dt
import time
from typing import Any, Optional

import yfinance as yf

from . import config


# ============================================================
# Trigger window — Screen 2 fires on names this far out.
# T+3..T+7 *trading* days, inclusive on both ends.
# ============================================================
TRIGGER_WINDOW_MIN_DAYS = 3
TRIGGER_WINDOW_MAX_DAYS = 7

# ============================================================
# US market holidays — full-day closures only.
# Half-days (e.g. day after Thanksgiving) are still trading days
# and are deliberately NOT listed here.
# UPDATE THIS LIST EACH JANUARY. Source: NYSE holiday calendar.
# ============================================================
US_MARKET_HOLIDAYS: set[dt.date] = {
    # 2026
    dt.date(2026, 1, 1),    # New Year's Day
    dt.date(2026, 1, 19),   # Martin Luther King Jr. Day
    dt.date(2026, 2, 16),   # Washington's Birthday
    dt.date(2026, 4, 3),    # Good Friday
    dt.date(2026, 5, 25),   # Memorial Day
    dt.date(2026, 6, 19),   # Juneteenth
    dt.date(2026, 7, 3),    # Independence Day (observed)
    dt.date(2026, 9, 7),    # Labor Day
    dt.date(2026, 11, 26),  # Thanksgiving
    dt.date(2026, 12, 25),  # Christmas
    # 2027 — early entries so a late-Dec run still counts forward
    # correctly across the year boundary.
    dt.date(2027, 1, 1),    # New Year's Day
    dt.date(2027, 1, 18),   # Martin Luther King Jr. Day
    dt.date(2027, 2, 15),   # Washington's Birthday
    dt.date(2027, 3, 26),   # Good Friday
    dt.date(2027, 5, 31),   # Memorial Day
    dt.date(2027, 6, 18),   # Juneteenth (observed)
    dt.date(2027, 7, 5),    # Independence Day (observed)
    dt.date(2027, 9, 6),    # Labor Day
    dt.date(2027, 11, 25),  # Thanksgiving
    dt.date(2027, 12, 24),  # Christmas (observed)
}


def is_trading_day(d: dt.date) -> bool:
    """True if d is a weekday and not a full-day market holiday."""
    if d.weekday() >= 5:  # 5 = Saturday, 6 = Sunday
        return False
    return d not in US_MARKET_HOLIDAYS


def trading_days_between(start: dt.date, end: dt.date) -> int:
    """
    Count trading days strictly after `start`, up to and including `end`.

    The count is T+N where T is `start`: the day after `start` is +1.
    `start` itself is never counted. If `end` is on or before `start`,
    the result is <= 0 (an earnings date that has already passed, or is
    today, is not in any future trigger window).

    Example: start=Mon, end=Thu, no holidays -> 3 (Tue, Wed, Thu).
    """
    if end <= start:
        # Negative/zero — earnings already passed or is today.
        # Sign is informative for callers; magnitude is not meaningful.
        return (end - start).days
    count = 0
    cursor = start
    while cursor < end:
        cursor += dt.timedelta(days=1)
        if is_trading_day(cursor):
            count += 1
    return count


def _coerce_date(value: Any) -> Optional[dt.date]:
    """
    Normalize the assorted date types yfinance returns into a date.

    Handles datetime.date, datetime.datetime, pandas Timestamp, and
    ISO-ish strings. Returns None on anything unrecognized rather than
    raising — a bad date from one ticker must not abort the scan.
    """
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    # pandas Timestamp exposes .to_pydatetime(); duck-type it.
    to_pydatetime = getattr(value, "to_pydatetime", None)
    if callable(to_pydatetime):
        try:
            return to_pydatetime().date()
        except Exception:
            return None
    if isinstance(value, str):
        text = value.strip()
        for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                return dt.datetime.strptime(text[:len(fmt) + 2], fmt).date()
            except ValueError:
                continue
        # Last resort: ISO parser.
        try:
            return dt.date.fromisoformat(text[:10])
        except ValueError:
            return None
    return None


def _next_date_from_calendar(t: "yf.Ticker", today: dt.date) -> Optional[dt.date]:
    """
    Primary source: Ticker.calendar.

    `calendar` is usually a dict with an "Earnings Date" key whose value
    is a date or a list of dates (yfinance gives a range when the date
    is not yet confirmed). Returns the earliest date on or after `today`,
    or None.
    """
    try:
        cal = t.calendar
    except Exception:
        return None
    if not cal or not isinstance(cal, dict):
        return None

    raw = cal.get("Earnings Date")
    if raw is None:
        return None

    candidates = raw if isinstance(raw, (list, tuple)) else [raw]
    future: list[dt.date] = []
    for item in candidates:
        d = _coerce_date(item)
        if d is not None and d >= today:
            future.append(d)
    return min(future) if future else None


def _next_date_from_earnings_dates(
    t: "yf.Ticker", today: dt.date
) -> tuple[Optional[dt.date], bool]:
    """
    Fallback source: Ticker.get_earnings_dates().

    Returns (date, is_estimate). `is_estimate` is True when the row has
    no reported EPS — i.e. the date is a forecast, not a confirmed
    historical print. Returns (None, False) on any failure.
    """
    try:
        df = t.get_earnings_dates(limit=12)
    except Exception:
        return None, False
    if df is None or len(df) == 0:
        return None, False

    best: Optional[dt.date] = None
    best_is_estimate = False
    # The DataFrame index is the earnings datetime.
    for idx, row in df.iterrows():
        d = _coerce_date(idx)
        if d is None or d < today:
            continue
        if best is None or d < best:
            best = d
            # "Reported EPS" present and non-null => already reported.
            reported = None
            try:
                reported = row.get("Reported EPS")
            except Exception:
                reported = None
            best_is_estimate = reported is None or _is_nan(reported)
    return best, best_is_estimate


def _is_nan(value: Any) -> bool:
    """True if value is a float NaN. Cheap, no numpy import needed."""
    return isinstance(value, float) and value != value


def fetch_next_earnings_date(ticker: str, today: dt.date) -> dict[str, Any]:
    """
    Fetch the next earnings date for one ticker.

    Returns a dict, always — never raises. Shape:
      {
        "ticker": str,
        "earnings_date": "YYYY-MM-DD" | None,
        "source": "calendar" | "earnings_dates" | "none",
        "is_estimate": bool,        # yfinance flagged the date a forecast
        "sources_disagree": bool,   # calendar and earnings_dates differ
        "error": str,               # present only on hard failure
      }

    `sources_disagree` is the signal the 5-name verification step
    watches: when both sources return a date and they differ, at least
    one is wrong and the name should be eyeballed.
    """
    result: dict[str, Any] = {
        "ticker": ticker,
        "earnings_date": None,
        "source": "none",
        "is_estimate": False,
        "sources_disagree": False,
    }
    try:
        t = yf.Ticker(ticker)
        cal_date = _next_date_from_calendar(t, today)
        ed_date, ed_is_estimate = _next_date_from_earnings_dates(t, today)

        if cal_date is not None and ed_date is not None:
            result["sources_disagree"] = cal_date != ed_date

        # `calendar` is primary; `get_earnings_dates()` is fallback.
        if cal_date is not None:
            result["earnings_date"] = cal_date.isoformat()
            result["source"] = "calendar"
            # calendar gives no estimate flag; if the fallback agreed on
            # the same date and called it an estimate, carry that through.
            if ed_date == cal_date:
                result["is_estimate"] = ed_is_estimate
        elif ed_date is not None:
            result["earnings_date"] = ed_date.isoformat()
            result["source"] = "earnings_dates"
            result["is_estimate"] = ed_is_estimate
    except Exception as e:
        result["error"] = str(e)
    return result


def find_triggered_names(
    universe: list[str],
    today: Optional[dt.date] = None,
    window_min: int = TRIGGER_WINDOW_MIN_DAYS,
    window_max: int = TRIGGER_WINDOW_MAX_DAYS,
) -> dict[str, Any]:
    """
    Scan a universe and return the names whose next earnings date lands
    inside the T+window_min .. T+window_max trading-day window.

    `today` defaults to the system date; pass an explicit date for
    deterministic tests.

    Returns:
      {
        "as_of": "YYYY-MM-DD",
        "window": [window_min, window_max],
        "triggered": [   # sorted by trading_days_out, ascending
          {ticker, earnings_date, trading_days_out, source,
           is_estimate, sources_disagree},
          ...
        ],
        "scanned": int,          # universe size
        "with_date": int,        # names yfinance returned a date for
        "errors": [{ticker, error}, ...],
      }
    """
    if today is None:
        today = dt.date.today()

    triggered: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    with_date = 0

    for ticker in universe:
        info = fetch_next_earnings_date(ticker, today)
        if "error" in info:
            errors.append({"ticker": ticker, "error": info["error"]})
            time.sleep(0.1)
            continue
        if info["earnings_date"] is None:
            time.sleep(0.1)
            continue

        with_date += 1
        ed = dt.date.fromisoformat(info["earnings_date"])
        days_out = trading_days_between(today, ed)
        if window_min <= days_out <= window_max:
            triggered.append({
                "ticker": ticker,
                "earnings_date": info["earnings_date"],
                "trading_days_out": days_out,
                "source": info["source"],
                "is_estimate": info["is_estimate"],
                "sources_disagree": info["sources_disagree"],
            })
        time.sleep(0.1)  # be polite to Yahoo

    triggered.sort(key=lambda x: x["trading_days_out"])
    return {
        "as_of": today.isoformat(),
        "window": [window_min, window_max],
        "triggered": triggered,
        "scanned": len(universe),
        "with_date": with_date,
        "errors": errors,
    }


# ============================================================
# Standalone verification — the roadmap's "verify accuracy on 5
# known names before relying" gate. Run:  python -m agent.earnings_calendar
#
# Picks 5 large, well-covered names whose earnings dates are easy to
# cross-check against any finance site. Prints each source's date and
# whether they agree. This is a human-eyeball check, not an assertion
# test — yfinance dates drift, so the goal is to SEE the data, not to
# pass/fail a hardcoded expectation.
# ============================================================
_VERIFY_NAMES = ["AAPL", "MSFT", "JPM", "WMT", "NVDA"]


def _self_check() -> None:
    today = dt.date.today()
    print(f"earnings_calendar self-check  —  as of {today.isoformat()}")
    print(f"trigger window: T+{TRIGGER_WINDOW_MIN_DAYS}"
          f"..T+{TRIGGER_WINDOW_MAX_DAYS} trading days\n")

    # Trading-day counter sanity check (offline, deterministic).
    mon = dt.date(2026, 5, 18)   # a Monday
    thu = dt.date(2026, 5, 21)   # that Thursday
    next_tue = dt.date(2026, 5, 26)  # Tue after Memorial Day (Mon 5/25)
    print("trading-day counter:")
    print(f"  Mon 5/18 -> Thu 5/21 = {trading_days_between(mon, thu)} "
          f"(expect 3)")
    print(f"  Mon 5/18 -> Tue 5/26 = {trading_days_between(mon, next_tue)} "
          f"(expect 5; Memorial Day Mon 5/25 skipped)\n")

    print("earnings dates for 5 known names "
          "(cross-check against a finance site):")
    for name in _VERIFY_NAMES:
        info = fetch_next_earnings_date(name, today)
        if "error" in info:
            print(f"  {name:6s}  ERROR: {info['error']}")
            time.sleep(0.1)
            continue
        ed = info["earnings_date"]
        if ed is None:
            print(f"  {name:6s}  no date returned by either source")
            time.sleep(0.1)
            continue
        days_out = trading_days_between(
            today, dt.date.fromisoformat(ed)
        )
        flags = []
        if info["is_estimate"]:
            flags.append("ESTIMATE")
        if info["sources_disagree"]:
            flags.append("SOURCES DISAGREE")
        flag_str = f"  [{', '.join(flags)}]" if flags else ""
        print(f"  {name:6s}  {ed}  (T+{days_out} trading days, "
              f"src={info['source']}){flag_str}")
        time.sleep(0.1)

    print("\nIf any 'SOURCES DISAGREE' appears, eyeball that name before "
          "trusting the scan.")


if __name__ == "__main__":
    _self_check()