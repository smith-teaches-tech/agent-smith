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


def main() -> None:
    parser = argparse.ArgumentParser(description="agent-smith orchestrator")
    parser.add_argument(
        "mode",
        choices=["us", "tw", "all"],
        help="which analysis to run",
    )
    args = parser.parse_args()

    print(f"=== agent-smith run [{args.mode}] {datetime.now(timezone.utc).isoformat()} ===")

    if args.mode in ("us", "all"):
        try:
            run_us()
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

     print("=== done ===")


if __name__ == "__main__":
    main()
