"""
agent-smith paper portfolio (Phase 1.5-lite + F1 multi-screen).

State machine that:
  - Reads/writes docs/data/portfolios/{screen_id}.json          (current state per screen)
  - Reads/writes docs/data/portfolios/{screen_id}_history.json  (append-only audit log per screen)
  - Marks positions to market via yfinance on every run
  - Applies buy/sell decisions from Claude's portfolio pass
  - Enforces guardrails: cash ≥ 0, position pct, sector pct, min cash

F1 multi-screen note: every public function accepts an optional `screen_id`
keyword argument that defaults to `config.DEFAULT_SCREEN_ID` ("screen_0").
With one registered screen, behavior is byte-identical to pre-F1. Adding
screens (F2+) requires no portfolio.py changes — the new screen just calls
the same functions with its own screen_id and gets its own state file.

The mutation logic itself (sizing, fees, mark-to-market, FIFO sells) is
screen-agnostic and operates on whatever `state` dict it's handed.

Executes paper trades at the **next US regular-session open** after a decision.
That's appropriate given Michael runs the bot from Saudi Arabia (AST) and the
22:00 AST portfolio pass fires ~1 hour before the NYSE close — decisions made
then can't realistically fill same-session.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Iterable

import yfinance as yf

from . import config


# ============================================================
# File I/O — screen-aware
# ============================================================

def _resolve_paths(screen_id: str | None) -> dict[str, str]:
    """
    Resolve the (portfolio, history) file paths for a screen.
    None falls back to DEFAULT_SCREEN_ID so callers that pre-date
    F1 keep working without modification.
    """
    sid = screen_id or config.DEFAULT_SCREEN_ID
    return config.screen_paths(sid)


def _ensure_dirs(screen_id: str | None = None) -> None:
    paths = _resolve_paths(screen_id)
    Path(paths["portfolio"]).parent.mkdir(parents=True, exist_ok=True)


def _empty_state(screen_id: str | None = None) -> dict[str, Any]:
    """
    Construct a fresh empty state for a given screen. The bankroll is
    drawn from the screen's own config entry so per-screen bankrolls
    are honored on first initialization.
    """
    sid = screen_id or config.DEFAULT_SCREEN_ID
    screen = config.get_screen(sid)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "screen_id": sid,
        "bankroll_start": screen["bankroll"],
        "cash": screen["bankroll"],
        "open_positions": [],
        "closed_positions": [],
        "trade_log": [],
    }


def load_state(screen_id: str | None = None) -> dict[str, Any]:
    """Load current portfolio state for a screen, initializing if absent."""
    paths = _resolve_paths(screen_id)
    path = Path(paths["portfolio"])
    if not path.exists():
        return _empty_state(screen_id)
    try:
        state = json.loads(path.read_text())
        # Backfill any missing keys from older versions. screen_id is
        # backfilled from the parameter, not the stored value, so a
        # mis-located file (renamed across screens) gets corrected on
        # next save.
        for k, v in _empty_state(screen_id).items():
            state.setdefault(k, v)
        # Ensure stored screen_id matches the one we loaded from.
        # Defensive — catches the case where someone copies a file
        # under a different name without re-stamping.
        state["screen_id"] = screen_id or config.DEFAULT_SCREEN_ID
        return state
    except (json.JSONDecodeError, OSError) as e:
        print(f"[portfolio] WARN: could not parse {path}: {e}. Starting fresh.")
        return _empty_state(screen_id)


def save_state(state: dict[str, Any], screen_id: str | None = None) -> None:
    """
    Persist state to the screen's portfolio file. screen_id is taken
    from the parameter if supplied, else from the state dict, else
    falls back to DEFAULT_SCREEN_ID.
    """
    sid = screen_id or state.get("screen_id") or config.DEFAULT_SCREEN_ID
    _ensure_dirs(sid)
    state["generated_at"] = datetime.now(timezone.utc).isoformat()
    state["screen_id"] = sid  # keep the file's own claim consistent
    paths = _resolve_paths(sid)
    Path(paths["portfolio"]).write_text(
        json.dumps(state, indent=2, ensure_ascii=False)
    )


def append_history(event: dict[str, Any], screen_id: str | None = None) -> None:
    """Append-only audit log for a screen. We never rewrite earlier entries."""
    sid = screen_id or config.DEFAULT_SCREEN_ID
    _ensure_dirs(sid)
    paths = _resolve_paths(sid)
    path = Path(paths["history"])
    history: list[dict[str, Any]] = []
    if path.exists():
        try:
            history = json.loads(path.read_text())
            if not isinstance(history, list):
                history = []
        except (json.JSONDecodeError, OSError):
            history = []
    event = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "screen_id": sid,
        **event,
    }
    history.append(event)
    path.write_text(json.dumps(history, indent=2, ensure_ascii=False))


# ============================================================
# Fee model — IBKR Pro Tiered
# ============================================================

def compute_fees(
    shares: int,
    price: float,
    side: str,  # "BUY" or "SELL"
) -> float:
    """
    Calculate realistic IBKR Pro Tiered fees for a single order.
    Returns total fees in USD, always a positive number.

    Components:
      - Commission:  max($0.0035 × shares, $0.35), capped at 1% of notional
      - Pass-through: NYSE per-share + clearing per-share (both sides)
      - Sells only:  FINRA TAF per-share + SEC fee (% of notional)

    These are paper estimates — real fills on low-float names can deviate,
    but this is within a penny or two of reality on blue-chip volume.
    """
    if shares <= 0 or price <= 0:
        return 0.0
    notional = shares * price

    # Commission
    commission = max(
        shares * config.IBKR_COMMISSION_PER_SHARE,
        config.IBKR_COMMISSION_MIN,
    )
    commission = min(commission, notional * config.IBKR_COMMISSION_MAX_PCT)

    # Pass-through fees (both sides)
    passthru = shares * (
        config.IBKR_NYSE_PASSTHRU_PER_SHARE
        + config.IBKR_CLEARING_PER_SHARE
    )

    # Sell-only regulatory fees
    reg_fees = 0.0
    if side == "SELL":
        reg_fees = (
            shares * config.IBKR_FINRA_TAF_PER_SHARE
            + notional * config.IBKR_SEC_FEE_PCT
        )

    return round(commission + passthru + reg_fees, 4)


def apply_slippage(price: float, side: str) -> float:
    """Adverse slippage: pay up on buys, sell down on sells."""
    slip = config.PAPER_SLIPPAGE_PCT
    if side == "BUY":
        return round(price * (1 + slip), 4)
    return round(price * (1 - slip), 4)


# ============================================================
# Mark-to-market
# ============================================================

def fetch_current_prices(tickers: Iterable[str]) -> dict[str, float | None]:
    """
    Fetch most recent close for each ticker. Returns None for any that fail —
    callers must cope with missing prices rather than crashing the run.
    """
    out: dict[str, float | None] = {}
    for tkr in set(tickers):
        try:
            t = yf.Ticker(tkr)
            hist = t.history(period="2d", interval="1d")
            if len(hist) == 0:
                out[tkr] = None
                continue
            out[tkr] = round(float(hist.iloc[-1]["Close"]), 4)
        except Exception as e:
            print(f"[portfolio] WARN: price fetch failed for {tkr}: {e}")
            out[tkr] = None
    return out


def mark_to_market(state: dict[str, Any]) -> dict[str, Any]:
    """Update current_price / value / unrealized P&L on every open position."""
    if not state["open_positions"]:
        return state
    prices = fetch_current_prices(p["ticker"] for p in state["open_positions"])
    for pos in state["open_positions"]:
        current = prices.get(pos["ticker"])
        if current is None:
            # Leave last-known price; note the stale read.
            pos["price_stale"] = True
            continue
        pos["price_stale"] = False
        pos["current_price"] = current
        pos["value"] = round(pos["shares"] * current, 2)
        pos["unrealized_pnl"] = round(pos["value"] - pos["cost_total"], 2)
        pos["unrealized_pct"] = (
            round((pos["unrealized_pnl"] / pos["cost_total"]) * 100, 2)
            if pos["cost_total"]
            else 0.0
        )
        # Days held counter
        try:
            opened = datetime.fromisoformat(pos["opened_at"].replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            pos["days_held"] = max(0, (now - opened).days)
        except (KeyError, ValueError):
            pos.setdefault("days_held", 0)
    return state


# ============================================================
# Guardrails
# ============================================================

def total_equity(state: dict[str, Any]) -> float:
    invested = sum(p.get("value", 0.0) for p in state["open_positions"])
    return state["cash"] + invested


def check_buy_allowed(
    state: dict[str, Any],
    ticker: str,
    sector: str | None,
    price: float,
    shares: int,
) -> tuple[bool, str]:
    """
    Enforce position/sector/cash limits BEFORE executing a paper buy.
    Returns (allowed, reason). Reason is human-readable and safe to write
    into suggestions.json when a buy is blocked.
    """
    if shares <= 0:
        return False, "shares must be positive"
    if price <= 0:
        return False, "price must be positive"

    fill_price = apply_slippage(price, "BUY")
    notional = shares * fill_price
    fees = compute_fees(shares, fill_price, "BUY")
    total_cost = notional + fees

    # Cash check — cash can never go negative.
    if total_cost > state["cash"]:
        return False, (
            f"insufficient cash: need ${total_cost:.2f}, have ${state['cash']:.2f}"
        )

    # Min cash reserve after the trade
    equity = total_equity(state)  # includes current cash
    projected_cash = state["cash"] - total_cost
    min_cash_required = equity * config.PAPER_PORTFOLIO_MIN_CASH_PCT
    if projected_cash < min_cash_required:
        return False, (
            f"would breach min cash reserve: projected cash ${projected_cash:.2f} "
            f"< required ${min_cash_required:.2f} ({config.PAPER_PORTFOLIO_MIN_CASH_PCT*100:.0f}% of equity)"
        )

    # Position size limit — existing position + new add combined
    existing_value = sum(
        p.get("value", 0.0)
        for p in state["open_positions"]
        if p["ticker"] == ticker
    )
    projected_position_value = existing_value + notional
    max_position = equity * config.PAPER_PORTFOLIO_MAX_POSITION_PCT
    if projected_position_value > max_position:
        return False, (
            f"would breach max position size: projected {ticker} value "
            f"${projected_position_value:.2f} > limit ${max_position:.2f} "
            f"({config.PAPER_PORTFOLIO_MAX_POSITION_PCT*100:.0f}%)"
        )

    # Sector limit — only enforced when sector is known.
    if sector:
        sector_value = sum(
            p.get("value", 0.0)
            for p in state["open_positions"]
            if p.get("sector") == sector
        )
        projected_sector_value = sector_value + notional
        max_sector = equity * config.PAPER_PORTFOLIO_MAX_SECTOR_PCT
        if projected_sector_value > max_sector:
            return False, (
                f"would breach max sector exposure ({sector}): projected "
                f"${projected_sector_value:.2f} > limit ${max_sector:.2f} "
                f"({config.PAPER_PORTFOLIO_MAX_SECTOR_PCT*100:.0f}%)"
            )

    return True, "ok"


# ============================================================
# Trade execution
# ============================================================

def _fetch_next_open_price(ticker: str) -> float | None:
    """
    Look up the next regular-session open price for a ticker.
    Returns None if we don't yet have one (weekend, holiday, or the next
    session hasn't printed yet) — caller should then defer execution.
    """
    try:
        t = yf.Ticker(ticker)
        # Get the last few days of daily bars; the most recent Open is our fill.
        hist = t.history(period="5d", interval="1d")
        if len(hist) == 0:
            return None
        return round(float(hist.iloc[-1]["Open"]), 4)
    except Exception as e:
        print(f"[portfolio] WARN: open-price fetch failed for {ticker}: {e}")
        return None


def _fetch_benchmark_opens() -> dict[str, float | None]:
    """
    Capture the next-session open for each benchmark in config.BENCHMARK_TICKERS.

    Uses the same `_fetch_next_open_price` path the traded ticker uses, so
    the benchmark reference is the same kind of price (next regular-session
    open) as the trade fill — keeping the alpha comparison honest.

    Best-effort: a benchmark that fails to fetch is stored as None. The
    caller must never gate a trade on this returning complete data.
    """
    return {bm: _fetch_next_open_price(bm) for bm in config.BENCHMARK_TICKERS}


def _fetch_benchmark_closes() -> dict[str, float | None]:
    """
    Capture the most-recent close for each benchmark, for use when a
    position is sold. Mirror of `_fetch_benchmark_opens` for the exit side.

    Sells fill at the next-session open too, but at sell time the cleanest
    available reference is the latest close via `fetch_current_prices`;
    the small open-vs-close mismatch is immaterial against a multi-day
    hold and keeps the exit path from needing a second open-price lookup.
    """
    prices = fetch_current_prices(config.BENCHMARK_TICKERS)
    return {bm: prices.get(bm) for bm in config.BENCHMARK_TICKERS}


def _fetch_market_cap(ticker: str) -> float | None:
    """
    Best-effort market cap lookup for a ticker, in USD.

    Used at sell time to record `market_cap_at_close`, which the dashboard
    reads to decide whether SPY or IWM is the primary benchmark for that
    trade (per config.BENCHMARK_PRIMARY_CAP_USD). A failed lookup returns
    None — the dashboard then falls back to SPY as primary, so this never
    gates a sell.
    """
    try:
        info = yf.Ticker(ticker).info
        cap = info.get("marketCap")
        return float(cap) if cap else None
    except Exception as e:  # noqa: BLE001 — best-effort, never blocks a sell
        print(f"[portfolio] WARN: market cap fetch failed for {ticker}: {e}")
        return None


def _compute_benchmark_alpha(
    benchmark_at_open: dict[str, float | None] | None,
    benchmark_at_close: dict[str, float | None] | None,
    realized_pct: float,
) -> dict[str, Any]:
    """
    Compute alpha vs each benchmark: the position's realized return minus
    the benchmark's own return over the same open->close window.

    Returns a dict shaped:
      {
        "benchmark_at_close": {"SPY": x, "IWM": y},   # echoed for the record
        "benchmark_return_pct": {"SPY": ..., "IWM": ...},
        "alpha_pct": {"SPY": ..., "IWM": ...},
      }

    Any benchmark missing an open OR a close price yields null for that
    benchmark's return and alpha — pre-change positions (no benchmark_at_open)
    and failed fetches both degrade gracefully to null rather than crashing.
    """
    benchmark_at_open = benchmark_at_open or {}
    benchmark_at_close = benchmark_at_close or {}
    bench_return: dict[str, float | None] = {}
    alpha: dict[str, float | None] = {}

    for bm in config.BENCHMARK_TICKERS:
        p_open = benchmark_at_open.get(bm)
        p_close = benchmark_at_close.get(bm)
        if p_open and p_close and p_open > 0:
            ret = round(((p_close - p_open) / p_open) * 100, 2)
            bench_return[bm] = ret
            alpha[bm] = round(realized_pct - ret, 2)
        else:
            bench_return[bm] = None
            alpha[bm] = None

    return {
        "benchmark_at_close": {
            bm: benchmark_at_close.get(bm) for bm in config.BENCHMARK_TICKERS
        },
        "benchmark_return_pct": bench_return,
        "alpha_pct": alpha,
    }


def execute_buy(
    state: dict[str, Any],
    *,
    ticker: str,
    name: str,
    sector: str | None,
    shares: int,
    flag_classification: str,
    flag_confidence: int,
    flag_horizon: str,
    thesis: str,
    catalyst: str | None = None,
    reference_price: float | None = None,
    screen_id: str | None = None,
    tier: str | None = None,
) -> tuple[bool, str, dict[str, Any] | None]:
    """
    Execute a BUY at the next US regular-session open. Returns
    (success, reason, trade_dict). On failure, state is unchanged.

    screen_id routes the audit-log entry to the correct per-screen
    history file. Defaults to the state's stored screen_id, or
    DEFAULT_SCREEN_ID if state has none (legacy state files).

    tier: "conviction" | "exploratory" | None. Persisted to the position
    record for downstream cap enforcement (main.py counts open
    exploratory positions before opening a new one) and dashboard
    display. None defaults to "conviction" on read for backward
    compatibility with pre-May-12 positions that have no tier field.
    """
    sid = screen_id or state.get("screen_id") or config.DEFAULT_SCREEN_ID
    next_open = _fetch_next_open_price(ticker)
    if next_open is None:
        return False, "no next-open price available yet; deferring", None

    # Capture benchmark opens at the same moment as the trade fill.
    # Best-effort — a failed fetch stores null and the buy still proceeds.
    benchmark_at_open = _fetch_benchmark_opens()

    # Apply slippage and fees on top of the open print.
    fill = apply_slippage(next_open, "BUY")
    fees = compute_fees(shares, fill, "BUY")
    notional = round(shares * fill, 2)
    total_cost = round(notional + fees, 2)

    # Re-check guardrails using the actual fill price, not the reference.
    allowed, reason = check_buy_allowed(state, ticker, sector, fill, shares)
    if not allowed:
        return False, reason, None

    # Mutate state atomically at the end, after all checks pass.
    state["cash"] = round(state["cash"] - total_cost, 2)

    position = {
        "ticker": ticker,
        "name": name,
        "sector": sector,
        "shares": shares,
        "cost_basis": fill,
        "cost_total": total_cost,   # includes fees — so P&L reads honestly
        "current_price": fill,
        "value": notional,
        "unrealized_pnl": round(notional - total_cost, 2),  # will be -fees initially
        "unrealized_pct": round(((notional - total_cost) / total_cost) * 100, 2),
        "opened_at": datetime.now(timezone.utc).isoformat(),
        "days_held": 0,
        "flag_classification": flag_classification,
        "flag_confidence": flag_confidence,
        "flag_horizon": flag_horizon,
        "thesis": thesis,
        "thesis_status": "intact",
        "latest_reasoning": f"Opened position at ${fill:.2f}, +${fees:.2f} fees.",
        "next_action": "HOLD",
        "catalyst": catalyst,
        "tier": tier or "conviction",
        # Benchmark prices captured at open. Read back at sell time to
        # compute alpha. Values may be null if a fetch failed.
        "benchmark_at_open": benchmark_at_open,
    }
    state["open_positions"].append(position)

    trade = {
        "date": datetime.now(timezone.utc).isoformat(),
        "side": "BUY",
        "ticker": ticker,
        "shares": shares,
        "fill_price": fill,
        "fees": fees,
        "notional": notional,
        "why": f"Opened per flag: {flag_classification} conf {flag_confidence} — {catalyst or 'thesis'}",
    }
    state["trade_log"].insert(0, trade)  # newest first
    append_history({"kind": "buy", **trade}, screen_id=sid)
    return True, "ok", trade


def execute_sell(
    state: dict[str, Any],
    *,
    ticker: str,
    shares: int | None = None,  # None = sell all
    exit_reasoning: str = "",
    screen_id: str | None = None,
) -> tuple[bool, str, dict[str, Any] | None]:
    """
    Execute a SELL at the next US regular-session open. Closes (or trims)
    a single position. Returns (success, reason, trade_dict).

    If the portfolio holds multiple lots of the same ticker (not currently
    allowed by the BUY path but guarded here anyway), we FIFO through them.

    screen_id routes the audit-log entry to the correct per-screen
    history file. Defaults to the state's stored screen_id, or
    DEFAULT_SCREEN_ID if state has none (legacy state files).
    """
    sid = screen_id or state.get("screen_id") or config.DEFAULT_SCREEN_ID
    positions = [p for p in state["open_positions"] if p["ticker"] == ticker]
    if not positions:
        return False, f"no open position in {ticker}", None

    next_open = _fetch_next_open_price(ticker)
    if next_open is None:
        return False, "no next-open price available yet; deferring", None

    fill = apply_slippage(next_open, "SELL")

    # Capture benchmark closes once for this sell — every closed lot
    # produced below shares the same exit-side benchmark reference.
    benchmark_at_close = _fetch_benchmark_closes()
    # Market cap at close — lets the dashboard pick SPY vs IWM as the
    # primary benchmark for this trade. Best-effort; None -> SPY default.
    market_cap_at_close = _fetch_market_cap(ticker)

    # Determine share count: None → sell whole position (all lots).
    total_open = sum(p["shares"] for p in positions)
    shares_to_sell = total_open if shares is None else min(shares, total_open)
    if shares_to_sell <= 0:
        return False, "no shares to sell", None

    fees = compute_fees(shares_to_sell, fill, "SELL")
    proceeds_gross = round(shares_to_sell * fill, 2)
    proceeds_net = round(proceeds_gross - fees, 2)
    state["cash"] = round(state["cash"] + proceeds_net, 2)

    # Allocate the sale across FIFO lots.
    remaining = shares_to_sell
    total_cost_sold = 0.0
    fully_closed_positions: list[dict[str, Any]] = []
    positions_sorted = sorted(positions, key=lambda p: p.get("opened_at", ""))

    for pos in positions_sorted:
        if remaining <= 0:
            break
        if pos["shares"] <= remaining:
            # Close this lot entirely.
            lot_cost = pos["cost_total"]
            total_cost_sold += lot_cost
            remaining -= pos["shares"]
            closed = {
                "ticker": pos["ticker"],
                "name": pos.get("name", pos["ticker"]),
                "sector": pos.get("sector"),
                "shares": pos["shares"],
                "avg_cost": pos["cost_basis"],
                "exit_price": fill,
                "opened_at": pos["opened_at"],
                "closed_at": datetime.now(timezone.utc).isoformat(),
                "days_held": pos.get("days_held", 0),
                "realized_pnl": round(pos["shares"] * fill - lot_cost, 2),
                "realized_pct": round(
                    ((pos["shares"] * fill - lot_cost) / lot_cost) * 100, 2
                ) if lot_cost else 0.0,
                "fees_total": fees,  # fees from this sell (all attributed here for simplicity)
                "flag_classification": pos.get("flag_classification"),
                "flag_confidence": pos.get("flag_confidence"),
                "tier": pos.get("tier", "conviction"),
                "exit_reasoning": exit_reasoning,
            }
            closed["benchmark_at_open"] = pos.get("benchmark_at_open")
            closed["market_cap_at_close"] = market_cap_at_close
            closed.update(
                _compute_benchmark_alpha(
                    pos.get("benchmark_at_open"),
                    benchmark_at_close,
                    closed["realized_pct"],
                )
            )
            fully_closed_positions.append(closed)
            state["open_positions"].remove(pos)
        else:
            # Partial sale — leave lot with fewer shares and pro-rated cost.
            pct_sold = remaining / pos["shares"]
            cost_portion = pos["cost_total"] * pct_sold
            total_cost_sold += cost_portion
            pos["shares"] -= remaining
            pos["cost_total"] = round(pos["cost_total"] - cost_portion, 2)
            pos["value"] = round(pos["shares"] * pos["current_price"], 2)
            pos["unrealized_pnl"] = round(pos["value"] - pos["cost_total"], 2)
            pos["unrealized_pct"] = (
                round((pos["unrealized_pnl"] / pos["cost_total"]) * 100, 2)
                if pos["cost_total"] else 0.0
            )
            remaining = 0
            # Log a partial-close record under closed_positions too,
            # so the UI timeline shows the trim.
            partial = {
                "ticker": pos["ticker"],
                "name": pos.get("name", pos["ticker"]),
                "sector": pos.get("sector"),
                "shares": shares_to_sell - (shares_to_sell - remaining),
                "avg_cost": pos["cost_basis"],
                "exit_price": fill,
                "opened_at": pos["opened_at"],
                "closed_at": datetime.now(timezone.utc).isoformat(),
                "days_held": pos.get("days_held", 0),
                "realized_pnl": round(shares_to_sell * fill - cost_portion, 2),
                "realized_pct": round(
                    ((shares_to_sell * fill - cost_portion) / cost_portion) * 100, 2
                ) if cost_portion else 0.0,
                "fees_total": fees,
                "flag_classification": pos.get("flag_classification"),
                "flag_confidence": pos.get("flag_confidence"),
                "tier": pos.get("tier", "conviction"),
                "exit_reasoning": exit_reasoning + " (partial)",
            }
            partial["benchmark_at_open"] = pos.get("benchmark_at_open")
            partial["market_cap_at_close"] = market_cap_at_close
            partial.update(
                _compute_benchmark_alpha(
                    pos.get("benchmark_at_open"),
                    benchmark_at_close,
                    partial["realized_pct"],
                )
            )
            fully_closed_positions.append(partial)

    state["closed_positions"] = fully_closed_positions + state["closed_positions"]

    trade = {
        "date": datetime.now(timezone.utc).isoformat(),
        "side": "SELL",
        "ticker": ticker,
        "shares": shares_to_sell,
        "fill_price": fill,
        "fees": fees,
        "notional": proceeds_gross,
        "why": exit_reasoning or "closed position",
    }
    state["trade_log"].insert(0, trade)
    append_history({"kind": "sell", **trade}, screen_id=sid)
    return True, "ok", trade


# ============================================================
# Position sizing helper
# ============================================================

def size_position(
    state: dict[str, Any],
    *,
    price: float,
    sector: str | None,
    confidence: int,
    target_pct_override: float | None = None,
) -> int:
    """
    Pick a share count for a new buy that respects ALL guardrails
    and scales with confidence (or a caller-supplied override).

    Confidence multipliers on the base allocation:
      conf 5 → 25% of equity (hits position cap)
      conf 4 → 20%
      conf 3 → 15%  (the minimum for a buy)
      conf 2 → 10%  (not used — min_buy_confidence is 3)
      conf 1 → 5%

    target_pct_override: if set, bypass the confidence multiplier and
    use this percentage of equity as the target notional. Used by the
    exploratory tier (6% per position, regardless of confidence).
    All position/sector/cash guardrails still apply on top — the
    override only changes the *desired* sizing, not the caps.
    """
    if price <= 0:
        return 0

    equity = total_equity(state)
    if target_pct_override is not None:
        target_pct = target_pct_override
    else:
        multiplier_by_conf = {5: 0.25, 4: 0.20, 3: 0.15, 2: 0.10, 1: 0.05}
        target_pct = multiplier_by_conf.get(int(confidence), 0.10)
    target_notional = equity * target_pct

    # Respect the max position ceiling no matter what.
    max_notional = equity * config.PAPER_PORTFOLIO_MAX_POSITION_PCT
    target_notional = min(target_notional, max_notional)

    # Respect the sector ceiling.
    if sector:
        sector_value = sum(
            p.get("value", 0.0)
            for p in state["open_positions"]
            if p.get("sector") == sector
        )
        sector_headroom = max(
            0.0, equity * config.PAPER_PORTFOLIO_MAX_SECTOR_PCT - sector_value
        )
        target_notional = min(target_notional, sector_headroom)

    # Respect cash-on-hand, including the min-cash reserve.
    min_cash_required = equity * config.PAPER_PORTFOLIO_MIN_CASH_PCT
    cash_headroom = max(0.0, state["cash"] - min_cash_required)
    # We'll pay roughly price×shares + ~0.5% for fees+slippage. Budget for it.
    budget = cash_headroom / (1 + config.PAPER_SLIPPAGE_PCT + 0.005)
    target_notional = min(target_notional, budget)

    shares = math.floor(target_notional / apply_slippage(price, "BUY"))
    return max(0, shares)


# ============================================================
# Public entrypoint for main.py
# ============================================================

def refresh(
    state: dict[str, Any] | None = None,
    screen_id: str | None = None,
) -> dict[str, Any]:
    """
    Called every run. Marks positions to market and saves.
    Does NOT make new trading decisions — that's the portfolio pass's job.

    screen_id picks which screen's portfolio file to refresh. Defaults to
    DEFAULT_SCREEN_ID. When state is supplied, screen_id is read from
    state["screen_id"] if not explicitly passed.
    """
    sid = screen_id or (state.get("screen_id") if state else None) or config.DEFAULT_SCREEN_ID
    if state is None:
        state = load_state(screen_id=sid)
    state = mark_to_market(state)
    save_state(state, screen_id=sid)
    return state


def refresh_all() -> list[dict[str, Any]]:
    """
    Refresh every registered screen's portfolio. Convenience for the
    main orchestrator's non-portfolio-pass path (mark-to-market on every
    run regardless of which run it is). Returns the list of refreshed
    states in registration order.
    """
    return [refresh(screen_id=s["id"]) for s in config.SCREENS]


if __name__ == "__main__":
    # Quick sanity check when run directly. Iterates registered screens
    # so the manual run gives an honest view of the whole paper book.
    for screen in config.SCREENS:
        sid = screen["id"]
        s = load_state(screen_id=sid)
        s = mark_to_market(s)
        save_state(s, screen_id=sid)
        print(
            f"[{sid}] equity=${total_equity(s):.2f} "
            f"cash=${s['cash']:.2f} open={len(s['open_positions'])}"
        )