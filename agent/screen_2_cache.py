"""
agent.screens.screen_2_cache — Per-ticker filings-bundle memoization for Screen 2.

THE PROBLEM
-----------
Screen 2 re-reads the same 10-K + 10-Q + last-4 8-Ks for the same
~5 tickers ~5 days in a row by construction (earnings 3-7 trading
days out = same name appears in ~5 consecutive trigger windows).
Inspection of seven consecutive May 2026 runs showed:

  - 9 unique tickers across 7 runs = 35 trigger slots
  - ESTC / MDB / ASAN each re-evaluated 6 times
  - Filings did not change between runs (every run logged
    "all 8-Ks fell back to cover documents")
  - Model verdict bounced (OVER -> OVER -> SKIP -> SKIP) on
    identical input

At ~$1/day, the steady-state slate is being paid for ~5x. This module
fixes that by memoizing the per-ticker discovery row, keyed on a stable
hash of the filings bundle.

DESIGN
------
- One file per (ticker, filings_hash). Re-paid only when a NEW filing
  appears (new 8-K accession, new 10-Q filing date, etc.) — which is
  the only condition under which the model's output should rationally
  change.
- Stored under `docs/data/screen_2_cache/`. File-based, no DB.
- Each file holds ONE discovery row (the per-ticker dict that would
  have ended up in `parsed["discoveries"]` or `parsed["skipped"]`),
  plus a small `_cache_meta` block for observability.
- Lookup is by filings_hash, not by date. A ticker can sit in cache
  for as long as its filings don't change. When a new filing lands,
  the hash changes and the next run re-pays for that one ticker.
- No TTL by default — filings are the source of truth. An optional
  `max_age_days` parameter is exposed for defensive flushing.

WHAT IS NOT CACHED
------------------
- Discoveries are cached at the per-ticker row level, not at the
  pass-level (`run_summary`, `no_signals_note`). Pass-level fields
  remain fresh on every run, since they reflect the slate composition
  on that run, not any single ticker.
- The cache does not store the system prompt or user content — only
  the structured per-ticker output the model produced. If the system
  prompt changes (a Screen 2 re-design), bumping PROMPT_VERSION below
  invalidates all entries so the next run re-pays under the new
  prompt.

INVARIANTS
----------
- A cache hit MUST be indistinguishable from a fresh model row in
  downstream consumers (history files, grading, dashboard). The
  `_cache_meta` block is the only added field; everything else is
  pass-through.
- A cache miss MUST NOT corrupt other tickers in the same run. Per-
  ticker failures (read or write) are logged and degrade to "no
  cache hit, treat as fresh" — never raise.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
from datetime import datetime, timezone
from typing import Any


# ============================================================
# Storage location
# ============================================================
# Files written under docs/data/screen_2_cache/ alongside the existing
# history/ and red_team/ data directories. Resolved relative to this
# module so the path is independent of the cron's working directory.

_CACHE_DIR = (
    pathlib.Path(__file__).resolve().parent.parent.parent
    / "docs" / "data" / "screen_2_cache"
)


# ============================================================
# Versioning
# ============================================================
# Bumped any time the Screen 2 discovery prompt changes in a way that
# would make a cached row stale even when the underlying filings have
# not changed. Examples: tightening the OVERDONE / UNDERDONE rules,
# adding a new schema field, changing the confidence calibration.
#
# Bumping this number invalidates every existing cache entry — the
# next run will re-pay for every ticker once, under the new prompt.
# Cheap; the existing files stay on disk (debuggable).

PROMPT_VERSION = 1


# ============================================================
# Hashing the filings bundle
# ============================================================

def filings_hash(filings: dict[str, Any]) -> str:
    """
    Compute a stable hash identifying the filings bundle. Two bundles
    with the same hash represent the same underlying filings and would
    rationally produce the same discovery row.

    Hashes filing IDENTITY, not filing TEXT:
      - 10-K Business:       filing_date + truncated flag
      - 10-K Risk Factors:   filing_date + truncated flag
      - 10-Q Risk Factors:   filing_date + truncated flag
      - earnings_8ks:        ordered list of (filing_date, fell_back_to_primary)

    Rationale: the text is large and stable. Filing dates are small,
    monotonic identifiers — they change exactly when the underlying
    SEC document changes (a new 10-Q is filed; a new 8-K appears in
    the bundle). Hashing the dates rather than the bodies makes the
    hash cheap and the invariant easy to reason about.

    The PROMPT_VERSION is mixed in so a prompt redesign automatically
    invalidates the cache.

    Returns a 12-character hex digest — short enough for readable
    filenames, long enough to avoid collisions across 80 universe
    tickers (chance of collision in our scale: ~0).

    Defensive: returns "unhashable" if anything goes wrong, which
    causes every lookup to miss (safe — we re-pay rather than serve
    stale data).
    """
    try:
        ident: dict[str, Any] = {
            "_prompt_version": PROMPT_VERSION,
        }

        for key in ("business", "k10_risk", "q10_risk"):
            f = filings.get(key)
            if not f:
                ident[key] = None
            else:
                ident[key] = {
                    "filing_date": f.get("filing_date"),
                    "truncated": bool(f.get("truncated")),
                }

        eights: list[dict[str, Any]] = []
        for e in (filings.get("earnings_8ks") or []):
            eights.append({
                "filing_date": e.get("filing_date"),
                "fell_back_to_primary": bool(e.get("fell_back_to_primary")),
                "truncated": bool(e.get("truncated")),
            })
        ident["earnings_8ks"] = eights

        # sort_keys for stability — dict insertion order must not
        # affect the hash.
        payload = json.dumps(ident, sort_keys=True).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()[:12]
    except Exception as e:
        print(f"[screen_2_cache] filings_hash failed: {e}")
        return "unhashable"


# ============================================================
# Cache file paths
# ============================================================

def _cache_path(ticker: str, fh: str) -> pathlib.Path:
    """Path for a (ticker, filings_hash) cache entry."""
    ticker = (ticker or "").upper()
    return _CACHE_DIR / f"{ticker}_{fh}.json"


def _ensure_dir() -> None:
    """Create the cache directory on first use. Idempotent."""
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        # Read-only filesystem or permission issue — degrade to
        # "no cache" rather than crash the screen.
        print(f"[screen_2_cache] cannot create {_CACHE_DIR}: {e}")


# ============================================================
# Public API: lookup
# ============================================================

def lookup(
    ticker: str,
    filings: dict[str, Any],
    max_age_days: int | None = None,
) -> dict[str, Any] | None:
    """
    Look up a cached discovery row for this ticker + filings bundle.

    Args:
      ticker:       case-insensitive ticker symbol.
      filings:      the filings bundle produced by
                    edgar.get_filings_for_pre_earnings_read.
      max_age_days: optional defensive age cap. When set, cache
                    entries written more than this many days ago are
                    treated as misses (re-paid). Default None = no
                    age cap (filings identity is the only invariant).

    Returns:
      The cached discovery row (a dict matching the shape Opus would
      have produced for this ticker), or None on a cache miss.

    Never raises. Read errors degrade to a miss.
    """
    ticker = (ticker or "").upper()
    if not ticker:
        return None

    fh = filings_hash(filings)
    if fh == "unhashable":
        return None

    path = _cache_path(ticker, fh)
    if not path.exists():
        return None

    try:
        row = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"[screen_2_cache] read failed for {path.name}: {e}")
        return None

    # Optional defensive age check
    if max_age_days is not None:
        cached_at_iso = (row.get("_cache_meta") or {}).get("cached_at")
        if cached_at_iso:
            try:
                cached_at = datetime.fromisoformat(cached_at_iso)
                age_days = (datetime.now(timezone.utc) - cached_at).total_seconds() / 86400
                if age_days > max_age_days:
                    return None
            except ValueError:
                # Bad timestamp — treat as miss.
                return None

    # Stamp HIT metadata onto the returned row so the run JSON shows
    # which discoveries were served from cache. Doesn't mutate the
    # file on disk; just annotates the in-memory copy returned to the
    # caller.
    meta = dict(row.get("_cache_meta") or {})
    meta["served_from_cache"] = True
    meta["served_at"] = datetime.now(timezone.utc).isoformat()
    out = dict(row)
    out["_cache_meta"] = meta
    return out


# ============================================================
# Public API: store
# ============================================================

def store(
    ticker: str,
    filings: dict[str, Any],
    row: dict[str, Any],
) -> None:
    """
    Cache one per-ticker discovery row.

    `row` is the per-ticker dict the model produced (one entry from
    parsed["discoveries"] or parsed["skipped"]). It is written
    verbatim, with one added `_cache_meta` block.

    Never raises. Write errors are logged.
    """
    ticker = (ticker or "").upper()
    if not ticker:
        return

    fh = filings_hash(filings)
    if fh == "unhashable":
        # If we can't hash the input, we can't safely cache the output.
        return

    _ensure_dir()
    path = _cache_path(ticker, fh)

    payload = dict(row)
    payload["_cache_meta"] = {
        "ticker": ticker,
        "filings_hash": fh,
        "prompt_version": PROMPT_VERSION,
        "cached_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as e:
        print(f"[screen_2_cache] write failed for {path.name}: {e}")


# ============================================================
# Public API: partition a batch into hits and misses
# ============================================================

def partition_enriched(
    enriched: list[dict[str, Any]],
    max_age_days: int | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    Split an enriched candidate list into (live, cached).

    Args:
      enriched: list of triggered candidates after filings have been
                attached (each has a `screen_2_filings` key).
      max_age_days: passed through to lookup().

    Returns:
      (live, cached) where:
        live    - candidates that NEED a fresh model call. Each is
                  the original enriched dict, unchanged.
        cached  - cached discovery rows ready to splice into the
                  run output. Each is the cached row (with `_cache_meta`
                  annotation), already in the shape the model would
                  have produced.

    Used by run_screen_2_discovery to send only the uncached subset
    of candidates to Opus, then splice the cached rows back in.
    """
    live: list[dict[str, Any]] = []
    cached: list[dict[str, Any]] = []

    for c in enriched:
        ticker = (c.get("ticker") or "").upper()
        filings = c.get("screen_2_filings") or {}
        hit = lookup(ticker, filings, max_age_days=max_age_days)
        if hit is None:
            live.append(c)
        else:
            cached.append(hit)

    return live, cached


