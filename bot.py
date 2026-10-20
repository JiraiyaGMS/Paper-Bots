"""
Paper-trading crypto momentum bot for Alpaca.

- Trades crypto (runs 24/7, no market hours, no day-trade rule).
- Holds at most ONE position at a time, all-in on the strongest setup (aggressive).
- Entry: 15-min EMA(9) above EMA(21), price above EMA(50), RSI between 50 and 75.
- Exit: +6% take-profit, -3% stop-loss, or EMA(9) crosses back below EMA(21).
- Stops trading once equity hits the goal (2x start) or drops below the floor.

Stateless: all state lives in your Alpaca account, so it can run once per
scheduled job (GitHub Actions) or in a loop on your own computer (--loop).

HARD-CODED TO PAPER TRADING. Do not change paper=True.
"""

import os
import sys
import time
from datetime import datetime, timedelta, timezone

from alpaca.data.historical import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest

# ---------------- settings (edit freely) ----------------
SYMBOLS = ["BTC/USD", "ETH/USD", "SOL/USD", "DOGE/USD", "AVAX/USD", "LINK/USD"]
START_EQUITY = float(os.getenv("START_EQUITY", "100"))
GOAL_EQUITY = START_EQUITY * 2      # stop when doubled
FLOOR_EQUITY = START_EQUITY * 0.5   # stop if half is gone
TAKE_PROFIT = 0.06                  # +6%
STOP_LOSS = -0.03                   # -3%
POSITION_FRACTION = 0.95            # use 95% of cash (leave room for fees)
LOOP_SECONDS = 300                  # for --loop mode
# --------------------------------------------------------

KEY = os.environ["ALPACA_API_KEY"]
SECRET = os.environ["ALPACA_SECRET_KEY"]

trading = TradingClient(KEY, SECRET, paper=True)
data = CryptoHistoricalDataClient(KEY, SECRET)


def log(msg):
    print(f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}Z] {msg}", flush=True)


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
    req = CryptoBarsRequest(
        symbol_or_symbols=SYMBOLS,
        timeframe=TimeFrame(15, TimeFrameUnit.Minute),
        start=datetime.now(timezone.utc) - timedelta(days=2),
    )
    bars = data.get_crypto_bars(req).data
    return {s: [b.close for b in bars.get(s, [])] for s in SYMBOLS}


def signals(closes):
    """Return dict symbol -> (is_buy, is_trend_broken, score)."""
    out = {}
    for s, c in closes.items():
        if len(c) < 60:
            continue
        e9, e21, e50 = ema(c[-60:], 9), ema(c[-60:], 21), ema(c[-60:], 50)
        r = rsi(c)
        momentum = c[-1] / c[-5] - 1  # ~1 hour change
        buy = e9 > e21 and c[-1] > e50 and 50 < r < 75 and momentum > 0
        broken = e9 < e21
        out[s] = (buy, broken, momentum)
    return out


def to_pos_symbol(s):
    return s.replace("/", "")


def run_once():
    acct = trading.get_account()
    equity = float(acct.equity)
    cash = float(acct.cash)
    log(f"equity=${equity:.2f} cash=${cash:.2f} (goal ${GOAL_EQUITY:.0f}, floor ${FLOOR_EQUITY:.0f})")

    positions = {p.symbol: p for p in trading.get_all_positions()}

    if equity >= GOAL_EQUITY or equity <= FLOOR_EQUITY:
        reason = "GOAL REACHED" if equity >= GOAL_EQUITY else "FLOOR HIT"
        log(f"{reason} - closing everything and standing down.")
        if positions:
            trading.close_all_positions(cancel_orders=True)
        return

    sig = signals(get_closes())

    # ---- manage the open position ----
    for sym in SYMBOLS:
        p = positions.get(to_pos_symbol(sym))
        if not p:
            continue
        pl = float(p.unrealized_plpc)
        broken = sig.get(sym, (False, False, 0))[1]
        log(f"holding {sym} qty={p.qty} P/L={pl*100:+.2f}%")
        if pl >= TAKE_PROFIT or pl <= STOP_LOSS or broken:
            why = "take-profit" if pl >= TAKE_PROFIT else "stop-loss" if pl <= STOP_LOSS else "trend broke"
            log(f"SELL {sym} ({why})")
            trading.close_position(p.symbol)
        return  # one position at a time; don't buy in the same run as a sell

    # ---- look for a new entry ----
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
        side=OrderSide.BUY, time_in_force=TimeInForce.GTC,
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
