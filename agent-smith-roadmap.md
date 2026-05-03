# agent-smith — project status & roadmap

*Last updated: April 30, 2026*

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
- **First real paper trade:** 85 shares of EXTR opened April 23, 2026 at $18.72. As of Apr 30: +16.7% unrealized, day 6 of horizon, thesis "weakening" per Haiku, action HOLD.

**Phase 1.5-lite dashboard polish — IN PROGRESS** (Apr 30 session)

This session's three deployed fixes:

- ✅ **Watching page no longer shows held positions as skipped flags** (`docs/suggestions.html`). Read-time filter against `portfolio.json`'s `open_positions`. EXTR was sitting in the "skipped" list while simultaneously in the portfolio at +16.7%. Now it's correctly hidden from the watch list.
- ✅ **Front page wired to live data** (`docs/index.html`). Previously rendered hardcoded April-22 sample data — every visit was looking at a fossil. Now reads `latest_us.json` and renders the actual run summary, market tone, sector breakdown, and 7 real discoveries (FFIV, PTCT, BLDR, AVAV, SAIA, DECK, EXTR).
- ✅ **Front-page holdings strip** (`docs/index.html`). One-line summary just below the stats row: ticker, P&L%, days held, thesis status, next action. Hidden when no positions. Click ticker → `portfolio.html`.
- ✅ **Classification normalizer in dashboard** (`docs/index.html`). Handles `LIKELY OVERDONE` / `PARTIALLY RATIONAL` etc. — strips prefix for CSS class lookup, keeps full label visible in pill text, normalizes for filter chip matching.

---

## What's queued

### Now-ish (small, isolated, low risk)

These are leftover Tier-1 dashboard fixes from the Apr 30 session. Each is a single-file JavaScript change with the now-proven `Promise.allSettled` + graceful-degrade pattern.

