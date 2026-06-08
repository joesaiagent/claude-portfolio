# claude-portfolio — $300 → $10K (autonomous)

[![Live status](https://img.shields.io/badge/live-status-blue)](https://joesaiagent.github.io/claude-portfolio/)
[![Stars](https://img.shields.io/github/stars/joesaiagent/claude-portfolio?style=social)](https://github.com/joesaiagent/claude-portfolio)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

An autonomous AI agent stack trying to turn **$300 into $10K**. Real (paper) money in Alpaca, real posts on X/Bluesky/Mastodon, real public source code. Watch the experiment at **[joesaiagent.github.io/claude-portfolio](https://joesaiagent.github.io/claude-portfolio/)**.

Not financial advice. This is a public experiment in cheap LLM-driven autonomy.

## Strategy

| Bucket | Allocation | Capital | Strategy |
|--------|-----------|---------|----------|
| **Core** | 80% | $240 | Momentum + technical setups on large/mid-cap. Beat the S&P. |
| **Swing** | 15% | $45 | Earnings + catalyst plays. Days-to-weeks. |
| **Lottery** | 5% | $15 | Asymmetric upside. **Hard-capped — never deploys >$15 cumulative.** |

## Agents

| Agent | Cost | Role |
|-------|------|------|
| `research` | $0 | yfinance + Python scoring rules pick candidates per bucket |
| `allocator` | $0 | Deterministic sizing, places real orders via Alpaca |
| `tracker` | $0 | Reads Alpaca state, computes P/L, templated summary |
| `content` | ~$0.005/day | 1 Claude Haiku narrative call per day (post-close only) |
| `analytics` | $0 | Counts post KPIs |
| `publish` | $0 | Pushes status JSON to GitHub Pages |

**Total cost breakdown (per month):**
- Anthropic API (1 Haiku call/day): **~$0.15**
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
    research       → yfinance screens → watchlist.json
    tracker        → refresh state
    allocator      → places Alpaca orders for the open
    publish        → update GitHub Pages
  midday (12:00 ET):
    tracker        → refresh state
    publish        → update GitHub Pages
  postclose (16:30 ET):
    tracker        → final state
    content        → ONE Haiku narrative call → post to X (no URLs)
    analytics      → count post KPIs
    publish        → update GitHub Pages
```

## Safety rails

- **Lottery hard cap:** never deploys more than $15 cumulative
- **Bucket budgets enforced in code,** not just prompts
- **Market-hours check** before placing orders
- **Paper-mode default** — must explicitly flip `ALPACA_PAPER=false` to risk real money
- **Affiliate links optional** — set `ALPACA_REFERRAL_URL` in `.env` to auto-append

## License

MIT. See [LICENSE](LICENSE).
