"""
grading.py — Performance grading for agent-smith past picks.

Reads historical discovery runs from docs/data/history/*.json, pulls current
and intermediate price data for each flagged ticker, and grades the call.

Output: docs/data/trends.json, consumed by docs/trends.html.

Grading logic is versioned so threshold tweaks don't invalidate old grades.
Each grade record stores the logic_version that produced it; a new version
regrades only new calls and leaves historical grades stable unless the user
explicitly requests a full rebuild.

Grading definitions (LOGIC_VERSION 1):
    HIT       — price moved in predicted direction by ≥3% within horizon
    MISS      — moved in opposite direction by ≥3% within horizon
    AMBIGUOUS — neither threshold crossed within horizon
    PENDING   — horizon hasn't elapsed yet

Horizon mapping (from call's time_horizon field):
    "days"   → 5 trading days
    "weeks"  → 20 trading days
    "months" → 60 trading days
    missing  → defaults to 5 trading days

Direction mapping (from classification + move_pct):
    OVERDONE    → expects mean-reversion (opposite of move_pct direction)
    UNDERDONE   → expects continuation (same direction as move_pct)
    RATIONAL    → not graded (no directional call)
    UNCLEAR     → not graded (no directional call)

OVERDONE example:
    CALX dropped -14% (move_pct=-14), classified OVERDONE → expected direction is UP.
    If CALX is up ≥3% within horizon → HIT.
    If CALX is down ≥3% more within horizon → MISS.

UNDERDONE example:
    MANH up +5.9%, classified UNDERDONE → expected direction is UP (more upside).
    If MANH up another ≥3% within horizon → HIT.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, Optional

import yfinance as yf
import pandas as pd

logger = logging.getLogger(__name__)

# ============================================================
# Versioning
# ============================================================

LOGIC_VERSION = 1

GRADING_PARAMS = {
    1: {
        "hit_threshold_pct": 3.0,
        "horizon_days_map": {
            "days": 5,
            "weeks": 20,
            "months": 60,
        },
        "default_horizon_days": 5,
        "graded_classifications": {"OVERDONE", "UNDERDONE"},
    }
}

# ============================================================
# Data classes
# ============================================================

GradeLabel = Literal["HIT", "MISS", "AMBIGUOUS", "PENDING", "NOT_GRADED", "DATA_ERROR"]


@dataclass
class Grade:
    """One graded call."""

    ticker: str
    name: str
    sector: Optional[str]
    flagged_at: str  # ISO timestamp of the run that made the call
    run_file: str  # source history file for traceability
    classification: str
    confidence: int
    move_pct_at_flag: float
    expected_direction: Optional[Literal["up", "down"]]
    horizon_days: int
    grade: GradeLabel
    return_pct_in_horizon: Optional[float]
    max_favorable_pct: Optional[float]
    max_adverse_pct: Optional[float]
    price_at_flag: Optional[float]
    price_at_horizon_end: Optional[float]
    logic_version: int
    notes: Optional[str] = None


# ============================================================
# Horizon + direction logic
# ============================================================


def _expected_direction(classification: str, move_pct: float) -> Optional[str]:
    """Return 'up' / 'down' based on classification, or None if not graded."""
    c = (classification or "").upper()
    if c == "OVERDONE":
        # Expect mean reversion — direction opposite to the move
        return "up" if move_pct < 0 else "down"
    if c == "UNDERDONE":
        # Expect continuation — direction same as the move
        return "up" if move_pct > 0 else "down"
    return None


def _horizon_days(time_horizon: Optional[str], version: int = LOGIC_VERSION) -> int:
    params = GRADING_PARAMS[version]
    if not time_horizon:
        return params["default_horizon_days"]
    return params["horizon_days_map"].get(
        time_horizon.lower().strip(), params["default_horizon_days"]
    )


# ============================================================
# Price fetching
# ============================================================


def _fetch_price_window(
    ticker: str, start: datetime, end: datetime
) -> Optional[pd.DataFrame]:
    """
    Fetch daily OHLC for ticker from start to end (inclusive).
    Returns None on failure — caller handles gracefully.
    """
    try:
        # yfinance is end-exclusive, pad by 1 day
        df = yf.Ticker(ticker).history(
            start=start.strftime("%Y-%m-%d"),
            end=(end + timedelta(days=2)).strftime("%Y-%m-%d"),
            auto_adjust=False,
        )
        if df is None or df.empty:
            return None
        return df
    except Exception as e:
        logger.warning("Price fetch failed for %s: %s", ticker, e)
        return None


# ============================================================
# Core grading
# ============================================================


def grade_call(
    discovery: dict[str, Any],
    flagged_at: datetime,
    run_file: str,
    version: int = LOGIC_VERSION,
    now: Optional[datetime] = None,
) -> Grade:
    """Grade a single discovery entry."""
    params = GRADING_PARAMS[version]
    now = now or datetime.now(timezone.utc)

    ticker = discovery.get("ticker", "")
    name = discovery.get("name", ticker)
    sector = discovery.get("sector")
    classification = (discovery.get("classification") or "").upper()
    confidence = int(discovery.get("confidence", 0) or 0)
    move_pct = float(discovery.get("move_pct", 0) or 0)
    time_horizon = discovery.get("time_horizon")

    horizon_days = _horizon_days(time_horizon, version)
    expected_dir = _expected_direction(classification, move_pct)

    base = dict(
        ticker=ticker,
        name=name,
        sector=sector,
        flagged_at=flagged_at.isoformat(),
        run_file=run_file,
        classification=classification,
        confidence=confidence,
        move_pct_at_flag=move_pct,
        expected_direction=expected_dir,
        horizon_days=horizon_days,
        logic_version=version,
    )

    # Not graded — no directional call
    if classification not in params["graded_classifications"] or expected_dir is None:
        return Grade(
            **base,
            grade="NOT_GRADED",
            return_pct_in_horizon=None,
            max_favorable_pct=None,
            max_adverse_pct=None,
            price_at_flag=None,
            price_at_horizon_end=None,
            notes=f"classification={classification} not in graded set",
        )

    # Fetch prices
    df = _fetch_price_window(ticker, flagged_at - timedelta(days=2), now)
    if df is None or df.empty:
        return Grade(
            **base,
            grade="DATA_ERROR",
            return_pct_in_horizon=None,
            max_favorable_pct=None,
            max_adverse_pct=None,
            price_at_flag=None,
            price_at_horizon_end=None,
            notes="No price data returned",
        )

    # Normalize tz — yfinance returns tz-aware or naive depending on version
    if df.index.tz is None:
        df.index = df.index.tz_localize("America/New_York").tz_convert("UTC")
    else:
        df.index = df.index.tz_convert("UTC")

    # Find the close on or just before the flag timestamp as entry price
    entry_rows = df[df.index <= flagged_at]
    if entry_rows.empty:
        # Fall back to first available bar
        entry_rows = df.head(1)
    price_at_flag = float(entry_rows["Close"].iloc[-1])

    # Determine horizon end — N *trading* days after flag
    post_flag = df[df.index > flagged_at]
    if post_flag.empty:
        return Grade(
            **base,
            grade="PENDING",
            return_pct_in_horizon=None,
            max_favorable_pct=None,
            max_adverse_pct=None,
            price_at_flag=price_at_flag,
            price_at_horizon_end=None,
            notes="No post-flag bars yet",
        )

    trading_days_elapsed = len(post_flag)
    horizon_reached = trading_days_elapsed >= horizon_days
    horizon_slice = post_flag.head(horizon_days)

    # Compute max favorable / adverse excursion within horizon (or so-far)
    # Favorable = moves in expected direction.
    if expected_dir == "up":
        highest = float(horizon_slice["High"].max())
        lowest = float(horizon_slice["Low"].min())
        max_favorable_pct = (highest - price_at_flag) / price_at_flag * 100
        max_adverse_pct = (lowest - price_at_flag) / price_at_flag * 100
    else:  # down
        highest = float(horizon_slice["High"].max())
        lowest = float(horizon_slice["Low"].min())
        max_favorable_pct = (price_at_flag - lowest) / price_at_flag * 100
        max_adverse_pct = (price_at_flag - highest) / price_at_flag * 100

    # Did we cross hit threshold first, or miss threshold first?
    threshold = params["hit_threshold_pct"]
    hit_crossed_idx = None
    miss_crossed_idx = None

    for i, (_, row) in enumerate(horizon_slice.iterrows()):
        if expected_dir == "up":
            fav = (row["High"] - price_at_flag) / price_at_flag * 100
            adv = (row["Low"] - price_at_flag) / price_at_flag * 100  # negative
            if fav >= threshold and hit_crossed_idx is None:
                hit_crossed_idx = i
            if adv <= -threshold and miss_crossed_idx is None:
                miss_crossed_idx = i
        else:  # down
            fav = (price_at_flag - row["Low"]) / price_at_flag * 100
            adv = (price_at_flag - row["High"]) / price_at_flag * 100
            if fav >= threshold and hit_crossed_idx is None:
                hit_crossed_idx = i
            if adv <= -threshold and miss_crossed_idx is None:
                miss_crossed_idx = i
        if hit_crossed_idx is not None and miss_crossed_idx is not None:
            break

    # Horizon-end return
    price_at_horizon_end = float(horizon_slice["Close"].iloc[-1])
    if expected_dir == "up":
        return_pct = (price_at_horizon_end - price_at_flag) / price_at_flag * 100
    else:
        return_pct = (price_at_flag - price_at_horizon_end) / price_at_flag * 100

    # Decide grade
    if hit_crossed_idx is not None and (
        miss_crossed_idx is None or hit_crossed_idx < miss_crossed_idx
    ):
        grade_label: GradeLabel = "HIT"
    elif miss_crossed_idx is not None and (
        hit_crossed_idx is None or miss_crossed_idx < hit_crossed_idx
    ):
        grade_label = "MISS"
    elif horizon_reached:
        grade_label = "AMBIGUOUS"
    else:
        grade_label = "PENDING"

    return Grade(
        **base,
        grade=grade_label,
        return_pct_in_horizon=round(return_pct, 2),
        max_favorable_pct=round(max_favorable_pct, 2),
        max_adverse_pct=round(max_adverse_pct, 2),
        price_at_flag=round(price_at_flag, 4),
        price_at_horizon_end=round(price_at_horizon_end, 4),
        notes=None if horizon_reached else f"only {trading_days_elapsed} bars elapsed",
    )


# ============================================================
# History walker
# ============================================================


def _parse_history_filename(path: Path) -> Optional[datetime]:
    """
    Extract timestamp from filenames like us_20260422T200601Z.json.
    Returns UTC datetime or None if unparseable.
    """
    name = path.stem  # e.g., us_20260422T200601Z
    try:
        # strip prefix before the date
        parts = name.split("_", 1)
        if len(parts) != 2:
            return None
        ts_str = parts[1].rstrip("Z")  # 20260422T200601
        return datetime.strptime(ts_str, "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def grade_all_history(
    history_dir: Path,
    version: int = LOGIC_VERSION,
    existing_grades: Optional[list[dict]] = None,
) -> list[Grade]:
    """
    Walk history_dir, grade every discovery at its original flag time.

    If existing_grades is provided, re-use any grade whose (ticker, flagged_at,
    logic_version) matches and whose grade is not PENDING/DATA_ERROR. This avoids
    re-fetching prices for already-resolved calls.
    """
    existing_lookup: dict[tuple[str, str, int], dict] = {}
    if existing_grades:
        for g in existing_grades:
            key = (g["ticker"], g["flagged_at"], g["logic_version"])
            if g["grade"] not in ("PENDING", "DATA_ERROR"):
                existing_lookup[key] = g

    results: list[Grade] = []
    now = datetime.now(timezone.utc)

    for path in sorted(history_dir.glob("*.json")):
        flagged_at = _parse_history_filename(path)
        if flagged_at is None:
            logger.warning("Skipping unparseable history file: %s", path.name)
            continue

        try:
            data = json.loads(path.read_text())
        except Exception as e:
            logger.warning("Failed to parse %s: %s", path.name, e)
            continue

        discoveries = (data.get("discovery") or {}).get("discoveries") or []
        for disc in discoveries:
            ticker = disc.get("ticker", "")
            key = (ticker, flagged_at.isoformat(), version)

            if key in existing_lookup:
                # Reuse
                cached = existing_lookup[key]
                results.append(Grade(**cached))
                continue

            grade = grade_call(
                disc, flagged_at, run_file=path.name, version=version, now=now
            )
            results.append(grade)

    return results


# ============================================================
# Aggregation for dashboard
# ============================================================


def compute_trends(grades: list[Grade]) -> dict[str, Any]:
    """
    Roll up per-classification, per-confidence, per-sector, per-horizon hit rates.
    Excludes NOT_GRADED, DATA_ERROR, PENDING from the denominator.
    """

    def bucket_stats(items: list[Grade]) -> dict[str, Any]:
        resolved = [g for g in items if g.grade in ("HIT", "MISS", "AMBIGUOUS")]
        n = len(resolved)
        hits = sum(1 for g in resolved if g.grade == "HIT")
        misses = sum(1 for g in resolved if g.grade == "MISS")
        amb = sum(1 for g in resolved if g.grade == "AMBIGUOUS")
        avg_return = (
            sum(g.return_pct_in_horizon or 0 for g in resolved) / n if n else None
        )
        return {
            "n_resolved": n,
            "n_hit": hits,
            "n_miss": misses,
            "n_ambiguous": amb,
            "hit_rate": round(hits / n * 100, 1) if n else None,
            "avg_return_pct": round(avg_return, 2) if avg_return is not None else None,
        }

    overall = bucket_stats(grades)

    by_classification: dict[str, Any] = {}
    for cls in ("OVERDONE", "UNDERDONE"):
        by_classification[cls] = bucket_stats([g for g in grades if g.classification == cls])

    by_confidence: dict[str, Any] = {}
    for conf in range(1, 6):
        by_confidence[str(conf)] = bucket_stats(
            [g for g in grades if g.confidence == conf]
        )

    by_sector: dict[str, Any] = {}
    sectors = {g.sector for g in grades if g.sector}
    for sec in sectors:
        by_sector[sec] = bucket_stats([g for g in grades if g.sector == sec])

    by_horizon: dict[str, Any] = {}
    for h in (5, 20, 60):
        by_horizon[str(h)] = bucket_stats([g for g in grades if g.horizon_days == h])

    # Leaderboards
    resolved = [g for g in grades if g.grade in ("HIT", "MISS", "AMBIGUOUS")]
    best = sorted(
        resolved, key=lambda g: g.return_pct_in_horizon or 0, reverse=True
    )[:10]
    worst = sorted(resolved, key=lambda g: g.return_pct_in_horizon or 0)[:10]

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "logic_version": LOGIC_VERSION,
        "grading_params": GRADING_PARAMS[LOGIC_VERSION],
        "n_total_calls": len(grades),
        "n_not_graded": sum(1 for g in grades if g.grade == "NOT_GRADED"),
        "n_pending": sum(1 for g in grades if g.grade == "PENDING"),
        "n_data_error": sum(1 for g in grades if g.grade == "DATA_ERROR"),
        "overall": overall,
        "by_classification": by_classification,
        "by_confidence": by_confidence,
        "by_sector": by_sector,
        "by_horizon_days": by_horizon,
        "best_calls": [asdict(g) for g in best],
        "worst_calls": [asdict(g) for g in worst],
        "all_grades": [asdict(g) for g in grades],
    }


# ============================================================
# CLI entrypoint
# ============================================================


def run(
    history_dir: Path = Path("docs/data/history"),
    output_path: Path = Path("docs/data/trends.json"),
    rebuild: bool = False,
) -> dict[str, Any]:
    """
    Entry point. Reads existing trends.json (if any), grades all history,
    writes updated trends.json.

    rebuild=True ignores cached grades and regrades everything.
    """
    existing_grades = None
    if output_path.exists() and not rebuild:
        try:
            existing = json.loads(output_path.read_text())
            if existing.get("logic_version") == LOGIC_VERSION:
                existing_grades = existing.get("all_grades")
        except Exception as e:
            logger.warning("Failed to read existing trends.json, rebuilding: %s", e)

    grades = grade_all_history(history_dir, existing_grades=existing_grades)
    trends = compute_trends(grades)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(trends, indent=2, default=str))
    logger.info(
        "Wrote %s — %d total calls, %d resolved, %d pending",
        output_path,
        trends["n_total_calls"],
        trends["overall"]["n_resolved"],
        trends["n_pending"],
    )
    return trends


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    run()
