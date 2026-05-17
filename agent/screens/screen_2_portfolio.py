"""
agent.screens.screen_2_portfolio — Screen 2's portfolio decision pass.

WHAT THIS IS:
  The Haiku pass that turns Screen 2's discovery flags (from screen_2.py)
  into BUY / WATCH / SKIP decisions and HOLD / TRIM / EXIT decisions on
  open Screen 2 positions. It is to screen_2.py what
  ai_sympathy.build_screen_1_portfolio_prompt is to ai_sympathy's
  discovery pass — a per-screen portfolio prompt with the discipline
  that makes Screen 2 *Screen 2* and not Screen 0.

WHY A DEDICATED PASS (not the generic analyze.run_portfolio_pass):
  The generic pass is Screen 0's. It speaks the language of price
  movers — OVERDONE/UNDERDONE labels on *price action*, the two-tier
  conviction/exploratory sizing model, and the red-team second pass.
  None of that fits Screen 2:
    - Screen 2's UNDERDONE/OVERDONE is a read on a FILING, not on a
      price move. There is no move_pct, no "the market overreacted".
    - Screen 2 has no exploratory tier. The exploratory tier exists to
      generate graded volume from marginal MOVER flags; Screen 2's
      volume is governed by the earnings calendar, not a sizing knob.
      Screen 2 sizes by confidence alone (conviction-style).
    - Screen 2's holding window is the print itself: T-2 entry,
      T+1 exit, ~4 trading days. That is the screen's whole edge claim
      — pre-print reading quality — and the portfolio pass is where
      that window is currently enforced.
  main.run_portfolio_for_screen previously routed Screen 2 through the
  generic pass as a known stopgap (see its docstring). This module is
  the dedicated pass that stopgap pointed at.

SCOPE DECISIONS (locked in this build):
  - LONG-ONLY. The paper book has no short mechanic. Screen 2's
    TRADEABLE flags are UNDERDONE and OVERDONE, but only UNDERDONE can
    become a BUY. OVERDONE flags are still recorded by the discovery
    pass and surface on the watching page — they are part of the
    pedagogical record — they simply never reach a position here.
    Haiku is told to SKIP every OVERDONE flag with that one-line reason.
  - HOLDING WINDOW IS PROMPT-ENFORCED IN THIS VERSION. Haiku is given a
    hard, unambiguous instruction to EXIT any position whose earnings
    print has passed (T+1 reached). A code-enforced hard exit — a
    trading-day counter from the entry date, independent of Haiku — is
    the better long-run design (same auditability argument as the
    25/40/10 guardrails) and is the top follow-up item in roadmap.md.
    Until then this prompt is the discipline. Screen 1 ships
    prompt-only enforcement too, so this is consistent with its sibling.

OUTPUT SCHEMA:
  Identical to Screen 0 / Screen 1 (position_decisions + new_decisions),
  so main.run_portfolio_for_screen's screen-agnostic apply-decisions
  block consumes it unchanged. The ONE schema difference from Screen 0:
  new_decisions carry NO `tier` field — Screen 2 has no exploratory
  tier. main._try_buy treats tier=None as conviction sizing, which is
  exactly what Screen 2 wants, so emitting no tier is correct and needs
  no special-casing downstream.
"""
from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timezone
from typing import Any

from anthropic import Anthropic

from .. import config
from .. import portfolio as pf
from ..analyze import (
    INJECTION_GUARD,
    OUTPUT_DISCIPLINE,
    NO_CLAUDE_MODE,
    _stream_message,
    _parse_json_response,
    _print_prompt,
)


# ============================================================
# Portfolio prompt — system
# ============================================================

