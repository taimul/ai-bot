"""
================================================================
  PROFESSIONAL MULTI-PAIR CRYPTO TRADING BOT
  Platform  : Bybit TESTNET (Spot)

  STRATEGY  : Dual-Timeframe Bollinger Band Mean Reversion
  ─────────────────────────────────────────────────────────
  TREND (1H):  EMA20 > EMA50 → confirmed uptrend
  ENTRY (15m): Price touches lower BB  +  RSI < 50
               + MACD histogram turning positive
  EXIT  (15m): Price reaches BB middle band  OR
               RSI > 62  OR  MACD turns bearish  OR
               1H trend has reversed (EMA20 < EMA50)

  RISK MANAGEMENT:
    - Risk-based position sizing (2% of account per trade)
    - Hard Stop Loss (3%)
    - Take Profit at BB middle band (dynamic) or 6% max
    - Trailing Stop (1.5%) to lock in profits
    - Max 4 concurrent positions
    - Daily loss circuit breaker (5%)
    - Trade log saved to CSV
================================================================
"""

import time
import csv
import os
import pandas as pd
from datetime import datetime
from pybit.unified_trading import HTTP

# ================================================================
#  CONFIG
# ================================================================
API_KEY    = "YndDHQr6Mpx2i3fElS"
API_SECRET = "oGjioa9ZVih7rw1b4dBVMJJ2UkGQZ4LrF5cR"

SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT",
    "DOGEUSDT", "BNBUSDT", "AVAXUSDT", "ADAUSDT",
]

INTERVAL_15M = "15"    # Entry timeframe
INTERVAL_1H  = "60"    # Trend timeframe

# -- Risk Management --
RISK_PCT         = 0.02   # 2% of balance risked per trade
STOP_LOSS_PCT    = 0.03   # Hard stop: -3% from entry
TAKE_PROFIT_PCT  = 0.06   # Max hard TP: +6% (BB mid usually hit first)
TRAIL_STOP_PCT   = 0.015  # Trailing stop activates after +1% gain
MAX_POSITIONS    = 4      # Max concurrent open trades
DAILY_LOSS_LIMIT = 0.05   # Pause 1h if daily loss exceeds 5% of balance
MIN_TRADE_USDT   = 5      # Minimum order size

# -- Indicator Settings --
RSI_PERIOD  = 14
RSI_BUY     = 50          # Entry: RSI below this (dip in uptrend)
RSI_SELL    = 62          # Exit: RSI above this (mean reversion complete)
BB_PERIOD   = 20          # Bollinger Band period
BB_STD      = 2.0         # Bollinger Band standard deviation
BB_ENTRY    = 0.20        # Enter when price is in bottom 20% of BB range
BB_EXIT     = 0.55        # Exit when price reaches 55% of BB range (near midline)
MACD_FAST   = 12
MACD_SLOW   = 26
MACD_SIG    = 9
EMA_1H_FAST = 20          # 1H fast EMA for trend
EMA_1H_SLOW = 50          # 1H slow EMA for trend

SLEEP_SEC   = 60
TRADE_LOG   = "trades.csv"
# ================================================================

session = HTTP(testnet=True, api_key=API_KEY, api_secret=API_SECRET)

# ================================================================
#  LOGGING
# ================================================================
def log(msg, level="INFO"):
    icons = {"INFO": "   ", "BUY": ">>", "SELL": "<<", "WARN": "!!", "ERR": "XX"}
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {icons.get(level,'  ')} {msg}")

def init_trade_log():
    if not os.path.exists(TRADE_LOG):
        with open(TRADE_LOG, "w", newline="") as f:
            csv.writer(f).writerow([
                "datetime", "symbol", "side", "price", "qty_usdt",
                "entry_price", "pnl_usdt", "pnl_pct", "reason"
            ])

