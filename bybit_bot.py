"""
========================================
  RSI + Moving Average Crypto Trading Bot
  Platform : Bybit TESTNET (demo money)
  Author   : Your Bot
  Strategy : Buy  -> RSI < 40 AND Fast MA crosses above Slow MA
             Sell -> RSI > 60 OR  Fast MA crosses below Slow MA
========================================

SETUP INSTRUCTIONS
------------------
1. Create a FREE Bybit Testnet account:
   https://testnet.bybit.com  (no ID needed, get free demo USDT)

2. Generate API keys on Testnet:
   Dashboard -> API Management -> Create New Key
   Enable: Read + Trade permissions
   Copy your API_KEY and API_SECRET below

3. Install required library:
   pip install pybit pandas

4. Run the bot:
   python bybit_rsi_ma_bot.py

"""

import time
import pandas as pd
from datetime import datetime
from pybit.unified_trading import HTTP

# ============================================================
#  CONFIG — Fill these in after creating your Testnet account
# ============================================================
API_KEY    = "YndDHQr6Mpx2i3fElS"
API_SECRET = "oGjioa9ZVih7rw1b4dBVMJJ2UkGQZ4LrF5cR"

SYMBOL       = "BTCUSDT"      # Trading pair
INTERVAL     = "15"           # Candle interval in minutes: 1,3,5,15,30,60
TRADE_USDT   = 50             # How much USDT to use per trade (from your demo balance)
RSI_PERIOD   = 14             # RSI lookback period
MA_FAST      = 9              # Fast moving average period
MA_SLOW      = 21             # Slow moving average period
RSI_BUY      = 40             # Buy when RSI is below this
RSI_SELL     = 60             # Sell when RSI is above this
SLEEP_SEC    = 60             # How often to check (seconds)
# ============================================================

# Connect to Bybit Testnet
session = HTTP(
    testnet=True,
    api_key=API_KEY,
    api_secret=API_SECRET,
)

def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}")

def get_candles(symbol, interval, limit=100):
    """Fetch recent OHLCV candles from Bybit Testnet."""
    resp = session.get_kline(
        category="spot",
        symbol=symbol,
        interval=interval,
        limit=limit,
    )
    raw = resp["result"]["list"]
    df = pd.DataFrame(raw, columns=["timestamp","open","high","low","close","volume","turnover"])
    df = df[::-1].reset_index(drop=True)   # oldest first
    df["close"] = df["close"].astype(float)
    return df

def calc_rsi(series, period=14):
    """Calculate RSI indicator."""
    delta = series.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss
    return 100 - (100 / (1 + rs))

def calc_ma(series, period):
    """Calculate Simple Moving Average."""
    return series.rolling(period).mean()

def get_indicators(symbol, interval):
    """Fetch candles and compute all indicators."""
    df = get_candles(symbol, interval, limit=max(RSI_PERIOD, MA_SLOW) + 20)
    df["rsi"]     = calc_rsi(df["close"], RSI_PERIOD)
    df["ma_fast"] = calc_ma(df["close"],  MA_FAST)
    df["ma_slow"] = calc_ma(df["close"],  MA_SLOW)
    return df

def get_balance(coin="USDT"):
    """Get available balance for a coin."""
    resp = session.get_wallet_balance(accountType="UNIFIED", coin=coin)
    coins = resp["result"]["list"][0]["coin"]
    for c in coins:
        if c["coin"] == coin:
            return float(c["availableToWithdraw"])
    return 0.0

def get_position(symbol):
    """Check if we currently hold a position (non-USDT balance)."""
    base_coin = symbol.replace("USDT", "")
    resp = session.get_wallet_balance(accountType="UNIFIED", coin=base_coin)
    try:
        coins = resp["result"]["list"][0]["coin"]
        for c in coins:
            if c["coin"] == base_coin:
                qty = float(c["availableToWithdraw"])
                return qty
    except Exception:
        pass
    return 0.0

def get_current_price(symbol):
    """Get latest market price."""
    resp = session.get_tickers(category="spot", symbol=symbol)
    return float(resp["result"]["list"][0]["lastPrice"])

def place_buy(symbol, usdt_amount):
    """Place a market BUY order using USDT amount."""
    try:
        resp = session.place_order(
            category="spot",
            symbol=symbol,
            side="Buy",
            orderType="Market",
            qty=str(usdt_amount),       # For spot market buy, qty is in quote (USDT)
            marketUnit="quoteCoin",
        )
        log(f"BUY ORDER PLACED | ${usdt_amount} USDT -> {symbol}")
        log(f"  Order ID: {resp['result']['orderId']}")
        return True
    except Exception as e:
        log(f"BUY ORDER FAILED: {e}")
        return False