SCREEN_2_PORTFOLIO_SYSTEM = f"""You are running Screen 2's portfolio
decision pass for Michael Smith's agent-smith paper portfolio. Michael
does NOT act on this portfolio — it is a feedback loop that grades
Screen 2's judgement over time.

WHAT SCREEN 2 IS:
Screen 2 is the pre-earnings filings-read screen. For a mid-cap company
that reports earnings in a few trading days, an earlier Opus pass read
the company's 10-K Business section, 10-K and 10-Q Risk Factors, and the
last ~4 quarters of 8-K earnings press releases — and formed a
directional view on whether the upcoming print will land BETTER
(UNDERDONE) or WORSE (OVERDONE) than the market currently expects. The
screen's edge claim is pre-print READING QUALITY, done with the same
discipline across the whole universe. The edge is entirely before the
print; the screen has NO edge after it.

YOUR JOB:
For each recent Screen 2 flag, decide BUY / WATCH / SKIP. For each open
Screen 2 position, decide HOLD / TRIM / EXIT. Output one decision per
flag and one per open position, in the JSON schema below.

================================
THE HOLDING WINDOW — SCREEN 2'S CORE DISCIPLINE
================================
Screen 2 positions exist to span ONE earnings print and nothing more:
  - Entry:  ~2 trading days before the print (T-2)
  - Exit:   ~1 trading day after the print (T+1)
  - Total:  roughly 4 trading days
This window IS the screen's discipline. The edge is pre-print reading;
once the print has happened, Screen 2 has no further edge on the name,
win or lose. A Screen 2 position held past T+1 has stopped being a
Screen 2 trade.

The T+1 exit is enforced in CODE, before this decision pass runs: a
sweep closes every position whose earnings print has already passed,
so in normal operation you will not even see a post-print position in
the portfolio state — it has already been exited. You therefore mostly
decide HOLD on the positions you DO see (their print is still upcoming).

You must STILL apply the rule below, as a backstop. The code sweep
reads the earnings date from each position's `catalyst` string; if a
position's catalyst is malformed and the date cannot be parsed, the
code cannot act and the position is handed to you instead. So:
  - If the position's earnings print has ALREADY OCCURRED (the earnings
    date is in the past, i.e. T+1 has been reached or passed): the
    next_action MUST be EXIT. This is non-negotiable and is independent
    of profit or loss. A winning post-print position is exited for the
    same reason as a losing one — the thesis window is over. Do NOT
    rationalise holding a winner "a bit longer", and do NOT average
    down on a loser. Set thesis_status to "played-out" and EXIT.
  - If the print has NOT yet occurred and the thesis is intact: HOLD.
  - If fresh information has genuinely broken the pre-print thesis
    before the print (rare — you have no price data here, so this would
    be a flag-level contradiction, not a price move): EXIT early with
    thesis_status "broken".
Screen 2 does NOT support ADD as a position action. Averaging into a
position inside a 4-day event window violates the discipline. Use HOLD,
TRIM, or EXIT only.

================================
BUY ELIGIBILITY — SCREEN 2 SPECIFIC
================================
A Screen 2 flag may become a BUY only if ALL of these hold:
  - classification is UNDERDONE. This paper book is LONG-ONLY: it has
    no short mechanic. An OVERDONE flag is a real, recorded Screen 2
    signal, but it cannot be expressed as a long position — so every
    OVERDONE flag is a SKIP here, with the one-line reason
    "OVERDONE — long-only book has no way to express this". That is
    not a quality judgement on the flag; it is a structural limit.
  - confidence is >= the screen's min_buy_confidence (see screen_config).
  - the upcoming earnings print has NOT already passed. A flag whose
    earnings_date is in the past is stale — the event it was timed to
    is over. SKIP it with the reason "earnings print already passed —
    flag is stale".
  - the ticker is not already held in this screen's portfolio.
  - there is plausible cash / position / sector headroom. The execution
    layer enforces the 25% position / 40% sector / 10%-cash guardrails
    exactly — do NOT re-derive the arithmetic, just don't propose a
    trade that is obviously blocked.

If a flag passes every gate, your decision is BUY. If it is genuinely
borderline — confidence exactly at the threshold, or the filings
evidence reads thin even though the classification is UNDERDONE —
WATCH is the honest call. Otherwise SKIP with a one-line reason citing
the specific gate it failed.

SIZING / TIERS:
Screen 2 has NO exploratory tier. The exploratory tier is a Screen 0
device for generating graded volume from marginal price-mover flags;
Screen 2's trade volume is set by the earnings calendar, not by a
sizing knob. Every Screen 2 BUY is sized conviction-style — the
execution layer scales 15-25% of equity by the flag's confidence.
DO NOT emit a `tier` field on Screen 2 decisions. Omit it entirely.

================================
JUDGEMENT NOTES
================================
- You will see Screen 2's own grading track record where available.
  Take it seriously: if UNDERDONE pre-earnings calls at confidence 3
  have been hitting poorly, be more reluctant on new conf-3 UNDERDONE
  flags. If they have been hitting well and you are still SKIP-ing
  most of them, that is evidence to be less conservative.
- You have NO price data and NO post-print data by design. Every
  decision is grounded in the flag's filings_evidence,
  guidance_pattern, thesis, what_confirms, and what_kills. If a flag's
  pedagogical fields are vague platitudes rather than situation-
  specific filing reads, that is itself a reason to WATCH or SKIP — a
  thin flag is a thin trade.
- A NOTE ON ACTIVITY. This is a learning system. Chronic inaction
  produces no graded trades and therefore no signal about whether
  Screen 2's reads are right. When a flag clears every gate cleanly,
  BUY it — do not invent reasons to WATCH a flag that qualifies.

CRITICAL BIAS WARNING:
You are made by Anthropic. Some candidates' filings describe AI
products, AI-driven demand, or AI as a competitive threat. You may have
a bias toward over-crediting AI tailwinds and under-weighting AI as a
risk. Hold an AI-themed thesis to exactly the same evidence standard as
any other — the evidence is in the filing numbers and the specific
language, not in the word "AI".

{INJECTION_GUARD}

{OUTPUT_DISCIPLINE}

JSON SCHEMA:
{{
  "run_summary": "1-2 sentence read on Screen 2's stance this run",
  "position_decisions": [
    {{
      "ticker": "SYMBOL",
      "thesis_status": "intact | weakening | broken | played-out",
      "next_action": "HOLD | TRIM | EXIT",
      "shares_to_sell": 0,
      "reasoning": "cite the earnings date, whether the print has passed, and days_held",
      "confidence_in_decision": 3
    }}
  ],
  "new_decisions": [
    {{
      "ticker": "SYMBOL",
      "decision": "BUY | WATCH | SKIP",
      "reasoning": "why this passes the Screen 2 bar — cite classification, confidence, and the earnings date — or the specific gate it failed",
      "confidence_in_decision": 4
    }}
  ],
  "no_action_note": "optional: explain if no flags or positions warranted action this run"
}}

SCHEMA NOTES:
- Do NOT emit a `tier` field. Screen 2 has no exploratory tier; every
  BUY is conviction-sized by the execution layer.
- `shares_to_sell` is read only on TRIM. It is ignored on HOLD and on
  EXIT (EXIT always closes the whole position).
- An OVERDONE flag is always a SKIP in this long-only book — never a
  BUY and never a WATCH-for-later (there is nothing to wait for; the
  book structurally cannot hold it).
"""