def log_trade(symbol, side, price, qty_usdt, entry_price=0, pnl=0, reason=""):
    pnl_pct = (pnl / qty_usdt * 100) if qty_usdt > 0 else 0
    with open(TRADE_LOG, "a", newline="") as f:
        csv.writer(f).writerow([
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            symbol, side, f"{price:.6f}", f"{qty_usdt:.2f}",
            f"{entry_price:.6f}", f"{pnl:.4f}", f"{pnl_pct:.2f}", reason
        ])

# ================================================================
#  DATA & INDICATORS
# ================================================================
def safe_float(val, default=0.0):
    try:
        v = float(val)
        return v if v == v else default
    except (ValueError, TypeError):
        return default

def get_candles(symbol, interval, limit=120):
    resp = session.get_kline(category="spot", symbol=symbol, interval=interval, limit=limit)
    raw  = resp["result"]["list"]
    df   = pd.DataFrame(raw, columns=["timestamp","open","high","low","close","volume","turnover"])
    df   = df[::-1].reset_index(drop=True)
    for col in ["open","high","low","close","volume"]:
        df[col] = df[col].astype(float)
    return df

def get_1h_trend(symbol):
    """
    Dual-Timeframe Trend Check (1H chart):
    Returns True if EMA20 > EMA50 (bullish) on the 1-hour timeframe.
    """
    df = get_candles(symbol, INTERVAL_1H, limit=60)
    df["ema_fast"] = df["close"].ewm(span=EMA_1H_FAST, adjust=False).mean()
    df["ema_slow"] = df["close"].ewm(span=EMA_1H_SLOW, adjust=False).mean()
    last = df.iloc[-1]
    return last["ema_fast"] > last["ema_slow"], last["ema_fast"], last["ema_slow"]

def add_15m_indicators(df):
    close = df["close"]
    vol   = df["volume"]

    # RSI
    delta     = close.diff()
    gain      = delta.clip(lower=0).rolling(RSI_PERIOD).mean()
    loss      = (-delta.clip(upper=0)).rolling(RSI_PERIOD).mean()
    df["rsi"] = 100 - (100 / (1 + gain / loss))

    # Bollinger Bands
    df["bb_mid"]   = close.rolling(BB_PERIOD).mean()
    df["bb_std"]   = close.rolling(BB_PERIOD).std()
    df["bb_upper"] = df["bb_mid"] + BB_STD * df["bb_std"]
    df["bb_lower"] = df["bb_mid"] - BB_STD * df["bb_std"]
    # BB %B: 0 = at lower band, 0.5 = at midline, 1 = at upper band
    bb_range       = df["bb_upper"] - df["bb_lower"]
    df["bb_pct"]   = (close - df["bb_lower"]) / bb_range.replace(0, float("nan"))

    # MACD
    ema_f          = close.ewm(span=MACD_FAST, adjust=False).mean()
    ema_s          = close.ewm(span=MACD_SLOW, adjust=False).mean()
    df["macd"]     = ema_f - ema_s
    df["macd_sig"] = df["macd"].ewm(span=MACD_SIG, adjust=False).mean()
    df["macd_hist"]= df["macd"] - df["macd_sig"]

    # Volume ratio
    df["vol_ratio"] = vol / vol.rolling(20).mean()

    return df

# ================================================================
#  ACCOUNT
# ================================================================
def get_balance():
    resp  = session.get_wallet_balance(accountType="UNIFIED", coin="USDT")
    coins = resp["result"]["list"][0]["coin"]
    for c in coins:
        if c["coin"] == "USDT":
            return safe_float(c.get("availableBalance") or c.get("walletBalance"))
    return 0.0

def get_position_qty(symbol):
    base = symbol.replace("USDT", "")
    try:
        resp  = session.get_wallet_balance(accountType="UNIFIED", coin=base)
        coins = resp["result"]["list"][0]["coin"]
        for c in coins:
            if c["coin"] == base:
                return safe_float(c.get("availableBalance") or c.get("walletBalance"))
    except Exception:
        pass
    return 0.0

