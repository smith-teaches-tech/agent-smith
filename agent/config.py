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
    "max_candidates_per_run": 40,        # Hard cap before sending to Claude
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

# Taiwan financial news (Chinese — translated by Claude in analysis pass)
TAIWAN_NEWS_SOURCES_ZH = [
    ("Anue 鉅亨網", "https://www.cnyes.com/rss/cat/tw_stock"),
    ("Economic Daily 經濟日報", "https://money.udn.com/rssfeed/news/1001/5590/5612?ch=money"),
    ("CommonWealth 天下", "https://www.cw.com.tw/rss/finance"),
]

# Taiwan English-language sources
TAIWAN_NEWS_SOURCES_EN = [
    ("Focus Taiwan Business", "https://focustaiwan.tw/rss/business.xml"),
    ("Taipei Times Business", "https://www.taipeitimes.com/xml/biz.rss"),
    ("DigiTimes", "https://www.digitimes.com/rss/daily.xml"),
]

# ============================================================
# TAIWAN COVERAGE
# ============================================================

# Major Taiwan tickers for context (.TW = Taiwan Stock Exchange)
TAIWAN_CONTEXT = [
    "2330.TW",  # TSMC
    "2454.TW",  # MediaTek
    "2317.TW",  # Hon Hai / Foxconn
    "2308.TW",  # Delta Electronics
    "2382.TW",  # Quanta Computer
    "3008.TW",  # Largan Precision
    "3711.TW",  # ASE Technology
    "2303.TW",  # UMC
    "^TWII",    # TAIEX index
    "0050.TW",  # FTSE TWSE Taiwan 50 ETF
    "EWT",      # iShares MSCI Taiwan (US-listed)
]

# ADRs to monitor for arbitrage vs local listing
TAIWAN_ADR_PAIRS = [
    ("TSM", "2330.TW"),
    ("UMC", "2303.TW"),
    ("ASX", "3711.TW"),
    ("HIMX", "3504.TW"),
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
CLAUDE_MAX_TOKENS = 4096
CLAUDE_TEMPERATURE = 0.3  # Low — we want consistent analytical output

# Time horizon for "recent" news in each run
NEWS_LOOKBACK_HOURS = 12

# ============================================================
# OUTPUT
# ============================================================

OUTPUT_LATEST_US = "docs/data/latest_us.json"
OUTPUT_LATEST_TW = "docs/data/latest_tw.json"
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
GRADING_CLASSIFICATIONS_TO_GRADE = ["OVERDONE", "UNDERDONE"]

# Phase 1.5-lite output path
OUTPUT_TRENDS = "docs/data/trends.json"
