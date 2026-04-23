"""
agent-smith main orchestrator.

Run modes:
  python -m agent.main us       # US discovery + AI passes
  python -m agent.main tw       # Taiwan pass only
  python -m agent.main all      # Everything

Output written to docs/data/ as JSON for the dashboard to render.
A copy is also archived in docs/data/history/ with timestamp.
"""
import sys
import json
import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config, market, news, truth, analyze, grading
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


def run_us() -> dict[str, Any]:
    """Run US analysis: discovery + AI passes."""
    print("[us] fetching market context...")
    context_quotes = market.fetch_context_quotes(
        config.INDICES + config.SECTOR_ETFS + config.MEGA_CAP_CONTEXT
    )

    print("[us] scanning discovery universe...")
    candidates = market.get_discovery_candidates()
    print(f"[us] {len(candidates)} candidates to evaluate")
    universe = market.fetch_movers_universe(candidates)
    print(f"[us] {len(universe)} passed filters")
    movers = market.filter_unusual_movers(universe)
    print(f"[us] {len(movers)} unusual movers identified")

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

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "market_context": context_quotes,
        "movers_count": len(movers),
        "news_count": len(tagged_news),
        "trump_posts_count": len(posts),
        "discovery": discovery,
        "ai_analysis": ai_analysis,
    }
    _write_output(output, config.OUTPUT_LATEST_US, "us")
    return output


def run_tw() -> dict[str, Any]:
    """Run Taiwan analysis pass."""
    print("[tw] fetching Taiwan market context...")
    quotes = market.fetch_taiwan_quotes()

    print("[tw] checking ADR arbitrage...")
    adr = market.fetch_adr_arb_opportunities()

    print("[tw] fetching Taiwan news...")
    tw_news = news.fetch_taiwan_news()
    print(f"[tw] {len(tw_news['zh'])} ZH items, {len(tw_news['en'])} EN items")

    print("[tw] running Taiwan analysis (Claude)...")
    tw_analysis = analyze.run_taiwan_pass(
        taiwan_quotes=quotes,
        taiwan_news_zh=tw_news["zh"],
        taiwan_news_en=tw_news["en"],
        adr_arb=adr,
    )

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "market_context": quotes,
        "adr_arbitrage": adr,
        "news_counts": {"zh": len(tw_news["zh"]), "en": len(tw_news["en"])},
        "analysis": tw_analysis,
    }
    _write_output(output, config.OUTPUT_LATEST_TW, "tw")
    return output

# ============================================================
# PATCH 2: Add this helper to main.py (place it after run_tw()
# and before the CLI/main entrypoint at the bottom).
# ============================================================
 
def run_portfolio(us_output: dict[str, Any] | None = None) -> dict[str, Any]:
    """
    Portfolio pass: refresh mark-to-market, let Claude decide what to do,
    apply decisions, write suggestions.json. Runs AFTER run_us() so we
    can feed Claude the newest discoveries.
 
    Args:
      us_output: the dict returned by run_us() on this same invocation.
                 If None, we read the latest discoveries off disk instead.
    """
    from pathlib import Path
    import json
 
    print("[portfolio] loading state and marking to market...")
    state = pf.load_state()
    state = pf.mark_to_market(state)
    pf.save_state(state)
    print(f"[portfolio] equity=${pf.total_equity(state):.2f} cash=${state['cash']:.2f} open={len(state['open_positions'])}")
 
    # ---- Gather recent flags ----------------------------------
    # We want buy-eligible discoveries from the last N days. The newest
    # run's output is passed in directly (us_output); older ones come
    # from history/us_*.json.
    print(f"[portfolio] gathering flags from last {config.PAPER_PORTFOLIO_DECISION_WINDOW_DAYS}d...")
    recent_flags = _collect_recent_flags(
        us_output=us_output,
        window_days=config.PAPER_PORTFOLIO_DECISION_WINDOW_DAYS,
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
    print(f"[portfolio] running decision pass ({config.CLAUDE_PORTFOLIO_MODEL})...")
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
            ok = _try_buy(state, flag, reasoning_override=reasoning)
            if ok: trade_summary["buys"] += 1
            else: trade_summary["blocked"] += 1
        elif action in ("TRIM", "EXIT"):
            shares = int(d.get("shares_to_sell") or 0) if action == "TRIM" else None
            ok, msg, _ = pf.execute_sell(
                state,
                ticker=tkr,
                shares=shares,
                exit_reasoning=reasoning,
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
            ok = _try_buy(state, flag, reasoning_override=reasoning)
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
 
    pf.save_state(state)
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
) -> bool:
    """Size & execute a buy from a discovery flag. Returns True on success."""
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
    Add SKIP rows for RATIONAL/UNCLEAR flags from the current run,
    so the watching page shows the bot's full decision surface.
    """
    if not us_output:
        return
    for d in (us_output.get("discovery") or {}).get("discoveries", []):
        cls = d.get("classification", "")
        conf = int(d.get("confidence") or 0)
        if cls in ("OVERDONE", "UNDERDONE") and conf >= config.PAPER_PORTFOLIO_MIN_BUY_CONFIDENCE:
            continue  # already handled by the decision loop
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
        choices=["us", "tw", "all"],
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
    args = parser.parse_args()
    print(f"=== agent-smith run [{args.mode}] portfolio={args.portfolio} {datetime.now(timezone.utc).isoformat()} ===")

    us_output = None
    if args.mode in ("us", "all"):
        try:
            us_output = run_us()
        except Exception as e:
            print(f"[us] FAILED: {e}", file=sys.stderr)
            if args.mode == "us":
                sys.exit(1)

    if args.mode in ("tw", "all"):
        try:
            run_tw()
        except Exception as e:
            print(f"[tw] FAILED: {e}", file=sys.stderr)
            if args.mode == "tw":
                sys.exit(1)

    # Grading runs on every US/all invocation; cheap (no LLM) and builds history.
    if args.mode in ("us", "all"):
        try:
            print("[grading] running...")
            grading_out = grading.run()
            n = grading_out.get("overall", {}).get("n_resolved", 0)
            total = grading_out.get("n_total_calls", 0)
            print(f"[grading] {n}/{total} calls resolved; wrote {config.OUTPUT_TRENDS}")
        except Exception as e:
            print(f"[grading] FAILED: {e}")

    # Portfolio pass: only on the designated 22:00 AST run (triggered via --portfolio).
    # Mark-to-market on every other run so the dashboard stays current.
    if args.portfolio:
        if args.mode not in ("us", "all"):
            print("[portfolio] skipped: --portfolio requires mode=us or all")
        else:
            try:
                run_portfolio(us_output=us_output)
            except Exception as e:
                print(f"[portfolio] FAILED: {e}")
                import traceback
                traceback.print_exc()
    else:
        try:
            pf.refresh()
        except Exception as e:
            print(f"[portfolio] mark-to-market failed (non-fatal): {e}")

    print("=== done ===")


if __name__ == "__main__":
    main()