# ============================================================
# Public API: bulk-store from a model response
# ============================================================

def store_from_response(
    enriched_subset: list[dict[str, Any]],
    parsed: dict[str, Any],
) -> int:
    """
    Walk the model response and cache one entry per ticker that was
    in the live (uncached) subset.

    Args:
      enriched_subset: the `live` list that was sent to the model
                       (so we can recover each ticker's filings bundle).
      parsed:          the model response with `discoveries` and
                       `skipped` lists.

    Caches both `discoveries` and `skipped` rows — both are valid
    per-ticker outputs that would be reproduced on identical filings.

    Returns the number of rows cached. Never raises.
    """
    # Index filings by ticker for O(1) lookup as we walk the response.
    filings_by_ticker: dict[str, dict[str, Any]] = {}
    for c in enriched_subset:
        t = (c.get("ticker") or "").upper()
        if t:
            filings_by_ticker[t] = c.get("screen_2_filings") or {}

    n = 0
    for bucket_key in ("discoveries", "skipped"):
        bucket = parsed.get(bucket_key) or []
        for row in bucket:
            t = (row.get("ticker") or "").upper()
            if not t:
                continue
            f = filings_by_ticker.get(t)
            if f is None:
                # Model emitted a ticker that wasn't in the live
                # subset — shouldn't happen, but don't cache it
                # against the wrong filings.
                continue
            store(t, f, row)
            n += 1
    return n


