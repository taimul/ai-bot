"""
================================================================
  PROFESSIONAL MULTI-PAIR CRYPTO TRADING BOT
  Platform  : Bybit TESTNET (Spot)
  Strategy  : RSI + MACD + EMA Trend Filter
  Features  :
    - Multi-indicator entry confirmation
    - Risk-based position sizing (% of account)
    - Hard Stop Loss + Take Profit
    - Trailing Stop Loss
    - Max concurrent positions cap
    - Daily loss circuit breaker
    - Volume confirmation filter
    - Trade log saved to CSV
    - Per-symbol win/loss statistics
    - Exponential backoff on errors
================================================================
"""

import time
import csv
import os
import pandas as pd
from datetime import datetime, date
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

INTERVAL = "15"          # 15-minute candles

# -- Risk Management --
RISK_PCT         = 0.02  # Risk 2% of account balance per trade
STOP_LOSS_PCT    = 0.03  # Hard stop loss: exit if price falls 3% from entry
TAKE_PROFIT_PCT  = 0.06  # Take profit: exit if price rises 6% from entry (2:1 R:R)
TRAIL_STOP_PCT   = 0.015 # Trailing stop: sell if price drops 1.5% from highest since entry
MAX_POSITIONS    = 4     # Maximum open trades at once
DAILY_LOSS_LIMIT = 0.05  # Circuit breaker: stop trading if daily loss > 5% of starting balance
MIN_TRADE_USDT   = 5     # Minimum order size in USDT

# -- Indicators --
RSI_PERIOD   = 14
RSI_BUY      = 45        # Buy zone: RSI below this
RSI_SELL     = 60        # Sell zone: RSI above this
EMA_TREND    = 200       # Macro trend filter: only buy above this EMA
MA_FAST      = 9
MA_SLOW      = 21
MACD_FAST    = 12
MACD_SLOW    = 26
MACD_SIG     = 9
VOL_MULT     = 1.2       # Volume must be 1.2x the 20-bar average to confirm entry

SLEEP_SEC    = 60
TRADE_LOG    = "trades.csv"
# ================================================================

session = HTTP(testnet=True, api_key=API_KEY, api_secret=API_SECRET)

# ================================================================
#  LOGGING
# ================================================================
def log(msg, level="INFO"):
    prefix = {"INFO": "   ", "BUY": ">>", "SELL": "<<", "WARN": "!!", "ERR": "XX"}.get(level, "  ")
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {prefix} {msg}")

def init_trade_log():
    if not os.path.exists(TRADE_LOG):
        with open(TRADE_LOG, "w", newline="") as f:
            csv.writer(f).writerow([
                "datetime", "symbol", "side", "price", "qty_usdt",
                "entry_price", "pnl_usdt", "pnl_pct", "reason"
            ])

def log_trade(symbol, side, price, qty_usdt, entry_price=0, pnl=0, reason=""):
    pnl_pct = (pnl / (entry_price * qty_usdt / price)) * 100 if entry_price > 0 and price > 0 else 0
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
        return v if v == v else default  # NaN check
    except (ValueError, TypeError):
        return default

def get_candles(symbol, limit=250):
    resp = session.get_kline(category="spot", symbol=symbol, interval=INTERVAL, limit=limit)
    raw  = resp["result"]["list"]
    df   = pd.DataFrame(raw, columns=["timestamp","open","high","low","close","volume","turnover"])
    df   = df[::-1].reset_index(drop=True)
    for col in ["open","high","low","close","volume"]:
        df[col] = df[col].astype(float)
    return df

def add_indicators(df):
    close = df["close"]
    vol   = df["volume"]

    # RSI
    delta     = close.diff()
    gain      = delta.clip(lower=0).rolling(RSI_PERIOD).mean()
    loss      = (-delta.clip(upper=0)).rolling(RSI_PERIOD).mean()
    df["rsi"] = 100 - (100 / (1 + gain / loss))

    # Moving Averages
    df["ma_fast"] = close.rolling(MA_FAST).mean()
    df["ma_slow"] = close.rolling(MA_SLOW).mean()
    df["ema200"]  = close.ewm(span=EMA_TREND, adjust=False).mean()

    # MACD
    ema_fast      = close.ewm(span=MACD_FAST, adjust=False).mean()
    ema_slow      = close.ewm(span=MACD_SLOW, adjust=False).mean()
    df["macd"]    = ema_fast - ema_slow
    df["macd_sig"]= df["macd"].ewm(span=MACD_SIG, adjust=False).mean()
    df["macd_hist"]= df["macd"] - df["macd_sig"]

    # Volume ratio vs 20-bar average
    df["vol_avg"]   = vol.rolling(20).mean()
    df["vol_ratio"] = vol / df["vol_avg"]

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
    """Risk-based sizing: risk RISK_PCT of balance, SL is STOP_LOSS_PCT away."""
    risk_amt = balance * RISK_PCT
    size     = risk_amt / STOP_LOSS_PCT
    max_size = balance * 0.20      # never more than 20% of balance in one trade
    return round(min(size, max_size), 2)

