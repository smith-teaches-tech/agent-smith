"""
News collection from RSS feeds.
Pulls recent items, filters by lookback window, identifies catalyst keywords.
"""
import feedparser
from datetime import datetime, timedelta, timezone
from typing import Any
import re

from . import config


def _parse_date(entry: Any) -> datetime | None:
    """Best-effort date parsing from a feedparser entry."""
    for attr in ("published_parsed", "updated_parsed"):
        val = getattr(entry, attr, None) or entry.get(attr) if isinstance(entry, dict) else getattr(entry, attr, None)
        if val:
            try:
                return datetime(*val[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
    return None


def fetch_rss_feed(name: str, url: str, lookback_hours: int) -> list[dict[str, Any]]:
    """Fetch and filter a single RSS feed by recency."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    items: list[dict[str, Any]] = []
    try:
        parsed = feedparser.parse(url)
        for entry in parsed.entries:
            pub = _parse_date(entry)
            if pub and pub < cutoff:
                continue
            items.append({
                "source": name,
                "title": entry.get("title", "").strip(),
                "summary": _clean(entry.get("summary", ""))[:500],
                "link": entry.get("link", ""),
                "published": pub.isoformat() if pub else None,
            })
    except Exception as e:
        items.append({"source": name, "error": str(e)})
    return items


def _clean(html: str) -> str:
    """Strip HTML tags and collapse whitespace from RSS summaries."""
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def fetch_all_english_news(
    lookback_hours: int = None,
) -> list[dict[str, Any]]:
    """Fetch all configured English RSS feeds."""
    if lookback_hours is None:
        lookback_hours = config.NEWS_LOOKBACK_HOURS
    out: list[dict[str, Any]] = []
    for name, url in config.RSS_FEEDS_EN:
        out.extend(fetch_rss_feed(name, url, lookback_hours))
    return out


def tag_catalysts(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Tag each news item with detected catalyst keywords.
    Adds 'catalysts' field with list of matched terms.
    Items with at least one catalyst are higher priority for Claude.
    """
    keywords_lower = [k.lower() for k in config.CATALYST_KEYWORDS]
    red_flags_lower = [r.lower() for r in config.RED_FLAGS]
    for item in items:
        if "error" in item:
            continue
        text = f"{item.get('title', '')} {item.get('summary', '')}".lower()
        item["catalysts"] = [
            k for k, kl in zip(config.CATALYST_KEYWORDS, keywords_lower)
            if kl in text
        ]
        item["red_flags"] = [
            r for r, rl in zip(config.RED_FLAGS, red_flags_lower)
            if rl in text
        ]
    return items


def fetch_ai_news(lookback_hours: int = None) -> list[dict[str, Any]]:
    """
    Fetch AI announcement news specifically.
    These get a separate analytical pass with bias safeguards.
    """
    if lookback_hours is None:
        lookback_hours = config.NEWS_LOOKBACK_HOURS * 2  # AI news lookback longer
    out: list[dict[str, Any]] = []
    for name, url in config.AI_NEWS_SOURCES:
        # Some AI sources are HTML pages, not RSS. Try RSS first; if no
        # entries returned, skip — the analyzer will note the gap.
        out.extend(fetch_rss_feed(name, url, lookback_hours))
    return out


def fetch_taiwan_news(lookback_hours: int = None) -> dict[str, list[dict[str, Any]]]:
    """
    Fetch Taiwan news in both Chinese and English.
    Returns {'zh': [...], 'en': [...]} — analyzer translates Chinese as needed.
    """
    if lookback_hours is None:
        lookback_hours = config.NEWS_LOOKBACK_HOURS
    zh: list[dict[str, Any]] = []
    en: list[dict[str, Any]] = []
    for name, url in config.TAIWAN_NEWS_SOURCES_ZH:
        zh.extend(fetch_rss_feed(name, url, lookback_hours))
    for name, url in config.TAIWAN_NEWS_SOURCES_EN:
        en.extend(fetch_rss_feed(name, url, lookback_hours))
    return {"zh": zh, "en": en}


if __name__ == "__main__":
    # Smoke test
    items = fetch_all_english_news(lookback_hours=24)
    items = tag_catalysts(items)
    print(f"Pulled {len(items)} items")
    flagged = [i for i in items if i.get("catalysts")]
    print(f"With catalysts: {len(flagged)}")
    for i in flagged[:5]:
        print(f"  - [{i.get('catalysts')}] {i.get('title')}")