# ============================================================
# Diagnostics
# ============================================================

def stats() -> dict[str, Any]:
    """Quick stats on the cache directory. For debugging / smoke tests."""
    if not _CACHE_DIR.exists():
        return {"exists": False, "count": 0}
    files = list(_CACHE_DIR.glob("*.json"))
    return {
        "exists": True,
        "path": str(_CACHE_DIR),
        "count": len(files),
        "tickers": sorted({f.stem.split("_", 1)[0] for f in files}),
    }


# ============================================================
# Standalone smoke test
# ============================================================
if __name__ == "__main__":
    print("[screen_2_cache] stats:")
    print(json.dumps(stats(), indent=2))

    print("\n[screen_2_cache] dry-run hash test:")
    sample = {
        "business": {"filing_date": "2026-03-13", "truncated": False},
        "k10_risk": {"filing_date": "2026-03-13", "truncated": True},
        "q10_risk": {"filing_date": "2026-02-05", "truncated": False},
        "earnings_8ks": [
            {"filing_date": "2026-03-02", "fell_back_to_primary": True},
            {"filing_date": "2025-12-02", "fell_back_to_primary": True},
            {"filing_date": "2025-09-02", "fell_back_to_primary": True},
            {"filing_date": "2025-06-03", "fell_back_to_primary": True},
        ],
    }
    h1 = filings_hash(sample)
    h2 = filings_hash(sample)
    print(f"  identical bundle -> identical hash: {h1 == h2} ({h1})")

    sample2 = dict(sample)
    sample2["earnings_8ks"] = sample["earnings_8ks"] + [
        {"filing_date": "2026-05-28", "fell_back_to_primary": True}
    ]
    h3 = filings_hash(sample2)
    print(f"  added 8-K -> hash changes: {h1 != h3} ({h3})")