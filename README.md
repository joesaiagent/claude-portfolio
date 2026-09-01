# claude-portfolio — $300 → $10K (autonomous)

[![Live status](https://img.shields.io/badge/live-status-blue)](https://joesaiagent.github.io/claude-portfolio/)
[![Stars](https://img.shields.io/github/stars/joesaiagent/claude-portfolio?style=social)](https://github.com/joesaiagent/claude-portfolio)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

An autonomous AI agent stack trying to turn **$300 into $10K**. Real (paper) money in Alpaca, real posts on X/Bluesky/Mastodon, real public source code. Watch the experiment at **[joesaiagent.github.io/claude-portfolio](https://joesaiagent.github.io/claude-portfolio/)**.

Not financial advice. This is a public experiment in cheap LLM-driven autonomy.

## Strategy

| Bucket | Allocation | Capital | Strategy |
|--------|-----------|---------|----------|
| **Core** | 70% | $210 | Momentum + technical setups on large/mid-cap. Beat the S&P. |
| **Swing** | 20% | $60 | Earnings + catalyst plays. Days-to-weeks. |
| **Lottery** | 10% | $30 | Asymmetric upside. **Hard-capped — never holds >$30 of cost basis.** |

## Agents

| Agent | Cost | Role |
|-------|------|------|
| `research` | $0 | Alpaca daily bars + Python scoring rules pick candidates per bucket (yfinance fallback) |
| `analyst` | ~$0.006/day | 1 batched Haiku call reads candidates' headlines → bounded score tilt (±6) |
| `allocator` | $0 | Deterministic sizing, places real orders via Alpaca |
| `tracker` | $0 | Reads Alpaca state, computes P/L, templated summary |
| `content` | ~$0.005/day | 1 Claude Haiku narrative call per day (post-close only) |
| `analytics` | $0 | Counts post KPIs |
| `publish` | $0 | Pushes status JSON to GitHub Pages |

**Total cost breakdown (per month):**
- Anthropic API (3 Haiku calls/day: analyst + midday post + daily post): **~$0.35**
- X API (Pay Per Use, 1 plain-text post/day, no URLs): **~$0.45**
- Alpaca, yfinance, GitHub Pages, Bluesky, Mastodon: **$0**
- **Grand total: ~$0.60/month**

Free-tier architecture: heavy lifting is Python rules + free APIs (yfinance, Alpaca), Claude is reserved for the daily narrative post, and we post **once per day** to keep X API cost minimal. Affiliate links go in the X profile bio (free), not in tweets ($0.20/tweet penalty for URLs).

## Setup

```bash
git clone https://github.com/joesaiagent/claude-portfolio.git
cd claude-portfolio
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # fill in keys (see below)
```

Required keys:
- **Alpaca paper** (free): https://alpaca.markets → Paper Trading → API Keys
- **Anthropic** (~$5 buys months of runway at this rate): https://console.anthropic.com/settings/keys

Optional (for social fan-out):
- **Bluesky** app password: https://bsky.app/settings/app-passwords (instant, free)
- **Mastodon** access token: any instance's Preferences → Development → New Application
- **X** API keys: https://developer.x.com (1-3 day approval)

## Run

```bash
# Dashboard
streamlit run dashboard.py

# Autonomous loop (3 cycles/day at 9:00, 12:00, 16:30 ET)
caffeinate -i python runner.py

# Single cycle
python runner.py --dry
```

## Architecture

```
runner.py          → schedules 3 daily cycles in ET (9:00 / 12:00 / 16:30)
  premarket (9:00 ET):
    exits          → stops / trailing / time-stop sells for the open
    research       → yfinance screens → watchlist.json
    tracker        → refresh state
    allocator      → places Alpaca orders for the open
    publish        → update GitHub Pages
  midday (12:00 ET):
    intraday       → real-time IEX prices refresh high-water-marks + breach alarm
    exits          → intraday stop check
    tracker        → refresh state
    allocator      → second-chance deploy of idle/just-settled cash (skips under $25)
    midday post    → what traded today and why
    publish        → update GitHub Pages
  postclose (16:30 ET):
    tracker        → final state
    content        → ONE Haiku narrative call → post to X (no URLs)
    analytics      → count post KPIs + signal attribution (which entry signals predict forward returns)
    publish        → update GitHub Pages
```

## Safety rails

- **ATR risk parity (2026-09-01):** each entry is sized so a stop-out at its ATR-scaled stop (2.5×ATR, floored by the bucket hard stop) loses ~1% of equity — a high-ATR name gets fewer dollars, not the same bet with more risk
- **Entry-extension filter (2026-09-01):** candidates too far above their 50DMA (core >12% / swing >18%) or with RSI>75 are skipped — momentum is bought, never chased
- **Swing stale trail (2026-09-01):** past max hold, only flat/losing swings are dumped; winners ride a tight 5% give-back trail instead of being force-sold
- **Lottery bucket RETIRED (2026-09-01):** −$15.16 realized, zero wins; its sleeve moved to core, where spare budget pyramids the strongest proven winner (+15%+, 7-day cooldown, account cap enforced)
- **Lottery hard cap:** never holds more than $30 of cost basis at once (residual protection; bucket no longer buys)
- **Per-position cap:** no single holding exceeds 20% of account equity (`MAX_POSITION_ACCOUNT_FRAC`); buys spread across enough names to deploy a bucket without breaching it
- **Theme cap:** at most ONE name per correlated theme cluster (BTC miners/crypto, quantum, space, small-cap AI) per bucket — a momentum run in one theme can't stack a bucket into a single bet
- **Lottery time stop:** a lottery name that never reaches +20% within ~15 trading days is sold — the thesis is a fast asymmetric pop, and a name that hasn't popped is dead capital
- **Lottery trailing stop arms at +15%** (give-back 25% from peak) with a **-40% hard floor**, so a runup can't silently round-trip to a deep loss
- **Broker-side stops:** every position carries a stop-loss order AT ALPACA (re-laid each cycle; fractional orders must be DAY), so the floor is enforced continuously during market hours, not just at the 3 daily checkpoints
- **Pre-earnings de-risk:** swing never enters within 10 days of a confirmed earnings report and exits 2 days before one — the bucket plays post-print reactions, it doesn't gamble prints
- **Bucket budgets enforced in code,** not just prompts
- **Market-hours check** before placing orders
- **Paper-mode default** — must explicitly flip `ALPACA_PAPER=false` to risk real money
- **Affiliate links optional** — set `ALPACA_REFERRAL_URL` in `.env` to auto-append

## License

MIT. See [LICENSE](LICENSE).
