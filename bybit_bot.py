"""
================================================================
  PROFESSIONAL MULTI-PAIR CRYPTO TRADING BOT
  Platform  : Bybit TESTNET (Spot)

  STRATEGY  : Dual-Timeframe Bollinger Band Mean Reversion (Scalp)
  ─────────────────────────────────────────────────────────────────
  TREND (15m): EMA20 > EMA50 → confirmed uptrend
  ENTRY  (5m): Price in lower BB zone (BB%B < 0.35)
               + RSI < 55  + MACD histogram positive
  EXIT   (5m): Price reaches BB midline  OR  RSI > 62
               OR  MACD turns bearish   OR  15m trend broken
  TARGET     : 3–10 signals per hour across 15 pairs

  PROTECTION (PC-OFF SAFE):
    - Stop Loss & Take Profit placed ON BYBIT SERVERS at entry
    - Trailing Stop managed by bot (best effort)
    - State saved to JSON — bot recovers open trades on restart
    - If bot restarts it re-attaches to existing open positions
================================================================
"""

import time
import csv
import os
import json
import math
import pandas as pd
from datetime import datetime
from pybit.unified_trading import HTTP

# ================================================================
#  CONFIG
# ================================================================
API_KEY    = "YndDHQr6Mpx2i3fElS"
API_SECRET = "oGjioa9ZVih7rw1b4dBVMJJ2UkGQZ4LrF5cR"

# Confirmed active pairs on Bybit Testnet (verified with real candle data)
# MAINNET: expand this list freely — all major pairs work on mainnet
SYMBOLS = [
    "ETHUSDT",   # ✓ active
    "SOLUSDT",   # ✓ active
    "XRPUSDT",   # ✓ active
    "ADAUSDT",   # ✓ active
    "TRXUSDT",   # ✓ active
]
# NOTE: BTCUSDT excluded from testnet — price is static, RSI always 0
# Add it back when switching to mainnet

INTERVAL_5M  = "5"    # Entry timeframe (scalp)
INTERVAL_15M = "15"   # Trend timeframe

# -- Risk Management --
RISK_PCT         = 0.02
STOP_LOSS_PCT    = 0.015  # Tighter SL for 5m scalp
TAKE_PROFIT_PCT  = 0.03   # Realistic TP on 5m
TRAIL_STOP_PCT   = 0.008  # Tight trailing stop
MAX_POSITIONS    = 6      # More concurrent trades
DAILY_LOSS_LIMIT = 0.05
MIN_TRADE_USDC   = 5

# -- Indicators --
RSI_PERIOD   = 14
RSI_BUY      = 55     # Relaxed — catches more dips
RSI_SELL     = 62
BB_PERIOD    = 20
BB_STD       = 2.0
BB_ENTRY     = 0.50   # Entry zone (bottom 50% of BB — middle and below)
BB_EXIT      = 0.55
MACD_FAST    = 12
MACD_SLOW    = 26
MACD_SIG     = 9
EMA_15M_FAST = 20     # 15m trend EMAs
EMA_15M_SLOW = 50

SLEEP_SEC  = 60
TRADE_LOG  = "trades.csv"
STATE_FILE = "bot_state.json"   # persists open positions across restarts
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
                "datetime","symbol","side","price","qty_usdt",
                "entry_price","pnl_usdt","pnl_pct","reason"
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
#  STATE PERSISTENCE  (survives PC restart)
# ================================================================
def save_state(entry_prices, highest_price, entry_usdt, bb_mid_at_entry,
               sl_prices, tp_prices, trade_count, win_count, total_pnl):
    """Save all open position data to disk so bot can recover after restart."""
    state = {
        "saved_at"       : datetime.now().isoformat(),
        "trade_count"    : trade_count,
        "win_count"      : win_count,
        "total_pnl"      : total_pnl,
        "positions"      : {}
    }
    for s in SYMBOLS:
        if entry_prices[s] > 0:
            state["positions"][s] = {
                "entry_price"    : entry_prices[s],
                "highest_price"  : highest_price[s],
                "entry_usdt"     : entry_usdt[s],
                "bb_mid_at_entry": bb_mid_at_entry[s],
                "sl_price"       : sl_prices[s],
                "tp_price"       : tp_prices[s],
            }
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def load_state():
    """Load previously saved state on restart."""
    if not os.path.exists(STATE_FILE):
        return None
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
        log(f"Recovered state from {state['saved_at']}", "WARN")
        return state
    except Exception as e:
        log(f"Could not load state: {e}", "WARN")
        return None

