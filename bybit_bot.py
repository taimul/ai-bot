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

  PROTECTION:
    - Stop Loss & Take Profit checked in software every cycle
    - Trailing Stop ratchets SL up as price rises
    - State saved to JSON — bot recovers open trades on restart
    - If bot restarts it re-attaches to existing open positions
================================================================
"""

import time
import csv
import os
import json
import math
import argparse
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
    "XRPUSDT",   # ✓ active
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
STOP_LOSS_PCT    = 0.015
TAKE_PROFIT_PCT  = 0.03
TRAIL_STOP_PCT   = 0.008
MAX_POSITIONS    = 6
DAILY_LOSS_LIMIT = 0.05
MIN_TRADE_USDT   = 5       # Dust filter — ignore coin balances below $5
MIN_ORDER_USDT   = 180     # Minimum order size per trade
MAX_ORDER_USDT   = 200     # Maximum order size per trade

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

SLEEP_SEC        = 60   # idle cycle (no open positions)
SLEEP_IN_TRADE   = 10  # fast cycle when holding a position
TRADE_LOG    = "trades.csv"
STATE_FILE   = "bot_state.json"
LEARN_FILE   = "learning.json"
LEARN_EVERY  = 5    # retrain after every N completed trades
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
#  STARTUP SWEEP — convert leftover coins back to USDT
# ================================================================
def sweep_coins_to_usdt(protected_coins=None):
    """
    Sell all non-USDT coin balances back to USDT at bot startup.
    Skips coins that are in active positions (protected_coins set).
    This ensures all capital is liquid before the main loop begins.
    """
    protected_coins = protected_coins or set()
    MIN_SWEEP_USD   = 1.0   # ignore dust below $1

    log("SWEEP  Checking for non-USDT balances to convert...", "WARN")
    try:
        resp  = session.get_wallet_balance(accountType="UNIFIED")
        coins = resp["result"]["list"][0]["coin"]
    except Exception as e:
        log(f"SWEEP  Could not fetch wallet: {e}", "ERR")
        return

    swept_any = False
    for c in coins:
        coin = c["coin"]
        if coin == "USDT":
            continue

        bal = safe_float(c.get("availableBalance") or c.get("walletBalance"))
        if bal <= 0:
            continue

        # Skip coins the bot currently holds as open positions
        if coin in protected_coins:
            log(f"SWEEP  {coin:<6} — skipped (active position)", "WARN")
            continue

        symbol = f"{coin}USDT"
        price  = get_price(symbol)
        if price <= 0:
            log(f"SWEEP  {coin:<6} — skipped (no price feed)", "WARN")
            continue

        usd_value = bal * price
        if usd_value < MIN_SWEEP_USD:
            log(f"SWEEP  {coin:<6} {bal:.6f} (~${usd_value:.4f}) — dust, skipping", "WARN")
            continue

        sell_qty = round_qty(symbol, bal)
        if sell_qty <= 0:
            log(f"SWEEP  {coin:<6} {bal:.6f} — rounds to 0, skipping", "WARN")
            continue

        log(f"SWEEP  {coin:<6} {bal:.6f} (~${usd_value:.2f}) → selling {sell_qty} for USDT", "WARN")
        # Split into chunks ≤ MAX_ORDER_USDT to respect per-order exchange limits
        chunk_coins = math.floor(MAX_ORDER_USDT / price) if price > 0 else sell_qty
        chunk_coins = max(chunk_coins, 1)
        remaining   = sell_qty
        chunk_ok    = True
        while remaining > 0 and chunk_ok:
            chunk = min(remaining, chunk_coins)
            chunk = round_qty(symbol, chunk)
            if chunk <= 0:
                break
            try:
                resp = session.place_order(
                    category  = "spot",
                    symbol    = symbol,
                    side      = "Sell",
                    orderType = "Market",
                    qty       = str(chunk),
                )
                log(f"SWEEP  {coin:<6} sold {chunk} | order {resp['result']['orderId']}", "WARN")
                swept_any  = True
                remaining -= chunk
                time.sleep(0.4)
            except Exception as e:
                log(f"SWEEP  {coin:<6} sell failed: {e}", "ERR")
                chunk_ok = False

    if swept_any:
        time.sleep(1.5)   # let fills settle before reading balance
        log(f"SWEEP  Done. New USDT balance: ${get_balance():.2f}", "WARN")
    else:
        log("SWEEP  Nothing to sweep — wallet is clean.", "WARN")


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
    """
    Returns total available USDT + USD value of all held coins.
    This gives the true portfolio value so position sizing and risk
    calculations are based on real capital, not just idle USDT.
    """
    try:
        resp  = session.get_wallet_balance(accountType="UNIFIED")
        coins = resp["result"]["list"][0]["coin"]
        total = 0.0
        for c in coins:
            coin = c["coin"]
            bal  = safe_float(c.get("availableBalance") or c.get("walletBalance"))
            if bal <= 0:
                continue
            if coin == "USDT":
                total += bal
            else:
                # Convert coin value to USD via live price
                symbol = f"{coin}USDT"
                price  = get_price(symbol)
                if price > 0:
                    total += bal * price
        return round(total, 4)
    except Exception as e:
        log(f"get_balance error: {e}", "ERR")
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
    # Always trade between MIN_ORDER_USDT and MAX_ORDER_USDT
    # as long as balance can cover it
    size = max(MIN_ORDER_USDT, min(MAX_ORDER_USDT, balance * 0.40))
    return round(size, 2)

def round_price(price):
    """Round price to correct decimal places based on price magnitude."""
    if price >= 100:  return round(price, 2)   # SOL, ETH, BTC
    if price >= 1:    return round(price, 4)   # XRP, TRX
    return            round(price, 4)          # ADA, DOGE — 4 decimals max

# ================================================================
#  ORDERS  — SL/TP attached to buy order (lives on Bybit servers)
# ================================================================
def place_buy(symbol, usdt_amount, sl_price, tp_price):
    """
    Places a market buy order. SL/TP are managed in software by the main loop,
    and also attempted on the exchange via set_trading_stop after fill.
    """
    try:
        resp = session.place_order(
            category   = "spot",
            symbol     = symbol,
            side       = "Buy",
            orderType  = "Market",
            qty        = str(usdt_amount),
            marketUnit = "quoteCoin",
        )
        order_id = resp['result']['orderId']
        log(f"BUY  {symbol} | ${usdt_amount:.2f} USDT | "
            f"SL=${round_price(sl_price)} | TP=${round_price(tp_price)} | "
            f"ID:{order_id}", "BUY")
        return True
    except Exception as e:
        log(f"BUY FAILED {symbol}: {e}", "ERR")
        return False

def update_sl_on_exchange(symbol, new_sl_price):
    # set_trading_stop is futures-only; spot SL is managed in software
    log(f"  Trail SL → ${round_price(new_sl_price)} (software)", "INFO")

def round_qty(symbol, qty):
    """Floor-round coin quantity to avoid 'insufficient balance' on sell."""
    price = get_price(symbol)
    if price >= 1000:  return math.floor(qty * 10000) / 10000  # 4 dp  (ETH)
    if price >= 10:    return math.floor(qty * 1000)  / 1000   # 3 dp  (SOL, BNB)
    if price >= 0.1:   return math.floor(qty * 10)    / 10     # 1 dp  (XRP, ADA, TRX — even sub-$1)
    return             float(math.floor(qty))                   # int   (very cheap)

def place_sell(symbol, coin_qty, reason="signal"):
    try:
        qty_rounded = round_qty(symbol, coin_qty)
        qty_str = str(qty_rounded)
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
def buy_signal(df, trend_ok, rsi_thresh=None, bb_thresh=None, macd_min=0.0):
    """
    Entry on 5m when ALL true:
      - 15m EMA20 > EMA50 (uptrend)
      - Price in lower BB zone (bb_pct < bb_thresh)
      - RSI < rsi_thresh (not overbought)
      - MACD histogram > macd_min (bullish momentum)
    Thresholds default to config values but are overridden by learned params.
    """
    last = df.iloc[-1]
    _rsi = rsi_thresh if rsi_thresh is not None else RSI_BUY
    _bb  = bb_thresh  if bb_thresh  is not None else BB_ENTRY

    at_lower_bb  = safe_float(last["bb_pct"]) < _bb
    rsi_dip      = last["rsi"] < _rsi
    macd_bullish = last["macd_hist"] > macd_min

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
#  LEARNING ENGINE  — v2  (daily compression + deep adaptation)
# ================================================================
class LearningEngine:
    """
    Day-by-day learning engine.  Stores:
      • params          – global entry thresholds (RSI, BB, MACD, trend gap)
      • symbol_params   – per-symbol overrides tuned from that pair's history
      • trades          – rolling window of the last MAX_RAW_TRADES (never grows)
      • daily_summaries – one compact record per day, kept for MAX_DAILY_DAYS
      • symbol_stats    – lifetime win/loss + consecutive-loss counter
      • avoid_hours     – hours of day with consistently poor win rates

    File size is permanently bounded:
        50 raw trades  × ~200 bytes  =  ~10 KB
        60 daily rows  × ~400 bytes  =  ~24 KB
        params / stats                =  ~1  KB
        Total maximum                ≈  ~35 KB   (forever)

    What it learns:
      1. RSI threshold  — tighten when losses happen at high RSI
      2. BB threshold   — tighten when losses happen mid-band
      3. MACD minimum   — raise when weak-MACD entries keep losing
      4. Trend gap min  — raise when weak-trend entries keep losing
      5. Per-symbol RSI/BB — each pair tuned independently
      6. Hour avoidance — skips hours with <25% win rate over ≥3 trades
      7. Consecutive loss cool-down — symbol paused after 3 straight losses
      8. Cross-day pattern — tightens further after multiple bad days in a row
    """

    # Hard boundaries — params never go outside these
    LIMITS = {
        "RSI_BUY":       (35,   60),
        "BB_ENTRY":      (0.20, 0.70),
        "MACD_MIN":      (0.0,  0.01),
        "TREND_GAP_MIN": (0.0,  0.005),
    }

    MAX_RAW_TRADES    = 50   # rolling raw-trade window
    MAX_DAILY_DAYS    = 60   # daily summaries to keep (~2 months)
    CONSEC_LOSS_PAUSE = 3    # pause symbol after N consecutive losses
    COOLDOWN_MINUTES  = 60   # how long the cooldown lasts before retrying

    def __init__(self):
        self.params = {
            "RSI_BUY":       RSI_BUY,
            "BB_ENTRY":      BB_ENTRY,
            "MACD_MIN":      0.0,
            "TREND_GAP_MIN": 0.0,
        }
        self.trades          = []
        self.daily_summaries = []
        self.cooldowns       = {}  # symbol -> datetime when cooldown expires
        self.symbol_stats    = {s: {"wins": 0, "losses": 0, "consec_losses": 0}
                                for s in SYMBOLS}
        self.symbol_params   = {s: {"RSI_BUY": RSI_BUY, "BB_ENTRY": BB_ENTRY}
                                for s in SYMBOLS}
        self.avoid_hours     = []
        self._current_day    = datetime.now().strftime("%Y-%m-%d")
        self._load()

    # ── Persistence ──────────────────────────────────────────────
    def _save(self):
        data = {
            "params":          self.params,
            "trades":          self.trades[-self.MAX_RAW_TRADES:],
            "daily_summaries": self.daily_summaries[-self.MAX_DAILY_DAYS:],
            "symbol_stats":    self.symbol_stats,
            "symbol_params":   self.symbol_params,
            "avoid_hours":     self.avoid_hours,
            "cooldowns":       {s: t.isoformat() for s, t in self.cooldowns.items()},
        }
        with open(LEARN_FILE, "w") as f:
            json.dump(data, f, indent=2)

    def _load(self):
        if not os.path.exists(LEARN_FILE):
            return
        try:
            with open(LEARN_FILE) as f:
                data = json.load(f)
            self.params          = data.get("params",          self.params)
            self.trades          = data.get("trades",          [])
            self.daily_summaries = data.get("daily_summaries", [])
            self.symbol_stats    = data.get("symbol_stats",    self.symbol_stats)
            self.symbol_params   = data.get("symbol_params",   self.symbol_params)
            self.avoid_hours     = data.get("avoid_hours",     [])
            # Restore cooldowns, dropping any that already expired
            now = datetime.now()
            for s, iso in data.get("cooldowns", {}).items():
                expiry = datetime.fromisoformat(iso)
                if expiry > now:
                    self.cooldowns[s] = expiry
            # Ensure TREND_GAP_MIN exists in older files
            self.params.setdefault("TREND_GAP_MIN", 0.0)
            log(f"LEARN  {len(self.trades)} trades | {len(self.daily_summaries)} days | "
                f"RSI<{self.params['RSI_BUY']}  BB<{self.params['BB_ENTRY']:.2f}  "
                f"MACD>{self.params['MACD_MIN']:.4f}  "
                f"TrendGap>{self.params['TREND_GAP_MIN']:.4f}", "WARN")
            if self.avoid_hours:
                log(f"LEARN  Avoiding hours: {self.avoid_hours}", "WARN")
        except Exception as e:
            log(f"LEARN  Could not load: {e}", "WARN")

    # ── Day rollover ──────────────────────────────────────────────
    def _check_day_rollover(self):
        today = datetime.now().strftime("%Y-%m-%d")
        if today != self._current_day:
            self._compress_day(self._current_day)
            self._current_day = today

    def _compress_day(self, date_str):
        """Distil all raw trades for date_str into one compact daily summary."""
        day_trades = [t for t in self.trades if t["time"].startswith(date_str)]
        if not day_trades:
            return

        wins   = [t for t in day_trades if t["win"]]
        losses = [t for t in day_trades if not t["win"]]

        def avg(seq, key):
            return round(sum(t[key] for t in seq) / len(seq), 4) if seq else None

        summary = {
            "date":         date_str,
            "trades":       len(day_trades),
            "wins":         len(wins),
            "losses":       len(losses),
            "win_rate":     round(len(wins) / len(day_trades), 3),
            "avg_pnl_pct":  avg(day_trades, "pnl_pct"),
            "sl_rate":      round(
                sum(1 for t in day_trades if t["exit_reason"] == "stop_loss")
                / len(day_trades), 3),
            "params_eod":   dict(self.params),
            "win_avg_rsi":  avg(wins,   "entry_rsi"),
            "loss_avg_rsi": avg(losses, "entry_rsi"),
            "win_avg_bb":   avg(wins,   "entry_bb"),
            "loss_avg_bb":  avg(losses, "entry_bb"),
            "win_avg_macd": avg(wins,   "entry_macd"),
            "loss_avg_macd":avg(losses, "entry_macd"),
            "by_symbol": {
                sym: {
                    "trades": len(st := [t for t in day_trades if t["symbol"] == sym]),
                    "wins":   sum(1 for t in st if t["win"]),
                    "avg_pnl": avg(st, "pnl_pct"),
                }
                for sym in {t["symbol"] for t in day_trades}
            },
        }
        # Replace existing summary for this date (idempotent)
        self.daily_summaries = [s for s in self.daily_summaries
                                 if s["date"] != date_str]
        self.daily_summaries.append(summary)
        self.daily_summaries = self.daily_summaries[-self.MAX_DAILY_DAYS:]
        log(f"LEARN  Day {date_str} compressed → "
            f"{len(day_trades)} trades | WR={summary['win_rate']:.0%} | "
            f"SL={summary['sl_rate']:.0%}", "WARN")

    # ── Record a completed trade ──────────────────────────────────
    def record(self, symbol, entry_rsi, entry_bb, entry_macd,
               entry_trend_gap, exit_reason, pnl_pct):
        self._check_day_rollover()

        win = pnl_pct > 0
        self.trades.append({
            "time":            datetime.now().isoformat(),
            "symbol":          symbol,
            "entry_rsi":       round(entry_rsi, 2),
            "entry_bb":        round(entry_bb, 3),
            "entry_macd":      round(entry_macd, 5),
            "entry_trend_gap": round(entry_trend_gap, 4),
            "exit_reason":     exit_reason,
            "pnl_pct":         round(pnl_pct, 4),
            "win":             win,
            "hour":            datetime.now().hour,
        })

        # Update symbol stats + consecutive loss counter
        ss = self.symbol_stats.setdefault(
            symbol, {"wins": 0, "losses": 0, "consec_losses": 0})
        if win:
            ss["wins"]          += 1
            ss["consec_losses"]  = 0
        else:
            ss["losses"]        += 1
            ss["consec_losses"]  = ss.get("consec_losses", 0) + 1

        # Hard cap on raw trades — file never grows beyond MAX_RAW_TRADES
        self.trades = self.trades[-self.MAX_RAW_TRADES:]

        self._save()

        if len(self.trades) % LEARN_EVERY == 0:
            self._adapt()

    # ── Core adaptation logic ─────────────────────────────────────
    def _adapt(self):
        recent = self.trades          # already capped at MAX_RAW_TRADES
        if len(recent) < LEARN_EVERY:
            return

        wins   = [t for t in recent if t["win"]]
        losses = [t for t in recent if not t["win"]]
        wr     = len(wins) / len(recent)
        changes = []

        # ── 1. RSI threshold ──────────────────────────────────────
        if wins and losses:
            avg_loss_rsi = sum(t["entry_rsi"] for t in losses) / len(losses)
            avg_win_rsi  = sum(t["entry_rsi"] for t in wins)   / len(wins)
            if avg_loss_rsi > avg_win_rsi + 3:
                new = self._clamp("RSI_BUY", self.params["RSI_BUY"] - 2)
                if new != self.params["RSI_BUY"]:
                    changes.append(
                        f"RSI {self.params['RSI_BUY']}→{new} "
                        f"(losses@{avg_loss_rsi:.1f} vs wins@{avg_win_rsi:.1f})")
                    self.params["RSI_BUY"] = new

        if wr > 0.65 and len(recent) >= 10:
            new = self._clamp("RSI_BUY", self.params["RSI_BUY"] + 1)
            if new != self.params["RSI_BUY"]:
                changes.append(f"RSI {self.params['RSI_BUY']}→{new} (WR={wr:.0%} strong)")
                self.params["RSI_BUY"] = new

        # ── 2. BB threshold ───────────────────────────────────────
        if wins and losses:
            avg_loss_bb = sum(t["entry_bb"] for t in losses) / len(losses)
            avg_win_bb  = sum(t["entry_bb"] for t in wins)   / len(wins)
            if avg_loss_bb > avg_win_bb + 0.07:
                new = self._clamp("BB_ENTRY", round(self.params["BB_ENTRY"] - 0.05, 2))
                if new != self.params["BB_ENTRY"]:
                    changes.append(
                        f"BB {self.params['BB_ENTRY']:.2f}→{new:.2f} "
                        f"(losses@BB={avg_loss_bb:.2f} vs wins@{avg_win_bb:.2f})")
                    self.params["BB_ENTRY"] = new

        # ── 3. Stop-loss rate — tighten both BB and RSI ───────────
        sl_rate = sum(1 for t in recent if t["exit_reason"] == "stop_loss") / len(recent)
        if sl_rate > 0.40:
            new_bb  = self._clamp("BB_ENTRY", round(self.params["BB_ENTRY"] - 0.05, 2))
            new_rsi = self._clamp("RSI_BUY",  self.params["RSI_BUY"] - 2)
            changes.append(f"SL rate {sl_rate:.0%} → BB→{new_bb:.2f} RSI→{new_rsi}")
            self.params["BB_ENTRY"] = new_bb
            self.params["RSI_BUY"]  = new_rsi

        # ── 4. MACD minimum strength ──────────────────────────────
        if losses:
            avg_loss_macd = sum(t["entry_macd"] for t in losses) / len(losses)
            if avg_loss_macd < 0.001:
                new = self._clamp("MACD_MIN",
                                  round(self.params["MACD_MIN"] + 0.0002, 4))
                if new != self.params["MACD_MIN"]:
                    changes.append(f"MACD_MIN →{new:.4f} (weak-MACD entries losing)")
                    self.params["MACD_MIN"] = new

        # ── 5. Trend gap filter ───────────────────────────────────
        if wins and losses:
            avg_loss_gap = sum(t["entry_trend_gap"] for t in losses) / len(losses)
            avg_win_gap  = sum(t["entry_trend_gap"] for t in wins)   / len(wins)
            if avg_win_gap > avg_loss_gap * 1.5 and avg_win_gap > 0:
                new = self._clamp("TREND_GAP_MIN",
                                  round(avg_loss_gap * 0.5, 4))
                if new != self.params["TREND_GAP_MIN"]:
                    changes.append(
                        f"TREND_GAP_MIN →{new:.4f} "
                        f"(wins gap={avg_win_gap:.4f} vs losses={avg_loss_gap:.4f})")
                    self.params["TREND_GAP_MIN"] = new

        # ── 6. Hour-of-day avoidance ──────────────────────────────
        if len(recent) >= 20:
            hour_stats = {}
            for t in recent:
                h = t.get("hour", -1)
                if h < 0:
                    continue
                hs = hour_stats.setdefault(h, {"wins": 0, "total": 0})
                hs["total"] += 1
                if t["win"]:
                    hs["wins"] += 1

            bad_hours = [h for h, s in hour_stats.items()
                         if s["total"] >= 3 and s["wins"] / s["total"] < 0.25]
            if bad_hours:
                new_avoid = sorted(set(self.avoid_hours + bad_hours))
                if new_avoid != self.avoid_hours:
                    changes.append(f"Avoid hours {bad_hours} added (WR<25%)")
                    self.avoid_hours = new_avoid

            # Un-avoid hours whose win rate has recovered
            recovered = [h for h in self.avoid_hours
                         if (s := hour_stats.get(h)) and
                            s["total"] >= 3 and s["wins"] / s["total"] >= 0.50]
            if recovered:
                self.avoid_hours = [h for h in self.avoid_hours
                                    if h not in recovered]
                changes.append(f"Restored hours {recovered} (WR≥50%)")

        # ── 7. Per-symbol parameter tuning ───────────────────────
        for sym in SYMBOLS:
            sym_trades = [t for t in recent if t["symbol"] == sym]
            if len(sym_trades) < 5:
                continue
            sym_wins   = [t for t in sym_trades if t["win"]]
            sym_losses = [t for t in sym_trades if not t["win"]]
            sym_wr     = len(sym_wins) / len(sym_trades)
            sp = self.symbol_params.setdefault(
                sym, {"RSI_BUY": RSI_BUY, "BB_ENTRY": BB_ENTRY})

            # Tighten RSI for symbols where losses happen at higher RSI
            if sym_wins and sym_losses:
                sl_rsi = sum(t["entry_rsi"] for t in sym_losses) / len(sym_losses)
                wn_rsi = sum(t["entry_rsi"] for t in sym_wins)   / len(sym_wins)
                if sl_rsi > wn_rsi + 4:
                    new_rsi = max(35, sp["RSI_BUY"] - 2)
                    if new_rsi != sp["RSI_BUY"]:
                        changes.append(f"{sym} RSI→{new_rsi} (per-symbol losses@{sl_rsi:.1f})")
                        sp["RSI_BUY"] = new_rsi

            # Relax BB slightly if symbol is doing well; tighten if poor
            if sym_wr > 0.70 and len(sym_trades) >= 5:
                new_bb = min(0.50, round(sp["BB_ENTRY"] + 0.03, 2))
                if new_bb != sp["BB_ENTRY"]:
                    changes.append(f"{sym} BB→{new_bb:.2f} (WR={sym_wr:.0%} strong)")
                    sp["BB_ENTRY"] = new_bb
            elif sym_wr < 0.35 and len(sym_trades) >= 5:
                new_bb = max(0.20, round(sp["BB_ENTRY"] - 0.05, 2))
                if new_bb != sp["BB_ENTRY"]:
                    changes.append(f"{sym} BB→{new_bb:.2f} (WR={sym_wr:.0%} weak)")
                    sp["BB_ENTRY"] = new_bb

        # ── 8. Cross-day learning ─────────────────────────────────
        if len(self.daily_summaries) >= 3:
            recent_days  = self.daily_summaries[-7:]
            avg_daily_wr = sum(d["win_rate"] for d in recent_days) / len(recent_days)
            avg_daily_sl = sum(d["sl_rate"]  for d in recent_days) / len(recent_days)
            if avg_daily_wr < 0.45 and avg_daily_sl > 0.35:
                new_bb = self._clamp("BB_ENTRY",
                                     round(self.params["BB_ENTRY"] - 0.03, 2))
                if new_bb != self.params["BB_ENTRY"]:
                    changes.append(
                        f"Multi-day tighten: BB→{new_bb:.2f} "
                        f"(7d WR={avg_daily_wr:.0%} SL={avg_daily_sl:.0%})")
                    self.params["BB_ENTRY"] = new_bb

        self._save()

        if changes:
            log("─" * 70)
            log(f"LEARN  {len(recent)} trades | WR={wr:.0%} | {len(changes)} adjustments:", "WARN")
            for c in changes:
                log(f"LEARN    • {c}", "WARN")
            log("─" * 70)
        else:
            log(f"LEARN  WR={wr:.0%} over {len(recent)} trades — params stable", "WARN")

    # ── Helpers ───────────────────────────────────────────────────
    def _clamp(self, key, value):
        lo, hi = self.LIMITS[key]
        return max(lo, min(hi, value))

    def get_symbol_params(self, symbol):
        """Return per-symbol learned thresholds (falls back to global params)."""
        sp = self.symbol_params.get(symbol, {})
        return {
            "RSI_BUY":  sp.get("RSI_BUY",  self.params["RSI_BUY"]),
            "BB_ENTRY": sp.get("BB_ENTRY", self.params["BB_ENTRY"]),
        }

    def should_skip_hour(self):
        """Return True if the current hour has been flagged as consistently bad."""
        return datetime.now().hour in self.avoid_hours

    def is_symbol_banned(self, symbol):
        """
        Pause symbol for COOLDOWN_MINUTES after 3 consecutive losses
        or 5+ net losses. Automatically retries after the cooldown expires.
        """
        s   = self.symbol_stats.get(symbol, {})
        net = s.get("losses", 0) - s.get("wins", 0)
        cl  = s.get("consec_losses", 0)

        needs_cooldown = net >= 5 or cl >= self.CONSEC_LOSS_PAUSE
        if not needs_cooldown:
            self.cooldowns.pop(symbol, None)   # clear any expired cooldown
            return False

        now    = datetime.now()
        expiry = self.cooldowns.get(symbol)
        if expiry is None:
            # First time hitting the threshold — start the cooldown clock
            self.cooldowns[symbol] = now + __import__("datetime").timedelta(
                minutes=self.COOLDOWN_MINUTES)
            log(f"  {symbol}: cooldown started — pausing {self.COOLDOWN_MINUTES}min", "WARN")
            return True

        if now >= expiry:
            # Cooldown expired — reset consecutive counter and retry
            log(f"  {symbol}: cooldown over — resetting and retrying", "WARN")
            s["consec_losses"] = 0
            self.cooldowns.pop(symbol, None)
            return False

        mins_left = int((expiry - now).total_seconds() / 60)
        log(f"  {symbol:<10} COOLDOWN {mins_left}min left", "WARN")
        return True

    def summary(self):
        parts = []
        if self.trades:
            recent = self.trades[-20:]
            wr  = sum(1 for t in recent if t["win"]) / len(recent)
            avg = sum(t["pnl_pct"] for t in recent) / len(recent)
            parts.append(f"Recent {len(recent)}: WR={wr:.0%} AvgPnL={avg:+.2f}%")
        parts.append(
            f"RSI<{self.params['RSI_BUY']} BB<{self.params['BB_ENTRY']:.2f} "
            f"MACD>{self.params['MACD_MIN']:.4f} "
            f"TrendGap>{self.params['TREND_GAP_MIN']:.4f}")
        if self.daily_summaries:
            d = self.daily_summaries[-1]
            parts.append(
                f"Last day {d['date']}: {d['trades']} trades WR={d['win_rate']:.0%}")
        if self.avoid_hours:
            parts.append(f"Avoid hrs:{self.avoid_hours}")
        return " | ".join(parts)


# ================================================================
#  MAIN LOOP
# ================================================================
def run_bot():
    init_trade_log()
    learner = LearningEngine()

    log("=" * 70)
    log("  DUAL-TIMEFRAME BOLLINGER BAND SCALP BOT — Bybit TESTNET")
    log(f"  Pairs      : {', '.join(SYMBOLS)}")
    log(f"  Timeframes : {INTERVAL_15M}m trend  +  {INTERVAL_5M}m entry")
    log(f"  SL/TP      : Software-managed (SL/TP checked every cycle)")
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
    # Learning — entry conditions recorded at buy time
    entry_rsi       = {s: 0.0 for s in SYMBOLS}
    entry_bb        = {s: 0.0 for s in SYMBOLS}
    entry_macd      = {s: 0.0 for s in SYMBOLS}
    entry_trend_gap = {s: 0.0 for s in SYMBOLS}

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

    # Sweep leftover coins → USDT before trading begins.
    # Pass coins with active recovered positions so they aren't sold.
    protected = {s.replace("USDT", "") for s in SYMBOLS if entry_prices[s] > 0}
    sweep_coins_to_usdt(protected_coins=protected)

    starting_bal = get_balance()
    daily_limit  = starting_bal * DAILY_LOSS_LIMIT
    log(f"  Starting balance: ${starting_bal:.2f} USDT")
    log("=" * 70)

    while True:
        try:
            usdt_bal       = get_balance()
            open_positions = sum(1 for s in SYMBOLS if entry_prices[s] > 0)

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
            log(f"LEARN  {learner.summary()}", "WARN")
            log("─" * 70)

            for symbol in SYMBOLS:
                try:
                    # Skip symbols the learner has flagged as consistently losing
                    if learner.is_symbol_banned(symbol):
                        log(f"  {symbol:<10} SKIPPED by learner (too many losses)", "WARN")
                        continue

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
                    # in_pos requires bot to have opened the trade AND coin value >= $5
                    # This prevents wallet dust from repeated failed sells showing as IN TRADE
                    in_pos = entry_prices[symbol] > 0 and (qty * price) >= MIN_TRADE_USDT

                    # If position closed by exchange SL/TP while bot was offline,
                    # clean up our local state
                    if not in_pos and entry_prices[symbol] > 0:
                        log(f"  {symbol}: Coin balance gone — position closed externally or dust cleared", "WARN")
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
                    # Use per-symbol learned params (fallback to global)
                    sp     = learner.get_symbol_params(symbol)
                    L_RSI  = sp["RSI_BUY"]
                    L_BB   = sp["BB_ENTRY"]
                    L_MACD = learner.params["MACD_MIN"]
                    L_GAP  = learner.params["TREND_GAP_MIN"]
                    trend_gap = ema_f - ema_s

                    c1_ok = trend_ok
                    c2_ok = bb_pct_val < L_BB
                    c3_ok = last['rsi'] < L_RSI
                    c4_ok = macd_hist > L_MACD
                    c5_ok = trend_gap >= L_GAP
                    all_ok = c1_ok and c2_ok and c3_ok and c4_ok and c5_ok

                    t = "✓ Trend"  if c1_ok else f"✗ Trend(EMA20={ema_f:.2f} < EMA50={ema_s:.2f})"
                    b = "✓ BB"    if c2_ok else f"✗ BB({bb_pct_val:.2f} need <{L_BB})"
                    r = "✓ RSI"   if c3_ok else f"✗ RSI({last['rsi']:.1f} need <{L_RSI})"
                    m = "✓ MACD"  if c4_ok else f"✗ MACD({macd_sign}{macd_hist:.4f} need >{L_MACD:.4f})"
                    g = "✓ Gap"   if c5_ok else f"✗ Gap({trend_gap:.4f} need ≥{L_GAP:.4f})"

                    state = "IN TRADE" if in_pos else ("** BUY **" if all_ok else "waiting")
                    log(f"  {symbol:<10} ${price:>12,.4f}  RSI={last['rsi']:4.1f}  BB={bb_pct_val:.2f}  MACD={macd_sign}{macd_hist:.4f}  [{state}]")
                    log(f"           {t}   {b}   {r}   {m}   {g}   {'→ OPENING TRADE' if all_ok else ''}")

                    # ── MANAGE OPEN POSITION ──────────────────────
                    if in_pos:
                        ep   = entry_prices[symbol]
                        high = highest_price[symbol]
                        bb_tp = bb_mid_at_entry[symbol]

                        # Dust / stale state guard: if rounded qty is 0, clear state
                        sellable = round_qty(symbol, qty)
                        if sellable == 0.0:
                            log(f"  {symbol}: Dust/stale position ({qty:.4f} coins) — clearing state", "WARN")
                            entry_prices[symbol] = highest_price[symbol] = entry_usdt[symbol] = 0.0
                            bb_mid_at_entry[symbol] = sl_prices[symbol] = tp_prices[symbol] = 0.0
                            entry_rsi[symbol] = entry_bb[symbol] = entry_macd[symbol] = entry_trend_gap[symbol] = 0.0
                            save_state(entry_prices, highest_price, entry_usdt,
                                       bb_mid_at_entry, sl_prices, tp_prices,
                                       trade_count, win_count, total_pnl)
                            continue

                        trade_qty = entry_usdt[symbol] / ep if ep > 0 else qty
                        pnl_pct = (price - ep) / ep if ep > 0 else 0
                        pnl_usd  = (price - ep) * trade_qty
                        pnl_sign = "+" if pnl_usd >= 0 else ""
                        pnl_icon = "▲" if pnl_usd >= 0 else "▼"
                        log(f"  {'':10}  Entry=${ep:.4f}  SL=${sl_prices[symbol]:.4f}  TP=${tp_prices[symbol]:.4f}  P&L={pnl_sign}${abs(pnl_usd):.2f} ({pnl_pct*100:+.2f}%) {pnl_icon}")

                        def close_trade(reason):
                            nonlocal trade_count, win_count, total_pnl, daily_pnl
                            # Fetch actual coin balance fresh — entry-based math
                            # is always slightly high because Bybit deducts fees
                            # from the coins received on buy, leaving a little
                            # less than entry_usdt/ep in the wallet.
                            actual_bal = get_position_qty(symbol)
                            sell_qty   = round_qty(symbol, actual_bal)
                            if sell_qty <= 0:
                                log(f"    {reason.upper()} | wallet has only dust "
                                    f"({actual_bal:.6f}) — clearing state", "WARN")
                                entry_prices[symbol] = highest_price[symbol] = entry_usdt[symbol] = 0.0
                                bb_mid_at_entry[symbol] = sl_prices[symbol] = tp_prices[symbol] = 0.0
                                entry_rsi[symbol] = entry_bb[symbol] = entry_macd[symbol] = entry_trend_gap[symbol] = 0.0
                                save_state(entry_prices, highest_price, entry_usdt,
                                           bb_mid_at_entry, sl_prices, tp_prices,
                                           trade_count, win_count, total_pnl)
                                return
                            if place_sell(symbol, sell_qty, reason=reason):
                                pnl     = (price - ep) * sell_qty
                                pnl_pct_val = (price - ep) / ep if ep > 0 else 0
                                total_pnl += pnl
                                daily_pnl += pnl
                                trade_count += 1
                                if pnl > 0: win_count += 1
                                sign = "+" if pnl >= 0 else ""
                                log(f"    {reason.upper()} | P&L: {sign}${pnl:.2f} ({pnl_pct*100:.1f}%)", "SELL")
                                log_trade(symbol, "SELL", price, entry_usdt[symbol], ep, pnl, reason)
                                learner.record(
                                    symbol          = symbol,
                                    entry_rsi       = entry_rsi[symbol],
                                    entry_bb        = entry_bb[symbol],
                                    entry_macd      = entry_macd[symbol],
                                    entry_trend_gap = entry_trend_gap[symbol],
                                    exit_reason     = reason,
                                    pnl_pct         = pnl_pct_val * 100,
                                )
                                entry_prices[symbol] = highest_price[symbol] = entry_usdt[symbol] = 0.0
                                bb_mid_at_entry[symbol] = sl_prices[symbol] = tp_prices[symbol] = 0.0
                                entry_rsi[symbol] = entry_bb[symbol] = entry_macd[symbol] = entry_trend_gap[symbol] = 0.0
                                save_state(entry_prices, highest_price, entry_usdt,
                                           bb_mid_at_entry, sl_prices, tp_prices,
                                           trade_count, win_count, total_pnl)

                        # Trailing Stop — tighten SL as price rises (software-only)
                        if pnl_pct > 0.01:
                            trail_sl = high * (1 - TRAIL_STOP_PCT)
                            if trail_sl > sl_prices[symbol]:
                                sl_prices[symbol] = trail_sl
                                update_sl_on_exchange(symbol, trail_sl)
                                save_state(entry_prices, highest_price, entry_usdt,
                                           bb_mid_at_entry, sl_prices, tp_prices,
                                           trade_count, win_count, total_pnl)

                        # ── Exit priority (first match wins) ─────────
                        # 1. Software Stop Loss
                        if sl_prices[symbol] > 0 and price <= sl_prices[symbol]:
                            log(f"    STOP LOSS hit @ ${price:.4f} (SL=${sl_prices[symbol]:.4f})", "SELL")
                            close_trade("stop_loss")

                        # 2. Software Take Profit
                        elif tp_prices[symbol] > 0 and price >= tp_prices[symbol]:
                            log(f"    TAKE PROFIT hit @ ${price:.4f} (TP=${tp_prices[symbol]:.4f})", "SELL")
                            close_trade("take_profit")

                        # 3. BB midline target
                        elif bb_tp > 0 and price >= bb_tp:
                            log(f"    BB MIDLINE hit @ ${price:.4f} (target ${bb_tp:.4f})", "SELL")
                            close_trade("bb_midline")

                        # 4. Indicator sell signal
                        elif sell_signal(df, trend_ok):
                            log(f"    SELL SIGNAL | RSI={last['rsi']:.1f} BB={bb_pct_val:.2f}", "SELL")
                            close_trade("signal")

                    # ── LOOK FOR ENTRY ────────────────────────────
                    elif open_positions < MAX_POSITIONS:
                        # Hour filter — skip entry if this hour has a poor track record
                        if learner.should_skip_hour():
                            log(f"  {symbol}: skip — hour {datetime.now().hour:02d}:xx avoided by learner", "WARN")
                        else:
                            # Use the SAME learned params for actual entry decision
                            # (fixes the bug where buy_signal used hardcoded constants)
                            signal_ok, reasons = buy_signal(
                                df, trend_ok,
                                rsi_thresh=L_RSI,
                                bb_thresh=L_BB,
                                macd_min=L_MACD,
                            )
                            # Also apply trend gap filter
                            if signal_ok and trend_gap < L_GAP:
                                log(f"    Skip: trend gap {trend_gap:.4f} < min {L_GAP:.4f}", "WARN")
                                signal_ok = False
                            if signal_ok:
                                size = calc_position_size(usdt_bal)
                                if usdt_bal < size:
                                    log(f"    Skipping: need ${size:.2f} but only ${usdt_bal:.2f} USDT available", "WARN")
                                else:
                                    sl = price * (1 - STOP_LOSS_PCT)
                                    tp = max(safe_float(last["bb_mid"]), price * (1 + TAKE_PROFIT_PCT))
                                    log(f"    BUY SIGNAL [{' | '.join(reasons)}] | "
                                        f"Size=${size:.2f} | SL=${sl:.4f} | TP=${tp:.4f}", "BUY")
                                    if place_buy(symbol, size, sl, tp):
                                        entry_prices[symbol]    = price
                                        highest_price[symbol]   = price
                                        entry_usdt[symbol]      = size
                                        bb_mid_at_entry[symbol] = safe_float(last["bb_mid"])
                                        sl_prices[symbol]       = sl
                                        tp_prices[symbol]       = tp
                                        # Record entry conditions for learning
                                        entry_rsi[symbol]       = last["rsi"]
                                        entry_bb[symbol]        = bb_pct_val
                                        entry_macd[symbol]      = macd_hist
                                        entry_trend_gap[symbol] = trend_gap
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

        any_open = any(entry_prices[s] > 0 for s in SYMBOLS)
        time.sleep(SLEEP_IN_TRADE if any_open else SLEEP_SEC)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Bybit testnet scalp bot")
    parser.add_argument("--reset", action="store_true",
                        help="Clear saved state and trade log before starting")
    args = parser.parse_args()

    if args.reset:
        clear_state()
        if os.path.exists(TRADE_LOG):
            os.remove(TRADE_LOG)
        log("Reset done: state + trade log cleared. Starting from zero.", "WARN")

    run_bot()