# ================================================================
#  ORDER EXECUTION
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
        log(f"SELL {symbol} | {qty_str} {symbol.replace('USDT','')} | Reason:{reason} | ID:{resp['result']['orderId']}", "SELL")
        return True
    except Exception as e:
        log(f"SELL FAILED {symbol}: {e}", "ERR")
        return False

# ================================================================
#  SIGNALS
# ================================================================
def buy_signal(df):
    """
    All 4 conditions must be true:
      1. Price above EMA 200 (macro uptrend)
      2. RSI in buy zone (< RSI_BUY)
      3. MACD histogram turning positive (bullish momentum)
      4. Volume spike confirms move
    """
    last = df.iloc[-1]
    prev = df.iloc[-2]

    above_trend   = last["close"] > last["ema200"]
    rsi_ok        = last["rsi"] < RSI_BUY
    macd_bullish  = prev["macd_hist"] < 0 and last["macd_hist"] > 0   # MACD crossover
    vol_confirmed = last["vol_ratio"] >= VOL_MULT

    return above_trend and rsi_ok and macd_bullish and vol_confirmed

def sell_signal(df):
    """
    Any of:
      1. RSI overbought (> RSI_SELL)
      2. MACD turns bearish (histogram crosses below 0)
      3. MA fast drops below slow (trend break)
    """
    last = df.iloc[-1]
    prev = df.iloc[-2]

    rsi_high      = last["rsi"] > RSI_SELL
    macd_bearish  = prev["macd_hist"] > 0 and last["macd_hist"] < 0
    ma_crossunder = prev["ma_fast"] >= prev["ma_slow"] and last["ma_fast"] < last["ma_slow"]

    return rsi_high or macd_bearish or ma_crossunder

