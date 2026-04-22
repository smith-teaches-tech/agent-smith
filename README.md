# agent-smith

Personal market analysis agent. Runs three times daily, scans US mid-caps and Taiwan stocks for unusual moves, cross-references news (including AI announcements and Trump posts), and uses Claude to flag potentially mispriced situations.

**This is a pointer system, not a recommendation system.** It directs attention to interesting setups. All buy/sell decisions require independent research.

---

## What it does

Three scheduled runs per weekday (Sat–Sun skipped — markets closed):

| Time (AST) | Time (UTC) | Mode | Purpose |
|---|---|---|---|
| 09:00 | 06:00 | `tw` | Taiwan-focused (post Taipei close) |
| 17:00 | 14:00 | `all` | US morning + Taiwan refresh |
| 22:00 | 19:00 | `all` | US afternoon + Taiwan refresh |

Each run produces:

- **US dashboard** (`/index.html`) — discovery scan of mid-cap movers ($2B–$20B), AI announcement impact analysis, Trump signal flags
- **Taiwan dashboard** (`/tw.html`) — bilingual EN/中文 analysis of Taiwan market and ADR arbitrage

Output is committed back to the repo and served via GitHub Pages.

---

## Setup

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

⚠️ **Pages serves the `/docs` folder publicly even from a private repo.** Anyone with the URL sees your analysis. For v0 this is fine (no portfolio data yet, just analysis). When portfolio tracking is added, we'll move auth-protected.

### 4. Trigger first run

Actions → "agent-smith analysis" → Run workflow → choose mode → Run.

First run takes ~5 minutes. Check the Actions log for any failures.

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

Add tickers to `market.py` `SP400_SAMPLE` / `SP600_SAMPLE` to expand discovery universe. The current lists are samples — extend before relying on the system for real coverage.

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

## Roadmap

**Phase 1 (current)**
- [x] Discovery scan (US mid-caps)
- [x] AI announcement impact pass
- [x] Taiwan bilingual analysis
- [x] Trump post monitoring
- [x] Three scheduled runs

**Phase 1.5 (next)**
- [ ] Portfolio tracking ($1,500 cap, position logging)
- [ ] "Why I bought" + "Claude call ID" linkage
- [ ] Status reads on held positions
- [ ] Apps Script + Google Sheet integration for trade input

**Phase 2 (after weeks of data)**
- [ ] Grading workflow (weekly retrospective)
- [ ] Performance-weighted prompts (calibration loop)
- [ ] Earnings calendar integration
- [ ] Congressional trades (Capitol Trades)
- [ ] "What you missed" view
- [ ] Watchlist for stocks Claude flagged that you didn't buy

---

## Known limitations

- yfinance has no top-movers endpoint — discovery universe is a static SP400/SP600 sample. For broader coverage, integrate Finnhub `/stock/market_status` or similar.
- Trump post source (trumpstruth.org) is fragile. May break if they change their feed.
- Claude doesn't learn between runs — calibration must be built into prompts using stored grading data (Phase 2).
- AI announcements may include bias when Claude analyzes Anthropic news. Bias guard built into prompt; flagged in output for separate grading.
- Pages URL is technically public. Don't share it.

---

## Cost estimate

- GitHub Actions: 0 (well within 2,000 free minutes/month)
- GitHub Pages: 0
- yfinance, RSS feeds: 0
- Anthropic API: ~$0.50–$2.00 per run × 3 runs/day × 22 trading days ≈ **$30–$130/mo**

Set monthly cap on Anthropic console to bound this.
