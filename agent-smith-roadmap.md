# agent-smith — project status & roadmap

*Last updated: May 6, 2026*

*See also: ARCHITECTURE.md for deep state, file ownership, fragile seams, and design decisions.*

---

## Where we are right now

**Phase 1 — DEPLOYED** (Apr 22, 2026)

Three-pass analysis pipeline (discovery, AI impact, Taiwan), running on GitHub Actions, output committed back to repo and served via GitHub Pages.

**Phase 1.5-lite — DEPLOYED** (mid-late April 2026)

The big jump from "daily analysis dashboard" to "active paper-trading system":

- **Grading pipeline.** `agent/grading.py` walks history snapshots, fetches prices via yfinance, computes HIT / MISS / AMBIGUOUS / PENDING / NOT_GRADED on every directional flag. Versioned (v1) so threshold changes don't invalidate old grades. Output: `docs/data/trends.json`.
- **Paper portfolio state machine.** `agent/portfolio.py` runs a $10K bankroll with confidence-weighted sizing, IBKR Pro Tiered fee modeling, max-position/max-sector/min-cash guardrails. Output: `docs/data/portfolio.json` + `portfolio_history.json`.
- **Haiku-powered portfolio decision pass.** Runs only on the 22:00 AST cron (via `--portfolio` flag). Decides BUY/WATCH/SKIP for new flags and HOLD/ADD/TRIM/EXIT for held positions. Output: `docs/data/suggestions.json`.
- **Cost rebalancing.** Dropped the 17:00 AST "US morning" run to offset Haiku decision-pass cost. Now 2 runs/day instead of 3.
- **5-page dashboard** (current / trends / portfolio / watching / taiwan).

**Phase 1.5-lite dashboard polish — DEPLOYED** (Apr 30 session)

- ✅ **Watching page no longer shows held positions as skipped flags** (`docs/suggestions.html`). Read-time filter against `portfolio.json`'s `open_positions`.
- ✅ **Front page wired to live data** (`docs/index.html`). Previously rendered hardcoded April-22 sample data — every visit was looking at a fossil.
- ✅ **Front-page holdings strip** (`docs/index.html`). One-line summary just below the stats row.
- ✅ **Classification normalizer in dashboard** (`docs/index.html`). Handles `LIKELY OVERDONE` / `PARTIALLY RATIONAL` etc.

**Universe expansion — DEPLOYED** (May 5)

- ✅ **Discovery universe expanded from ~80 to ~1003 tickers.** Live fetch of SP400 + SP600 constituents from Wikipedia each run (was a static sample list).
- ✅ **CLAUDE_MAX_TOKENS bumped** from 4096 → 16384 (Layer 1 was hitting the cap mid-discovery on the larger universe).
- ✅ **max_candidates_per_run lowered** from 40 → 20 (Layer 2 cost control on bigger candidate pool).
- ⚠ **Known performance issue:** ~1003-ticker universe scan takes 9-12 minutes due to per-ticker yfinance call with 0.1s sleep. Within Actions tolerance but slow. Candidate fix: cache constituent list to a file, refresh manually (Wikipedia barely changes month-to-month).
- 📊 **New cost estimate:** ~$15-20/mo (up from ~$5.50/mo pre-expansion, but still well under $50/mo cap).
- 📋 **Taiwan dropped from scheduled runs.** Nav links removed from 4 dashboard pages. Code preserved, page orphaned but functional. Wife isn't actively using it; Michael can't directly trade Taiwan from his account.

**Paper trading record — first round-trip closed** (May 1)

- ✅ **EXTR closed at +17.9% realized gain ($284.91).** Opened April 23 at $18.72, closed May 1 at $22.08. 8 days held. Thesis: sympathy selling off CALX -14% earnings print; thesis played out as networking peers normalized.
- 📊 **Portfolio currently flat.** $10,284.22 cash, 0 open positions as of May 5 22:00 AST run.
- 📊 **Track record:** 5 resolved calls, 4 HIT / 1 MISS (BBY), 80% hit rate, +2.83% avg return. *Caveat: every resolved call was graded with "only 1-2 bars elapsed" — the grader marks early HITs when the +3% threshold is crossed before the full horizon. Real horizon-elapsed grades will be more honest.*

**Local development setup — DEPLOYED** (May 6 session)

