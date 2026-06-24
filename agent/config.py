"""
agent-smith configuration.
Edit this file to tune what gets tracked and analyzed.
No code changes needed elsewhere when adding tickers or sources.
"""

# ============================================================
# CONTEXT TICKERS - used to interpret broader market moves
# Not the focus, just background to detect "is this stock-specific
# or market/sector-wide?"
# ============================================================

INDICES = ["SPY", "QQQ", "DIA", "IWM", "^VIX"]

SECTOR_ETFS = [
    "XLK",   # Technology
    "XLF",   # Financials
    "XLE",   # Energy
    "XLV",   # Healthcare
    "XLI",   # Industrials
    "XLP",   # Consumer Staples
    "XLY",   # Consumer Discretionary
    "XLU",   # Utilities
    "XLB",   # Materials
    "XLRE",  # Real Estate
    "XLC",   # Communication Services
    "SMH",   # Semiconductors (key for AI cycle)
    "XBI",   # Biotech (small/mid cap heavy)
    "ITA",   # Aerospace & Defense
    "KRE",   # Regional Banks
]

# Mega-caps tracked only as context, never as discovery candidates
MEGA_CAP_CONTEXT = [
    "NVDA", "MSFT", "AAPL", "GOOGL", "AMZN", "META",
    "TSLA", "AVGO", "TSM", "ORCL",
]

# ============================================================
# DISCOVERY UNIVERSE FILTERS
# The system scans broadly, then filters to mid-cap sweet spot
# where mispricings actually exist and persist.
# ============================================================

DISCOVERY_FILTERS = {
    "min_market_cap": 2_000_000_000,    # $2B floor — avoids manipulation
    "max_market_cap": 20_000_000_000,   # $20B ceiling — still small enough for inefficiency
    "min_avg_dollar_volume": 10_000_000,  # $10M daily — ensures clean fills
    "min_price": 5.00,                   # Avoid penny territory
    "exclude_otc": True,
    "exclude_recent_ipos_days": 60,      # New IPOs are too volatile to read
}

# How big a move qualifies as "interesting" for the discovery scan
MOVEMENT_THRESHOLDS = {
    "intraday_pct_min": 4.0,             # +/- 4% intraday
    "volume_multiple_min": 2.5,          # 2.5x average volume
    "max_candidates_per_run": 20,        # Hard cap before sending to Claude
    # Stratified sampling: take K from each move-size bucket instead of
    # top-N globally. Motivation in selection_analysis.md (May 9 2026):
    # all 5 OVERDONE flags in 151-flag dataset lived in the 4-8% bucket.
    # Originally introduced to fight 1003-ticker universe dilution where
    # top-N-by-magnitude was dominated by 10%+ movers. After the May 12
    # rollback to the curated 80-ticker universe the buckets still pull
    # their weight: on quiet days the cascade backfills toward 4-8%
    # automatically, and the bucket-distribution diagnostic is a useful
    # health signal regardless of universe size. Caps sum to
    # max_candidates_per_run (20). Buckets are based on abs(change_pct).
    # Volume-only admits land in "<4%". Spillover from under-filled
    # buckets cascades downward (largest → smallest) to preserve the
    # small-mover bias when the big-mover buckets are sparse.
    "stratified_sampling": True,         # toggle for A/B comparison if needed
    "stratified_buckets": [
        # (label, lo_inclusive, hi_exclusive, cap)
        ("<4%",     0.0,    4.0,    2),
        ("4-6%",    4.0,    6.0,    6),
        ("6-8%",    6.0,    8.0,    4),
        ("8-10%",   8.0,   10.0,    3),
        ("10-15%", 10.0,   15.0,    3),
        ("15%+",   15.0, 9999.0,    2),
    ],
}

# ============================================================
# CATALYST KEYWORDS - what to scan news for
# ============================================================

CATALYST_KEYWORDS = [
    # Regulatory
    "FDA approval", "FDA rejection", "FDA clearance",
    "phase 3", "phase 2", "clinical trial", "PDUFA",
    "DOJ", "SEC investigation", "antitrust",
    # Corporate actions
    "acquisition", "merger", "takeover", "buyout",
    "spinoff", "spin-off", "tender offer",
    # Operational
    "contract win", "supply agreement", "design win",
    "guidance cut", "guidance raise", "preannouncement",
    "recall", "supply disruption", "facility shutdown",
    # Activist / short
    "Hindenburg", "Citron", "short report", "activist",
    "13D filing", "proxy fight",
    # Insider signals
    "insider buying", "10b5-1", "Form 4", "cluster buying",
    # Earnings
    "earnings beat", "earnings miss", "raised forecast",
    "lowered forecast", "withdrew guidance",
    # Macro/policy
    "tariff", "export ban", "sanctions", "executive order",
]