# ============================================================
# Portfolio prompt — user-content builder
# ============================================================

def _slim_open_position(p: dict[str, Any]) -> dict[str, Any]:
    """
    Reduce an open Screen 2 position to the fields the portfolio pass
    needs. Mirrors analyze._summarize_open_position /
    ai_sympathy's open-position slimming, with `catalyst` carried
    through prominently — for Screen 2 the catalyst string is the
    earnings event ("Q_ earnings YYYY-MM-DD"), which is exactly what
    Haiku needs to judge whether the print has passed.
    """
    return {
        "ticker": p["ticker"],
        "name": p.get("name"),
        "sector": p.get("sector"),
        "shares": p["shares"],
        "cost_basis": p["cost_basis"],
        "current_price": p.get("current_price"),
        "unrealized_pnl": p.get("unrealized_pnl"),
        "unrealized_pct": p.get("unrealized_pct"),
        "days_held": p.get("days_held"),
        "flag_classification": p.get("flag_classification"),
        "flag_confidence": p.get("flag_confidence"),
        "flag_horizon": p.get("flag_horizon"),
        "thesis": p.get("thesis"),
        # The earnings event. Screen 2 stores "Q_ earnings YYYY-MM-DD"
        # here at execute_buy time; it is the anchor for the
        # holding-window / print-has-passed judgement.
        "catalyst": p.get("catalyst"),
        "thesis_status": p.get("thesis_status"),
    }


def _slim_flag(f: dict[str, Any]) -> dict[str, Any]:
    """
    Reduce a Screen 2 discovery flag to the fields the portfolio pass
    needs. Screen 2's discovery schema (see screen_2.py
    SCREEN_2_DISCOVERY_SYSTEM) carries earnings_date / trading_days_out
    / filings_evidence / guidance_pattern — fields Screen 0's flags do
    not have — so this is a Screen-2-specific slimmer, not the generic
    analyze._summarize_discovery_for_portfolio.
    """
    return {
        "ticker": f.get("ticker"),
        "name": f.get("name"),
        "sector": f.get("sector"),
        "classification": f.get("classification"),
        "confidence": f.get("confidence"),
        "earnings_date": f.get("earnings_date"),
        "trading_days_out": f.get("trading_days_out"),
        "filings_evidence": f.get("filings_evidence"),
        "guidance_pattern": f.get("guidance_pattern"),
        "setup": f.get("setup"),
        "thesis": f.get("thesis"),
        "what_confirms": f.get("what_confirms"),
        "what_kills": f.get("what_kills"),
        "what_to_learn": f.get("what_to_learn"),
        "catalyst": f.get("catalyst"),
        "catalyst_url": f.get("catalyst_url"),
        "time_horizon": f.get("time_horizon", "days"),
    }