def place_sell(symbol, coin_qty):
    """Place a market SELL order for the full coin quantity."""
    try:
        # Round down to avoid precision errors
        coin_qty_str = f"{coin_qty:.6f}"
        resp = session.place_order(
            category="spot",
            symbol=symbol,
            side="Sell",
            orderType="Market",
            qty=coin_qty_str,
        )
        log(f"SELL ORDER PLACED | {coin_qty_str} {symbol.replace('USDT','')} -> USDT")
        log(f"  Order ID: {resp['result']['orderId']}")
        return True
    except Exception as e:
        log(f"SELL ORDER FAILED: {e}")
        return False

def should_buy(df):
    """
    Buy signal:
      - RSI crossed below RSI_BUY threshold
      - Fast MA is above Slow MA (uptrend confirmed)
    """
    last  = df.iloc[-1]
    prev  = df.iloc[-2]
    rsi_cross  = prev["rsi"] >= RSI_BUY and last["rsi"] < RSI_BUY
    ma_uptrend = last["ma_fast"] > last["ma_slow"]
    return rsi_cross and ma_uptrend

def should_sell(df):
    """
    Sell signal:
      - RSI crossed above RSI_SELL threshold
      - OR Fast MA dropped below Slow MA (trend reversal)
    """
    last = df.iloc[-1]
    prev = df.iloc[-2]
    rsi_cross     = prev["rsi"] <= RSI_SELL and last["rsi"] > RSI_SELL
    ma_crossunder = prev["ma_fast"] >= prev["ma_slow"] and last["ma_fast"] < last["ma_slow"]
    return rsi_cross or ma_crossunder

def print_status(df, in_position, price):
    last = df.iloc[-1]
    log("─" * 55)
    log(f"  Pair     : {SYMBOL}  |  Price: ${price:,.2f}")
    log(f"  RSI      : {last['rsi']:.1f}  (buy<{RSI_BUY}, sell>{RSI_SELL})")
    log(f"  Fast MA  : {last['ma_fast']:.2f}")
    log(f"  Slow MA  : {last['ma_slow']:.2f}")
    log(f"  Position : {'IN TRADE' if in_position else 'WAITING'}")
    log("─" * 55)

# ================================================================
#  MAIN LOOP
# ================================================================
def run_bot():
    log("=" * 55)
    log("  RSI + MA Trading Bot — Bybit TESTNET")
    log(f"  Pair: {SYMBOL} | Interval: {INTERVAL}m | Trade: ${TRADE_USDT} USDT")
    log("=" * 55)

    if API_KEY == "YOUR_TESTNET_API_KEY":
        log("ERROR: Please set your API_KEY and API_SECRET first!")
        log("  1. Go to https://testnet.bybit.com")
        log("  2. Create account -> API Management -> Create Key")
        log("  3. Paste the keys at the top of this file")
        return

    trade_count  = 0
    win_count    = 0
    entry_price  = 0.0
    total_pnl    = 0.0

    while True:
        try:
            df           = get_indicators(SYMBOL, INTERVAL)
            price        = get_current_price(SYMBOL)
            coin_qty     = get_position(SYMBOL)
            in_position  = coin_qty > 0.0001

            print_status(df, in_position, price)

            # ── BUY ──────────────────────────────────────────
            if not in_position and should_buy(df):
                usdt_bal = get_balance("USDT")
                use_usdt = min(TRADE_USDT, usdt_bal)
                if use_usdt < 5:
                    log("Not enough USDT balance to trade.")
                else:
                    log(f"BUY SIGNAL! RSI={df.iloc[-1]['rsi']:.1f}, FastMA > SlowMA")
                    if place_buy(SYMBOL, use_usdt):
                        entry_price = price
                        trade_count += 1

            # ── SELL ─────────────────────────────────────────
            elif in_position and should_sell(df):
                log(f"SELL SIGNAL! RSI={df.iloc[-1]['rsi']:.1f}")
                if place_sell(SYMBOL, coin_qty):
                    pnl = (price - entry_price) * coin_qty
                    total_pnl += pnl
                    if pnl > 0:
                        win_count += 1
                    log(f"  Trade P&L : {'+'if pnl>=0 else ''}${pnl:.2f}")
                    log(f"  Total P&L : {'+'if total_pnl>=0 else ''}${total_pnl:.2f}")
                    log(f"  Win Rate  : {win_count}/{trade_count} ({int(win_count/trade_count*100) if trade_count else 0}%)")
            else:
                log("No signal. Waiting for next candle...")

        except KeyboardInterrupt:
            log("\nBot stopped by user.")
            log(f"Final Stats -> Trades: {trade_count} | PnL: ${total_pnl:.2f} | Wins: {win_count}")
            break
        except Exception as e:
            log(f"Error: {e}")
            log("Retrying in 30s...")
            time.sleep(30)
            continue

        time.sleep(SLEEP_SEC)

if __name__ == "__main__":
    run_bot()
