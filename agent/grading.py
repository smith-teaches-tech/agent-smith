"""
grading.py — Performance grading for agent-smith past picks.

Reads historical discovery runs from docs/data/history/*.json, pulls current
and intermediate price data for each flagged ticker, and grades the call.

Output: docs/data/trends.json, consumed by docs/trends.html.

Grading logic is versioned so threshold tweaks don't invalidate old grades.
Each grade record stores the logic_version that produced it; a new version
regrades only new calls and leaves historical grades stable unless the user
explicitly requests a full rebuild.

Grading definitions (LOGIC_VERSION 4 — current):
    v4 keeps v3's HIT/MISS/AMBIGUOUS/PENDING horizon logic verbatim
    (see the v3 block below). The v4 change is confined to direction
    derivation: a flag carrying no move_pct (Screen 2 and any future
    filings-based screen) derives its expected direction from the
    classification alone — UNDERDONE expects the stock UP, OVERDONE
    expects it DOWN — rather than relative to a price move. See
    _expected_direction for the full rationale. Move-relative flags
    (Screen 0 / Screen 1) grade identically to v3.

Grading definitions (LOGIC_VERSION 3 — symmetric pre-horizon):
    HIT       — at horizon end: price crossed the directional ±3%
                threshold within the horizon AND the final-bar price is
                still on the right side of the flag price (positive for
                up-direction, negative for down-direction). The thesis
                survived the full horizon.
    MISS      — at horizon end: price crossed the *opposite* ±3%
                threshold within the horizon AND ended on the wrong
                side, OR the move never crossed either threshold but
                ended >3% in the wrong direction, OR a round-trip
                (touched favorable threshold then ended on the wrong
                side of flag).
    AMBIGUOUS — at horizon end: neither extreme reached and the
                final-bar move is within ±3% of flag price.
    PENDING   — horizon hasn't elapsed yet. v3 keeps every not-yet-
                elapsed record PENDING regardless of intermediate
                excursions. No early HIT, no early MISS — symmetric.
                Once a grade resolves under v3 it is effectively locked
                (horizon is closed, subsequent passes recompute on the
                same window and yield the same answer).
    NOT_GRADED— non-directional classification (RATIONAL, UNCLEAR).

The v3 symmetric-pre-horizon rule replaces v2's asymmetric early-MISS
branch, which biased the hit rate downward and allowed graded MISS
records to flip back to HIT on later passes as new bars softened the
adverse path. Surfaced by AVAV graded MISS at 6/20 bars elapsed
(May 2026).

Grading definitions (LOGIC_VERSION 2 — legacy, kept for cached grade replay):
    Same horizon-end logic as v3, but pre-horizon allowed an early MISS
    if the adverse threshold had been crossed AND the current return was
    >threshold in the wrong direction. No corresponding early HIT. This
    asymmetry was the bug v3 fixes.

Grading definitions (LOGIC_VERSION 1 — legacy, kept for cached grade replay):
    HIT       — price moved in predicted direction by ≥3% within horizon
                (whichever threshold crossed first wins)
    MISS      — moved in opposite direction by ≥3% within horizon first
    AMBIGUOUS — neither threshold crossed within horizon
    PENDING   — horizon hasn't elapsed yet

Horizon mapping (from call's time_horizon field):
    "days"   → 5 trading days
    "weeks"  → 20 trading days
    "months" → 60 trading days
    missing  → defaults to 5 trading days

Direction mapping:
    Move-relative flags (move_pct present — Screen 0 / Screen 1):
        OVERDONE  → expects mean-reversion (opposite of move_pct direction)
        UNDERDONE → expects continuation (same direction as move_pct)
    Classification-only flags (no move_pct — Screen 2 / filings-based):
        OVERDONE  → expects the stock DOWN (bearish filings read)
        UNDERDONE → expects the stock UP   (bullish filings read)
    RATIONAL / UNCLEAR → not graded (no directional call), either shape.

Classification normalization: the discovery prompt outputs longer labels
like "LIKELY OVERDONE" or "PARTIALLY RATIONAL". We normalize these down to
the short forms (OVERDONE / UNDERDONE / RATIONAL / UNCLEAR) at read-time
so grading logic and dashboard bucketing are clean.

OVERDONE example (v2/v3, horizon-end semantics identical):
    CALX dropped -14% (move_pct=-14), classified OVERDONE → expected up.
    If CALX rallies ≥3% within horizon AND ends ≥0% above entry → HIT.
    If CALX rallies ≥3% but then round-trips to ending ≥3% below entry → MISS.
    If never crosses either threshold and ends within ±3% → AMBIGUOUS.

UNDERDONE example (v2/v3, horizon-end semantics identical):
    MANH up +5.9%, classified UNDERDONE → expected continuation up.
    If MANH continues ≥3% AND ends positive at horizon → HIT.
    If MANH spikes +3% intraday but ends -4% at horizon → MISS.
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

from . import config
from .classifications import normalize_classification

logger = logging.getLogger(__name__)

# ============================================================
# Versioning
# ============================================================
# v1 = first-cross-wins ("touched profitable")
# v2 = path-aware (directional cross + correct final-bar side).
#      Pre-horizon: early MISS allowed when adverse threshold breached AND
#      currently >threshold in wrong direction. Early HIT NOT allowed.
#      This asymmetry biased the hit rate downward and let already-assigned
#      MISS grades flip back to HIT on subsequent passes as new bars
#      softened the adverse path. Surfaced May 2026 via AVAV graded MISS
#      at 6/20 bars elapsed.
# v3 = v2 logic at horizon, but symmetric pre-horizon: ALL not-yet-elapsed
#      records stay PENDING. No early HIT, no early MISS. Once a record
#      is graded HIT/MISS/AMBIGUOUS under v3 the horizon has fully elapsed,
#      so the grade is effectively locked (subsequent passes recompute on
#      the same closed window and yield the same answer).
# v4 = v3 horizon/pre-horizon logic UNCHANGED. The v4 difference is in
#      direction derivation (_expected_direction): classification-only
#      flags — those carrying no move_pct, i.e. Screen 2 and any future
#      filings-based screen — now derive direction from classification
#      alone (UNDERDONE→up, OVERDONE→down) instead of being forced
#      through the move-relative branch with a coerced move_pct=0.0,
#      which inverted every Screen 2 call. Move-relative flags
#      (Screen 0 / Screen 1) are graded identically to v3. The bump
#      exists so grade_all_history's cache invalidation sweeps the v3
#      grades — including any Screen 2 grades already mis-resolved — and
#      regrades them under the corrected direction logic. Thresholds and
#      horizons are unchanged from v3.
LOGIC_VERSION = 4


# Module-private alias for backwards compatibility with the existing
# call sites. The canonical implementation lives in agent.classifications;
# this module re-exports it under the old name so the diff stays small.
# New code should import normalize_classification from agent.classifications
# directly.
_normalize_classification = normalize_classification


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
    },
    2: {
        "hit_threshold_pct": 3.0,
        "horizon_days_map": {
            "days": 5,
            "weeks": 20,
            "months": 60,
        },
        "default_horizon_days": 5,
        "graded_classifications": {"OVERDONE", "UNDERDONE"},
    },
    3: {
        # Same thresholds as v2 — the v3 difference is in the branching
        # logic (pre-horizon always PENDING). Bumped so the cache
        # invalidation in grade_all_history sweeps v2 grades and
        # regrades them under v3 semantics.
        "hit_threshold_pct": 3.0,
        "horizon_days_map": {
            "days": 5,
            "weeks": 20,
            "months": 60,
        },
        "default_horizon_days": 5,
        "graded_classifications": {"OVERDONE", "UNDERDONE"},
    },
    4: {
        # Identical params to v3. The v4 difference is purely in
        # _expected_direction (classification-only flags derive
        # direction from classification alone), not in thresholds,
        # horizons, or the graded-classification set. Kept as its own
        # entry so the version bump invalidates the v3 cache and
        # regrades history under the corrected direction logic.
        "hit_threshold_pct": 3.0,
        "horizon_days_map": {
            "days": 5,
            "weeks": 20,
            "months": 60,
        },
        "default_horizon_days": 5,
        "graded_classifications": {"OVERDONE", "UNDERDONE"},
    },
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
    # F1 multi-screen — which screen produced this call. None means
    # "pre-F1 historic call, attribute to the default screen". The
    # aggregator (compute_trends) substitutes config.DEFAULT_SCREEN_ID
    # at read time so the by_screen bucket has no None key.
    screen_id: Optional[str] = None


# ============================================================
# Horizon + direction logic
# ============================================================


def _expected_direction(
    classification: str, move_pct: Optional[float]
) -> Optional[str]:
    """Return 'up' / 'down' based on classification, or None if not graded.

    Assumes `classification` has already been normalized to a short form
    (OVERDONE / UNDERDONE / RATIONAL / UNCLEAR) by _normalize_classification.

    Two flag shapes are supported, distinguished by whether `move_pct`
    is present:

    MOVE-RELATIVE flags (move_pct is a number) — Screen 0 / Screen 1.
        These flag a *price mover*. The classification's directional
        meaning is relative to the move:
          OVERDONE  → mean-reversion, opposite the move
          UNDERDONE → continuation, same direction as the move

    CLASSIFICATION-ONLY flags (move_pct is None) — Screen 2 and any
    future filings-based screen.
        These flag a company on an *event calendar*, not a price move.
        There is no move to reference, so the classification carries
        the entire directional claim on its own:
          OVERDONE  → the read is bearish  → expect the stock DOWN
          UNDERDONE → the read is bullish  → expect the stock UP
        Screen 2's UNDERDONE means "the filings say this print lands
        better than the market expects" — an unconditionally bullish
        call. Feeding it through the move-relative branch with a
        coerced move_pct=0.0 would resolve every UNDERDONE to 'down'
        and grade every correct Screen 2 call as a MISS. The
        move_pct-is-None check is what prevents that inversion.

    The detection is on the DATA, not the screen id: a flag with no
    move_pct is intrinsically classification-only, so any future
    filings-based screen is handled without touching this function.
    The check is `move_pct is None` specifically (not falsiness) so a
    genuine 0.0% mover, were one ever to occur, still takes the
    move-relative path.
    """
    c = (classification or "").upper()

    if move_pct is None:
        # Classification-only flag — classification alone sets direction.
        if c == "OVERDONE":
            return "down"
        if c == "UNDERDONE":
            return "up"
        return None

    # Move-relative flag — direction is relative to the price move.
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
    # Normalize up-front so everything downstream uses the short form
    classification = _normalize_classification(discovery.get("classification"))
    confidence = int(discovery.get("confidence", 0) or 0)
    # move_pct: present (a number) on Screen 0 / Screen 1 price-mover
    # flags; ABSENT on Screen 2 (and any future filings-based screen)
    # whose flags are classification-only. Read it as Optional and do
    # NOT coerce a missing value to 0.0 — _expected_direction needs the
    # genuine None to pick the classification-only direction branch.
    # Coercing here was the bug that would have inverted every Screen 2
    # grade. The `or None` guard also maps a stored JSON null to None.
    _raw_move = discovery.get("move_pct")
    move_pct: Optional[float] = (
        float(_raw_move) if _raw_move is not None else None
    )
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
        # move_pct_at_flag is a display/audit field on the Grade record
        # and never feeds the grade decision (only expected_dir does).
        # Store 0.0 when the flag carried no move_pct so the dataclass
        # field stays a concrete float; the genuine None was already
        # consumed by _expected_direction above.
        move_pct_at_flag=move_pct if move_pct is not None else 0.0,
        expected_direction=expected_dir,
        horizon_days=horizon_days,
        logic_version=version,
        # F1 multi-screen: discovery dicts carry the screen_id of the
        # screen that produced them (added in F2 onwards). Pre-F1
        # discoveries have no field; we leave screen_id=None and the
        # aggregator substitutes DEFAULT_SCREEN_ID at read time.
        screen_id=discovery.get("screen_id"),
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

    # Horizon-end return (signed in the *expected* direction — positive
    # means the directional thesis paid)
    price_at_horizon_end = float(horizon_slice["Close"].iloc[-1])
    if expected_dir == "up":
        return_pct = (price_at_horizon_end - price_at_flag) / price_at_flag * 100
    else:
        return_pct = (price_at_flag - price_at_horizon_end) / price_at_flag * 100

    # ----------------------------------------------------------
    # Decide grade (path-aware in v2; first-cross-wins in v1)
    # ----------------------------------------------------------
    if version >= 2:
        # v2/v3 — path-aware. The thesis must survive to the horizon.
        # Use the directional return (return_pct is already signed in the
        # expected direction) to decide which side of flag price we ended on.

        if not horizon_reached:
            # Pre-horizon behavior depends on version.
            #
            # v3 (current): symmetric — always PENDING. No early HIT, no
            # early MISS. Grades only resolve when the horizon has fully
            # elapsed, at which point the result is deterministic on a
            # closed window and effectively locked.
            #
            # v2 (legacy): asymmetric — early MISS when adverse threshold
            # breached AND currently >threshold in wrong direction; no
            # corresponding early HIT. Preserved here for replay of cached
            # v2 grades only; live grading runs at LOGIC_VERSION=3.
            if version >= 3:
                grade_label: GradeLabel = "PENDING"
            else:
                # v2 legacy branch
                if miss_crossed_idx is not None and return_pct <= -threshold:
                    grade_label = "MISS"
                else:
                    grade_label = "PENDING"
        else:
            # Horizon fully elapsed. Path-aware decision (shared by v2/v3).
            hit_crossed = hit_crossed_idx is not None
            miss_crossed = miss_crossed_idx is not None
            ended_right_side = return_pct >= 0  # positive = thesis still alive
            ended_decisive_wrong = return_pct <= -threshold

            if hit_crossed and ended_right_side:
                # Crossed in our favor and stayed there. Genuine HIT.
                grade_label = "HIT"
            elif ended_decisive_wrong:
                # Whether or not we touched the favorable threshold, the
                # final-bar print is decisively against us. MISS.
                grade_label = "MISS"
            elif miss_crossed and not ended_right_side:
                # Adverse threshold crossed and we never recovered to the
                # right side of flag. MISS even if not >threshold wrong.
                grade_label = "MISS"
            elif hit_crossed and not ended_right_side:
                # Round-trip — touched profit, gave it all back, ended on
                # the wrong side but not by >threshold. MISS (this is the
                # exact case v2 was built to catch).
                grade_label = "MISS"
            else:
                # Neither extreme reached, ended within ±threshold of flag.
                grade_label = "AMBIGUOUS"
    else:
        # v1 — legacy first-cross-wins. Kept for replay of cached grades.
        if hit_crossed_idx is not None and (
            miss_crossed_idx is None or hit_crossed_idx < miss_crossed_idx
        ):
            grade_label = "HIT"
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


def _parse_history_filename(path: Path) -> Optional[tuple[datetime, str]]:
    """
    Extract (timestamp, prefix) from history filenames.
 
    Filename pattern is `{prefix}_{YYYYmmddTHHMMSS}Z.json` where prefix
    is the discovery `kind` passed to _write_output:
      - "us"        → Screen 0 (general mispricing) history
      - "screen_1_us" → Screen 1 (AI-event sympathy fade) history
      - "screen_2_us" → Screen 2 (when added)
 
    Returns (UTC datetime, prefix string) on success, None on
    unparseable name.
 
    rsplit-on-last-underscore handles arbitrarily multi-segment
    prefixes — "screen_1_us" splits cleanly into prefix="screen_1_us"
    and timestamp="20260508T163122Z".
    """
    name = path.stem  # e.g., us_20260422T200601Z or screen_1_us_20260508T163122Z
    try:
        parts = name.rsplit("_", 1)
        if len(parts) != 2:
            return None
        prefix, ts_raw = parts[0], parts[1]
        ts_str = ts_raw.rstrip("Z")
        ts = datetime.strptime(ts_str, "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
        return ts, prefix
    except Exception:
        return None


_PREFIX_TO_SCREEN_ID: dict[str, Optional[str]] = {
    "us": "screen_0",
    "screen_1_us": "screen_1",
    "screen_2_us": "screen_2",
    # Taiwan experiment ran Apr 2026, deleted in Session E.5. 23 history
    # files preserved on disk as audit trail. Excluded from grading
    # because Taiwan ran under a different universe + prompt; rolling
    # its grades into Screen 0 would distort the multi-screen comparison
    # the architecture exists to enable.
    "tw": None,
}
 
 
def grade_all_history(
    history_dir: Path,
    version: int = LOGIC_VERSION,
    existing_grades: Optional[list[dict]] = None,
) -> list[Grade]:
    """
    Walk history_dir, grade every discovery at its original flag time.
 
    If existing_grades is provided, re-use any grade whose
    (ticker, flagged_at, logic_version) matches the *current* version
    and whose grade is not PENDING/DATA_ERROR. This avoids re-fetching
    prices for already-resolved calls. Grades from older versions are
    dropped — a version bump is an explicit invalidation signal.
 
    F2 multi-screen note: every file's prefix is mapped to a screen_id
    via _PREFIX_TO_SCREEN_ID. The screen_id stamped on each Grade
    overrides whatever may be in the discovery dict, because the file
    is the definitive source of "which screen produced this call." A
    file with an unknown prefix is attributed to screen_0 with a
    one-time warning — the grader never silently drops data on a
    missing-mapping bug.
    """
    existing_lookup: dict[tuple[str, str, int], dict] = {}
    if existing_grades:
        for g in existing_grades:
            # Only reuse cached grades that match the current logic
            # version. On a version bump, the dict will be empty and
            # everything regrades.
            if g.get("logic_version") != version:
                continue
            key = (g["ticker"], g["flagged_at"], g["logic_version"])
            if g["grade"] not in ("PENDING", "DATA_ERROR"):
                existing_lookup[key] = g
 
    results: list[Grade] = []
    now = datetime.now(timezone.utc)
    warned_unknown_prefixes: set[str] = set()
 
    for path in sorted(history_dir.glob("*.json")):
        parsed = _parse_history_filename(path)
        if parsed is None:
            logger.warning("Skipping unparseable history file: %s", path.name)
            continue
        flagged_at, prefix = parsed
 
        # Three-state lookup:
        #   - Known prefix → screen_id  → grade with that id
        #   - Known prefix → None       → skip the file entirely
        #   - Unknown prefix            → warn once, attribute to screen_0
        if prefix in _PREFIX_TO_SCREEN_ID:
            screen_id = _PREFIX_TO_SCREEN_ID[prefix]
            if screen_id is None:
                continue  # explicitly excluded (e.g. legacy Taiwan)
        else:
            if prefix not in warned_unknown_prefixes:
                logger.warning(
                    "Unknown history file prefix '%s' (file: %s); "
                    "attributing to screen_0. Add prefix to "
                    "_PREFIX_TO_SCREEN_ID in grading.py to fix.",
                    prefix, path.name,
                )
                warned_unknown_prefixes.add(prefix)
            screen_id = "screen_0"
 
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
                # Reuse cached grade — but stamp the file-derived
                # screen_id on it. This handles the migration case
                # where pre-F2 cached grades had screen_id=None: on
                # next run, they'll be re-emitted with their correct
                # screen_id without forcing a price refetch.
                cached = dict(existing_lookup[key])  # don't mutate the cache
                cached["screen_id"] = screen_id
                results.append(Grade(**cached))
                continue
 
            grade = grade_call(
                disc, flagged_at, run_file=path.name, version=version, now=now
            )
            # File-derived screen_id is authoritative — overrides
            # whatever grade_call may have read from the discovery
            # dict's screen_id field.
            grade.screen_id = screen_id
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

    # F1 multi-screen aggregation. Grades with screen_id=None (pre-F1
    # historic data) are attributed to DEFAULT_SCREEN_ID so they roll
    # cleanly into the legacy bucket. Aggregating from registered screens
    # rather than from grades' distinct screen_ids ensures every screen
    # appears in the output even with zero trades — important so a newly
    # added screen isn't invisible until its first trade.
    by_screen: dict[str, Any] = {}
    default_sid = config.DEFAULT_SCREEN_ID
    for screen in config.SCREENS:
        sid = screen["id"]
        screen_grades = [
            g for g in grades
            if (g.screen_id or default_sid) == sid
        ]
        by_screen[sid] = {
            "display_name": screen["display_name"],
            **bucket_stats(screen_grades),
        }

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
        "by_screen": by_screen,
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

    On a logic-version bump, the cached-grade reuse path is automatically
    bypassed (grade_all_history filters by version), so the next run after
    a version bump will fully regrade history under the new logic — no
    manual rebuild flag needed.
    """
    existing_grades = None
    if output_path.exists() and not rebuild:
        try:
            existing = json.loads(output_path.read_text())
            # Note: we still read all_grades regardless of the file's
            # logic_version. grade_all_history filters per-grade, so older
            # records are silently dropped while same-version records are
            # reused. This handles the version-bump transition cleanly.
            existing_grades = existing.get("all_grades")
        except Exception as e:
            logger.warning("Failed to read existing trends.json, rebuilding: %s", e)

    grades = grade_all_history(history_dir, existing_grades=existing_grades)
    trends = compute_trends(grades)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(trends, indent=2, default=str))
    logger.info(
        "Wrote %s — %d total calls, %d resolved, %d pending (logic v%d)",
        output_path,
        trends["n_total_calls"],
        trends["overall"]["n_resolved"],
        trends["n_pending"],
        LOGIC_VERSION,
    )
    return trends


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    run()