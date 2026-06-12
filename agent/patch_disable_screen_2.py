#!/usr/bin/env python3
"""
Disable Screen 2 (pre-earnings filings read) — June 12, 2026.

Why: discovery ran daily (~$1/day in Opus filings reads) but almost never
converted reads into positions. Cost/signal ratio doesn't justify the spend.

What this does:
  1. agent/config.py — adds `"enabled": False` to the screen_2 SCREENS entry.
  2. agent/main.py  — gates the run_screen_2() discovery call on that flag;
     when disabled, writes a fresh SKIPPED envelope to screen_2_us.json so
     the dashboard shows "disabled" instead of serving a stale file.
  3. agent/main.py  — run_portfolio() skips disabled screens, EXCEPT while
     open positions remain (drain pass: T+1 force-exit can still flatten
     the book; no fresh flags exist so no new buys are possible). Once
     flat, the screen is fully skipped.

NOT touched: pf.refresh_all() still iterates all of SCREENS, so screen_2
stays marked-to-market on every run (zero API cost, dashboard stays live).
Code, registry entry, and history files all preserved for a future revisit.

Run from repo root:  python patch_disable_screen_2.py
Then verify:         python -m py_compile agent/config.py agent/main.py
                     python -m agent.main us --no-claude
"""
import sys
from pathlib import Path


def patch(path: str, old: str, new: str, label: str) -> None:
    p = Path(path)
    if not p.exists():
        sys.exit(f"ABORT: {path} not found — run from repo root.")
    src = p.read_text()
    n = src.count(old)
    if n != 1:
        sys.exit(
            f"ABORT [{label}]: anchor found {n} times in {path} "
            f"(expected exactly 1). No files modified beyond prior steps.\n"
            f"Anchor starts: {old[:100]!r}"
        )
    p.write_text(src.replace(old, new))
    print(f"OK [{label}]: patched {path}")


# ------------------------------------------------------------------
# Patch 1: config.py — flag screen_2 disabled
# ------------------------------------------------------------------
patch(
    "agent/config.py",
    '''        "id": "screen_2",
        "display_name": "Pre-earnings filings read",''',
    '''        "id": "screen_2",
        # DISABLED June 12, 2026. ~3 weeks live: discovery ran daily
        # (~$1/day Opus filings reads across the curated universe) but
        # produced only 3 round-trip trades (MOD, GTLB, MDB, all closed
        # by the T+1 sweep within days) and then went quiet entirely.
        # Cost/signal ratio doesn't justify the daily spend.
        # enabled=False gates BOTH the discovery pass (main.run_us) and
        # the portfolio decision pass (main.run_portfolio — with a
        # drain-pass exception while positions remain open). Mark-to-
        # market via pf.refresh_all() is NOT gated, so the dashboard
        # equity stays current. Registry entry, code, and history files
        # preserved for a future revisit.
        "enabled": False,
        "display_name": "Pre-earnings filings read",''',
    "config: screen_2 enabled=False",
)

# ------------------------------------------------------------------
# Patch 2: main.py — gate the discovery call in run_us()
# ------------------------------------------------------------------
patch(
    "agent/main.py",
    '''    try:
        run_screen_2()
    except Exception as e:
        print(f"[us] run_screen_2 raised unexpectedly: {e}")
        import traceback
        traceback.print_exc()''',
    '''    if config.get_screen("screen_2").get("enabled", True):
        try:
            run_screen_2()
        except Exception as e:
            print(f"[us] run_screen_2 raised unexpectedly: {e}")
            import traceback
            traceback.print_exc()
    else:
        # Screen 2 disabled (config.SCREENS enabled=False). Skipping the
        # discovery pass skips the expensive Opus filings read. Write a
        # fresh SKIPPED envelope (latest file only — no history archive,
        # it would be daily noise) so the dashboard renders a neutral
        # "disabled" banner instead of serving a stale file.
        print("[us] screen_2 disabled in config.SCREENS — discovery skipped")
        try:
            Path("docs/data/screen_2_us.json").write_text(json.dumps({
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "screen_id": "screen_2",
                "status": "SKIPPED",
                "discovery": {
                    "run_summary": (
                        "Screen 2 is disabled in config.SCREENS "
                        "(enabled: False) — no discovery pass was run."
                    ),
                    "discoveries": [],
                    "skipped": [],
                },
            }, indent=2, ensure_ascii=False))
        except OSError as e:
            print(f"[us] could not write disabled screen_2 envelope: {e}")''',
    "main: gate run_screen_2() in run_us",
)

# ------------------------------------------------------------------
# Patch 3: main.py — skip disabled screens in run_portfolio(),
#          with drain-pass exception while positions remain open
# ------------------------------------------------------------------
patch(
    "agent/main.py",
    '''    results: dict[str, Any] = {}
    for screen in config.SCREENS:
        sid = screen["id"]
        try:''',
    '''    results: dict[str, Any] = {}
    for screen in config.SCREENS:
        sid = screen["id"]
        if not screen.get("enabled", True):
            # Disabled screen: discovery is already gated in run_us, so
            # no fresh flags exist. Skip the decision pass too — EXCEPT
            # while positions remain open, in which case the pass still
            # runs so the holding-window exits (e.g. Screen 2's T+1
            # force-exit sweep) can drain the book. The drain pass
            # cannot open new positions (no flags inside the decision
            # window); once flat, the screen is fully skipped and costs
            # nothing.
            _drain_state = pf.load_state(screen_id=sid)
            if not _drain_state.get("open_positions"):
                print(f"[portfolio] screen={sid} disabled — skipped (book is flat)")
                results[sid] = {"skipped": "disabled"}
                continue
            print(
                f"[portfolio] screen={sid} disabled but "
                f"{len(_drain_state['open_positions'])} position(s) still "
                f"open — running drain pass to flatten"
            )
        try:'''
    ,
    "main: skip disabled screens in run_portfolio",
)

print("\nAll 3 patches applied.")
print("Next: python -m py_compile agent/config.py agent/main.py")
print("Then: python -m agent.main us --no-claude   (free end-to-end check)")
