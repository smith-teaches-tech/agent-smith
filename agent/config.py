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
CLAUDE_TEMPERATURE = 0.3  # Low — we want consistent analytical output

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

# Current grading logic version. Stamped into every grade so
# threshold changes don't invalidate historical results.
GRADING_LOGIC_VERSION = 1

# ±pct move within horizon to count as HIT or MISS
GRADING_HIT_THRESHOLD_PCT = 3.0

# time_horizon (string) → trading-day count used by grader
GRADING_HORIZON_DAYS = {
    "days": 5,
    "weeks": 20,
    "months": 60,
}

# Only these classifications get graded; others are NOT_GRADED.
# Matches the discovery prompt's output labels (which include "LIKELY"
# and "PARTIALLY" prefixes). Anything with OVERDONE or UNDERDONE in it
# is directional enough to grade.
GRADING_CLASSIFICATIONS_TO_GRADE = [
    "OVERDONE",
    "UNDERDONE",
    "LIKELY OVERDONE",
    "LIKELY UNDERDONE",
    "PARTIALLY OVERDONE",
    "PARTIALLY UNDERDONE",
]

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

# Minimum confidence on a new discovery for Claude to consider a BUY.
# Lower confidence flags can still be WATCHed but never opened.
PAPER_PORTFOLIO_MIN_BUY_CONFIDENCE = 3

# Decision window: Claude sees last N days of flagged OVERDONE/UNDERDONE names
# when making buy/sell decisions.
PAPER_PORTFOLIO_DECISION_WINDOW_DAYS = 7

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
# PORTFOLIO PASS MODEL
# Opus is expensive; portfolio reasoning is cheaper and
# benefits less from maximum reasoning depth.
# ============================================================
CLAUDE_PORTFOLIO_MODEL = "claude-haiku-4-5-20251001"
CLAUDE_PORTFOLIO_MAX_TOKENS = 16384

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
    },
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
    }


# ============================================================
# Output paths (legacy single-screen)
#
# F1: these become backwards-compat aliases pointing at Screen 0's
# paths. New code should call screen_paths(screen_id) directly.
# Old code reads these constants and continues working unchanged.
#
# When the last consumer migrates to screen_paths(), these constants
# can be deleted in a focused cleanup session — no rush; the alias
# is cheap.
# ============================================================
OUTPUT_PORTFOLIO = screen_paths(DEFAULT_SCREEN_ID)["portfolio"]
OUTPUT_PORTFOLIO_HISTORY = screen_paths(DEFAULT_SCREEN_ID)["history"]
OUTPUT_SUGGESTIONS = "docs/data/suggestions.json"