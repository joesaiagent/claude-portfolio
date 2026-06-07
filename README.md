# AI Portfolio — $300 → $10K (Autonomous)

Fully autonomous multi-agent system trading $300 via Alpaca and posting to X. You supervise via Alpaca's web UI, X feed, and the local dashboard. No approval clicks.

## Strategy

| Bucket | Allocation | Capital | Strategy |
|--------|-----------|---------|----------|
| **Core** | 80% | $240 | Momentum + technical setups on large/mid-cap. Beat the S&P. |
| **Swing** | 15% | $45 | Earnings + catalyst plays. Days-to-weeks. |
| **Lottery** | 5% | $15 | Asymmetric upside. **Hard-capped — never deploys >$15 cumulative.** |

## Agents

| Agent | Role |
|-------|------|
| `research` | Web-search candidates per bucket → `watchlist.json`. |
| `allocator` | Sizes orders, **places them via Alpaca** when autonomous. |
| `tracker` | Reads positions + prices from Alpaca, per-bucket P/L, Claude summary. |
| `content` | Drafts posts, **auto-posts to X** when autonomous. |
| `analytics` | Pulls post KPIs (X analytics — stubbed for v1). |

All powered by `claude-opus-4-7` with adaptive thinking.

## Setup

### 1. Install
```bash
cd ~/ai-portfolio
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Get Alpaca paper keys (free, instant)
1. Sign up at https://alpaca.markets/
2. Dashboard → **Paper Trading** tab (top-right toggle)
3. Click **Generate New Keys** → copy `API Key ID` + `Secret Key`
4. Paste into `.env`:
   ```
   ALPACA_API_KEY=PK...
   ALPACA_SECRET_KEY=...
   ALPACA_PAPER=true
   ```

### 3. Get X (Twitter) API keys
1. Apply at https://developer.x.com — Free tier supports posting (50 tweets/day).
2. Create an App → Generate **Consumer Keys** (API Key + Secret)
3. Generate **Access Token + Secret** (must have read-and-write permission)
4. Paste all four into `.env`.

If X keys aren't ready yet, drafts will save to the dashboard and you can paste manually.

### 4. Run dashboard
```bash
streamlit run dashboard.py
```

### 5. Run the autonomous loop
```bash
python runner.py                    # 1-hour cycles, forever
python runner.py --once             # one cycle, exit
python runner.py --interval 1800    # 30-minute cycles
```

When `autonomous_mode: true` and market is open, the allocator places orders directly and the content agent auto-posts.

## Going Live

After 2-4 weeks in paper mode showing decent picks:

1. In Alpaca dashboard → toggle to **Live Trading** → fund via ACH from Robinhood ($300)
2. Generate **Live keys** → paste into `.env`, set `ALPACA_PAPER=false`
3. Restart runner. Same code, real money.

## Where to supervise

- **Brokerage:** Alpaca dashboard shows live positions, fills, P/L
- **Social:** X feed shows posts as they go up
- **Local:** Streamlit dashboard (`localhost:8501`) for buckets + watchlist + drafts
- **Logs:** every order is appended to `data/portfolio_state.json` → `order_log`

## Safety rails (still in place)

- **Lottery hard cap:** `lottery_deployed_total` ≥ $15 → allocator skips lottery buys forever.
- **Bucket budgets** enforced in code, not just prompts.
- **Market-hours check** before placing orders.
- **Paper mode default.** You must explicitly flip `ALPACA_PAPER=false` to risk real money.
