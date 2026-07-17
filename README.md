# agent-smith

**Status: Retired, 2026-07-17.** Ran for just under 3 months (first trade 2026-04-22, last commit 2026-07-16). See [Postmortem](#postmortem-2026-07-17) below before reviving or forking this.

Personal market analysis agent. Scanned US mid-caps (and, earlier on, Taiwan stocks) for unusual moves, cross-referenced news (including AI-lab announcements and Trump posts), and used Claude to flag potentially mispriced situations, then paper-traded the flags it liked best.

**This was a pointer system, not a recommendation system.** It directed attention to interesting setups; all buy/sell decisions were the agent's own paper-trading logic, not investment advice to act on.

---

## Postmortem (2026-07-17)

Three "screens" ran against a shared $10k paper bankroll each:

| Screen | Thesis | Result | vs. same-window SPY | Win rate |
|---|---|---|---|---|
| **Screen 0** — General mispricing | Wide-net OVERDONE/UNDERDONE labeling on any mover with a behaviorally-inconsistent catalyst. Comparison baseline. | +4.04% | SPY +6.63% (**-2.6pp**) | 48% (n=29 closed) |
| **Screen 1** — AI-event sympathy fade | Buy mid-caps that panic-sold on AI-lab announcements when filings show minimal real exposure; hold 5–15 days for institutional repricing. | **-8.98%** | SPY +1.64% (**-10.7pp**) | 28.6% (n=42 closed) |
| **Screen 2** — Pre-earnings filings read | Trade the T-2/T+1 window around earnings prints based on filings analysis. **Removed 2026-06-24** — thesis abandoned, poor cost/signal (~$1/day for 3 round-trips over 3 weeks, then silent). | +0.58% (n=3, before shutdown) | SPY +0.83% (roughly flat) | 66.7% (n=3, too small to mean anything) |

Combined, the ~$30k deployed across the three screens (each dated from its own first trade) was worth **$29,563.66** at retirement, a **-1.45%** return. The same dollars in a plain SPY buy-and-hold over the identical windows would have been worth **$30,910.22** (+3.0%) — a **-$1,346.56** gap in the market's favor, before the Anthropic API bill.

**Why Screen 1 failed — and why it wasn't a tuning problem.** Reconstructing every daily flag from git history (the `since_pct`/`verdict` outcome-tracking fields in `main.py` were stubbed and never actually populated) showed Screen 1 wasn't discriminating good sympathy-fade setups from bad ones. Its SKIP/NO_CASH reasoning was almost entirely mechanical ("already holding this," "basket rule — peer of a name we own," "no cash") rather than quality-based, and confidence score barely distinguished what got bought from what got skipped. The tell: the 4 trades it sized up as highest-conviction were its *worst* performers (-24.5pp alpha, 0-for-4) — if the confidence signal meant anything, conviction bets should have done better, not worse. Raising the minimum confidence bar wouldn't have fixed this either (confidence-3 trades averaged -7.3pp alpha, confidence-4 averaged -5.1pp — both losers). Sector beta wasn't the culprit: QQQ was flat (+0.01%) and tech-sector XLK was +1.78% over Screen 1's window, so tech itself wasn't a headwind. This looks broken at the mechanism level, not a dial to turn.

**Screen 0** was more genuine — it evaluated ~19 candidates/day and converted only 1.5% to buys (14 of 934 flags), correctly passing on most as "RATIONAL" (no real mispricing) or "UNCLEAR." That selectivity is plausibly why it landed close to market-neutral instead of a clear loser. Its "RATIONAL"-classified buys showed real edge (+9.2pp alpha, 60% win rate) — but only 5 trades, nowhere near enough to trust.

**Decision:** retired rather than fine-tuned. Screen 1's evidence against it (inverted conviction signal) is about as clean as this kind of data gets. Screen 0 didn't clear the bar of "worth the ongoing API spend and attention" on 3 months of roughly-breakeven data with a not-yet-provable edge.

---

## What it did (historical)

At retirement, one scheduled GitHub Actions run per weekday (US mode + paper-portfolio decision pass, ~22:00 AST / 19:00 UTC). Earlier versions ran up to 3x/day including a dedicated Taiwan pass and a separate US-morning pass — both were cut for cost as the project narrowed focus to the US screens.

Each run produced:

- **US dashboard** (`/index.html`) — discovery scan of mid-cap movers ($2B–$20B), AI announcement impact analysis, Trump signal flags, and per-screen paper-portfolio state
- **Taiwan dashboard** (`/tw.html`) — bilingual EN/中文 analysis of Taiwan market and ADR arbitrage (discovery only; never had a paper-portfolio screen)

Output was committed back to the repo and served via GitHub Pages. All historical data (`docs/data/`) is kept as the audit trail — nothing was deleted.

---

## Setup

Kept for reference if this is ever revived or forked.

### 1. Anthropic API key

Create an API key at console.anthropic.com under your funded org.

**Set monthly spend cap first.** $20–30/mo is plenty for this workload. Settings → Limits → Monthly limit. This is your fail-safe if anything goes wrong.

Set up usage alerts at 50/75/100% so you know within hours of any anomaly.

### 2. GitHub repo setup

Make this repo **private**. Settings → Danger Zone → Change visibility.

Add the API key to GitHub Secrets:
1. Settings → Secrets and variables → Actions
2. New repository secret
3. Name: `ANTHROPIC_API_KEY`
4. Value: paste your key
5. Save. You will not be able to view it again — only update or delete.

### 3. Enable GitHub Pages

Settings → Pages → Source: Deploy from a branch → Branch: `main`, folder: `/docs`.

Your dashboard will be at `https://<your-username>.github.io/agent-smith/`.

⚠️ **Pages serves the `/docs` folder publicly even from a private repo.** Anyone with the URL sees the analysis and paper-portfolio data.

### 4. Trigger a run

Actions → **analyze** → Run workflow → choose mode → Run.

Takes a few minutes. Check the Actions log for failures.

### To pause or resume runs

Actions → **analyze** → "..." menu (top right) → **Disable workflow** (or **Enable workflow** to resume). This stops both the scheduled cron and manual runs without touching any code or data.

---

## Local development

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Set your key locally — never commit .env
echo "ANTHROPIC_API_KEY=sk-ant-..." > .env
export $(cat .env | xargs)

# Run any mode
python -m agent.main us
python -m agent.main tw
python -m agent.main all
```

Output written to `docs/data/latest_*.json`. Open `docs/index.html` in browser to view.

---

## Configuration

Everything tunable lives in `agent/config.py`:

- `DISCOVERY_FILTERS` — market cap range, volume floor, etc.
- `MOVEMENT_THRESHOLDS` — what counts as "unusual"
- `CATALYST_KEYWORDS` — what news to flag
- `RSS_FEEDS_EN` — English news sources
- `TAIWAN_NEWS_SOURCES_ZH` / `_EN` — Taiwan news sources
- `AI_NEWS_SOURCES` — AI announcement feeds
- `TAIWAN_CONTEXT` — Taiwan tickers tracked
- `MEGA_CAP_CONTEXT` — context only, never discovery candidates
- `SCREENS` — the named-thesis paper-portfolio screens (Screen 0 general mispricing, Screen 1 AI-event sympathy fade; each with its own bankroll, position sizing, confidence threshold, and holding window)

Add tickers to `market.py` `SP400_SAMPLE` / `SP600_SAMPLE` to expand discovery universe. The lists were samples — extend before relying on the system for real coverage.

---

## Security checklist

- [x] Repo is private
- [x] API key stored in GitHub Secrets, never in code
- [x] `.env` in `.gitignore`
- [x] Spending cap set on Anthropic console
- [x] Usage alerts enabled
- [ ] Pre-commit hook installed (`pip install detect-secrets && detect-secrets scan > .secrets.baseline`)
- [ ] Key rotated every 60–90 days
- [ ] Pages URL not shared publicly

---

## Known limitations

- yfinance has no top-movers endpoint — discovery universe is a static SP400/SP600 sample. For broader coverage, integrate Finnhub `/stock/market_status` or similar.
- Trump post source (trumpstruth.org) is fragile. May break if they change their feed.
- Claude doesn't learn between runs — the outcome-tracking fields meant to feed a calibration loop (`since_pct`/`verdict` on skipped flags) were stubbed in code and never actually populated. If this is revived, building that loop for real is probably the single highest-leverage fix — see the Postmortem's point about Screen 1's confidence score not correlating with outcomes.
- AI announcements may include bias when Claude analyzes Anthropic news. Bias guard built into prompt; flagged in output for separate grading.
- Pages URL is technically public. Don't share it.

---

## Cost estimate

Original estimate (3 runs/day) was $30–130/mo. Actual final-state cost, after cutting to one scheduled run/day and removing Screen 2, was roughly **$1/day on weekdays** (~$20/mo) — the daily Opus discovery passes for Screen 0 and Screen 1 (`run_discovery_pass` and `run_ai_pass` in `analyze.py`). GitHub Actions, GitHub Pages, yfinance, and RSS feeds all remained free. No token/cost telemetry was ever written to the repo — this figure comes from GitHub Actions run cadence and the Anthropic console, not from logged data.
