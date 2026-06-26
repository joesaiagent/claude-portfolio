"""Alpaca broker wrapper — paper or live based on env var."""
import os
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import GetCalendarRequest, LimitOrderRequest, MarketOrderRequest

ET = ZoneInfo("America/New_York")
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest
from dotenv import load_dotenv

load_dotenv()


def _client() -> TradingClient:
    key = os.environ["ALPACA_API_KEY"]
    secret = os.environ["ALPACA_SECRET_KEY"]
    paper = os.getenv("ALPACA_PAPER", "true").lower() != "false"
    return TradingClient(key, secret, paper=paper)


def _data_client() -> StockHistoricalDataClient:
    return StockHistoricalDataClient(
        os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"]
    )


def account_info() -> dict:
    a = _client().get_account()
    return {
        "cash": float(a.cash),
        "buying_power": float(a.buying_power),
        "non_marginable_buying_power": float(getattr(a, "non_marginable_buying_power", a.buying_power) or 0.0),
        "equity": float(a.equity),
        "portfolio_value": float(a.portfolio_value),
        "paper": os.getenv("ALPACA_PAPER", "true").lower() != "false",
    }


def positions() -> list[dict]:
    out = []
    for p in _client().get_all_positions():
        out.append({
            "ticker": p.symbol,
            "shares": float(p.qty),
            "entry_price": float(p.avg_entry_price),
            "current_price": float(p.current_price),
            "market_value": float(p.market_value),
            "pl_dollars": float(p.unrealized_pl),
            "pl_pct": float(p.unrealized_plpc) * 100,
        })
    return out


def latest_price(ticker: str) -> float | None:
    try:
        req = StockLatestQuoteRequest(symbol_or_symbols=ticker)
        quote = _data_client().get_stock_latest_quote(req)[ticker]
        # Use midpoint of bid/ask if available, else ask, else bid.
        if quote.bid_price and quote.ask_price:
            return (float(quote.bid_price) + float(quote.ask_price)) / 2
        return float(quote.ask_price or quote.bid_price or 0) or None
    except Exception:
        return None


def open_orders() -> list[dict]:
    """Currently-open (unfilled) orders. The allocator nets these out so a buy
    queued by one cycle but not yet filled (e.g. a premarket DAY limit waiting
    for the 9:30 open) is never re-issued by the next cycle — making allocation
    idempotent across cycles. Degrades to [] on any API error."""
    from alpaca.trading.requests import GetOrdersRequest
    from alpaca.trading.enums import QueryOrderStatus
    try:
        orders = _client().get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))
    except Exception:
        return []
    out = []
    for o in orders:
        qty = float(o.qty) if o.qty is not None else 0.0
        price = float(o.limit_price) if getattr(o, "limit_price", None) else None
        out.append({
            "ticker": o.symbol,
            "side": "buy" if str(o.side).endswith("BUY") else "sell",
            "shares": qty,
            "limit_price": price,
            "notional": (price or 0.0) * qty,
        })
    return out


# Order statuses Alpaca treats as final: the order will never fill further. A
# DAY limit that never executed ends EXPIRED/CANCELED; a bad order REJECTED.
TERMINAL_ORDER_STATUSES = {"filled", "canceled", "expired", "rejected", "done_for_day", "replaced"}


def order_status(order_id: str) -> dict | None:
    """Look up an order's current status by id, for reconciling the order log
    against actual fills. Returns the raw status (matching submit_buy/submit_sell's
    `str(placed.status)` form, e.g. 'OrderStatus.FILLED'), a normalized lowercase
    `status_value`, a `terminal` flag, and the realized filled_qty/filled_avg_price.
    Degrades to None on any API error (mirrors open_orders())."""
    try:
        o = _client().get_order_by_id(order_id)
    except Exception:
        return None
    value = str(getattr(o.status, "value", o.status)).lower()
    filled_qty = float(o.filled_qty) if getattr(o, "filled_qty", None) else 0.0
    filled_avg = float(o.filled_avg_price) if getattr(o, "filled_avg_price", None) else None
    return {
        "order_id": str(o.id),
        "ticker": o.symbol,
        "status": str(o.status),
        "status_value": value,
        "terminal": value in TERMINAL_ORDER_STATUSES,
        "filled_qty": filled_qty,
        "filled_avg_price": filled_avg,
    }


def submit_buy(ticker: str, shares: float, limit_price: float | None = None) -> dict:
    """Place a buy. Defaults to a limit order at limit_price, falls back to market."""
    client = _client()
    if limit_price:
        order = LimitOrderRequest(
            symbol=ticker,
            qty=shares,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
            limit_price=round(limit_price, 2),
        )
    else:
        order = MarketOrderRequest(
            symbol=ticker, qty=shares, side=OrderSide.BUY, time_in_force=TimeInForce.DAY
        )
    placed = client.submit_order(order)
    return {
        "order_id": str(placed.id),
        "ticker": placed.symbol,
        "shares": float(placed.qty),
        "side": "buy",
        "status": str(placed.status),
        "limit_price": limit_price,
    }


def submit_sell(ticker: str, shares: float) -> dict:
    order = MarketOrderRequest(
        symbol=ticker, qty=shares, side=OrderSide.SELL, time_in_force=TimeInForce.DAY
    )
    placed = _client().submit_order(order)
    return {
        "order_id": str(placed.id),
        "ticker": placed.symbol,
        "shares": float(placed.qty),
        "side": "sell",
        "status": str(placed.status),
    }


def is_market_open() -> bool:
    try:
        return _client().get_clock().is_open
    except Exception:
        return False


def is_trading_day(day: date | None = None) -> bool:
    """True if `day` (default: today in ET) is a NYSE trading day.

    Uses Alpaca's calendar, so it correctly excludes both weekends AND market
    holidays (e.g. Juneteenth). Unlike is_market_open(), this is True for the
    whole day — so it works for the 9:00 premarket and 16:30 post-close cycles
    when the market isn't actively open. On a calendar API failure it falls
    back to a Mon–Fri check so a transient outage can't halt a normal weekday.
    """
    day = day or datetime.now(ET).date()
    try:
        cal = _client().get_calendar(GetCalendarRequest(start=day, end=day))
        return any(c.date == day for c in cal)
    except Exception:
        return day.weekday() < 5


def trading_days_since(start: date, end: date | None = None) -> int:
    """Count of NYSE trading days from `start` to `end` (default today), inclusive.
    Used for the social 'Day N' counter so it's computed, never guessed. Falls
    back to a weekday count (holidays uncounted) only if the calendar API fails."""
    end = end or datetime.now(ET).date()
    if end < start:
        return 0
    try:
        cal = _client().get_calendar(GetCalendarRequest(start=start, end=end))
        return sum(1 for c in cal if start <= c.date <= end)
    except Exception:
        # Approximate: inclusive weekday span (can't know holidays offline).
        from datetime import timedelta
        n, d = 0, start
        while d <= end:
            if d.weekday() < 5:
                n += 1
            d += timedelta(days=1)
        return n
