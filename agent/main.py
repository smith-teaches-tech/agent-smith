"""
agent-smith main orchestrator.

Run modes:
  python -m agent.main us       # US discovery + AI passes

Output written to docs/data/ as JSON for the dashboard to render.
A copy is also archived in docs/data/history/ with timestamp.
"""
import sys
import json
import argparse
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from . import config, market, news, truth, analyze, grading, catalysts
from . import portfolio as pf


def _ensure_output_dirs() -> None:
    Path(config.OUTPUT_HISTORY_DIR).mkdir(parents=True, exist_ok=True)
    Path("docs/data").mkdir(parents=True, exist_ok=True)


def _write_output(data: dict[str, Any], latest_path: str, kind: str) -> None:
    """Write output to latest + archive a timestamped copy."""
    _ensure_output_dirs()
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    history_path = Path(config.OUTPUT_HISTORY_DIR) / f"{kind}_{ts}.json"

    Path(latest_path).write_text(json.dumps(data, indent=2, ensure_ascii=False))
    history_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    print(f"  wrote {latest_path}")
    print(f"  archived to {history_path}")


def run_us(tickers_override: list[str] | None = None) -> dict[str, Any]:
    """
    Run US analysis: discovery + AI passes.

    tickers_override: when supplied, replaces the SP400+SP600 universe scan
    with this hand-picked list. Bypasses discovery filters AND the
    unusual-movement threshold (every supplied ticker is treated as a
    mover). Used by --tickers for cheap targeted testing. Production runs
    leave this None.
    """
    print("[us] fetching market context...")
    context_quotes = market.fetch_context_quotes(
        config.INDICES + config.SECTOR_ETFS + config.MEGA_CAP_CONTEXT
    )

    if tickers_override:
        # Targeted-test path: skip the universe scan and the
        # unusual-movement filter. Build mover dicts directly from the
        # override list using market.fetch_movers_universe with filtering
        # disabled, then pass them through unchanged.
        print(f"[us] --tickers override: using {len(tickers_override)} hand-picked names")
        movers = market.fetch_movers_universe(tickers_override, apply_filters=False)
        print(f"[us] {len(movers)} movers built from override (yfinance returned data for these)")
        if len(movers) < len(tickers_override):
            missing = set(tickers_override) - {m["ticker"] for m in movers}
            print(f"[us] WARNING: {len(missing)} tickers had no yfinance data and were dropped: {sorted(missing)}")
    else:
        print("[us] scanning discovery universe...")
        candidates = market.get_discovery_candidates()
        print(f"[us] {len(candidates)} candidates to evaluate")
        universe = market.fetch_movers_universe(candidates)
        print(f"[us] {len(universe)} passed filters")
        movers = market.filter_unusual_movers(universe)
        print(f"[us] {len(movers)} unusual movers identified")

    # Catalyst enrichment: attach 8-K filings, recent earnings, upcoming
    # earnings to each mover. Closes the catalyst-blindness gap that left
    # ~90% of May 2026 movers as "UNCLEAR conf 2" because the bot couldn't
    # see what triggered the price action.
    movers = catalysts.enrich_movers(movers)

    print("[us] fetching news...")
    raw_news = news.fetch_all_english_news()
    tagged_news = news.tag_catalysts(raw_news)
    print(f"[us] {len(tagged_news)} news items, {sum(1 for n in tagged_news if n.get('catalysts'))} with catalysts")

    print("[us] fetching Trump posts...")
    posts = truth.fetch_truth_posts(lookback_hours=24)
    posts = truth.flag_market_relevant(posts)
    market_relevant_posts = [p for p in posts if p.get("market_patterns") or "_warning" in p]
    print(f"[us] {len(posts)} posts, {len(market_relevant_posts)} flagged or warned")

    print("[us] fetching AI announcements...")
    ai_news_items = news.fetch_ai_news()
    print(f"[us] {len(ai_news_items)} AI news items")

    print("[us] running discovery analysis (Claude)...")
    discovery = analyze.run_discovery_pass(
        market_context=context_quotes,
        movers=movers,
        news=tagged_news,
        trump_posts=market_relevant_posts,
    )

    print("[us] running AI impact analysis (Claude)...")
    ai_analysis = analyze.run_ai_pass(
        ai_news=ai_news_items,
        related_movers=movers,
    )

    # Build per-pass status block. "OK" = parsed cleanly. "RECOVERED" = first
    # attempt parse-failed but retry with halved candidates succeeded.
    # "FAILED" = both attempts produced unparseable JSON. Errors list collects
    # diagnostic info for the dashboard banner and human debugging.
    # On retry: halve the movers list passed in. The list is sorted by
    # interestingness (filter_unusual_movers output), so movers[:N/2] keeps
    # the strongest signals. Retry uses the same 32k token cap; the win is
    # smaller output volume, not more headroom per call.
    status: dict[str, Any] = {"discovery": "OK", "ai_analysis": "OK", "errors": []}
    half_movers = movers[: max(1, len(movers) // 2)]

    if analyze.is_parse_error(discovery):
        original_err = discovery.get("_parse_error", "unknown")
        original_excerpt = discovery.get("_raw_response", "")[:500]
        print(f"[us] WARNING: discovery pass returned unparseable JSON: {original_err}", file=sys.stderr)
        print(f"[us] retrying discovery with {len(half_movers)} candidates (was {len(movers)})...")
        retry = analyze.run_discovery_pass(
            market_context=context_quotes,
            movers=half_movers,
            news=tagged_news,
            trump_posts=market_relevant_posts,
        )
        if analyze.is_parse_error(retry):
            print(f"[us] WARNING: discovery retry also failed: {retry.get('_parse_error', 'unknown')}", file=sys.stderr)
            status["discovery"] = "FAILED"
            status["errors"].append({
                "pass": "discovery",
                "error": original_err,
                "raw_excerpt": original_excerpt,
                "retry_attempted": True,
                "retry_error": retry.get("_parse_error", "unknown"),
            })
        else:
            print(f"[us] discovery retry succeeded; status=RECOVERED")
            status["discovery"] = "RECOVERED"
            status["errors"].append({
                "pass": "discovery",
                "error": original_err,
                "raw_excerpt": original_excerpt,
                "retry_attempted": True,
                "retry_succeeded": True,
            })
            discovery = retry  # use the recovered result downstream

    if analyze.is_parse_error(ai_analysis):
        original_err = ai_analysis.get("_parse_error", "unknown")
        original_excerpt = ai_analysis.get("_raw_response", "")[:500]
        print(f"[us] WARNING: ai_analysis pass returned unparseable JSON: {original_err}", file=sys.stderr)
        print(f"[us] retrying ai_analysis with {len(half_movers)} related_movers (was {len(movers)})...")
        retry = analyze.run_ai_pass(
            ai_news=ai_news_items,
            related_movers=half_movers,
        )
        if analyze.is_parse_error(retry):
            print(f"[us] WARNING: ai_analysis retry also failed: {retry.get('_parse_error', 'unknown')}", file=sys.stderr)
            status["ai_analysis"] = "FAILED"
            status["errors"].append({
                "pass": "ai_analysis",
                "error": original_err,
                "raw_excerpt": original_excerpt,
                "retry_attempted": True,
                "retry_error": retry.get("_parse_error", "unknown"),
            })
        else:
            print(f"[us] ai_analysis retry succeeded; status=RECOVERED")
            status["ai_analysis"] = "RECOVERED"
            status["errors"].append({
                "pass": "ai_analysis",
                "error": original_err,
                "raw_excerpt": original_excerpt,
                "retry_attempted": True,
                "retry_succeeded": True,
            })
            ai_analysis = retry  # use the recovered result downstream

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "market_context": context_quotes,
        "movers_count": len(movers),
        "news_count": len(tagged_news),
        "trump_posts_count": len(posts),
        "discovery": discovery,
        "ai_analysis": ai_analysis,
    }
    # Write partial output regardless of pass failures: market_context is
    # always good and dashboard needs to see the status block to render its
    # failure banner. AFTER writing, raise if anything FAILED (not RECOVERED)
    # so main()'s try/except triggers sys.exit(1) and the Actions run goes red.
    # RECOVERED runs produce usable output; the WARNING + status block are
    # enough signal without going red.
    _write_output(output, config.OUTPUT_LATEST_US, "us")
    failed_passes = [name for name in ("discovery", "ai_analysis") if status[name] == "FAILED"]
    if failed_passes:
        raise RuntimeError(
            f"us run completed but {len(failed_passes)} pass(es) failed after retry: "
            f"{', '.join(failed_passes)}. JSON written with status block; see WARNINGs above."
        )
    return output


def run_portfolio(us_output: dict[str, Any] | None = None) -> dict[str, Any]:
    """
    Outer portfolio orchestrator. Iterates every registered screen and
    runs the per-screen portfolio pass against each. Returns a dict
    keyed by screen_id so callers can inspect per-screen outcomes.

    F1 multi-screen wrapping: pre-F1 there was a single body that
    matched today's `run_portfolio_for_screen` exactly. Wrapping was
    chosen over inlining a screen-id parameter so the inner function
    body — which is large and exercises every guardrail in the
    portfolio module — stays one clean unit of execution per screen.
    With one registered screen (Screen 0) behavior is identical to
    pre-F1.
    """
    results: dict[str, Any] = {}
    for screen in config.SCREENS:
        sid = screen["id"]
        try:
            print(f"[portfolio] === screen={sid} ({screen['display_name']}) ===")
            results[sid] = run_portfolio_for_screen(sid, us_output=us_output)
        except Exception as e:
            # Per-screen failure does not abort the run — other screens
            # still get their portfolio pass. main()'s top-level except
            # catches truly fatal cases.
            print(f"[portfolio] screen={sid} FAILED: {e}")
            import traceback
            traceback.print_exc()
            results[sid] = {"error": str(e)}
    return results


def run_portfolio_for_screen(
    screen_id: str,
    us_output: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Portfolio pass for one screen: refresh mark-to-market, let Claude
    decide what to do, apply decisions, write suggestions.json. Runs
    AFTER run_us() so we can feed Claude the newest discoveries.

    Args:
      screen_id: which screen's bankroll, guardrails, and state file
                 to use. Must match a registered SCREENS entry.
      us_output: the dict returned by run_us() on this same invocation.
                 If None, we read the latest discoveries off disk instead.
    """
    from pathlib import Path
    import json

    screen = config.get_screen(screen_id)

    print(f"[portfolio] loading state and marking to market...")
    state = pf.load_state(screen_id=screen_id)
    state = pf.mark_to_market(state)
    pf.save_state(state, screen_id=screen_id)
    print(f"[portfolio] equity=${pf.total_equity(state):.2f} cash=${state['cash']:.2f} open={len(state['open_positions'])}")

    # ---- Gather recent flags ----------------------------------
    # We want buy-eligible discoveries from the last N days. The newest
    # run's output is passed in directly (us_output); older ones come
    # from history/us_*.json. Per-screen window may differ in the future;
    # for F1, Screen 0 inherits the global default.
    window_days = screen["decision_window_days"]
    print(f"[portfolio] gathering flags from last {window_days}d...")
    recent_flags = _collect_recent_flags(
        us_output=us_output,
        window_days=window_days,
    )
    print(f"[portfolio] {len(recent_flags)} flags in window")

    # ---- Trends summary ---------------------------------------
    trends_summary = None
    trends_path = Path(config.OUTPUT_TRENDS)
    if trends_path.exists():
        try:
            trends_summary = json.loads(trends_path.read_text())
        except json.JSONDecodeError:
            print("[portfolio] WARN: trends.json failed to parse; proceeding without")

    # ---- Ask Claude (Haiku) for decisions ---------------------
    # Per-screen model lookup — F2 onwards may want different models per
    # screen. For F1 every screen inherits CLAUDE_PORTFOLIO_MODEL.
    claude_model = screen["claude_model"]
    print(f"[portfolio] running decision pass ({claude_model})...")
    decisions = analyze.run_portfolio_pass(
        portfolio_state=state,
        recent_flags=recent_flags,
        trends_summary=trends_summary,
    )

    if "_parse_error" in decisions:
        print(f"[portfolio] ERROR: decision pass returned unparseable JSON: {decisions['_parse_error']}")
        # Write suggestions.json with no entries but the error noted, so the
        # UI doesn't silently fall over.
        _write_suggestions(entries=[], error=decisions["_parse_error"])
        return decisions

    # ---- Apply decisions --------------------------------------
    trade_summary = {"buys": 0, "sells": 0, "blocked": 0, "watched": 0, "skipped": 0}
    suggestion_entries: list[dict[str, Any]] = []

    # Build a lookup from ticker → original flag, so we can annotate
    # BUY decisions with thesis, horizon, catalyst, etc.
    flags_by_ticker = {f["ticker"]: f for f in recent_flags if f.get("ticker")}

    # 1) Apply position decisions (HOLD / ADD / TRIM / EXIT) first so that
    #    freed-up cash is available for any new BUYs.
    for d in decisions.get("position_decisions", []):
        tkr = d.get("ticker")
        action = (d.get("next_action") or "HOLD").upper()
        reasoning = d.get("reasoning", "")
        thesis_status = d.get("thesis_status", "intact")

        # Update the in-memory position with Claude's latest read,
        # whether or not it triggers a trade.
        for p in state["open_positions"]:
            if p["ticker"] == tkr:
                p["thesis_status"] = thesis_status
                p["next_action"] = action
                p["latest_reasoning"] = reasoning
                break

        if action == "HOLD":
            continue
        if action == "ADD":
            # Adds are treated as fresh BUYs at the current sizing.
            flag = flags_by_ticker.get(tkr)
            if not flag:
                print(f"[portfolio] SKIP ADD {tkr}: no fresh flag to justify")
                continue
            ok = _try_buy(state, flag, reasoning_override=reasoning, screen_id=screen_id)
            if ok: trade_summary["buys"] += 1
            else: trade_summary["blocked"] += 1
        elif action in ("TRIM", "EXIT"):
            shares = int(d.get("shares_to_sell") or 0) if action == "TRIM" else None
            ok, msg, _ = pf.execute_sell(
                state,
                ticker=tkr,
                shares=shares,
                exit_reasoning=reasoning,
                screen_id=screen_id,
            )
            if ok:
                trade_summary["sells"] += 1
                print(f"[portfolio] {action} {tkr}: {msg}")
            else:
                print(f"[portfolio] {action} {tkr} FAILED: {msg}")

    # 2) Apply new-name decisions.
    for d in decisions.get("new_decisions", []):
        tkr = d.get("ticker")
        decision = (d.get("decision") or "SKIP").upper()
        reasoning = d.get("reasoning", "")
        flag = flags_by_ticker.get(tkr)
        if not flag:
            continue

        if decision == "BUY":
            ok = _try_buy(state, flag, reasoning_override=reasoning, screen_id=screen_id)
            if ok:
                trade_summary["buys"] += 1
            else:
                # Blocked by a guardrail — log it as NO_CASH suggestion
                trade_summary["blocked"] += 1
                suggestion_entries.append(
                    _build_suggestion_entry(flag, "NO_CASH", reasoning)
                )
        elif decision == "WATCH":
            trade_summary["watched"] += 1
            suggestion_entries.append(
                _build_suggestion_entry(flag, "WATCH", reasoning)
            )
        else:
            trade_summary["skipped"] += 1
            suggestion_entries.append(
                _build_suggestion_entry(flag, "SKIP", reasoning)
            )

    # Also log decisions for ineligible flags (RATIONAL / UNCLEAR / conf<3)
    # so the watching page isn't empty — gives Michael the full picture.
    _extend_with_ineligible_flags(
        suggestion_entries,
        us_output=us_output,
    )

    pf.save_state(state, screen_id=screen_id)
    _write_suggestions(
        entries=suggestion_entries,
        error=None,
        run_summary=decisions.get("run_summary", ""),
    )

    print(
        f"[portfolio] applied: {trade_summary['buys']} buys, "
        f"{trade_summary['sells']} sells, "
        f"{trade_summary['blocked']} blocked, "
        f"{trade_summary['watched']} watch, "
        f"{trade_summary['skipped']} skip"
    )
    return {"state": state, "decisions": decisions, "trade_summary": trade_summary}
 
 
# ============================================================
# Helpers used by run_portfolio()
# ============================================================
 
def _try_buy(
    state: dict[str, Any],
    flag: dict[str, Any],
    reasoning_override: str = "",
    screen_id: str | None = None,
) -> bool:
    """
    Size & execute a buy from a discovery flag. Returns True on success.

    screen_id routes the trade's audit-log entry to the correct
    per-screen history file. Defaults to the state's stored screen_id.
    """
    tkr = flag.get("ticker")
    # Use current_price if available, else last close off yfinance.
    ref_price = flag.get("price") or flag.get("current_price")
    if ref_price is None:
        # Best-effort lookup at the reference price the execution layer will
        # use anyway — next-open. This is just for sizing.
        prices = pf.fetch_current_prices([tkr])
        ref_price = prices.get(tkr)
    if not ref_price or ref_price <= 0:
        print(f"[portfolio] BUY {tkr} DEFERRED: no reference price")
        return False
 
    shares = pf.size_position(
        state,
        price=ref_price,
        sector=flag.get("sector"),
        confidence=int(flag.get("confidence") or 3),
    )
    if shares <= 0:
        print(f"[portfolio] BUY {tkr} BLOCKED: sizing returned 0 shares (guardrail)")
        return False
 
    ok, msg, _ = pf.execute_buy(
        state,
        ticker=tkr,
        name=flag.get("name", tkr),
        sector=flag.get("sector"),
        shares=shares,
        flag_classification=flag.get("classification", "UNKNOWN"),
        flag_confidence=int(flag.get("confidence") or 3),
        flag_horizon=flag.get("time_horizon", "days"),
        thesis=(
            f"{flag.get('mechanism', 'no mechanism')}. "
            f"Catalyst: {flag.get('catalyst', 'n/a')}. "
            f"Falsified by: {flag.get('what_would_falsify', 'n/a')}."
        ),
        catalyst=flag.get("catalyst"),
        reference_price=ref_price,
        screen_id=screen_id,
    )
    if ok:
        print(f"[portfolio] BUY {tkr} × {shares}: {msg}")
    else:
        print(f"[portfolio] BUY {tkr} FAILED: {msg}")
    return ok
 
 
def _collect_recent_flags(
    *,
    us_output: dict[str, Any] | None,
    window_days: int,
) -> list[dict[str, Any]]:
    """
    Return a list of discovery flags from the last N days.
    Newest first. Deduplicates by ticker (keeps the latest flag per name).
    """
    from pathlib import Path
    import json
 
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    by_ticker: dict[str, dict[str, Any]] = {}
 
    # 1) Newest run passed in directly (avoids a disk read).
    if us_output:
        for d in (us_output.get("discovery") or {}).get("discoveries", []):
            t = d.get("ticker")
            if t:
                by_ticker[t] = d
 
    # 2) Older runs from history/us_*.json.
    hist_dir = Path(config.OUTPUT_HISTORY_DIR)
    if hist_dir.exists():
        for path in sorted(hist_dir.glob("us_*.json"), reverse=True):
            try:
                data = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            try:
                generated = datetime.fromisoformat(
                    data.get("generated_at", "").replace("Z", "+00:00")
                )
                if generated < cutoff:
                    break  # sorted newest-first, so we can stop
            except ValueError:
                continue
            for d in (data.get("discovery") or {}).get("discoveries", []):
                t = d.get("ticker")
                if t and t not in by_ticker:  # keep NEWEST per ticker
                    by_ticker[t] = d
 
    return list(by_ticker.values())
 
 
def _build_suggestion_entry(
    flag: dict[str, Any],
    decision: str,
    reasoning: str,
) -> dict[str, Any]:
    """Build one row for suggestions.json."""
    return {
        "ticker": flag.get("ticker"),
        "name": flag.get("name"),
        "sector": flag.get("sector"),
        "flagged_at": datetime.now(timezone.utc).isoformat(),
        "run_file": "latest",
        "classification": flag.get("classification"),
        "confidence": int(flag.get("confidence") or 0),
        "horizon_days": _horizon_to_days(flag.get("time_horizon", "days")),
        "move_pct_at_flag": flag.get("move_pct"),
        # Carry through the original discovery's catalyst attribution so the
        # watching page can render the [cite ↗] link. Both come from the
        # discovery flag dict (same shape passed at both call sites — the
        # decision loop and _extend_with_ineligible_flags). May be null on
        # older flags or RATIONAL/UNCLEAR rows; the dashboard handles that
        # defensively.
        "catalyst": flag.get("catalyst"),
        "catalyst_url": flag.get("catalyst_url"),
        # Carry the two most useful pedagogical fields (thesis = the read,
        # what_kills = the disconfirming evidence to watch for) so the
        # watching page can show context beyond the bot's portfolio-level
        # reasoning. setup / what_confirms / what_to_learn deliberately
        # NOT carried — current page is the right home for those.
        # Fallbacks handle the 7-day schema-transition window when old
        # entries (with `mechanism` / `what_would_falsify`) are still in
        # play. After ~1 week every visible row uses the new field names.
        "thesis": flag.get("thesis") or flag.get("mechanism"),
        "what_kills": flag.get("what_kills") or flag.get("what_would_falsify"),
        "decision": decision,
        "reasoning": reasoning,
        # Price/verdict fields are filled in on subsequent runs by the
        # suggestions-refresh step (not built yet — MVP leaves them null).
        "price_at_flag": None,
        "current_price": None,
        "since_pct": None,
        "verdict": "pending",
        "verdict_note": None,
    }
 
 
def _horizon_to_days(h: str) -> int:
    """Map 'days'/'weeks'/'months' strings to trading-day counts."""
    return config.GRADING_HORIZON_DAYS.get((h or "days").lower(), 5)
 
 
def _extend_with_ineligible_flags(
    entries: list[dict[str, Any]],
    *,
    us_output: dict[str, Any] | None,
) -> None:
    """
    Add SKIP rows for RATIONAL/UNCLEAR flags (or conf-below-threshold flags)
    so the watching page shows the bot's full decision surface.

    Uses the same 7-day history window as `_collect_recent_flags` (which
    feeds Haiku) — fixes a long-standing asymmetry where this function
    only saw the current run, leaving the watching page empty whenever
    discovery failed or returned no flags. Haiku already sees the window;
    so should this.

    Tickers already present in `entries` (i.e. Haiku already decided on
    them this run) are skipped to avoid duplicates.
    """
    already_decided = {e.get("ticker") for e in entries if e.get("ticker")}

    flags = _collect_recent_flags(
        us_output=us_output,
        window_days=config.PAPER_PORTFOLIO_DECISION_WINDOW_DAYS,
    )

    for d in flags:
        tkr = d.get("ticker")
        if not tkr or tkr in already_decided:
            continue
        cls = d.get("classification", "")
        conf = int(d.get("confidence") or 0)
        if cls in ("OVERDONE", "UNDERDONE") and conf >= config.PAPER_PORTFOLIO_MIN_BUY_CONFIDENCE:
            # Buy-eligible — Haiku should have decided on it. If it's not
            # in already_decided, that means Haiku saw it but didn't return
            # a decision (rare, possible if Haiku truncated). Skip rather
            # than fabricate a SKIP reason we don't actually have.
            continue
        entries.append(
            _build_suggestion_entry(
                d,
                "SKIP",
                f"{cls} classification — no directional edge."
                if cls in ("RATIONAL", "UNCLEAR")
                else f"confidence {conf} below buy threshold.",
            )
        )
 
 
def _write_suggestions(
    *,
    entries: list[dict[str, Any]],
    error: str | None,
    run_summary: str = "",
) -> None:
    """Write docs/data/suggestions.json in the schema suggestions.html expects."""
    from pathlib import Path
    import json
 
    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_days": config.PAPER_PORTFOLIO_DECISION_WINDOW_DAYS,
        "run_summary": run_summary,
        "entries": entries,
    }
    if error:
        out["_error"] = error
    path = Path(config.OUTPUT_SUGGESTIONS)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(f"  wrote {path}")

def main() -> None:
    parser = argparse.ArgumentParser(description="agent-smith orchestrator")
    parser.add_argument(
        "mode",
        choices=["us"],
        help="which analysis to run",
    )
    parser.add_argument(
        "--portfolio",
        action="store_true",
        help=(
            "after the main analysis pass, run the portfolio decision pass "
            "(should be enabled only on the 22:00 AST afternoon run)."
        ),
    )
    parser.add_argument(
        "--no-claude",
        action="store_true",
        help=(
            "skip every Claude API call. Each pass prints the prompt that "
            "would have been sent and returns a pipeline-safe stub. Use for "
            "free local iteration on the data layer / prompt structure."
        ),
    )
    parser.add_argument(
        "--tickers",
        type=str,
        default=None,
        help=(
            "comma-separated ticker list to use INSTEAD of the discovery "
            "universe scan (e.g. 'PRIM,TMDX,VECO'). Skips the ~10-min "
            "universe scan entirely and skips mover-filter thresholds; "
            "every supplied ticker is treated as a mover. Combine with "
            "--no-claude for free prompt iteration, or run alone for cheap "
            "(~$0.05) live tests on a hand-picked subset."
        ),
    )
    args = parser.parse_args()
    print(f"=== agent-smith run [{args.mode}] portfolio={args.portfolio} no_claude={args.no_claude} tickers={args.tickers or 'auto'} {datetime.now(timezone.utc).isoformat()} ===")

    if args.no_claude:
        analyze.NO_CLAUDE_MODE = True
        print("[main] --no-claude active: API calls will be skipped, prompts printed to stdout.")

    tickers_override: list[str] | None = None
    if args.tickers:
        tickers_override = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
        if not tickers_override:
            print("[main] --tickers parsed to empty list; aborting", file=sys.stderr)
            sys.exit(2)
        print(f"[main] --tickers active: scan replaced with {len(tickers_override)} hand-picked names: {','.join(tickers_override)}")

    us_output = None
    try:
        us_output = run_us(tickers_override=tickers_override)
    except Exception as e:
        print(f"[us] FAILED: {e}", file=sys.stderr)
        sys.exit(1)

    # Grading runs on every US invocation; cheap (no LLM) and builds history.
    try:
        print("[grading] running...")
        grading_out = grading.run()
        n = grading_out.get("overall", {}).get("n_resolved", 0)
        total = grading_out.get("n_total_calls", 0)
        print(f"[grading] {n}/{total} calls resolved; wrote {config.OUTPUT_TRENDS}")
    except Exception as e:
        print(f"[grading] FAILED: {e}")

    # Portfolio pass: only on the designated 22:00 AST run (triggered via --portfolio).
    # Mark-to-market on every other run so the dashboard stays current. With
    # multi-screen, refresh every registered screen — refresh_all() iterates
    # config.SCREENS so adding a screen automatically gets it marked-to-market
    # on every run.
    if args.portfolio:
        try:
            run_portfolio(us_output=us_output)
        except Exception as e:
            print(f"[portfolio] FAILED: {e}")
            import traceback
            traceback.print_exc()
    else:
        try:
            pf.refresh_all()
        except Exception as e:
            print(f"[portfolio] mark-to-market failed (non-fatal): {e}")

    print("=== done ===")


if __name__ == "__main__":
    main()