def build_screen_2_portfolio_prompt(
    *,
    portfolio_state: dict[str, Any],
    recent_flags: list[dict[str, Any]],
    screen_config: dict[str, Any],
    trends_summary: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """
    Build (system, user_content) for Screen 2's Haiku portfolio pass.

    Args:
      portfolio_state: output of pf.load_state(screen_id="screen_2")
                       after mark_to_market.
      recent_flags:    Screen 2 discoveries from the last N days
                       (pure Screen 2 — no Screen 0 / Screen 1
                       contamination; main collects these from
                       screen_2_us.json + history/screen_2_us_*.json).
      screen_config:   the SCREENS registry entry for screen_2
                       (bankroll, guardrail pcts, min_buy_confidence,
                       holding_window_days).
      trends_summary:  trends.json contents, or None if grading has not
                       produced Screen 2 data yet.

    Returns:
      (system_prompt, user_content) — caller hands these to
      _stream_message, exactly as ai_sympathy.build_screen_1_portfolio_
      prompt's return value is used.
    """
    open_positions = [
        _slim_open_position(p)
        for p in portfolio_state.get("open_positions", [])
    ]
    slim_flags = [_slim_flag(f) for f in recent_flags]

    # trends summary: reuse analyze's condenser if there is data.
    # Imported lazily to avoid a hard import-time dependency on a
    # private helper — keeps this module's import surface small.
    if trends_summary:
        from ..analyze import _summarize_trends_for_prompt
        trends_block = _summarize_trends_for_prompt(trends_summary)
    else:
        trends_block = "(no Screen 2 grading data yet — runs accumulating)"

    user_content = "\n".join([
        f"Run timestamp (UTC): {datetime.now(timezone.utc).isoformat()}",
        "",
        "<screen_config>",
        json.dumps({
            "screen_id": screen_config.get("id"),
            "display_name": screen_config.get("display_name"),
            "bankroll_start": screen_config.get("bankroll"),
            "max_position_pct": screen_config.get("max_position_pct"),
            "max_sector_pct": screen_config.get("max_sector_pct"),
            "min_cash_pct": screen_config.get("min_cash_pct"),
            "min_buy_confidence": screen_config.get("min_buy_confidence"),
            "holding_window_days": screen_config.get("holding_window_days"),
        }, indent=2),
        "</screen_config>",
        "",
        "<portfolio_state>",
        "Current Screen 2 paper portfolio (post mark-to-market):",
        json.dumps({
            "cash": portfolio_state.get("cash"),
            "bankroll_start": portfolio_state.get("bankroll_start"),
            "n_open": len(open_positions),
            "open_positions": open_positions,
        }, indent=2),
        "</portfolio_state>",
        "",
        "<trends>",
        "Screen 2's own track record (for self-calibration):",
        trends_block,
        "</trends>",
        "",
        "<recent_screen_2_flags>",
        (
            f"Screen 2 pre-earnings flags from the last "
            f"{screen_config.get('decision_window_days', '?')} days. "
            f"Each flag is tied to a specific upcoming earnings print."
        ),
        json.dumps(slim_flags, indent=2),
        "</recent_screen_2_flags>",
        "",
        "Return one decision per open position and one decision per "
        "flag, per the JSON schema in your instructions.",
    ])

    return SCREEN_2_PORTFOLIO_SYSTEM, user_content


# ============================================================
# T+1 hard exit — code-enforced holding-window discipline
#
# Screen 2's edge claim is pre-print reading. A position is meant to
# span exactly one earnings print: T-2 entry, T+1 exit. Past T+1 the
# screen has no edge on the name, win or lose.
#
# The portfolio prompt also instructs Haiku to EXIT post-print
# positions, but a prompt is a suggestion — one Haiku that decides to
# hold a post-print winner "a little longer" silently breaks the
# screen's whole thesis, and nothing in the audit trail flags it. This
# function makes the exit STRUCTURAL: it runs before the Haiku pass,
# closes every position whose print has passed, and Haiku never sees a
# post-print position to reason about. Same auditability principle as
# the 25/40/10 guardrails — discipline you can't skip.
#
# The earnings date is read straight off the position's `catalyst`
# string. Screen 2's discovery prompt mandates `catalyst` be
# "Q_ earnings YYYY-MM-DD", and execute_buy persists it on the
# position — so the print date is already on every Screen 2 position
# with no extra plumbing and no earnings_calendar dependency. Anchoring
# to the actual event date (not a trading-day count from entry) is also
# more correct: a deferred fill doesn't shift the exit.
# ============================================================

# Matches the YYYY-MM-DD inside a Screen 2 catalyst string such as
# "Q1 earnings 2026-05-22". Tolerant of surrounding text.
_CATALYST_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def _earnings_date_from_catalyst(catalyst: str | None) -> date | None:
    """
    Pull the earnings date out of a Screen 2 catalyst string.

    Returns a date on success, or None if the catalyst is missing or
    does not contain a parseable YYYY-MM-DD. A None return is a SIGNAL,
    not a silent pass — the caller logs it loudly, because a position
    whose date can't be read would otherwise never hit the hard exit.
    """
    if not catalyst or not isinstance(catalyst, str):
        return None
    m = _CATALYST_DATE_RE.search(catalyst)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        # Matched the shape but not a real calendar date (e.g. month 13).
        return None


def force_exit_elapsed_positions(
    state: dict[str, Any],
    *,
    screen_id: str = "screen_2",
    today: date | None = None,
) -> dict[str, Any]:
    """
    Close every open Screen 2 position whose earnings print has passed.

    This is Screen 2's holding-window discipline, code-enforced. Call it
    in run_portfolio_for_screen AFTER mark_to_market and BEFORE the Haiku
    portfolio pass, so the decision pass sees a book with no post-print
    positions left in it.

    "Print has passed" means: the earnings date parsed from the
    position's `catalyst` string is more than 1 calendar day before
    `today`. The 1-day grace is the "+1" in T+1 — the position is held
    through the print and one day after, then exited. (Sells fill at the
    next session open via pf.execute_sell, so a Friday print flagged on
    Saturday's run still exits ~1 trading day later, at Monday's open.)

    A position whose catalyst date cannot be parsed is NOT force-exited
    — there is no basis to act — but it IS logged as a warning. The
    Haiku pass still sees that position and its prompt instruction to
    EXIT post-print names is the backstop. This is the one path where
    the prompt, not the code, carries the discipline; the warning makes
    that visible rather than silent.

    Args:
      state:     the mark-to-market'd Screen 2 portfolio state. Mutated
                 in place by pf.execute_sell as positions close.
      screen_id: routes the sell's audit-log entry. Defaults to
                 "screen_2" — the only screen this function serves.
      today:     defaults to today's UTC date; injectable for testing.

    Returns:
      A summary dict: {exited, exit_failed, unparseable, details} —
      `details` is a per-ticker list for logging / the run summary.
    """
    if today is None:
        today = datetime.now(timezone.utc).date()

    summary: dict[str, Any] = {
        "exited": 0,
        "exit_failed": 0,
        "unparseable": 0,
        "details": [],
    }

    # Snapshot the ticker list first: execute_sell mutates
    # state["open_positions"], so we must not iterate it live.
    open_snapshot = list(state.get("open_positions", []))

    for pos in open_snapshot:
        ticker = pos.get("ticker")
        catalyst = pos.get("catalyst")
        earnings_dt = _earnings_date_from_catalyst(catalyst)

        if earnings_dt is None:
            # Cannot determine the print date — do NOT exit on a guess.
            # Log loudly; the Haiku prompt's EXIT instruction is the
            # backstop for this position.
            summary["unparseable"] += 1
            summary["details"].append({
                "ticker": ticker,
                "action": "skipped",
                "reason": f"unparseable catalyst {catalyst!r} — cannot "
                          f"determine earnings date; left for Haiku pass",
            })
            print(
                f"[screen_2] WARN: position {ticker} has unparseable "
                f"catalyst {catalyst!r} — T+1 hard exit cannot fire; "
                f"relying on the Haiku prompt's EXIT instruction instead."
            )
            continue

        # T+1: exit once the print is more than 1 calendar day past.
        days_since_print = (today - earnings_dt).days
        if days_since_print <= 1:
            # Print is upcoming, today, or just yesterday (T-/T0/T+1
            # not yet cleared) — position stays open, Haiku decides.
            continue

        # Print has passed. Hard exit, regardless of P&L.
        reasoning = (
            f"T+1 hard exit — earnings print ({earnings_dt.isoformat()}) "
            f"passed {days_since_print}d ago; Screen 2 holding window "
            f"closed. Code-enforced, independent of P&L."
        )
        ok, msg, _ = pf.execute_sell(
            state,
            ticker=ticker,
            shares=None,  # close the whole position
            exit_reasoning=reasoning,
            screen_id=screen_id,
        )
        if ok:
            summary["exited"] += 1
            summary["details"].append({
                "ticker": ticker,
                "action": "exited",
                "earnings_date": earnings_dt.isoformat(),
                "days_since_print": days_since_print,
            })
            print(f"[screen_2] T+1 hard exit {ticker}: {msg}")
        else:
            # Sell failed (e.g. no next-open price yet). Leave the
            # position; it will be retried on the next run. Haiku also
            # still sees it and can EXIT.
            summary["exit_failed"] += 1
            summary["details"].append({
                "ticker": ticker,
                "action": "exit_failed",
                "reason": msg,
            })
            print(f"[screen_2] WARN: T+1 hard exit {ticker} FAILED: {msg}")

    if summary["exited"] or summary["exit_failed"] or summary["unparseable"]:
        print(
            f"[screen_2] T+1 hard exit sweep: {summary['exited']} exited, "
            f"{summary['exit_failed']} failed, "
            f"{summary['unparseable']} unparseable"
        )
    return summary


# ============================================================
# Anthropic client
# ============================================================

def _client() -> Anthropic:
    """Anthropic client. Mirrors analyze._client / screen_2._client."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY not set. "
            "Set it via GitHub Secrets in Actions, or .env locally."
        )
    return Anthropic(api_key=key)


# ============================================================
# Public API: the portfolio pass
# ============================================================

def run_screen_2_portfolio_pass(
    *,
    portfolio_state: dict[str, Any],
    recent_flags: list[dict[str, Any]],
    screen_config: dict[str, Any],
    trends_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Execute Screen 2's portfolio decision pass.

    Sibling of analyze.run_portfolio_pass / run_portfolio_pass_screen_1.
    Delegates prompt construction to build_screen_2_portfolio_prompt and
    returns a dict in Screen 0's portfolio schema (position_decisions +
    new_decisions) so main.run_portfolio_for_screen's screen-agnostic
    apply-decisions block consumes it unchanged.

    Args:
      portfolio_state: pf.load_state(screen_id="screen_2") output, after
                       mark_to_market.
      recent_flags:    Screen 2 discovery flags from the last N days.
      screen_config:   the SCREENS registry entry for screen_2.
      trends_summary:  trends.json contents, or None.

    Returns:
      Parsed JSON per the schema. On a JSON parse failure the returned
      dict carries `_parse_error` — callers test with
      analyze.is_parse_error, exactly as for the other passes.
    """
    system, user_content = build_screen_2_portfolio_prompt(
        portfolio_state=portfolio_state,
        recent_flags=recent_flags,
        screen_config=screen_config,
        trends_summary=trends_summary,
    )

    if NO_CLAUDE_MODE:
        _print_prompt("portfolio_screen_2", system, user_content)
        # HOLD every open position, SKIP every flag — the pipeline-safe
        # no-op. main.run_portfolio_for_screen applies these cleanly:
        # no trades fire, a clean suggestions file is written, the
        # dashboard still renders. Same stub shape as Screen 1's
        # no-claude path.
        return {
            "run_summary": "(no-claude mode — Screen 2 portfolio pass skipped)",
            "position_decisions": [
                {
                    "ticker": p["ticker"],
                    "thesis_status": "intact",
                    "next_action": "HOLD",
                    "shares_to_sell": 0,
                    "reasoning": "(no-claude mode)",
                }
                for p in portfolio_state.get("open_positions", [])
            ],
            "new_decisions": [
                {
                    "ticker": f.get("ticker"),
                    "decision": "SKIP",
                    "reasoning": "(no-claude mode)",
                }
                for f in recent_flags
            ],
            "_no_claude": True,
        }

    client = _client()
    msg = _stream_message(
        client,
        model=screen_config.get("claude_model", config.CLAUDE_PORTFOLIO_MODEL),
        max_tokens=config.CLAUDE_PORTFOLIO_MAX_TOKENS,
        system=system,
        user_content=user_content,
    )
    return _parse_json_response(msg.content[0].text)