# Red flags - stocks/news to be extra skeptical of
RED_FLAGS = [
    "promoted by", "sponsored content", "investor awareness",
    "reverse stock split", "going concern", "delisting notice",
    "shell company", "SPAC merger announcement",
]

# ============================================================
# NEWS SOURCES
# ============================================================

# English RSS feeds — broad market and business news
RSS_FEEDS_EN = [
    ("Reuters Business", "https://feeds.reuters.com/reuters/businessNews"),
    ("CNBC Top News", "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
    ("MarketWatch Top", "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
    ("Yahoo Finance", "https://finance.yahoo.com/news/rssindex"),
    ("Seeking Alpha Market", "https://seekingalpha.com/market_currents.xml"),
    ("Benzinga", "https://www.benzinga.com/feed"),
]

# AI announcement sources — for the AI catalyst module
AI_NEWS_SOURCES = [
    ("Anthropic", "https://www.anthropic.com/news"),
    ("OpenAI", "https://openai.com/blog"),
    ("Google DeepMind", "https://deepmind.google/discover/blog/"),
    ("Meta AI", "https://ai.meta.com/blog/"),
    ("TechCrunch AI", "https://techcrunch.com/category/artificial-intelligence/feed/"),
    ("The Information AI", "https://www.theinformation.com/feed"),
]

# ============================================================
# TRUMP POSTS
# ============================================================

# trumpstruth.org aggregates Truth Social posts publicly
# This is fragile — may need adjustment if site changes
TRUTH_SOCIAL_FEED = "https://trumpstruth.org/feed"

# ============================================================
# CLAUDE / API CONFIG
# ============================================================

CLAUDE_MODEL = "claude-opus-4-7"  # Top model for analysis quality
CLAUDE_MAX_TOKENS = 16384  # May 7 PM: reduced from 32768 after Actions hit Anthropic SDK's
                           # "streaming required for >10min operations" gate. 32k was overkill;
                           # post-EDGAR-enrichment runs estimate ~14k tokens output, so 16k is
                           # comfortable headroom. Still 4x the original (broken) 4k cap.
                           # Followup: switch to streaming API (analyze.py) so we can raise this
                           # again. See agent-smith-roadmap.md "Session C followups".

# Time horizon for "recent" news in each run
NEWS_LOOKBACK_HOURS = 12

# ============================================================
# OUTPUT
# ============================================================

OUTPUT_LATEST_US = "docs/data/latest_us.json"
OUTPUT_HISTORY_DIR = "docs/data/history"

# ============================================================
# GRADING (Phase 1.5-lite)
# ============================================================
# Only GRADING_HORIZON_DAYS is consumed by code (main._horizon_to_days
# reads it). Threshold, logic version, and classifications-to-grade
# are owned by `grading.py` directly (LOGIC_VERSION = 2, GRADING_PARAMS,
# and the per-version classifications list); the prior config-side
# fossils were never wired up. Removed May 12.

# time_horizon (string) → trading-day count used by grader
GRADING_HORIZON_DAYS = {
    "days": 5,
    "weeks": 20,
    "months": 60,
}

# Phase 1.5-lite output path
OUTPUT_TRENDS = "docs/data/trends.json"

# ============================================================
# PAPER PORTFOLIO — global defaults
#
# These remain the "global defaults" — every screen registered in SCREENS
# below inherits these values unless it overrides them. Screen 0 (the
# legacy general-mispricing screen) inherits all of them so its behavior
# is byte-identical to the pre-F1 single-portfolio code path.
# ============================================================

PAPER_PORTFOLIO_BANKROLL = 10_000.0        # Starting cash in USD
PAPER_PORTFOLIO_MAX_POSITION_PCT = 0.25    # No single name > 25% of total bankroll
PAPER_PORTFOLIO_MAX_SECTOR_PCT = 0.40      # No single sector > 40% of total bankroll
PAPER_PORTFOLIO_MIN_CASH_PCT = 0.10        # Always keep at least 10% in cash

# Catastrophe stop-loss floor for the code-enforced exit sweep
# (portfolio.force_exit_stop_and_horizon). POSITIVE number meaning a
# NEGATIVE return: 15.0 -> force-close any position whose unrealized
# return is <= -15%. A wide *catastrophe* floor, not a trading stop --
# wide enough that normal mid-cap vol over a 5-20d horizon doesn't
# whipsaw a position out, tight enough to stop a slow bleeder from being
# ridden all the way down. Global across screens; can be lifted into the
# per-screen SCREENS dicts later if a screen needs its own.
STOP_LOSS_PCT = 15.0       # Force-exit at -15% unrealized

# Minimum confidence on a new discovery for Claude to consider a BUY.
# Lower confidence flags can still be WATCHed but never opened.
PAPER_PORTFOLIO_MIN_BUY_CONFIDENCE = 3

# Decision window: Claude sees last N days of flagged OVERDONE/UNDERDONE names
# when making buy/sell decisions.
PAPER_PORTFOLIO_DECISION_WINDOW_DAYS = 7

# ============================================================
# Exploratory tier (May 12, 2026 — Session D shipped)
#
# A second BUY lane for flags with a real catalyst + meaningful
# confidence that don't reach the conviction bar but Haiku judges
# worth a small test position. Goal: more grading data, faster
# learning loop, capped downside (6% × 4 positions = 24% of
# bankroll at peak vs. ~0% under the conviction-only regime that
# made one trade in 30 days).
#
# The eligibility rule below is a *gate* — flags that don't satisfy
# it never reach Haiku as exploratory candidates. Haiku still
# decides BUY vs WATCH vs SKIP within the gated pool, and decides
# CONVICTION vs EXPLORATORY tier per flag. The hard cap on
# simultaneous exploratory positions is enforced at apply time
# (not in prompt), so the cap is auditable.
#
# Sizing is enforced via `pf.size_position(..., target_pct_override=
# EXPLORATORY_TIER["position_pct_of_cash"])`, which routes through
# the same 25%/40%/10% guardrails as conviction sizing — so this
# tier can't sneak around the position/sector/cash caps.
# ============================================================
EXPLORATORY_TIER = {
    # Sizing
    "position_pct_of_cash": 0.06,            # 6% of equity per exploratory position
    "max_simultaneous": 4,                    # hard cap, per screen
    # Eligibility gate — a flag must pass ALL of these to even appear
    # to Haiku as an exploratory candidate (Haiku then decides tier).
    "eligibility": {
        "min_confidence": 3,                  # conf 3+ required
        "require_catalyst_url": True,         # must cite a real catalyst
        "require_thesis_fields_populated": True,  # pedagogical schema fields non-stub
    },
}

# ============================================================
# Re-entry guard (May 19, 2026)
#
# Problem this fixes: the portfolio pass evaluates every discovery
# flag cold. It has no memory of positions it recently closed. On
# May 18 the bot re-bought AEIS three days after closing AEIS at
# -6.48% — and the re-buy thesis was half "ongoing semicap sector
# pressure", which the *exit* reasoning three days earlier had
# explicitly identified as non-tradeable sector beta. The system
# re-bought a name on a thesis it had already falsified, because
# closed positions had no voice in the next decision.
#
# The guard, in two parts:
#   1. CONTEXT (prompt-side, analyze.run_portfolio_pass): if a
#      flagged ticker was closed recently, Haiku is shown the prior
#      exit post-mortem and instructed that re-entry requires
#      genuinely NEW information — not a restatement of a thesis
#      already closed out.
#   2. FLOOR (code-side, main.run_portfolio_for_screen, after the
#      red-team): any surviving BUY on a recently-closed ticker
#      whose flag confidence is below RE_ENTRY_MIN_CONFIDENCE is
#      downgraded to WATCH. The prompt instruction is a backstop;
#      this floor is the discipline. Mirrors how the red-team and
#      exploratory-cap downgrades already work.
#
# Fires on BOTH losses and wins: re-buying a name just sold for a
# gain is performance-chasing and deserves the same higher bar.
# The exit reasoning is surfaced either way so Haiku can frame the
# two cases differently.
#
# "Recently" is horizon-tied: the window is the closed position's
# own flag_horizon mapped through GRADING_HORIZON_DAYS (days→5,
# weeks→20, months→60 calendar days), floored at
# RE_ENTRY_WINDOW_FLOOR_DAYS so a short "days" horizon can't give
# a 5-day window that misses a day-6 re-buy. A name re-flagged
# while its prior thesis window is still conceptually live is
# exactly the suspicious case.
# ============================================================

# Minimum flag confidence to re-open a recently-closed name. One
# notch above PAPER_PORTFOLIO_MIN_BUY_CONFIDENCE (3): a fresh name
# can open at conf 3, a recently-closed name needs conf 4. If the
# new catalyst can't carry conf 4 on its own, re-entry is WATCH.
RE_ENTRY_MIN_CONFIDENCE = 4

# Floor for the horizon-tied lookback window, in calendar days.
# A "days"-horizon close maps to 5 days via GRADING_HORIZON_DAYS;
# this floor lifts that to 10 so a re-buy a week after exit is
# still caught. "weeks" (20) and "months" (60) already exceed it.
RE_ENTRY_WINDOW_FLOOR_DAYS = 10

# ============================================================
# IBKR Pro Tiered fees (paper-trading model)
# Numbers match IBKR's published pricing as of 2026.
# These are screen-agnostic — every screen pays the same broker.
# ============================================================
IBKR_COMMISSION_PER_SHARE = 0.0035         # Base tier
IBKR_COMMISSION_MIN = 0.35                 # Per-order minimum
IBKR_COMMISSION_MAX_PCT = 0.01             # Cap at 1% of trade value

# Pass-through exchange/regulatory fees
IBKR_NYSE_PASSTHRU_PER_SHARE = 0.003       # NYSE/ARCA fee for remove-liquidity
IBKR_FINRA_TAF_PER_SHARE = 0.000166        # FINRA Trading Activity Fee (sells only)
IBKR_SEC_FEE_PCT = 0.0000278               # SEC fee (sells only, % of notional)
IBKR_CLEARING_PER_SHARE = 0.0002           # Clearing/settlement

# Slippage assumption — we execute at open but don't get the exact print
PAPER_SLIPPAGE_PCT = 0.001                 # 0.1%

# ============================================================
# BENCHMARK COMPARISON (Build queue item 3, May 16, 2026)
#
# Every paper trade captures SPY and IWM prices at open and at close,
# so closed trades can report alpha-vs-benchmark instead of raw P&L.
# Raw "+17.9% on EXTR" is meaningless without "vs SPY +X% same window".
#
# Both benchmarks are always stored on every trade. Which one is the
# *primary* comparison is a display decision resolved at render time
# in portfolio.html using BENCHMARK_PRIMARY_CAP_USD:
#   market cap >= threshold  -> SPY is primary  (large/mid-cap proxy)
#   market cap <  threshold  -> IWM is primary  (small-cap proxy)
# Storing both keeps portfolio.py purely mechanical and means changing
# the rule is a one-line dashboard edit, not a data migration.
#
# Benchmark capture is best-effort: a failed fetch stores null and the
# trade still proceeds. Benchmark data is nice-to-have, never a gate
# on executing a paper trade.
# ============================================================
BENCHMARK_TICKERS = ["SPY", "IWM"]         # captured at every open and close
BENCHMARK_PRIMARY_CAP_USD = 5_000_000_000  # >= $5B -> SPY primary, else IWM

# ============================================================
# PORTFOLIO PASS MODEL
# Opus is expensive; portfolio reasoning is cheaper and
# benefits less from maximum reasoning depth.
# ============================================================
CLAUDE_PORTFOLIO_MODEL = "claude-haiku-4-5-20251001"
CLAUDE_PORTFOLIO_MAX_TOKENS = 16384

# ============================================================
# RED-TEAM PASS MODEL (queued item 2, May 12, 2026)
#
# After Haiku makes a BUY decision in the portfolio pass, the red-
# team pass argues the OPPOSITE case for that ticker and returns a
# survived/killed verdict per BUY. Only survivors proceed to
# _try_buy; killed BUYs are downgraded to WATCH with the bear
# critique as the reasoning, so the dissent is visible in the
# suggestions UI.
#
# Haiku again — cheap, structured, one-shot per BUY decision.
# Output is a small JSON list; 4096 max_tokens is plenty.
# Roadmap cost estimate: ~$0.005-0.01 per BUY × typically 1-3 BUYs
# per pass = ~$0.03/run worst case.
# ============================================================
CLAUDE_RED_TEAM_MODEL = "claude-haiku-4-5-20251001"
CLAUDE_RED_TEAM_MAX_TOKENS = 4096

# ============================================================
# SCREENS REGISTRY (F1 — multi-screen architecture)
#
# Each screen is one named bet on the market with its own paper
# portfolio, dashboard tab, and grading bucket. Adding a new screen
# is a new entry here plus a per-screen module under agent/screens/
# (the per-screen module lands in F2; F1 only registers Screen 0).
#
# Required keys per screen:
#   id                  — short stable identifier; becomes the file
#                         basename for portfolios/{id}.json. Must be
#                         filesystem-safe (lowercase, underscores).
#   display_name        — human-readable name for dashboard headers.
#   thesis_summary      — one-line description of what the screen bets
#                         on. Surfaces in the master dashboard.
#   bankroll            — starting paper cash in USD.
#   max_position_pct    — single-position cap as fraction of equity.
#   max_sector_pct      — single-sector cap as fraction of equity.
#   min_cash_pct        — minimum cash reserve as fraction of equity.
#   min_buy_confidence  — minimum discovery confidence for BUY eligibility.
#   decision_window_days— how far back the portfolio pass looks for flags.
#   claude_model        — model used for this screen's portfolio decisions.
#
# Screen 0 inherits every value from the PAPER_PORTFOLIO_* globals above
# so behavior is byte-identical to pre-F1 code paths. Future screens
# (F2+) can deviate per-screen — each named bet has different base-rate
# expectations and may want different sizing / window / model.
# ============================================================

SCREENS: list[dict] = [
    {
        "id": "screen_0",
        "display_name": "General mispricing",
        "thesis_summary": (
            "Wide-net OVERDONE/UNDERDONE labeling. Flags movers whose price "
            "action looks behaviorally inconsistent with their available "
            "catalyst signal. Legacy framing — runs as the comparison "
            "baseline for named-thesis screens."
        ),
        "bankroll": PAPER_PORTFOLIO_BANKROLL,
        "max_position_pct": PAPER_PORTFOLIO_MAX_POSITION_PCT,
        "max_sector_pct": PAPER_PORTFOLIO_MAX_SECTOR_PCT,
        "min_cash_pct": PAPER_PORTFOLIO_MIN_CASH_PCT,
        "min_buy_confidence": PAPER_PORTFOLIO_MIN_BUY_CONFIDENCE,
        "decision_window_days": PAPER_PORTFOLIO_DECISION_WINDOW_DAYS,
        "claude_model": CLAUDE_PORTFOLIO_MODEL,
        # Screen 0 uses the two-tier conviction/exploratory model. The
        # portfolio prompt requires tier on every BUY; main.py's
        # tier-gate enforces this.
        "uses_tiers": True,
        # Exploratory tier cap. Screen 0 flags ~daily on price-mover
        # signal, so 4 simultaneous is the right cap — keeps total
        # exploratory exposure to 24% of bankroll. (L2, May 27)
        "exploratory_cap": 4,
    },

        {
        "id": "screen_1",
        "display_name": "AI-event sympathy fade",
        "thesis_summary": (
            "Buys mid-caps that retail panic-sold on AI-lab announcements "
            "(OpenAI/Anthropic/etc.) when per-name 10-K + 10-Q reading "
            "shows the company's actual business is minimally or not "
            "exposed to the shipped capability. Holds 5–15 trading days "
            "while institutional money slowly reprices on filing analysis."
        ),
        "bankroll": PAPER_PORTFOLIO_BANKROLL,
        # Guardrails: match Screen 0 for now. May tighten later once
        # we have data on Screen 1's hit-rate distribution.
        "max_position_pct": PAPER_PORTFOLIO_MAX_POSITION_PCT,
        "max_sector_pct": PAPER_PORTFOLIO_MAX_SECTOR_PCT,
        "min_cash_pct": PAPER_PORTFOLIO_MIN_CASH_PCT,
        # Confidence threshold: match Screen 0 (conf>=4 to buy). Screen 1's
        # confidence calibration is in ai_sympathy.py's discovery prompt;
        # the threshold here gates which calls reach the portfolio pass.
        "min_buy_confidence": PAPER_PORTFOLIO_MIN_BUY_CONFIDENCE,
        # Decision window: 3 trading days. AI-event fade is fast — a flag
        # from 5+ days ago is stale (the institutional repricing has
        # already started or the move has already faded). Tighter than
        # Screen 0's window because the thesis itself has a tighter clock.
        "decision_window_days": 3,
        # Holding window: 15 trading days. Screen 1's thesis says
        # institutional reading-and-repricing happens within ~3 weeks of
        # the AI announcement. After 15 days, the sympathy-fade thesis
        # has failed for that name and the position is force-exited
        # regardless of P&L (the discipline that prevents Screen 1
        # drifting into long-term value territory).
        "holding_window_days": 15,
        # Portfolio-pass model: Haiku 4.5 (matches Screen 0 convention).
        # Discovery model is Opus, hardcoded in ai_sympathy.py.
        "claude_model": CLAUDE_PORTFOLIO_MODEL,
        # Screen 1 uses the two-tier conviction/exploratory model
        # (added May 20, 2026 — the original prompt didn't require
        # tier, which caused valid BUYs to auto-convert to WATCH).
        "uses_tiers": True,
        # Exploratory tier cap. Screen 1 only fires on AI-lab trigger
        # days, and when it fires it tends to produce a basket of 3-6
        # same-trigger candidates (e.g. May 27 Google Search event:
        # NCNO + PAYC + VRRM all flagged together). A cap of 4 starves
        # the basket; 6 lets Screen 1 size the whole trigger sweep
        # while still bounding total exposure to 36% of bankroll. (L2)
        "exploratory_cap": 6,
    },
    # Screen 2 (pre-earnings filings read) REMOVED 2026-06-24 — thesis
    # abandoned (poor cost/signal: ~$1/day Opus filings reads for 3
    # round-trips over 3 weeks, then silent). Discovery orchestrator,
    # portfolio branches, and screen_2 code modules deleted in the same
    # change. docs/data/*screen_2* history kept as an audit trail.
]

# ============================================================
# Screen lookup helpers
# ============================================================

# Default screen id used by code paths that pre-date F1. Pointed at
# Screen 0 so any unparameterized call lands on the legacy bucket.
DEFAULT_SCREEN_ID = "screen_0"

# Screens directory — per-screen state files live here.
# Layout:
#   docs/data/portfolios/screen_0.json              ← current state
#   docs/data/portfolios/screen_0_history.json      ← append-only log
# (suggestions.json is still written at docs/data/suggestions.json
# in F1; per-screen suggestions paths arrive when Screen 1 needs its
# own file in F2.)
PORTFOLIOS_DIR = "docs/data/portfolios"


def get_screen(screen_id: str) -> dict:
    """
    Return the SCREENS entry matching screen_id. Raises KeyError if
    no such screen is registered. Use this at the boundary where a
    screen_id from external input enters the system; downstream code
    can then trust the dict's shape.
    """
    for s in SCREENS:
        if s["id"] == screen_id:
            return s
    raise KeyError(
        f"unknown screen_id={screen_id!r}; registered: "
        f"{[s['id'] for s in SCREENS]}"
    )


def screen_paths(screen_id: str) -> dict[str, str]:
    """
    Return the canonical filesystem paths for a screen's state files.
    Centralized here so code paths agree on the layout and a future
    layout change touches one place.
    """
    return {
        "portfolio": f"{PORTFOLIOS_DIR}/{screen_id}.json",
        "history": f"{PORTFOLIOS_DIR}/{screen_id}_history.json",
        "thesis_log": f"{PORTFOLIOS_DIR}/{screen_id}_thesis_log.json",
        "suggestions": f"docs/data/{screen_id}_suggestions.json",
    }


# ============================================================
# Output paths
#
# screen_paths(screen_id) is the canonical API for new code. The
# OUTPUT_PORTFOLIO / OUTPUT_PORTFOLIO_HISTORY / OUTPUT_SUGGESTIONS
# back-compat aliases were removed May 12 after the last consumer
# migrated.
#
# OUTPUT_SUGGESTIONS_LEGACY remains: main._write_suggestions still
# also writes the un-prefixed `docs/data/suggestions.json` for the
# existing dashboard. Once `docs/suggestions.html` reads
# `screen_0_suggestions.json` directly, that alias write can be
# removed and this constant can be deleted too.
# ============================================================
OUTPUT_SUGGESTIONS_LEGACY = "docs/data/suggestions.json"

# Red-team verdict log directory. One append-only JSON array per
# screen at `red_team/{screen_id}.json` — same pattern as the
# portfolio history files. Append-only on write; consumers (dashboard
# strip, future grader) apply their own time window on read.
OUTPUT_RED_TEAM_DIR = "docs/data/red_team"