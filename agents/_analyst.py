"""Claude analyst pass — ONE batched Haiku call per premarket over the whole
watchlist. The z-scored factors can't tell durable, news-driven momentum from a
pump; this layer reads each candidate's recent headlines and applies a small,
BOUNDED tilt plus a one-line note to the thesis. Judgment on top of the math,
never instead of it.

Cost: ~5 headlines/candidate for ~15 candidates ≈ 2-3K input + ~500 output
tokens on claude-haiku-4-5 ($1/$5 per MTok) ≈ half a cent per day.

Failure of ANY piece (no API key, news fetch down, model error, schema
mismatch) degrades to a no-op — candidates pass through unchanged, exactly as
if this module didn't exist. An LLM outage must never block the deterministic
pipeline.
"""
import json
import os

import requests
from dotenv import load_dotenv

load_dotenv()

MODEL = "claude-haiku-4-5"
NEWS_URL = "https://data.alpaca.markets/v1beta1/news"
HEADLINES_PER_TICKER = 5
# Verdict is an integer -2..2 (avoid_strongly .. strong_tailwind); each step is
# worth 3 score points, so the whole tilt is capped at ±6 — smaller than the
# ±15 alt-data cap; the LLM nudges, it never dominates.
VIEW_POINTS = 3.0

VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string"},
                    "view": {"type": "integer", "enum": [-2, -1, 0, 1, 2]},
                    "note": {"type": "string"},
                },
                "required": ["ticker", "view", "note"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["verdicts"],
    "additionalProperties": False,
}

SYSTEM = (
    "You are a skeptical equity analyst reviewing momentum-screen candidates for a "
    "small autonomous portfolio. For each candidate you get the quant thesis and "
    "recent headlines. Judge ONLY what the quant factors cannot see: is the momentum "
    "news-driven and durable, or hype/pump/one-off? Are there red flags (dilution, "
    "investigations, guidance cuts, binary events) or genuine tailwinds (raised "
    "guidance, contract wins, upgrades with substance)?\n"
    "view: -2 avoid strongly, -1 headwinds, 0 neutral/no signal, +1 tailwinds, "
    "+2 strong durable tailwinds. No headlines or ambiguous -> 0. Be conservative: "
    "reserve ±2 for clear, material evidence.\n"
    "note: one blunt sentence (<=15 words) naming the reason. Return a verdict for "
    "EVERY candidate, same tickers as the input."
)


def _recent_headlines(tickers: list[str]) -> dict[str, list[str]]:
    """Latest headlines per ticker from Alpaca's free news API, one batched
    request. Returns {} on any failure (analyst then sees 'no headlines')."""
    try:
        r = requests.get(
            NEWS_URL,
            params={"symbols": ",".join(sorted(set(tickers))), "limit": 50},
            headers={"APCA-API-KEY-ID": os.environ["ALPACA_API_KEY"],
                     "APCA-API-SECRET-KEY": os.environ["ALPACA_SECRET_KEY"]},
            timeout=15,
        )
        items = r.json().get("news", [])
    except Exception:
        return {}
    out: dict[str, list[str]] = {}
    for n in items:
        for s in n.get("symbols", []):
            if s in tickers and len(out.setdefault(s, [])) < HEADLINES_PER_TICKER:
                out[s].append(f"{n.get('created_at', '')[:10]}: {n.get('headline', '')}")
    return out


def _ask_claude(payload: list[dict]) -> dict[str, dict]:
    """The one Haiku call. Structured output (json_schema) so the reply is
    guaranteed-valid JSON. Returns {ticker: {view, note}}; raises on failure
    (caller degrades)."""
    import anthropic
    client = anthropic.Anthropic()
    msg = client.messages.create(
        model=MODEL,
        max_tokens=1000,
        system=SYSTEM,
        output_config={"format": {"type": "json_schema", "schema": VERDICT_SCHEMA}},
        messages=[{"role": "user", "content": json.dumps(payload)}],
    )
    text = next(b.text for b in msg.content if b.type == "text")
    data = json.loads(text)
    return {v["ticker"]: v for v in data["verdicts"]}


def apply_views(candidates: list[dict], verdicts: dict[str, dict]) -> list[dict]:
    """Fold verdicts into candidate scores/theses. Pure function (unit-tested).
    Unknown tickers and view=0 pass through with no score change."""
    for c in candidates:
        v = verdicts.get(c["ticker"])
        if not v:
            continue
        view = max(-2, min(2, int(v.get("view", 0))))
        c["llm_view"] = view
        c["llm_note"] = v.get("note", "")
        if view:
            c["score"] = round(c["score"] + view * VIEW_POINTS, 2)
            c["conviction"] = min(5, max(1, int(round((c["score"] - 50) / 8 + 3))))
        if v.get("note"):
            c["thesis"] = f"{c.get('thesis', '')} | analyst: {v['note']}"
    candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates


def enrich_with_analyst(candidates: list[dict]) -> list[dict]:
    """Entry point used by research. Whole thing degrades to a no-op on any
    failure or when no ANTHROPIC_API_KEY is configured."""
    if not candidates or not os.getenv("ANTHROPIC_API_KEY"):
        return candidates
    try:
        headlines = _recent_headlines([c["ticker"] for c in candidates])
        payload = [{
            "ticker": c["ticker"],
            "bucket": c.get("bucket"),
            "quant_thesis": c.get("thesis", ""),
            "headlines": headlines.get(c["ticker"], []),
        } for c in candidates]
        verdicts = _ask_claude(payload)
        out = apply_views(candidates, verdicts)
        flagged = [(c["ticker"], c["llm_view"]) for c in out if c.get("llm_view")]
        print(f"[analyst] 1 {MODEL} call, {len(verdicts)} verdicts, non-neutral: {flagged or 'none'}")
        return out
    except Exception as e:
        print(f"[analyst] degraded to no-op: {str(e)[:150]}")
        return candidates
