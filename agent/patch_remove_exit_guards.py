#!/usr/bin/env python3
"""
Remove the Screen 0 / Screen 1 code-enforced exit guards — June 12, 2026.

Why: the May-28 guards (-15% stop-loss + grading-horizon force-close)
overcorrected. With nearly every flag carrying a "days" horizon, the
5-day expiry mass-closes positions regardless of thesis or P&L — the
May 29 sweep force-closed DCO/SHAK/BLDR/DECK in one pass (DECK at
+16.5%), and since then nothing has been allowed to develop. The stop
side never fired once in live data; the horizon side did all the
premature selling.

What this does (one anchor-asserted patch):
  agent/main.py — removes the run_portfolio_for_screen block that calls
  pf.force_exit_stop_and_horizon() for screen_0/screen_1, replacing it
  with a tombstone comment. Exits return fully to Haiku's judgement
  (EXIT/TRIM in the decision pass).

NOT touched:
  - pf.force_exit_stop_and_horizon in portfolio.py — preserved un-wired
    (same convention as the orphaned Taiwan code) in case a recalibrated
    version is wanted later.
  - config.STOP_LOSS_PCT — still referenced by the un-wired function.
  - Screen 2's T+1 exit — separate mechanism, unaffected (and Screen 2
    is disabled anyway).
  - The thesis-status journal (append_thesis_log) — observation only,
    never sells anything; it stays so the weakening→drawdown question
    can still be answered with data.

Run from repo root:  python patch_remove_exit_guards.py
Then verify:         python -m py_compile agent/main.py
                     python -m agent.main us --no-claude
"""
import sys
from pathlib import Path

OLD = '''    # ---- Screen 0 / Screen 1: stop-loss + horizon hard exits -----
    # Code-enforced exit discipline, run in the same post-MTM /
    # pre-decision slot as Screen 2's T+1 exit above. Two
    # thesis-independent triggers per open position: a -STOP_LOSS_PCT
    # catastrophe floor, and a grading-horizon expiry (days_held >=
    # GRADING_HORIZON_DAYS[flag_horizon]). The Haiku prompt's EXIT
    # instruction is the backstop; this is the discipline. Screen 2 is
    # excluded -- its T+1 print exit is already its holding-window rule,
    # so running both would double-handle the same position.
    if screen_id in ("screen_0", "screen_1"):
        code_exits = pf.force_exit_stop_and_horizon(state, screen_id=screen_id)
        if code_exits["exited"] or code_exits["exit_failed"]:
            pf.save_state(state, screen_id=screen_id)
            print(
                f"[portfolio] post-code-exit: "
                f"{code_exits['by_stop']} stop, {code_exits['by_horizon']} horizon, "
                f"{code_exits['exit_failed']} deferred, {code_exits['skipped']} skipped; "
                f"equity=${pf.total_equity(state):.2f} cash=${state['cash']:.2f} "
                f"open={len(state['open_positions'])}"
            )
'''

NEW = '''    # ---- Screen 0 / Screen 1: code-enforced exit guards (REMOVED) -
    # Tried, didn't work (May 28 - June 12, 2026). The guard pair
    # (-15% stop + grading-horizon force-close via
    # pf.force_exit_stop_and_horizon) was added after positions were
    # held through full round-trips. It overcorrected: nearly every
    # flag carries a "days" horizon, so the 5-day expiry mass-closed
    # positions regardless of thesis or P&L (May 29 sweep: DCO, SHAK,
    # BLDR, DECK -- DECK at +16.5% -- all force-closed in one pass).
    # In ~2 weeks live the stop trigger never fired once; ALL premature
    # selling came from the horizon trigger. Removed June 12, 2026 --
    # exits are back to Haiku's judgement (EXIT/TRIM in the decision
    # pass). The function is preserved un-wired in portfolio.py if a
    # recalibrated version (e.g. stop-only, or horizon >> grading
    # window) is ever wanted. Screen 2's T+1 exit is separate and
    # untouched.
'''

path = Path("agent/main.py")
if not path.exists():
    sys.exit("ABORT: agent/main.py not found — run from repo root.")
src = path.read_text()
n = src.count(OLD)
if n != 1:
    sys.exit(
        f"ABORT: anchor found {n} times in agent/main.py (expected exactly 1). "
        f"Nothing modified. If the block has drifted, re-inspect before patching."
    )
path.write_text(src.replace(OLD, NEW))
print("OK: exit-guard block removed from agent/main.py")
print("Next: python -m py_compile agent/main.py")
print("Then: python -m agent.main us --no-claude   (free end-to-end check)")