1. **Trends page: pending-grades countdown.** `docs/trends.html` currently shows "0 of 0 graded" which reads as broken. Compute the count of PENDING grades from `trends.json`, surface as "8 calls awaiting grade — first resolves [date]".
2. **Front page: catalyst chains.** `latest_us.json` already populates `discovery.catalyst_chains` with rich primary-event → secondary-opportunities mappings. Render them on the front page (the data has been there the whole time, the page just wasn't reading it).
3. **Watching page: classification normalizer.** Apply the same pattern from `index.html` to `suggestions.html`. The `cls-pill cls-LIKELY OVERDONE` selector currently matches no CSS rule — pills render unstyled.

### Next focused Python session — Phase 1.6 (data lifecycle hardening)

These touch `agent/main.py` and `agent/portfolio.py`. Higher blast radius than dashboard work, deserves a clean session with testing.

1. **Skip held positions at write time.** Currently we filter held tickers out of suggestions in the dashboard. The underlying `suggestions.json` still contains EXTR as a SKIP row. Move the dedup into `_build_suggestion_entry()` so the JSON itself is clean. Leaves the dashboard read-time filter as belt-and-suspenders.
2. **Persist `price_at_flag` on suggestions.** This is the unlock for the watching page's `verdict: "missed" | "right-to-skip"` logic. Without it, the page's most interesting feature (was the bot right to skip?) can't compute. Touches `_build_suggestion_entry()`.
3. **Stable `flag_id`.** Add `flag_id = "{ticker}_{run_timestamp}"` at discovery time. Cascade through suggestions, portfolio positions, grades. Worth doing now before more data accumulates without IDs.
4. **Optional: consolidate classification normalization** into one Python helper imported by graders, portfolio eligibility, and (eventually) the suggestion writer. Currently three independent implementations exist (Python grader, Python portfolio analyze, JS dashboard).
5. **Optional: git push retry** in `.github/workflows/analyze.yml`. The Apr 29 Taiwan run lost its snapshot to a transient GitHub 500. A 3-attempt retry with backoff would have caught it.

### Pending decision — Drop the Taiwan run?

Wife isn't actively using the Taiwan dashboard. Michael can't directly trade Taiwanese stocks from his account. The Opus 4.7 Taiwan pass is a major cost line.

**Proposal:** drop the 09:00 AST `tw` cron. Add Taiwan-exposed US-listed names (TSM, UMC, ASX, HIMX, EWT) to the US discovery universe so semi-cycle and Taiwan-context plays still surface. Archive `tw.html`. The ADR-vs-local arbitrage signal — the only piece that genuinely needs Taipei prices — could become a tiny non-LLM check (compare TSM close to 2330.TW close, flag divergences >1.5%).

**Estimated savings:** ~$20/mo. **Functional loss:** minimal given non-engagement.

Decision: **pending Michael's confirmation.** Discussed Apr 30, leaning toward yes.

### Phase 2 — Real-trade tracking & full feedback loop

Trigger: after several weeks of paper data accumulates, or when Michael decides to start logging real trades — whichever comes first.

- **Real-trade input mechanism.** Apps Script + Google Sheet form vs. dashboard form vs. brokerage API — undecided. Apps Script has best mobile UX and matches existing personal-tooling patterns; broker API is most accurate but heaviest.
- **Real-portfolio dashboard.** Separate page (or section of `portfolio.html`?) tracking actual positions, with cost basis, "why I bought" attribution, and link back to the originating Claude flag.
- **Performance-weighted prompts in the discovery pass.** Currently only the portfolio decision pass sees `trends_summary`. Plumb it into discovery so the bot calibrates its own confidence against its track record. *This is the core "Claude learns" loop.*
- **Calibration dashboard breakdowns** (hit rate by classification × confidence × sector × horizon × Anthropic-related). Already structurally supported by `compute_trends()` but needs more graded data than exists today (62 total calls, only 2 resolved — 1 HIT, 1 MISS — the rest NOT_GRADED because they're UNCLEAR/RATIONAL).
- **Move dashboard to Vercel with auth** — only if real money is involved. Public repo is fine for paper.

### Phase 2.5 — Coverage expansion

- **Real top-movers feed** (replace static SP400/SP600 sample). Finnhub or Polygon. Eliminates SAVE/ENV/ITCI delisted-ticker noise.
- **Earnings calendar.** Knowing which positions report this week is a major ergonomic win.
- **Standalone watchlist.** Separate from the "watching" page (which is decision history). This is "names Michael wants to track that aren't currently flagged."
- **Congressional trades** — only if still wanted after seeing how stale the data is.

### Phase 3 — Polish

- Mobile-optimized layout (current is desktop-first)
- Email/Telegram daily digest summary
- Personal universe learning (patterns Michael acts on)
- Export-to-PDF morning brief

---

## Decisions still pending

(See ARCHITECTURE.md "Pending design decisions" for full context on each.)

1. Where to consolidate **classification normalization** (one helper, write-time field, or document the three-way fragility?)
2. **Drop the Taiwan run** — proposal articulated, awaiting Michael's confirmation
3. **Buy-the-dip vs. buy-the-rally** framing for discovery — defer until grading data accumulates
4. Specifically what counts as a **"missed" call** for the watching-page verdict logic — conf 3+? 4+?
5. **Real-trade input mechanism** for Phase 2 — Sheet vs. form vs. broker API
6. **Local development setup** — would save API tokens on test runs; rationale is clear, just hasn't been built yet

---

## Working patterns that have proven useful

These are tactical lessons from active development, kept here so they don't have to be re-learned.

- **One file at a time.** Browser-based GitHub editing caused four bugs in a single session previously. Standard tool now: VS Code + GitHub Desktop, push via terminal or Desktop. The Apr 30 session deployed three changes cleanly using this workflow.
- **Read-time fixes preferred over write-time fixes** for dashboard issues. Lower blast radius, fully reversible. Write-time fixes can corrupt the next run if there's a bug.
- **Graceful degrade with sample-data fallback.** Every dashboard page carries its own `SAMPLE_DATA` constant. If `fetch()` fails or schema doesn't match, the page falls back rather than rendering blank. The `Promise.allSettled` pattern (rather than sequential awaits) lets one fetch fail without blocking the others.
- **Live diff before deploying.** Before saving any patched HTML file, run `diff` against the original to verify only intended changes happened. Caught zero issues this session — meaning the planning was tight, not that the practice is unnecessary.
- **Update ARCHITECTURE.md and the roadmap at the end of each substantive session.** Otherwise state has to be rebuilt from chat history, which is expensive and lossy.
