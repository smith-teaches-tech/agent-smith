"""
agent.classifications — single source of truth for classification labels.

The discovery prompt outputs hedged labels like "LIKELY OVERDONE" or
"PARTIALLY RATIONAL". Downstream consumers — grading, buy-eligibility,
dashboards — want the un-prefixed short form for filtering, CSS class
lookup, and HIT/MISS gating decisions.

Before this module existed, four call sites independently implemented
this normalization (Python: grading.py, analyze.py; JS: index.html
inline, suggestions.html missing entirely). That was Known fragile
seam §1: drift between the four was the bug source. Multi-screen
amplifies the risk because each new screen would be another site.

This module is the Python source of truth. The two dashboard files
carry an inline JS port that mirrors the algorithm below exactly;
keep them in sync by visual diff against the JS_REFERENCE_PORT
docstring at the bottom of this file.

Algorithm (must match the JS port verbatim):
  1. If input is falsy or non-string → "UNCLEAR".
  2. Trim, uppercase.
  3. Strip a leading "LIKELY " or "PARTIALLY " prefix if present.
  4. If the remainder is one of the four base classifications, return it.
  5. Otherwise → "UNCLEAR" (safe fallback; CSS always matches).
"""

from __future__ import annotations

from typing import Optional

# The only valid post-normalization values. Any consumer doing CSS
# class lookup, filter chips, or hit/miss gating should assume the
# normalized output is one of these four strings.
BASE_CLASSIFICATIONS: tuple[str, ...] = (
    "OVERDONE",
    "UNDERDONE",
    "RATIONAL",
    "UNCLEAR",
)

# Hedging modifiers the discovery prompt may prepend. Kept here (not in
# config.py) because the normalizer is the only thing that should care
# about them — config.py describes tuning knobs, not parser internals.
KNOWN_PREFIXES: tuple[str, ...] = ("LIKELY", "PARTIALLY")

# The two directional classifications. Used by grading (which only
# grades directional calls) and by the portfolio buy-eligibility
# check (which only buys on directional calls). Non-directional
# classifications are observational, not actionable.
GRADED_CLASSIFICATIONS: frozenset[str] = frozenset({"OVERDONE", "UNDERDONE"})


def normalize_classification(raw: Optional[str]) -> str:
    """
    Normalize a raw classification string to its base form.

    Examples:
        "LIKELY OVERDONE"      → "OVERDONE"
        "PARTIALLY RATIONAL"   → "RATIONAL"
        "OVERDONE"             → "OVERDONE"
        "  likely  overdone  " → "OVERDONE"
        "OVERDONE WITH CAVEATS"→ "UNCLEAR"   (unrecognized suffix)
        None / "" / non-string → "UNCLEAR"

    Returns one of BASE_CLASSIFICATIONS. Never raises.
    """
    if not raw or not isinstance(raw, str):
        return "UNCLEAR"
    upper = raw.strip().upper()
    # Strip one known prefix (and any whitespace following it). We only
    # collapse internal whitespace via .split() on this match — full
    # whitespace normalization is intentionally not applied to the rest
    # of the string so unexpected inputs fall through to UNCLEAR.
    for prefix in KNOWN_PREFIXES:
        token = prefix + " "
        if upper.startswith(token):
            upper = upper[len(token):].lstrip()
            break
    if upper in BASE_CLASSIFICATIONS:
        return upper
    return "UNCLEAR"


def is_directional(raw: Optional[str]) -> bool:
    """
    True iff the (normalized) classification is OVERDONE or UNDERDONE.

    Convenience for grading.py (decides what to grade) and analyze.py
    (decides what's buy-eligible). Both consumers want the same
    "directional vs. observational" boolean, so it lives here.
    """
    return normalize_classification(raw) in GRADED_CLASSIFICATIONS


# ============================================================
# JS_REFERENCE_PORT — keep in sync with index.html and
# suggestions.html. If you edit normalize_classification above,
# edit this string AND the two dashboard files.
# ============================================================
JS_REFERENCE_PORT = """
// agent-smith classification normalization — JS port of
// agent/classifications.py:normalize_classification.
// Algorithm must match the Python version verbatim.
const CLS_BASE = ['OVERDONE', 'UNDERDONE', 'RATIONAL', 'UNCLEAR'];
const CLS_PREFIXES = ['LIKELY', 'PARTIALLY'];

function normalizeClassification(raw) {
  if (!raw || typeof raw !== 'string') return 'UNCLEAR';
  let upper = raw.trim().toUpperCase();
  for (const prefix of CLS_PREFIXES) {
    const token = prefix + ' ';
    if (upper.startsWith(token)) {
      upper = upper.slice(token.length).replace(/^\\s+/, '');
      break;
    }
  }
  return CLS_BASE.includes(upper) ? upper : 'UNCLEAR';
}
"""