- ✅ **Python 3.13 + venv configured locally.** Repo cloned to `~/Documents/agent-smith`, virtualenv at `.venv`, dependencies installed from `requirements.txt`.
- ✅ **API key wired via shell env** (Smith Labs key, session-only `export ANTHROPIC_API_KEY=...`). No `.env` file or `dotenv` dependency added — `analyze.py` reads `os.environ` directly, same as GitHub Actions.
- ✅ **End-to-end smoke test passed.** Full `python -m agent.main us` run completed in ~22 minutes, $0.23 spend, identical output structure to production. Production data reverted via `git checkout docs/data/`.
- 💡 **Established workflow:** `cd ~/Documents/agent-smith && source .venv/bin/activate && export ANTHROPIC_API_KEY="..."`. Three commands per session, ~5 seconds.

**EDGAR + earnings fetchers — BUILT, NOT WIRED** (May 6 session)

Two new standalone modules tested against the May 5 mover set (DOCN, IPGP, CYTK, OSIS, AEIS, ADEA, GXO, ECG):

- ✅ **`agent/edgar.py`** — SEC EDGAR 8-K fetcher. 100% hit rate: every May 5 mystery mover had an 8-K filed within 1-2 days. Free, no API key, requires polite User-Agent header per SEC policy.
- ✅ **`agent/earnings.py`** — yfinance-based earnings calendar (upcoming + recent). 100% hit rate cross-validating EDGAR results.
- 📊 **Quantitative finding:** the May 6 smoke test surfaced 20 movers but the news feed only attached catalysts to 2 of them. **90% catalyst-blindness rate**, now provably solvable.
- 📊 **Trump posts signal density:** 1 of 48 flagged on May 6 ≈ 2%. Worth deprioritizing in a future cleanup.
- ⏳ **Not yet wired into discovery.** Modules are committed locally but not integrated into the analyze.py pipeline. Next session.

---

## What's queued (priority order)

### Session C — Wire EDGAR + earnings into discovery (next)

The genuinely high-leverage build. EDGAR + earnings fetchers exist but don't yet improve the bot's output.

