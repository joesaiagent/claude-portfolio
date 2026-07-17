"""Streamlit dashboard — human-visible view of the autonomous portfolio."""
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

from agents._state import (
    PENDING_TRADES_FILE,
    POSTS_FILE,
    STATE_FILE,
    TRACKER_REPORT,
    WATCHLIST_FILE,
    load_state,
    save_state,
)

st.set_page_config(page_title="AI Portfolio", layout="wide", page_icon="📈")


def load_json(path: Path, default):
    return json.loads(path.read_text()) if path.exists() else default


def run_agent(module: str) -> tuple[int, str]:
    result = subprocess.run(
        ["python", "-m", f"agents.{module}.agent"],
        capture_output=True, text=True, cwd=Path(__file__).parent,
    )
    return result.returncode, (result.stdout + result.stderr)


def run_cycle_once() -> tuple[int, str]:
    result = subprocess.run(
        ["python", "runner.py", "--once"],
        capture_output=True, text=True, cwd=Path(__file__).parent,
    )
    return result.returncode, (result.stdout + result.stderr)


state = load_state()
report = load_json(TRACKER_REPORT, {})

# ---- Header ----
mode = report.get("broker_mode", "—")
mode_color = "🟢 LIVE" if mode == "LIVE" else ("🟡 PAPER" if mode == "paper" else "⚪ no data")
autonomous = "🤖 AUTONOMOUS" if state.get("autonomous_mode") else "👤 MANUAL"
st.title(f"📈 AI Portfolio — $300 → $10K")
st.caption(f"{mode_color}  •  {autonomous}  •  core 70% / swing 20% / lottery 10% (capped at $30)")

# ---- Sidebar ----
with st.sidebar:
    st.header("Mode")
    new_auto = st.toggle("Autonomous mode", value=state.get("autonomous_mode", False))
    if new_auto != state.get("autonomous_mode"):
        state["autonomous_mode"] = new_auto
        save_state(state)
        st.rerun()

    paper_env = os.getenv("ALPACA_PAPER", "true").lower() != "false"
    st.caption(f"Broker: Alpaca {'(paper)' if paper_env else '(LIVE)'}")
    st.caption("Change `ALPACA_PAPER=false` in .env to go live.")

    st.divider()
    st.header("Run agents")
    if st.button("🔁 Full cycle (research → buy → tracker → post)"):
        with st.spinner("Running full cycle..."):
            rc, out = run_cycle_once()
        st.code(out[-2500:])
    if st.button("🔍 Research"):
        with st.spinner("Searching..."):
            rc, out = run_agent("research")
        st.code(out[-1500:])
    if st.button("📊 Allocator"):
        with st.spinner("Sizing/placing..."):
            rc, out = run_agent("allocator")
        st.code(out[-1500:])
    if st.button("🔄 Tracker"):
        with st.spinner("Fetching..."):
            rc, out = run_agent("tracker")
        st.code(out[-1500:])
    if st.button("✍️ Content"):
        with st.spinner("Drafting/posting..."):
            rc, out = run_agent("content")
        st.code(out[-1500:])
    if st.button("📈 Analytics"):
        with st.spinner("Analyzing..."):
            rc, out = run_agent("analytics")
        st.code(out[-1500:])

# ---- Top metrics ----
col1, col2, col3, col4 = st.columns(4)
col1.metric(
    "Portfolio Value",
    f"${report.get('total_portfolio_value', state['starting_capital']):.2f}",
    f"{report.get('total_pl_pct', 0):+.2f}%",
)
col2.metric("Starting", f"${state['starting_capital']:.2f}")
col3.metric("Cash", f"${report.get('cash', 0):.2f}")
col4.metric("P/L $", f"${report.get('total_pl_dollars', 0):+.2f}")

if report.get("summary"):
    st.info(report["summary"])
else:
    st.warning("Run the Tracker (sidebar) to fetch live state from Alpaca.")

# ---- Bucket cards ----
st.subheader("Buckets")
buckets = report.get("buckets") or {
    name: {"target_dollars": cfg["target_dollars"], "deployed_cost_basis": 0,
           "current_value": 0, "unrealized_pl_dollars": 0, "unrealized_pl_pct": 0,
           "realized_pl_dollars": 0, "position_count": 0}
    for name, cfg in state["allocation_targets"].items()
}
bcols = st.columns(3)
for i, (name, b) in enumerate(buckets.items()):
    with bcols[i]:
        st.markdown(f"### {name.title()}")
        st.metric(
            f"Value (target ${b['target_dollars']:.0f})",
            f"${b['current_value']:.2f}",
            f"{b['unrealized_pl_pct']:+.2f}%",
        )
        st.caption(state["allocation_targets"][name]["strategy"])
        st.caption(
            f"Positions: {b['position_count']}  •  Cost basis: ${b['deployed_cost_basis']:.2f}  •  "
            f"Realized P/L: ${b['realized_pl_dollars']:+.2f}"
        )

# ---- Positions ----
st.subheader("Positions")
positions = report.get("positions", [])
if positions:
    st.dataframe(positions, hide_index=True, width="stretch")
else:
    st.caption("No positions yet.")

# ---- Pending (only shown when not autonomous) ----
pending = load_json(PENDING_TRADES_FILE, [])
if pending:
    st.subheader("Pending Trades (manual approval)")
    for i, t in enumerate(pending):
        c1, c2 = st.columns([7, 1])
        c1.markdown(
            f"**[{t.get('bucket','?')}] {t.get('ticker','?')}** — "
            f"{t.get('shares',0)} sh @ ≤${t.get('max_price',0):.2f}  "
            f"_{t.get('rationale','')}_"
        )
        if c2.button("Clear", key=f"clear-{i}"):
            remaining = [x for j, x in enumerate(pending) if j != i]
            PENDING_TRADES_FILE.write_text(json.dumps(remaining, indent=2))
            st.rerun()

# ---- Watchlist ----
with st.expander(f"Watchlist ({len(load_json(WATCHLIST_FILE, []))})"):
    wl = load_json(WATCHLIST_FILE, [])
    if wl:
        st.dataframe(wl, hide_index=True, width="stretch")

# ---- Posts ----
st.subheader("Posts")
posts = load_json(POSTS_FILE, [])
if posts:
    posted = [p for p in posts if p.get("status") == "posted"][-10:]
    drafts = [p for p in posts if p.get("status") == "draft"]
    if drafts:
        st.markdown("**Drafts (X creds missing — paste manually):**")
        for p in drafts[-5:]:
            st.markdown(f"- _{p.get('text','')}_")
    if posted:
        st.markdown(f"**Posted ({len(posted)} shown):**")
        for p in posted:
            st.markdown(f"- ✓ [{p.get('topic','')}] {p.get('text','')}")

# ---- Order log ----
with st.expander(f"Order Log ({len(state.get('order_log', []))})"):
    log = state.get("order_log", [])
    if log:
        st.dataframe(log[-50:], hide_index=True, width="stretch")