# ================================================================
#  MAIN LOOP
# ================================================================
def run_bot():
    init_trade_log()

    log("=" * 65)
    log("  PROFESSIONAL MULTI-PAIR BOT — Bybit TESTNET")
    log(f"  Pairs     : {', '.join(SYMBOLS)}")
    log(f"  Interval  : {INTERVAL}m  |  Max Positions: {MAX_POSITIONS}")
    log(f"  Risk/Trade: {RISK_PCT*100:.0f}%  |  SL: {STOP_LOSS_PCT*100:.0f}%  |  TP: {TAKE_PROFIT_PCT*100:.0f}%  |  Trail: {TRAIL_STOP_PCT*100:.1f}%")
    log(f"  Daily Loss Limit: {DAILY_LOSS_LIMIT*100:.0f}% of starting balance")
    log(f"  Entry: EMA{EMA_TREND} trend + RSI<{RSI_BUY} + MACD cross + Volume x{VOL_MULT}")
    log("=" * 65)

    # Per-symbol state
    entry_prices  = {s: 0.0 for s in SYMBOLS}
    highest_price = {s: 0.0 for s in SYMBOLS}   # for trailing stop
    entry_usdt    = {s: 0.0 for s in SYMBOLS}    # usdt spent on entry

    # Session stats
    trade_count   = 0
    win_count     = 0
    total_pnl     = 0.0
    daily_pnl     = 0.0
    starting_bal  = get_balance()
    daily_limit   = starting_bal * DAILY_LOSS_LIMIT
    error_streak  = 0

    log(f"  Starting balance: ${starting_bal:.2f} USDT", "INFO")
    log("=" * 65)

    while True:
        try:
            usdt_bal     = get_balance()
            open_positions = sum(1 for s in SYMBOLS if get_position_qty(s) > 0.0001)

            # ── Daily Loss Circuit Breaker ────────────────────────────
            if daily_pnl <= -daily_limit:
                log(f"CIRCUIT BREAKER: Daily loss ${daily_pnl:.2f} exceeded limit ${-daily_limit:.2f}. Pausing 1h.", "WARN")
                time.sleep(3600)
                daily_pnl    = 0.0
                daily_limit  = get_balance() * DAILY_LOSS_LIMIT
                continue

            log(f"Balance: ${usdt_bal:.2f} USDT | Open: {open_positions}/{MAX_POSITIONS} | "
                f"Session PnL: {'+'if total_pnl>=0 else ''}${total_pnl:.2f} | "
                f"Day PnL: {'+'if daily_pnl>=0 else ''}${daily_pnl:.2f}")
            log("─" * 65)

            for symbol in SYMBOLS:
                try:
                    df       = add_indicators(get_candles(symbol))
                    price    = get_price(symbol)
                    coin_qty = get_position_qty(symbol)
                    in_pos   = coin_qty > 0.0001
                    last     = df.iloc[-1]

                    # Update trailing stop tracker
                    if in_pos and price > highest_price[symbol]:
                        highest_price[symbol] = price

                    # Status line
                    trend_str = "^" if last["close"] > last["ema200"] else "v"
                    macd_str  = "+" if last["macd_hist"] > 0 else "-"
                    log(f"  {symbol:<12} ${price:<12,.4f} "
                        f"RSI={last['rsi']:5.1f}  "
                        f"MACD={macd_str}  "
                        f"Trend={trend_str}  "
                        f"Vol={last['vol_ratio']:.1f}x  "
                        f"[{'IN TRADE' if in_pos else 'watching'}]")

                    # ── MANAGE OPEN POSITION ─────────────────────────
                    if in_pos:
                        ep         = entry_prices[symbol]
                        high       = highest_price[symbol]
                        pnl_pct    = (price - ep) / ep if ep > 0 else 0

                        # Hard Stop Loss
                        if pnl_pct <= -STOP_LOSS_PCT:
                            log(f"    STOP LOSS hit {pnl_pct*100:.1f}%  @ ${price:.4f}", "WARN")
                            if place_sell(symbol, coin_qty, reason="stop_loss"):
                                pnl = (price - ep) * coin_qty
                                total_pnl += pnl; daily_pnl += pnl; trade_count += 1
                                log_trade(symbol, "SELL", price, entry_usdt[symbol], ep, pnl, "stop_loss")
                                entry_prices[symbol] = highest_price[symbol] = entry_usdt[symbol] = 0.0
                            continue

                        # Take Profit
                        if pnl_pct >= TAKE_PROFIT_PCT:
                            log(f"    TAKE PROFIT hit +{pnl_pct*100:.1f}% @ ${price:.4f}", "SELL")
                            if place_sell(symbol, coin_qty, reason="take_profit"):
                                pnl = (price - ep) * coin_qty
                                total_pnl += pnl; daily_pnl += pnl; trade_count += 1; win_count += 1
                                log_trade(symbol, "SELL", price, entry_usdt[symbol], ep, pnl, "take_profit")
                                entry_prices[symbol] = highest_price[symbol] = entry_usdt[symbol] = 0.0
                            continue

                        # Trailing Stop (only active after 1% gain)
                        trail_drop = (high - price) / high if high > 0 else 0
                        if pnl_pct > 0.01 and trail_drop >= TRAIL_STOP_PCT:
                            log(f"    TRAILING STOP hit (high=${high:.4f}, now=${price:.4f}, drop={trail_drop*100:.1f}%)", "WARN")
                            if place_sell(symbol, coin_qty, reason="trail_stop"):
                                pnl = (price - ep) * coin_qty
                                total_pnl += pnl; daily_pnl += pnl; trade_count += 1
                                if pnl > 0: win_count += 1
                                log_trade(symbol, "SELL", price, entry_usdt[symbol], ep, pnl, "trail_stop")
                                entry_prices[symbol] = highest_price[symbol] = entry_usdt[symbol] = 0.0
                            continue

                        # Indicator Sell Signal
                        if sell_signal(df):
                            log(f"    SELL SIGNAL RSI={last['rsi']:.1f}", "SELL")
                            if place_sell(symbol, coin_qty, reason="signal"):
                                pnl = (price - ep) * coin_qty
                                total_pnl += pnl; daily_pnl += pnl; trade_count += 1
                                if pnl > 0: win_count += 1
                                log_trade(symbol, "SELL", price, entry_usdt[symbol], ep, pnl, "signal")
                                entry_prices[symbol] = highest_price[symbol] = entry_usdt[symbol] = 0.0

                    # ── LOOK FOR ENTRY ───────────────────────────────
                    elif open_positions < MAX_POSITIONS and buy_signal(df):
                        size = calc_position_size(usdt_bal)
                        if size < MIN_TRADE_USDT:
                            log(f"    Skipping: position size ${size:.2f} below minimum", "WARN")
                        else:
                            log(f"    BUY SIGNAL | RSI={last['rsi']:.1f} MACD+ Vol={last['vol_ratio']:.1f}x | Size=${size:.2f}", "BUY")
                            if place_buy(symbol, size):
                                entry_prices[symbol]  = price
                                highest_price[symbol] = price
                                entry_usdt[symbol]    = size
                                open_positions       += 1
                                usdt_bal             -= size
                                log_trade(symbol, "BUY", price, size, price, 0, "signal")

                except Exception as e:
                    log(f"  {symbol}: {e}", "ERR")
                    continue

            # Stats summary
            wr = int(win_count / trade_count * 100) if trade_count else 0
            log("─" * 65)
            log(f"  Trades: {trade_count} | Wins: {win_count} ({wr}%) | "
                f"Session PnL: {'+'if total_pnl>=0 else ''}${total_pnl:.2f}")
            log("─" * 65)

            error_streak = 0  # reset on successful cycle

        except KeyboardInterrupt:
            log("\nBot stopped by user.")
            log(f"Final: Trades={trade_count} | Wins={win_count} | PnL=${total_pnl:.2f}")
            break
        except Exception as e:
            error_streak += 1
            wait = min(30 * (2 ** error_streak), 300)  # exponential backoff, max 5 min
            log(f"Error (streak={error_streak}): {e} — retrying in {wait}s", "ERR")
            time.sleep(wait)
            continue

        time.sleep(SLEEP_SEC)

if __name__ == "__main__":
    run_bot()