def clear_state():
    if os.path.exists(STATE_FILE):
        os.remove(STATE_FILE)

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

def get_15m_trend(symbol):
    """
    Trend check on 15m: EMA20 > EMA50 = uptrend.
    Fetches 150 candles so EMA50 is fully warmed up (needs 50 to converge).
    Returns (trend_ok, ema_fast, ema_slow) or (None, 0, 0) if insufficient data.
    """
    df = get_candles(symbol, INTERVAL_15M, limit=150)
    if len(df) < EMA_15M_SLOW + 5:
        return None, 0.0, 0.0
    df["ema_fast"] = df["close"].ewm(span=EMA_15M_FAST, adjust=False).mean()
    df["ema_slow"] = df["close"].ewm(span=EMA_15M_SLOW, adjust=False).mean()
    last = df.iloc[-1]
    return last["ema_fast"] > last["ema_slow"], last["ema_fast"], last["ema_slow"]

def add_15m_indicators(df):
    close = df["close"]
    vol   = df["volume"]

    delta     = close.diff()
    gain      = delta.clip(lower=0).rolling(RSI_PERIOD).mean()
    loss      = (-delta.clip(upper=0)).rolling(RSI_PERIOD).mean()
    df["rsi"] = 100 - (100 / (1 + gain / loss))

    df["bb_mid"]   = close.rolling(BB_PERIOD).mean()
    df["bb_std"]   = close.rolling(BB_PERIOD).std()
    df["bb_upper"] = df["bb_mid"] + BB_STD * df["bb_std"]
    df["bb_lower"] = df["bb_mid"] - BB_STD * df["bb_std"]
    bb_range       = df["bb_upper"] - df["bb_lower"]
    df["bb_pct"]   = (close - df["bb_lower"]) / bb_range.replace(0, float("nan"))

    ema_f          = close.ewm(span=MACD_FAST, adjust=False).mean()
    ema_s          = close.ewm(span=MACD_SLOW, adjust=False).mean()
    df["macd"]     = ema_f - ema_s
    df["macd_sig"] = df["macd"].ewm(span=MACD_SIG, adjust=False).mean()
    df["macd_hist"]= df["macd"] - df["macd_sig"]
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
    size = (balance * RISK_PCT) / STOP_LOSS_PCT
    return round(min(size, balance * 0.20), 2)

def round_price(price):
    """Round price to correct decimal places based on price magnitude."""
    if price >= 100:   return round(price, 2)   # SOL $93, ETH $2169, BTC $89k
    if price >= 1:     return round(price, 4)   # XRP $1.56, TRX $0.30
    return             round(price, 6)          # ADA $0.265, DOGE $0.10