def get_price(symbol):
    resp = session.get_tickers(category="spot", symbol=symbol)
    return safe_float(resp["result"]["list"][0]["lastPrice"])

def calc_position_size(balance):
    """2% account risk per trade, capped at 20% of balance."""
    size = (balance * RISK_PCT) / STOP_LOSS_PCT
    return round(min(size, balance * 0.20), 2)

# ================================================================
#  ORDERS
# ================================================================
def place_buy(symbol, usdt_amount):
    try:
        resp = session.place_order(
            category="spot", symbol=symbol, side="Buy",
            orderType="Market", qty=str(usdt_amount), marketUnit="quoteCoin",
        )
        log(f"BUY  {symbol} | ${usdt_amount:.2f} USDT | ID:{resp['result']['orderId']}", "BUY")
        return True
    except Exception as e:
        log(f"BUY FAILED {symbol}: {e}", "ERR")
        return False

def place_sell(symbol, coin_qty, reason="signal"):
    try:
        qty_str = f"{coin_qty:.6f}"
        resp = session.place_order(
            category="spot", symbol=symbol, side="Sell",
            orderType="Market", qty=qty_str,
        )
        log(f"SELL {symbol} | {qty_str} {symbol.replace('USDT','')} | [{reason}] | ID:{resp['result']['orderId']}", "SELL")
        return True
    except Exception as e:
        log(f"SELL FAILED {symbol}: {e}", "ERR")
        return False

# ================================================================
#  SIGNALS  (Option 1 + Option 2 Combined)
# ================================================================
def buy_signal(df, trend_ok):
    """
    Entry requires ALL of:
      [Option 2] 1H EMA20 > EMA50  → macro uptrend confirmed
      [Option 1] BB %B < BB_ENTRY  → price at lower Bollinger Band (dip)
      [Option 1] RSI < RSI_BUY     → oversold on 15m
      [Option 1] MACD hist turning positive → momentum reversing up
    """
    last = df.iloc[-1]
    prev = df.iloc[-2]

    at_lower_bb   = safe_float(last["bb_pct"]) < BB_ENTRY
    rsi_dip       = last["rsi"] < RSI_BUY
    macd_turning  = prev["macd_hist"] < 0 and last["macd_hist"] > 0

    reasons = []
    if trend_ok:     reasons.append("1H-UP")
    if at_lower_bb:  reasons.append(f"BB={last['bb_pct']:.2f}")
    if rsi_dip:      reasons.append(f"RSI={last['rsi']:.1f}")
    if macd_turning: reasons.append("MACD+")

    return trend_ok and at_lower_bb and rsi_dip and macd_turning, reasons

def sell_signal(df, trend_ok):
    """
    Exit on ANY of:
      [Option 1] Price back at BB midline (mean reversion complete)
      [Option 1] RSI > RSI_SELL (overbought)
      [Option 1] MACD histogram turns negative
      [Option 2] 1H trend reversed (EMA20 < EMA50)
    """
    last = df.iloc[-1]
    prev = df.iloc[-2]

    at_midline    = safe_float(last["bb_pct"]) >= BB_EXIT
    rsi_high      = last["rsi"] > RSI_SELL
    macd_bearish  = prev["macd_hist"] > 0 and last["macd_hist"] < 0
    trend_broken  = not trend_ok

    return at_midline or rsi_high or macd_bearish or trend_broken

