"""
Trump Truth Social post collection.
Uses trumpstruth.org's public RSS feed as the primary source.

This is intentionally fragile — if the source breaks, we degrade
gracefully rather than crash the whole pipeline.
"""
import feedparser
from datetime import datetime, timedelta, timezone
from typing import Any
import re

from . import config
from .news import _parse_date, _clean


def fetch_truth_posts(lookback_hours: int = 24) -> list[dict[str, Any]]:
    """
    Fetch recent Trump Truth Social posts.
    Returns empty list (with warning record) if source is unavailable.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    out: list[dict[str, Any]] = []
    try:
        parsed = feedparser.parse(config.TRUTH_SOCIAL_FEED)
        if parsed.bozo and not parsed.entries:
            return [{
                "_warning": "Trump posts source unavailable",
                "_source": config.TRUTH_SOCIAL_FEED,
                "_error": str(parsed.bozo_exception) if parsed.bozo_exception else "no entries",
            }]
        for entry in parsed.entries:
            pub = _parse_date(entry)
            if pub and pub < cutoff:
                continue
            text = _clean(entry.get("summary", "") or entry.get("description", ""))
            out.append({
                "title": entry.get("title", "").strip(),
                "text": text[:1000],
                "link": entry.get("link", ""),
                "published": pub.isoformat() if pub else None,
            })
    except Exception as e:
        return [{
            "_warning": "Trump posts fetch failed",
            "_error": str(e),
        }]
    return out


# Patterns that have historically moved markets when Trump posts
MARKET_MOVING_PATTERNS = [
    r"\btariff",
    r"\bchina\b",
    r"\btaiwan\b",
    r"\bfed\b",
    r"\binterest rate",
    r"\bpowell\b",
    r"\boil\b",
    r"\bopec\b",
    r"\bsanction",
    r"\btrade deal",
    r"\bpharm",
    r"\bdrug pric",
    r"\bauto",
    r"\bsemiconductor",
    r"\bchip",
    r"\bbitcoin\b|\bcrypto\b",
]


def flag_market_relevant(posts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flag posts containing patterns historically tied to market moves."""
    compiled = [re.compile(p, re.IGNORECASE) for p in MARKET_MOVING_PATTERNS]
    for post in posts:
        if "_warning" in post:
            continue
        text = f"{post.get('title', '')} {post.get('text', '')}"
        matches = []
        for pat, raw in zip(compiled, MARKET_MOVING_PATTERNS):
            if pat.search(text):
                matches.append(raw.replace("\\b", "").replace(r"\\", ""))
        post["market_patterns"] = matches
    return posts


if __name__ == "__main__":
    posts = fetch_truth_posts(lookback_hours=72)
    posts = flag_market_relevant(posts)
    print(f"Fetched {len(posts)} posts")
    for p in posts[:5]:
        if "_warning" in p:
            print(f"  WARNING: {p}")
        else:
            print(f"  [{p.get('market_patterns')}] {p.get('title')[:80]}")