# ================================================================
#  ORDERS  — SL/TP attached to buy order (lives on Bybit servers)
# ================================================================
def place_buy(symbol, usdt_amount, sl_price, tp_price):
    """
    Places a market buy with Stop Loss and Take Profit set directly
    on Bybit's servers. These will execute even if this bot is offline.
    """
    try:
        resp = session.place_order(
            category    = "spot",
            symbol      = symbol,
            side        = "Buy",
            orderType   = "Market",
            qty         = str(usdt_amount),
            marketUnit  = "quoteCoin",
            stopLoss    = str(round_price(sl_price)),   # ← ON BYBIT SERVERS
            takeProfit  = str(round_price(tp_price)),   # ← ON BYBIT SERVERS
            slOrderType = "Market",
            tpOrderType = "Market",
            tpTriggerBy = "LastPrice",
            slTriggerBy = "LastPrice",
        )
        log(f"BUY  {symbol} | ${usdt_amount:.2f} USDT | "
            f"SL=${round_price(sl_price)} | TP=${round_price(tp_price)} | "
            f"ID:{resp['result']['orderId']}", "BUY")
        return True
    except Exception as e:
        log(f"BUY FAILED {symbol}: {e}", "ERR")
        return False

def update_sl_on_exchange(symbol, new_sl_price):
    """
    Update the stop loss on Bybit when trailing stop tightens it.
    This keeps the trailing stop protection alive even if bot restarts.
    """
    try:
        session.set_trading_stop(
            category    = "spot",
            symbol      = symbol,
            stopLoss    = str(round_price(new_sl_price)),
            slOrderType = "Market",
            slTriggerBy = "LastPrice",
        )
        log(f"  Trail SL updated → ${round_price(new_sl_price)}", "INFO")
    except Exception as e:
        log(f"  Trail SL update failed {symbol}: {e}", "WARN")

def place_sell(symbol, coin_qty, reason="signal"):
    try:
        qty_str = f"{coin_qty:.6f}"
        resp = session.place_order(
            category="spot", symbol=symbol, side="Sell",
            orderType="Market", qty=qty_str,
        )
        log(f"SELL {symbol} | {qty_str} | [{reason}] | ID:{resp['result']['orderId']}", "SELL")
        return True
    except Exception as e:
        log(f"SELL FAILED {symbol}: {e}", "ERR")
        return False

# ================================================================
#  SIGNALS
# ================================================================
def buy_signal(df, trend_ok):
    """
    Entry on 5m when ALL true:
      - 15m EMA20 > EMA50 (uptrend)
      - Price in lower 35% of Bollinger Band (dip zone)
      - RSI < 55 (not overbought)
      - MACD histogram > 0 (bullish momentum active)
    """
    last = df.iloc[-1]

    at_lower_bb  = safe_float(last["bb_pct"]) < BB_ENTRY
    rsi_dip      = last["rsi"] < RSI_BUY
    macd_bullish = last["macd_hist"] > 0   # histogram positive (not crossover)

    reasons = []
    if trend_ok:     reasons.append("15m-UP")
    if at_lower_bb:  reasons.append(f"BB={last['bb_pct']:.2f}")
    if rsi_dip:      reasons.append(f"RSI={last['rsi']:.1f}")
    if macd_bullish: reasons.append("MACD+")

    return trend_ok and at_lower_bb and rsi_dip and macd_bullish, reasons

def sell_signal(df, trend_ok):
    last = df.iloc[-1]
    prev = df.iloc[-2]

    at_midline   = safe_float(last["bb_pct"]) >= BB_EXIT
    rsi_high     = last["rsi"] > RSI_SELL
    macd_bearish = prev["macd_hist"] > 0 and last["macd_hist"] < 0
    trend_broken = not trend_ok

    return at_midline or rsi_high or macd_bearish or trend_broken

