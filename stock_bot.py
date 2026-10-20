"""
Paper-trading STOCK momentum bot for Alpaca.

- Trades only while the US market is open (9:30am-4pm ET, Mon-Fri).
- Uses fractional shares, so $100 can buy pricey stocks like NVDA.
- Holds at most ONE position at a time, all-in on the strongest setup (aggressive).
- Entry: 15-min EMA(9) > EMA(21), price > EMA(50), RSI 50-75, rising over the last hour.
- Exit: +6% take-profit, -3% stop-loss, or EMA(9) crosses back below EMA(21).
- Pattern Day Trader guard: accounts under $25k get max 3 day trades per 5 days.
  The bot won't sell a stock it bought today unless it has day trades to spare;
  otherwise it holds overnight and sells next session.
- Stops trading once equity hits the goal (2x start) or drops below the floor.

Stateless: all state lives in your Alpaca account.
HARD-CODED TO PAPER TRADING. Do not change paper=True.
"""

import os
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import GetOrdersRequest, MarketOrderRequest

# ---------------- settings (edit freely) ----------------
# Liquid, fast-moving names. Leveraged ETFs (TQQQ, SOXL) move ~3x the index.
SYMBOLS = ["NVDA", "TSLA", "AMD", "PLTR", "COIN", "TQQQ", "SOXL", "META"]
START_EQUITY = float(os.getenv("START_EQUITY", "100"))
GOAL_EQUITY = START_EQUITY * 2
FLOOR_EQUITY = START_EQUITY * 0.5
TAKE_PROFIT = 0.06
STOP_LOSS = -0.03
POSITION_FRACTION = 0.98
MAX_DAY_TRADES = 3          # PDT limit for accounts under $25k
NO_NEW_BUYS_AFTER = (15, 30)  # ET; avoid buying right before the close
LOOP_SECONDS = 300
# --------------------------------------------------------

ET = ZoneInfo("America/New_York")
KEY = os.environ["ALPACA_API_KEY"]
SECRET = os.environ["ALPACA_SECRET_KEY"]

trading = TradingClient(KEY, SECRET, paper=True)
data = StockHistoricalDataClient(KEY, SECRET)


def log(msg):
    print(f"[{datetime.now(ET):%Y-%m-%d %H:%M:%S} ET] {msg}", flush=True)


def ema(values, n):
    k = 2 / (n + 1)
    out = values[0]
    for v in values[1:]:
        out = v * k + out * (1 - k)
    return out


def rsi(values, n=14):
    gains, losses = [], []
    for a, b in zip(values[-n - 1:-1], values[-n:]):
        d = b - a
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_loss = sum(losses) / n
    if avg_loss == 0:
        return 100.0
    rs = (sum(gains) / n) / avg_loss
    return 100 - 100 / (1 + rs)


def get_closes():
    req = StockBarsRequest(
        symbol_or_symbols=SYMBOLS,
        timeframe=TimeFrame(15, TimeFrameUnit.Minute),
        start=datetime.now(timezone.utc) - timedelta(days=7),
        feed=DataFeed.IEX,  # free data feed
    )
    bars = data.get_stock_bars(req).data
    return {s: [b.close for b in bars.get(s, [])] for s in SYMBOLS}


def signals(closes):
    """Return dict symbol -> (is_buy, is_trend_broken, score)."""
    out = {}
    for s, c in closes.items():
        if len(c) < 60:
            continue
        e9, e21, e50 = ema(c[-60:], 9), ema(c[-60:], 21), ema(c[-60:], 50)
        r = rsi(c)
        momentum = c[-1] / c[-5] - 1
        buy = e9 > e21 and c[-1] > e50 and 50 < r < 75 and momentum > 0
        out[s] = (buy, e9 < e21, momentum)
    return out


def bought_today(symbol):
    start = datetime.now(ET).replace(hour=0, minute=0, second=0, microsecond=0)
    orders = trading.get_orders(GetOrdersRequest(
        status=QueryOrderStatus.CLOSED, symbols=[symbol], after=start,
    ))
    return any(o.side == OrderSide.BUY and o.filled_at for o in orders)


def run_once():
    if not trading.get_clock().is_open:
        log("market closed - nothing to do")
        return

    acct = trading.get_account()
    equity, cash = float(acct.equity), float(acct.cash)
    day_trades = int(acct.daytrade_count or 0)
    log(f"equity=${equity:.2f} cash=${cash:.2f} day_trades_used={day_trades}/{MAX_DAY_TRADES} "
        f"(goal ${GOAL_EQUITY:.0f}, floor ${FLOOR_EQUITY:.0f})")

    positions = {p.symbol: p for p in trading.get_all_positions()}

    if equity >= GOAL_EQUITY or equity <= FLOOR_EQUITY:
        log(("GOAL REACHED" if equity >= GOAL_EQUITY else "FLOOR HIT") + " - closing out and standing down.")
        if positions:
            trading.close_all_positions(cancel_orders=True)
        return

    if trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN)):
        log("an order is still pending - waiting")
        return

    sig = signals(get_closes())

    # ---- manage the open position ----
    for sym, p in positions.items():
        pl = float(p.unrealized_plpc)
        broken = sig.get(sym, (False, False, 0))[1]
        log(f"holding {sym} qty={p.qty} P/L={pl*100:+.2f}%")
        if not (pl >= TAKE_PROFIT or pl <= STOP_LOSS or broken):
            return
        why = "take-profit" if pl >= TAKE_PROFIT else "stop-loss" if pl <= STOP_LOSS else "trend broke"
        if bought_today(sym) and day_trades >= MAX_DAY_TRADES:
            log(f"wants to sell ({why}) but out of day trades - holding until next session")
            return
        log(f"SELL {sym} ({why})")
        trading.close_position(sym)
        return

    # ---- look for a new entry ----
    now = datetime.now(ET)
    if (now.hour, now.minute) >= NO_NEW_BUYS_AFTER:
        log("too close to the close for new buys")
        return
    candidates = [(score, s) for s, (buy, _, score) in sig.items() if buy]
    if not candidates:
        log("no setup - staying in cash")
        return
    score, best = max(candidates)
    notional = round(cash * POSITION_FRACTION, 2)
    if notional < 1:
        log("not enough cash to trade")
        return
    log(f"BUY {best} ${notional} (1h momentum {score*100:+.2f}%)")
    trading.submit_order(MarketOrderRequest(
        symbol=best, notional=notional,
        side=OrderSide.BUY, time_in_force=TimeInForce.DAY,  # fractional orders must be DAY
    ))


if __name__ == "__main__":
    if "--loop" in sys.argv:
        while True:
            try:
                run_once()
            except Exception as e:
                log(f"error: {e}")
            time.sleep(LOOP_SECONDS)
    else:
        run_once()