# ================================================================
#  MAIN LOOP
# ================================================================
def run_bot():
    init_trade_log()

    log("=" * 68)
    log("  DUAL-TIMEFRAME BOLLINGER BAND BOT — Bybit TESTNET")
    log(f"  Pairs      : {', '.join(SYMBOLS)}")
    log(f"  Timeframes : {INTERVAL_1H}m trend  +  {INTERVAL_15M}m entry")
    log(f"  Trend      : 1H EMA{EMA_1H_FAST} > EMA{EMA_1H_SLOW}")
    log(f"  Entry      : BB%B < {BB_ENTRY}  +  RSI < {RSI_BUY}  +  MACD turning +")
    log(f"  Exit       : BB%B > {BB_EXIT}  OR  RSI > {RSI_SELL}  OR  MACD -  OR  Trend broken")
    log(f"  Risk/Trade : {RISK_PCT*100:.0f}%  |  SL: {STOP_LOSS_PCT*100:.0f}%  |  TP: {TAKE_PROFIT_PCT*100:.0f}%  |  Trail: {TRAIL_STOP_PCT*100:.1f}%")
    log(f"  Max Pos    : {MAX_POSITIONS}  |  Daily Loss Limit: {DAILY_LOSS_LIMIT*100:.0f}%")
    log("=" * 68)

    entry_prices  = {s: 0.0 for s in SYMBOLS}
    highest_price = {s: 0.0 for s in SYMBOLS}
    entry_usdt    = {s: 0.0 for s in SYMBOLS}
    bb_mid_at_entry = {s: 0.0 for s in SYMBOLS}   # dynamic TP target

    trade_count  = 0
    win_count    = 0
    total_pnl    = 0.0
    daily_pnl    = 0.0
    starting_bal = get_balance()
    daily_limit  = starting_bal * DAILY_LOSS_LIMIT
    error_streak = 0

    log(f"  Starting balance: ${starting_bal:.2f} USDT")
    log("=" * 68)

    while True:
        try:
            usdt_bal       = get_balance()
            open_positions = sum(1 for s in SYMBOLS if get_position_qty(s) > 0.0001)

            # ── Circuit Breaker ───────────────────────────────────────
            if daily_pnl <= -daily_limit:
                log(f"CIRCUIT BREAKER: Daily loss ${daily_pnl:.2f} hit limit. Pausing 1 hour.", "WARN")
                time.sleep(3600)
                daily_pnl   = 0.0
                daily_limit = get_balance() * DAILY_LOSS_LIMIT
                continue

            wr = int(win_count / trade_count * 100) if trade_count else 0
            log(f"Balance: ${usdt_bal:.2f}  |  Open: {open_positions}/{MAX_POSITIONS}  |  "
                f"Trades: {trade_count}  |  Win: {wr}%  |  PnL: {'+'if total_pnl>=0 else ''}${total_pnl:.2f}")
            log("─" * 68)

            for symbol in SYMBOLS:
                try:
                    # Fetch both timeframes
                    trend_ok, ema_f, ema_s = get_1h_trend(symbol)
                    df    = add_15m_indicators(get_candles(symbol, INTERVAL_15M))
                    price = get_price(symbol)
                    qty   = get_position_qty(symbol)
                    in_pos = qty > 0.0001
                    last   = df.iloc[-1]

                    if in_pos and price > highest_price[symbol]:
                        highest_price[symbol] = price

                    # Status line
                    trend_icon = "▲" if trend_ok else "▼"
                    macd_icon  = "+" if last["macd_hist"] > 0 else "-"
                    bb_pct_val = safe_float(last["bb_pct"])
                    log(f"  {symbol:<12} ${price:<12,.4f} "
                        f"RSI={last['rsi']:5.1f}  "
                        f"BB={bb_pct_val:.2f}  "
                        f"MACD={macd_icon}  "
                        f"1H={trend_icon}  "
                        f"[{'IN TRADE' if in_pos else 'watching'}]")

                    # ── MANAGE OPEN POSITION ──────────────────────────
                    if in_pos:
                        ep      = entry_prices[symbol]
                        high    = highest_price[symbol]
                        bb_tp   = bb_mid_at_entry[symbol]
                        pnl_pct = (price - ep) / ep if ep > 0 else 0

                        def close_trade(reason):
                            nonlocal trade_count, win_count, total_pnl, daily_pnl
                            if place_sell(symbol, qty, reason=reason):
                                pnl = (price - ep) * qty
                                total_pnl += pnl
                                daily_pnl += pnl
                                trade_count += 1
                                if pnl > 0:
                                    win_count += 1
                                sign = "+" if pnl >= 0 else ""
                                log(f"    {reason.upper()} | P&L: {sign}${pnl:.2f} ({pnl_pct*100:.1f}%)", "SELL")
                                log_trade(symbol, "SELL", price, entry_usdt[symbol], ep, pnl, reason)
                                entry_prices[symbol] = highest_price[symbol] = entry_usdt[symbol] = bb_mid_at_entry[symbol] = 0.0

                        # Priority 1: Hard Stop Loss
                        if pnl_pct <= -STOP_LOSS_PCT:
                            log(f"    STOP LOSS {pnl_pct*100:.1f}% @ ${price:.4f}", "WARN")
                            close_trade("stop_loss")

                        # Priority 2: Hard Take Profit cap
                        elif pnl_pct >= TAKE_PROFIT_PCT:
                            log(f"    TAKE PROFIT +{pnl_pct*100:.1f}% @ ${price:.4f}", "SELL")
                            close_trade("take_profit")

                        # Priority 3: Trailing Stop (after +1% gain)
                        elif pnl_pct > 0.01 and (high - price) / high >= TRAIL_STOP_PCT:
                            log(f"    TRAIL STOP | High=${high:.4f} → Now=${price:.4f}", "WARN")
                            close_trade("trail_stop")

                        # Priority 4: BB midline hit (mean reversion target)
                        elif bb_tp > 0 and price >= bb_tp:
                            log(f"    BB MIDLINE TARGET hit @ ${price:.4f} (target=${bb_tp:.4f})", "SELL")
                            close_trade("bb_midline")

                        # Priority 5: Indicator sell signal
                        elif sell_signal(df, trend_ok):
                            log(f"    SELL SIGNAL | RSI={last['rsi']:.1f} BB={bb_pct_val:.2f} Trend={'OK' if trend_ok else 'BROKEN'}", "SELL")
                            close_trade("signal")

                    # ── LOOK FOR ENTRY ────────────────────────────────
                    elif open_positions < MAX_POSITIONS:
                        signal_ok, reasons = buy_signal(df, trend_ok)
                        if signal_ok:
                            size = calc_position_size(usdt_bal)
                            if size < MIN_TRADE_USDT:
                                log(f"    Skipping: size ${size:.2f} below minimum", "WARN")
                            else:
                                log(f"    BUY SIGNAL [{' | '.join(reasons)}] | Size=${size:.2f}", "BUY")
                                if place_buy(symbol, size):
                                    entry_prices[symbol]    = price
                                    highest_price[symbol]   = price
                                    entry_usdt[symbol]      = size
                                    bb_mid_at_entry[symbol] = safe_float(last["bb_mid"])
                                    open_positions         += 1
                                    usdt_bal               -= size
                                    log_trade(symbol, "BUY", price, size, price, 0, "signal")
                                    log(f"    Entry=${price:.4f} | SL=${price*(1-STOP_LOSS_PCT):.4f} | "
                                        f"TP target=${safe_float(last['bb_mid']):.4f} | "
                                        f"Max TP=${price*(1+TAKE_PROFIT_PCT):.4f}")

                except Exception as e:
                    log(f"  {symbol}: {e}", "ERR")
                    continue

            log("─" * 68)
            error_streak = 0

        except KeyboardInterrupt:
            log("\nBot stopped.")
            log(f"Final: Trades={trade_count} | Wins={win_count} | PnL=${total_pnl:.2f}")
            break
        except Exception as e:
            error_streak += 1
            wait = min(30 * (2 ** error_streak), 300)
            log(f"Error (streak={error_streak}): {e} — retry in {wait}s", "ERR")
            time.sleep(wait)
            continue

        time.sleep(SLEEP_SEC)

if __name__ == "__main__":
    run_bot()
