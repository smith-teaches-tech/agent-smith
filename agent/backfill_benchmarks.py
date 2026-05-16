"""
agent-smith — one-off benchmark backfill (Build queue item 3, May 16, 2026).

NOT wired into the cron. This is a manual migration, run once:

    python -m agent.backfill_benchmarks            # all screens, dry-run
    python -m agent.backfill_benchmarks --write     # all screens, persist
    python -m agent.backfill_benchmarks --screen screen_0 --write

Why this exists
---------------
`portfolio.py` only captures SPY/IWM prices for trades opened AFTER the
benchmark feature shipped. Screen 0 already has closed trades (EXTR, AEIS)
and open positions with no benchmark data. Without a backfill, the Jun 6
Screen 0 probation decision would rest on too few alpha-bearing trades.

What it does
------------
For each screen's portfolio state file:
  - Open positions       -> add `benchmark_at_open` (open side only).
  - Closed positions     -> add `benchmark_at_open`, `benchmark_at_close`,
                            `benchmark_return_pct`, `alpha_pct`.
Records that already carry `benchmark_at_open` are left untouched, so the
script is safe to re-run and never clobbers live-captured data.

Honesty note on method
----------------------
Live trades capture the benchmark at the *next-session open* to match the
fill. This backfill keys off the stored `opened_at` / `closed_at` UTC
timestamps and uses the daily OPEN bar for the corresponding US trading
date. For multi-day holds the small reference difference is immaterial,
but backfilled numbers are therefore *close to*, not byte-identical with,
forward-captured ones. Backfilled records are tagged `benchmark_backfilled:
true` so this provenance is visible downstream.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yfinance as yf

from . import config
from .portfolio import _compute_benchmark_alpha


def _us_trading_date_for(ts_iso: str) -> str | None:
    """
    Map a stored UTC timestamp to the US trading date whose OPEN best
    represents the trade fill.

    The bot runs from AST; a cron firing late in the UTC day fills at the
    *next* US session open. Heuristic: timestamps at or after 18:00 UTC
    (roughly 13:00 ET, i.e. mid-session or later) are treated as filling
    the next trading day's open; earlier timestamps fill the same day.
    Weekends roll forward to Monday. This mirrors portfolio.py's
    "next regular-session open" fill rule closely enough for a multi-day
    hold; exact intraday precision is not recoverable retroactively.
    """
    try:
        dt = datetime.fromisoformat(ts_iso.replace("Z", "+00:00")).astimezone(
            timezone.utc
        )
    except (ValueError, AttributeError):
        return None
    d = dt.date()
    if dt.hour >= 18:
        d = d + timedelta(days=1)
    # Roll weekends forward to Monday.
    while d.weekday() >= 5:  # 5 = Sat, 6 = Sun
        d = d + timedelta(days=1)
    return d.isoformat()


def _date_has_completed(date_iso: str) -> bool:
    """
    True only if `date_iso` is a US trading date whose session has
    finished — i.e. there should be a daily bar for it by now.

    The backfill maps Friday-evening (AST) trade timestamps to the
    following Monday's open. Run on a weekend, that Monday is still in
    the future and yfinance has no bar for it yet. Such records must be
    SKIPPED, not guessed — the live `execute_buy` / `execute_sell` path
    will capture their benchmark prices accurately when that session
    actually runs. Backfilling them with a stale earlier price would be
    worse than leaving them for the live path.

    Conservative rule: a date counts as completed only if it is strictly
    before today's UTC date. Today's own session may not have closed (or
    even opened) at the moment the backfill runs, so today is treated as
    not-yet-available too. This errs toward skipping a borderline record
    rather than backfilling it with an incomplete bar.
    """
    try:
        d = datetime.fromisoformat(date_iso).date()
    except (ValueError, AttributeError):
        return False
    return d < datetime.now(timezone.utc).date()


def _benchmark_opens_on(date_iso: str, cache: dict) -> dict[str, float | None]:
    """
    Daily OPEN price for each benchmark on a given US trading date.
    Results are cached per (ticker, date) so a screen with many trades
    on nearby dates does not re-hit yfinance. Missing data -> None.
    """
    out: dict[str, float | None] = {}
    for bm in config.BENCHMARK_TICKERS:
        key = (bm, date_iso)
        if key in cache:
            out[bm] = cache[key]
            continue
        price: float | None = None
        try:
            start = datetime.fromisoformat(date_iso).date()
            end = start + timedelta(days=1)
            hist = yf.Ticker(bm).history(
                start=start.isoformat(), end=end.isoformat(), interval="1d"
            )
            if len(hist) > 0:
                price = round(float(hist.iloc[0]["Open"]), 4)
        except Exception as e:  # noqa: BLE001 — best-effort migration
            print(f"  WARN: {bm} open fetch failed for {date_iso}: {e}")
        cache[key] = price
        out[bm] = price
    return out


def _backfill_screen(screen_id: str, write: bool) -> dict[str, int]:
    """Backfill one screen's state file. Returns a small counts summary."""
    paths = config.screen_paths(screen_id)
    path = Path(paths["portfolio"])
    counts = {"open_done": 0, "open_skip": 0, "closed_done": 0, "closed_skip": 0}

    if not path.exists():
        print(f"[{screen_id}] no state file at {path} — skipping")
        return counts

    state = json.loads(path.read_text())
    price_cache: dict = {}

    for pos in state.get("open_positions", []):
        if pos.get("benchmark_at_open"):
            counts["open_skip"] += 1
            continue
        date_iso = _us_trading_date_for(pos.get("opened_at", ""))
        if date_iso is None:
            print(f"  [{screen_id}] {pos.get('ticker')}: unparseable opened_at — skipped")
            counts["open_skip"] += 1
            continue
        if not _date_has_completed(date_iso):
            print(
                f"  [{screen_id}] open  {pos.get('ticker'):6s} "
                f"fill date {date_iso} not yet traded — skipped "
                f"(live execute_buy will capture it)"
            )
            counts["open_skip"] += 1
            continue
        pos["benchmark_at_open"] = _benchmark_opens_on(date_iso, price_cache)
        pos["benchmark_backfilled"] = True
        counts["open_done"] += 1
        print(f"  [{screen_id}] open  {pos.get('ticker'):6s} open@{date_iso} -> {pos['benchmark_at_open']}")

    for clo in state.get("closed_positions", []):
        if clo.get("benchmark_at_open"):
            counts["closed_skip"] += 1
            continue
        open_date = _us_trading_date_for(clo.get("opened_at", ""))
        close_date = _us_trading_date_for(clo.get("closed_at", ""))
        if open_date is None or close_date is None:
            print(f"  [{screen_id}] {clo.get('ticker')}: unparseable dates — skipped")
            counts["closed_skip"] += 1
            continue
        if not _date_has_completed(open_date) or not _date_has_completed(close_date):
            # A closed trade needs both ends to compute alpha. If the
            # close date maps into the future (e.g. a Friday-evening exit
            # backfilled over the weekend), skip the whole record and
            # re-run the backfill once that session has traded.
            print(
                f"  [{screen_id}] close {clo.get('ticker'):6s} "
                f"window {open_date}->{close_date} not fully traded yet — "
                f"skipped (re-run after that session)"
            )
            counts["closed_skip"] += 1
            continue
        bm_open = _benchmark_opens_on(open_date, price_cache)
        bm_close = _benchmark_opens_on(close_date, price_cache)
        clo["benchmark_at_open"] = bm_open
        clo.update(
            _compute_benchmark_alpha(bm_open, bm_close, clo.get("realized_pct", 0.0))
        )
        clo["benchmark_backfilled"] = True
        counts["closed_done"] += 1
        print(
            f"  [{screen_id}] close {clo.get('ticker'):6s} "
            f"{open_date}->{close_date}  alpha={clo['alpha_pct']}"
        )

    if write:
        path.write_text(json.dumps(state, indent=2, ensure_ascii=False))
        print(f"[{screen_id}] WRITTEN to {path}")
    else:
        print(f"[{screen_id}] dry-run — no file written (pass --write to persist)")
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backfill benchmark data onto existing trades.")
    parser.add_argument("--screen", default=None, help="Single screen id; default = all registered.")
    parser.add_argument("--write", action="store_true", help="Persist changes; omit for a dry run.")
    args = parser.parse_args(argv)

    screen_ids = [args.screen] if args.screen else [s["id"] for s in config.SCREENS]
    grand: dict[str, int] = {"open_done": 0, "open_skip": 0, "closed_done": 0, "closed_skip": 0}
    for sid in screen_ids:
        c = _backfill_screen(sid, write=args.write)
        for k in grand:
            grand[k] += c[k]
        print()

    print(
        f"TOTAL  open: {grand['open_done']} filled / {grand['open_skip']} skipped   "
        f"closed: {grand['closed_done']} filled / {grand['closed_skip']} skipped"
    )
    if not args.write:
        print("Dry run only. Re-run with --write to persist.")
    return 0


if __name__ == "__main__":
    sys.exit(main())