# ================================================================
#  MAIN LOOP
# ================================================================
def run_bot():
    init_trade_log()

    log("=" * 70)
    log("  DUAL-TIMEFRAME BOLLINGER BAND SCALP BOT — Bybit TESTNET")
    log(f"  Pairs      : {', '.join(SYMBOLS)}")
    log(f"  Timeframes : {INTERVAL_15M}m trend  +  {INTERVAL_5M}m entry")
    log(f"  SL/TP      : SET ON BYBIT SERVERS (safe if PC turns off)")
    log(f"  SL: -{STOP_LOSS_PCT*100:.1f}%  |  TP: +{TAKE_PROFIT_PCT*100:.1f}%  |  Trail: {TRAIL_STOP_PCT*100:.1f}%")
    log(f"  Risk/Trade : {RISK_PCT*100:.0f}% of balance  |  Max Pos: {MAX_POSITIONS}")
    log(f"  Target     : 3–10 signals/hour across {len(SYMBOLS)} pairs")
    log(f"  State file : {STATE_FILE}  (auto-recovers on restart)")
    log("=" * 70)

    # Per-symbol state
    entry_prices    = {s: 0.0 for s in SYMBOLS}
    highest_price   = {s: 0.0 for s in SYMBOLS}
    entry_usdt      = {s: 0.0 for s in SYMBOLS}
    bb_mid_at_entry = {s: 0.0 for s in SYMBOLS}
    sl_prices       = {s: 0.0 for s in SYMBOLS}
    tp_prices       = {s: 0.0 for s in SYMBOLS}

    trade_count = 0
    win_count   = 0
    total_pnl   = 0.0
    daily_pnl   = 0.0
    error_streak = 0

    # ── Recover state from previous run ──────────────────────────
    saved = load_state()
    if saved:
        trade_count = saved.get("trade_count", 0)
        win_count   = saved.get("win_count", 0)
        total_pnl   = saved.get("total_pnl", 0.0)
        for s, pos in saved.get("positions", {}).items():
            if s in SYMBOLS:
                entry_prices[s]    = pos["entry_price"]
                highest_price[s]   = pos["highest_price"]
                entry_usdt[s]      = pos["entry_usdt"]
                bb_mid_at_entry[s] = pos["bb_mid_at_entry"]
                sl_prices[s]       = pos["sl_price"]
                tp_prices[s]       = pos["tp_price"]
                log(f"  Recovered position: {s} | Entry=${pos['entry_price']:.4f} | "
                    f"SL=${pos['sl_price']:.4f} | TP=${pos['tp_price']:.4f}", "WARN")

    starting_bal = get_balance()
    daily_limit  = starting_bal * DAILY_LOSS_LIMIT
    log(f"  Starting balance: ${starting_bal:.2f} USDT")
    log("=" * 70)

    while True:
        try:
            usdt_bal       = get_balance()
            open_positions = sum(1 for s in SYMBOLS if get_position_qty(s) > 0.0001)

            # ── Circuit Breaker ───────────────────────────────────
            if daily_pnl <= -daily_limit:
                log(f"CIRCUIT BREAKER: Daily loss ${daily_pnl:.2f} hit limit. Pausing 1 hour.", "WARN")
                time.sleep(3600)
                daily_pnl   = 0.0
                daily_limit = get_balance() * DAILY_LOSS_LIMIT
                continue

            wr = int(win_count / trade_count * 100) if trade_count else 0
            log(f"Balance: ${usdt_bal:.2f}  |  Open: {open_positions}/{MAX_POSITIONS}  |  "
                f"Trades: {trade_count}  |  Win: {wr}%  |  PnL: {'+'if total_pnl>=0 else ''}${total_pnl:.2f}")
            log("─" * 70)

            for symbol in SYMBOLS:
                try:
                    df = add_15m_indicators(get_candles(symbol, INTERVAL_5M))

                    # Need at least 2 rows for prev/last comparisons + enough for indicators
                    min_rows = BB_PERIOD + RSI_PERIOD + 5
                    if len(df) < min_rows:
                        log(f"  {symbol:<12} skipping — only {len(df)} candles (need {min_rows})", "WARN")
                        continue

                    last = df.iloc[-1]

                    # Skip if indicators are invalid (NaN, or RSI out of range)
                    rsi_val = safe_float(last["rsi"], default=-1)
                    bb_val  = safe_float(last["bb_pct"], default=-1)
                    if math.isnan(last["rsi"]) or math.isnan(last["bb_pct"]) or rsi_val < 5:
                        log(f"  {symbol:<12} skipping — invalid indicators (RSI={rsi_val:.1f})", "WARN")
                        continue

                    price = get_price(symbol)
                    qty   = get_position_qty(symbol)
                    in_pos = qty > 0.0001

                    # If position closed by exchange SL/TP while bot was offline,
                    # clean up our local state
                    if not in_pos and entry_prices[symbol] > 0:
                        log(f"  {symbol}: Position closed by exchange (SL/TP hit while offline)", "WARN")
                        pnl = (price - entry_prices[symbol]) * (entry_usdt[symbol] / entry_prices[symbol])
                        total_pnl += pnl
                        trade_count += 1
                        if pnl > 0: win_count += 1
                        log_trade(symbol, "SELL", price, entry_usdt[symbol],
                                  entry_prices[symbol], pnl, "exchange_sl_tp")
                        entry_prices[symbol] = highest_price[symbol] = entry_usdt[symbol] = 0.0
                        bb_mid_at_entry[symbol] = sl_prices[symbol] = tp_prices[symbol] = 0.0
                        save_state(entry_prices, highest_price, entry_usdt,
                                   bb_mid_at_entry, sl_prices, tp_prices,
                                   trade_count, win_count, total_pnl)

                    # Update trailing high
                    if in_pos and price > highest_price[symbol]:
                        highest_price[symbol] = price

                    trend_ok, ema_f, ema_s = get_15m_trend(symbol)
                    if trend_ok is None:
                        log(f"  {symbol:<10} skipping — not enough 15m candles", "WARN")
                        continue

                    trend_icon  = "▲" if trend_ok else "▼"
                    macd_hist   = safe_float(last["macd_hist"])
                    macd_sign   = "+" if macd_hist > 0 else ""
                    bb_pct_val  = safe_float(last["bb_pct"])

                    # ── Compact status (2 lines per pair) ─────────
                    c1_ok = trend_ok
                    c2_ok = bb_pct_val < BB_ENTRY
                    c3_ok = last['rsi'] < RSI_BUY
                    c4_ok = macd_hist > 0
                    all_ok = c1_ok and c2_ok and c3_ok and c4_ok

                    t = "✓ Trend"  if c1_ok else f"✗ Trend(EMA20={ema_f:.2f} < EMA50={ema_s:.2f})"
                    b = "✓ BB"    if c2_ok else f"✗ BB({bb_pct_val:.2f} need <{BB_ENTRY})"
                    r = "✓ RSI"   if c3_ok else f"✗ RSI({last['rsi']:.1f} need <{RSI_BUY})"
                    m = "✓ MACD"  if c4_ok else f"✗ MACD({macd_sign}{macd_hist:.4f} need >0)"

                    state = "IN TRADE" if in_pos else ("** BUY **" if all_ok else "waiting")
                    log(f"  {symbol:<10} ${price:>12,.4f}  RSI={last['rsi']:4.1f}  BB={bb_pct_val:.2f}  MACD={macd_sign}{macd_hist:.4f}  [{state}]")
                    log(f"           {t}   {b}   {r}   {m}   {'→ OPENING TRADE' if all_ok else ''}")

                    # ── MANAGE OPEN POSITION ──────────────────────
                    if in_pos:
                        ep      = entry_prices[symbol]
                        high    = highest_price[symbol]
                        bb_tp   = bb_mid_at_entry[symbol]
                        pnl_pct = (price - ep) / ep if ep > 0 else 0
                        pnl_usd  = (price - ep) * qty
                        pnl_sign = "+" if pnl_usd >= 0 else ""
                        pnl_icon = "▲" if pnl_usd >= 0 else "▼"
                        log(f"  {'':10}  Entry=${ep:.4f}  SL=${sl_prices[symbol]:.4f}  TP=${tp_prices[symbol]:.4f}  P&L={pnl_sign}${abs(pnl_usd):.2f} ({pnl_pct*100:+.2f}%) {pnl_icon}")

                        def close_trade(reason):
                            nonlocal trade_count, win_count, total_pnl, daily_pnl
                            if place_sell(symbol, qty, reason=reason):
                                pnl = (price - ep) * qty
                                total_pnl += pnl
                                daily_pnl += pnl
                                trade_count += 1
                                if pnl > 0: win_count += 1
                                sign = "+" if pnl >= 0 else ""
                                log(f"    {reason.upper()} | P&L: {sign}${pnl:.2f} ({pnl_pct*100:.1f}%)", "SELL")
                                log_trade(symbol, "SELL", price, entry_usdt[symbol], ep, pnl, reason)
                                entry_prices[symbol] = highest_price[symbol] = entry_usdt[symbol] = 0.0
                                bb_mid_at_entry[symbol] = sl_prices[symbol] = tp_prices[symbol] = 0.0
                                save_state(entry_prices, highest_price, entry_usdt,
                                           bb_mid_at_entry, sl_prices, tp_prices,
                                           trade_count, win_count, total_pnl)

                        # Trailing Stop — tighten the exchange SL as price rises
                        # (SL/TP already on exchange, this just improves the SL)
                        if pnl_pct > 0.01:
                            trail_sl = high * (1 - TRAIL_STOP_PCT)
                            if trail_sl > sl_prices[symbol]:
                                sl_prices[symbol] = trail_sl
                                update_sl_on_exchange(symbol, trail_sl)
                                save_state(entry_prices, highest_price, entry_usdt,
                                           bb_mid_at_entry, sl_prices, tp_prices,
                                           trade_count, win_count, total_pnl)

                        # BB midline target reached → sell (indicator exit)
                        if bb_tp > 0 and price >= bb_tp:
                            log(f"    BB MIDLINE hit @ ${price:.4f} (target ${bb_tp:.4f})", "SELL")
                            close_trade("bb_midline")

                        # Indicator sell signal
                        elif sell_signal(df, trend_ok):
                            log(f"    SELL SIGNAL | RSI={last['rsi']:.1f} BB={bb_pct_val:.2f}", "SELL")
                            close_trade("signal")

                    # ── LOOK FOR ENTRY ────────────────────────────
                    elif open_positions < MAX_POSITIONS:
                        signal_ok, reasons = buy_signal(df, trend_ok)
                        if signal_ok:
                            size = calc_position_size(usdt_bal)
                            if size < MIN_TRADE_USDC:
                                log(f"    Skipping: size ${size:.2f} below minimum", "WARN")
                            else:
                                sl = price * (1 - STOP_LOSS_PCT)
                                tp = max(safe_float(last["bb_mid"]), price * (1 + TAKE_PROFIT_PCT * 0.5))
                                log(f"    BUY SIGNAL [{' | '.join(reasons)}] | "
                                    f"Size=${size:.2f} | SL=${sl:.4f} | TP=${tp:.4f}", "BUY")
                                if place_buy(symbol, size, sl, tp):
                                    entry_prices[symbol]    = price
                                    highest_price[symbol]   = price
                                    entry_usdt[symbol]      = size
                                    bb_mid_at_entry[symbol] = safe_float(last["bb_mid"])
                                    sl_prices[symbol]       = sl
                                    tp_prices[symbol]       = tp
                                    open_positions         += 1
                                    usdt_bal               -= size
                                    log_trade(symbol, "BUY", price, size, price, 0, "signal")
                                    # Save state immediately after every buy
                                    save_state(entry_prices, highest_price, entry_usdt,
                                               bb_mid_at_entry, sl_prices, tp_prices,
                                               trade_count, win_count, total_pnl)

                except Exception as e:
                    log(f"  {symbol}: {e}", "ERR")
                    continue

            log("─" * 70)
            error_streak = 0

        except KeyboardInterrupt:
            log("\nBot stopped by user.")
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