1. **Modify the discovery prompt** to consume EDGAR catalyst data + earnings calendar context. Each mover gets enriched: "DOCN +40%, 8-K filed today, just reported earnings."
2. **Restructure thesis output** to be more pedagogical (per Michael's "I want to learn how to trade" goal). New schema: setup / why mispriced / what confirms / what kills / what to learn from this trade.
3. **Test on small ticker subset** (3-5 names from May 5 movers) before any production change. Should cost $0.02-0.05 per test, not $0.50.
4. **Add `--no-claude` flag** to `main.py` for free testing of non-AI parts. ~30 lines of code, makes future iteration free for data-pipeline work.

### Session D — Followups loop + exploratory position-sizing tier

Gives the bot working memory across runs.

1. **Followups pass** between discovery and portfolio. Reads pending flags from last 7-14 days, fetches current prices, asks Claude: thesis intact / thesis broken / upgrade conviction / downgrade to noise. Output: `followups` array in `latest_us.json`.
2. **Exploratory position-sizing tier** (~5-8% of bankroll). For trades where the bot wants to test a thesis but isn't fully convinced. More trades, smaller stakes, faster learning loop. Per Michael's "I want to learn by seeing money move" goal.
3. **Watching page becomes active conversation** rather than passive log. The bot revisits its own UNCLEAR calls and either upgrades or kills them — this also gives telemetry on whether UNCLEAR is hiding signal.

### Session E+ — Buffett teaching layer + housekeeping

- **Buffett-style deep-dive page** (`docs/learn.html`). Once-weekly, single-name fundamental analysis (intrinsic value, moat, management quality, margin of safety). Pure educational stream — *not* connected to the paper portfolio. Per Michael's "would be great as an added layer for learning" framing. Cheap (weekly cadence, single ticker), no urgency.
- **`SAMPLE_PORTFOLIO` fallback fix on `portfolio.html`.** May 6 bug: page silently rendered the April-22 sample data on transient fetch failure, briefly showing 6 phantom trades and confusing Michael into thinking real trades had occurred. Fix: either add visible "sample data" banner (mirrors `index.html` pattern from Apr 30) OR drop the fallback entirely now that real data exists. Probably the latter. Same treatment for `SAMPLE_SUGGESTIONS` if it exists.
- **Constituent list caching.** Cache the SP400 + SP600 ticker list to a file, refresh manually monthly. Saves 1-2 minutes per run, eliminates Wikipedia dependency on every cron fire.
- **Trump posts evaluation.** With 2% signal density, decide whether to keep the input at all. If kept, deprioritize in the prompt. If dropped, reclaim some prompt budget.

### Pending Python session — Phase 1.6 (data lifecycle hardening)

Lower priority than the EDGAR work, but still queued. These touch `agent/main.py` and `agent/portfolio.py`. Higher blast radius than dashboard work, deserves a clean session.

1. **Skip held positions at write time.** Move the dashboard's read-time dedup into `_build_suggestion_entry()` so the JSON itself is clean.
2. **Persist `price_at_flag` on suggestions.** Unlocks the watching page's `verdict: "missed" | "right-to-skip"` logic.
3. **Stable `flag_id`.** Add `flag_id = "{ticker}_{run_timestamp}"` at discovery time. Cascade through suggestions, portfolio positions, grades.
4. **Optional: consolidate classification normalization** into one Python helper.
5. **Optional: git push retry** in `.github/workflows/analyze.yml`.

### Phase 2 — Real-trade tracking & full feedback loop

Trigger: after several weeks of paper data accumulates, or when Michael decides to start logging real trades — whichever comes first.

- **Real-trade input mechanism.** Apps Script + Google Sheet form vs. dashboard form vs. brokerage API — undecided.
- **Real-portfolio dashboard.** Separate page (or section of `portfolio.html`?).
- **Performance-weighted prompts in the discovery pass.** Currently only the portfolio decision pass sees `trends_summary`. Plumb it into discovery so the bot calibrates its own confidence against its track record. *This is the core "Claude learns" loop.*
- **Move dashboard to Vercel with auth** — only if real money is involved. Public repo is fine for paper.

### Phase 3 — Polish

- Mobile-optimized layout (current is desktop-first)
- Email/Telegram daily digest summary
- Personal universe learning (patterns Michael acts on)
- Export-to-PDF morning brief

---

## Decisions still pending

(See ARCHITECTURE.md "Pending design decisions" for full context on each.)

1. Where to consolidate **classification normalization** (one helper, write-time field, or document the three-way fragility?)
2. **Buy-the-dip vs. buy-the-rally** framing for discovery — defer until grading data accumulates
3. Specifically what counts as a **"missed" call** for the watching-page verdict logic — conf 3+? 4+?
4. **Real-trade input mechanism** for Phase 2 — Sheet vs. form vs. broker API
5. **`SAMPLE_PORTFOLIO` fallback strategy** — drop entirely or banner-and-keep?
6. **Trump posts signal value** — drop, deprioritize, or keep? 2% signal density on May 6 was thin.

---

## Working patterns that have proven useful

These are tactical lessons from active development, kept here so they don't have to be re-learned.

- **One file at a time.** Browser-based GitHub editing caused four bugs in a single session previously. Standard tool now: VS Code + GitHub Desktop, push via terminal or Desktop.
- **Local dev for cost-controlled iteration.** Running the bot end-to-end locally costs the same as production. The savings come from testing *subsets* (small ticker lists, mocked Claude responses, EDGAR-only with no Claude calls). Discipline matters more than the existence of the env.
- **Read-time fixes preferred over write-time fixes** for dashboard issues. Lower blast radius, fully reversible.
- **Graceful degrade with sample-data fallback.** Every dashboard page carries its own `SAMPLE_DATA` constant. *Caveat: see `portfolio.html` May 6 phantom-trades bug — fallback without visible indicator can mislead.*
- **Live diff before deploying.** Before saving any patched HTML file, run `diff` against the original to verify only intended changes happened.
- **Update ARCHITECTURE.md and the roadmap at the end of each substantive session.** Otherwise state has to be rebuilt from chat history, which is expensive and lossy.
- **One module at a time, test standalone first.** EDGAR and earnings fetchers were built as standalone modules with `if __name__ == "__main__"` test loops *before* any integration with the existing pipeline. Lets us prove the data layer works without touching the Claude layer.
- **Stop on a clean win.** Each session ended with a working, testable artifact: local dev → smoke test passed; EDGAR → 100% hit rate; earnings → 100% cross-validation. Easier to resume.
