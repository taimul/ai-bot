"""
================================================================
  DCA TRADING BOT
  Platform  : Bybit TESTNET (Spot)

  STRATEGY  : Dollar-Cost Averaging with Indicator Guard
  ─────────────────────────────────────────────────────────────────
  ENTRY    : 15m uptrend + BB lower zone + RSI dip + MACD bullish
  DCA 1    : Price drops 1.5% below avg entry, RSI still > 30
  DCA 2    : Price drops 3.0% below avg entry, RSI still > 25
  EXIT WIN : Price >= avg_entry × 1.02  (2% profit on avg cost)
             OR BB midline crossed
  EXIT LOSS: Price drops 5% below INITIAL entry (hard stop)
             OR RSI < 22 (panic dump)
             OR 15m trend breaks (EMA20 crosses below EMA50)
             → Sell ALL coins immediately

  CAPITAL PER POSITION:
    Level 0 : $60    (initial entry)
    Level 1 : $80    (DCA buy on -1.5%)
    Level 2 : $100   (DCA buy on -3.0%)
    Max total: $240 per symbol

  PROTECTION:
    - Hard stop at 5% below initial entry — never removed
    - Trailing stop activates once price exceeds TP line
    - Panic RSI cut regardless of price level
    - Trend break = immediate full exit at any DCA level
    - State saved to JSON — recovers open positions on restart
    - Separate learning from scalp bot (dca_learning.json)
    - Symbol cooldown after 3 consecutive losses
================================================================
"""

import time
import csv
import os
import json
import math
import argparse
import pandas as pd
from datetime import datetime, timedelta
from pybit.unified_trading import HTTP

# ================================================================
#  CONFIG
# ================================================================
# Credentials: set BYBIT_API_KEY / BYBIT_API_SECRET env vars for mainnet.
# Testnet keys are kept here only for development convenience.
_TESTNET_KEY    = "YndDHQr6Mpx2i3fElS"
_TESTNET_SECRET = "oGjioa9ZVih7rw1b4dBVMJJ2UkGQZ4LrF5cR"
API_KEY    = os.environ.get("BYBIT_API_KEY",    _TESTNET_KEY)
API_SECRET = os.environ.get("BYBIT_API_SECRET", _TESTNET_SECRET)

# Base symbols — BTCUSDT added at startup when running --mainnet
# (testnet BTC price feed is static so it's excluded from testnet)
SYMBOLS = [
    "XRPUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "ADAUSDT",
    "TRXUSDT",
]

INTERVAL_5M  = "5"
INTERVAL_15M = "15"

# -- DCA Ladder --
#   Index 0 = initial entry, 1 = first DCA, 2 = second DCA
DCA_SIZES   = [60.0,  80.0,  100.0]   # USDT to spend at each level
DCA_DROPS   = [0.000, 0.015, 0.030]   # required % drop from avg entry to trigger level
DCA_RSI_MIN = [0,     30,    25   ]   # minimum RSI allowed when adding each level
MAX_DCA_LEVEL = len(DCA_SIZES) - 1    # = 2 (0-indexed)

# -- Risk --
HARD_STOP_PCT    = 0.05    # sell everything if price drops 5% below initial entry
TAKE_PROFIT_PCT  = 0.02    # 2% above avg entry = take profit
TRAIL_STOP_PCT   = 0.008   # trailing stop kicks in once price passes TP line
PANIC_RSI        = 22      # RSI below this → immediate exit, no questions asked
MAX_POSITIONS    = 2       # max simultaneous DCA positions
DAILY_LOSS_LIMIT = 0.05    # pause for 1h if daily loss hits 5%
MIN_TRADE_USDT   = 5       # minimum coin value to consider a real position
TAKER_FEE        = 0.001   # Bybit spot taker fee (0.1% per side, 0.2% round-trip)

# -- Volatility Filter --
ATR_PERIOD    = 14   # candles for ATR calculation
ATR_MA_PERIOD = 50   # candles for ATR baseline (what's "normal" for this symbol)
ATR_RATIO_MAX = 1.5  # block entry if current ATR > 1.5x its own recent average

# -- Indicators (same as scalp bot for consistency) --
RSI_PERIOD   = 14
RSI_BUY      = 55    # entry RSI threshold
RSI_SELL     = 65    # exit RSI threshold (higher than scalp — DCA can ride more)
BB_PERIOD    = 20
BB_STD       = 2.0
BB_ENTRY     = 0.45  # slightly tighter than scalp bot
BB_EXIT      = 0.55
MACD_FAST    = 12
MACD_SLOW    = 26
MACD_SIG     = 9
EMA_15M_FAST = 20
EMA_15M_SLOW = 50

SLEEP_SEC       = 60   # idle cycle
SLEEP_IN_TRADE  = 3    # fast cycle when holding a position
TRADE_LOG  = "dca_trades.csv"
STATE_FILE = "dca_state.json"
LEARN_FILE = "dca_learning.json"
LEARN_EVERY = 5

# ================================================================

# session, IS_MAINNET, and log file handle are initialised in __main__ after args
session    = None
IS_MAINNET = False
_log_fh    = None

# ================================================================
#  LOGGING
# ================================================================
def log(msg, level="INFO"):
    icons = {"INFO": "   ", "BUY": ">>", "SELL": "<<", "WARN": "!!", "ERR": "XX"}
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {icons.get(level,'  ')} {msg}"
    print(line)
    if _log_fh:
        _log_fh.write(line + "\n")
        _log_fh.flush()


# ================================================================
#  TRADE LOG (CSV)
# ================================================================
def init_trade_log():
    if not os.path.exists(TRADE_LOG):
        with open(TRADE_LOG, "w", newline="") as f:
            csv.writer(f).writerow([
                "datetime", "symbol", "side", "dca_level", "price",
                "qty_usdt", "avg_entry", "pnl_usdt", "pnl_pct", "reason"
            ])

def log_trade(symbol, side, dca_level, price, qty_usdt,
              avg_entry=0.0, pnl=0.0, reason=""):
    pnl_pct = (pnl / qty_usdt * 100) if qty_usdt > 0 else 0.0
    with open(TRADE_LOG, "a", newline="") as f:
        csv.writer(f).writerow([
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            symbol, side, dca_level,
            f"{price:.6f}", f"{qty_usdt:.2f}",
            f"{avg_entry:.6f}", f"{pnl:.4f}", f"{pnl_pct:.2f}", reason
        ])


# ================================================================
#  STATE PERSISTENCE
# ================================================================
def save_state(dca_levels, sl_prices, highest_price, trail_sl,
               bb_mid_at_entry, entry_rsi, entry_bb, entry_macd,
               entry_trend_gap, trade_count, win_count, total_pnl):
    state = {
        "saved_at"   : datetime.now().isoformat(),
        "trade_count": trade_count,
        "win_count"  : win_count,
        "total_pnl"  : total_pnl,
        "positions"  : {},
    }
    for s in SYMBOLS:
        if dca_levels[s]:
            state["positions"][s] = {
                "levels"         : dca_levels[s],
                "sl_price"       : sl_prices[s],
                "highest_price"  : highest_price[s],
                "trail_sl"       : trail_sl[s],
                "bb_mid_at_entry": bb_mid_at_entry[s],
                "entry_rsi"      : entry_rsi[s],
                "entry_bb"       : entry_bb[s],
                "entry_macd"     : entry_macd[s],
                "entry_trend_gap": entry_trend_gap[s],
            }
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def load_state():
    if not os.path.exists(STATE_FILE):
        return None
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception as e:
        log(f"Could not load state: {e}", "WARN")
        return None


def clear_state():
    if os.path.exists(STATE_FILE):
        os.remove(STATE_FILE)


# ================================================================
#  ACCOUNT HELPERS
# ================================================================
def safe_float(val, default=0.0):
    try:
        v = float(val)
        return v if v == v else default   # NaN guard
    except (ValueError, TypeError):
        return default


def get_balance():
    """Returns total USDT + USD value of all held coins."""
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
                price = get_price(f"{coin}USDT")
                if price > 0:
                    total += bal * price
        return round(total, 4)
    except Exception as e:
        log(f"get_balance error: {e}", "ERR")
        return 0.0


def get_usdt_balance():
    """Returns only the available USDT (not coin values)."""
    try:
        resp  = session.get_wallet_balance(accountType="UNIFIED", coin="USDT")
        coins = resp["result"]["list"][0]["coin"]
        for c in coins:
            if c["coin"] == "USDT":
                return safe_float(c.get("availableBalance") or c.get("walletBalance"))
    except Exception:
        pass
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


def round_price(price):
    if price >= 100: return round(price, 2)
    if price >= 1:   return round(price, 4)
    return               round(price, 4)


def round_qty(symbol, qty):
    """Floor-round coin qty to avoid 'too many decimals' sell errors."""
    price = get_price(symbol)
    if price >= 1000: return math.floor(qty * 10000) / 10000  # ETH etc.
    if price >= 10:   return math.floor(qty * 1000)  / 1000   # SOL, BNB
    if price >= 0.1:  return math.floor(qty * 10)    / 10     # XRP, ADA, TRX
    return                   float(math.floor(qty))            # very cheap coins


# ================================================================
#  ORDERS
# ================================================================
def place_buy(symbol, usdt_amount, level, avg_entry, sl_price):
    """Market buy using USDT amount (quoteCoin)."""
    try:
        resp = session.place_order(
            category   = "spot",
            symbol     = symbol,
            side       = "Buy",
            orderType  = "Market",
            qty        = str(usdt_amount),
            marketUnit = "quoteCoin",
        )
        order_id = resp["result"]["orderId"]
        log(f"DCA-BUY  {symbol}  Level={level}  ${usdt_amount:.2f} USDT  "
            f"AvgEntry=${round_price(avg_entry)}  SL=${round_price(sl_price)}  "
            f"ID:{order_id}", "BUY")
        return True
    except Exception as e:
        log(f"DCA-BUY FAILED {symbol} Level={level}: {e}", "ERR")
        return False


def place_sell(symbol, coin_qty, reason="exit"):
    """Market sell all coins."""
    try:
        qty_rounded = round_qty(symbol, coin_qty)
        if qty_rounded <= 0:
            log(f"SELL {symbol}: qty rounds to 0 ({coin_qty:.6f}) — skipping", "WARN")
            return False
        resp = session.place_order(
            category  = "spot",
            symbol    = symbol,
            side      = "Sell",
            orderType = "Market",
            qty       = str(qty_rounded),
        )
        log(f"DCA-SELL  {symbol}  {qty_rounded}  [{reason}]  "
            f"ID:{resp['result']['orderId']}", "SELL")
        return True
    except Exception as e:
        log(f"DCA-SELL FAILED {symbol}: {e}", "ERR")
        return False


# ================================================================
#  INDICATORS
# ================================================================
def get_candles(symbol, interval, limit=120):
    resp = session.get_kline(category="spot", symbol=symbol,
                             interval=interval, limit=limit)
    raw = resp["result"]["list"]
    df  = pd.DataFrame(raw,
                       columns=["timestamp","open","high","low",
                                "close","volume","turnover"])
    df  = df[::-1].reset_index(drop=True)
    for col in ["open","high","low","close","volume"]:
        df[col] = df[col].astype(float)
    return df


def add_indicators(df):
    close = df["close"]

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

    ema_f           = close.ewm(span=MACD_FAST, adjust=False).mean()
    ema_s           = close.ewm(span=MACD_SLOW, adjust=False).mean()
    df["macd"]      = ema_f - ema_s
    df["macd_sig"]  = df["macd"].ewm(span=MACD_SIG, adjust=False).mean()
    df["macd_hist"] = df["macd"] - df["macd_sig"]

    # ATR — true range accounts for gaps between candles
    prev_close      = close.shift(1)
    tr              = pd.concat([
                          df["high"] - df["low"],
                          (df["high"] - prev_close).abs(),
                          (df["low"]  - prev_close).abs(),
                      ], axis=1).max(axis=1)
    df["atr"]       = tr.rolling(ATR_PERIOD).mean()
    # Ratio vs its own baseline — self-calibrating across symbols and testnet/mainnet
    df["atr_ratio"] = df["atr"] / df["atr"].rolling(ATR_MA_PERIOD).mean()

    return df


def get_15m_trend(symbol):
    """EMA20 > EMA50 on 15m = uptrend. Returns (trend_ok, ema_fast, ema_slow)."""
    df = get_candles(symbol, INTERVAL_15M, limit=150)
    if len(df) < EMA_15M_SLOW + 5:
        return None, 0.0, 0.0
    df["ema_fast"] = df["close"].ewm(span=EMA_15M_FAST, adjust=False).mean()
    df["ema_slow"] = df["close"].ewm(span=EMA_15M_SLOW, adjust=False).mean()
    last = df.iloc[-1]
    return last["ema_fast"] > last["ema_slow"], last["ema_fast"], last["ema_slow"]


def calc_avg_entry(levels):
    """Weighted average entry price from DCA levels list."""
    total_coins = sum(lv["coins"] for lv in levels)
    total_usdt  = sum(lv["usdt"]  for lv in levels)
    if total_coins <= 0:
        return 0.0
    return total_usdt / total_coins


def calc_total_coins(levels):
    return sum(lv["coins"] for lv in levels)


def calc_total_usdt(levels):
    return sum(lv["usdt"] for lv in levels)


# ================================================================
#  SIGNALS
# ================================================================
def entry_signal(df, trend_ok, rsi_thresh=None, bb_thresh=None):
    """Initial entry: ATR volatility check first, then trend/BB/RSI/MACD."""
    last = df.iloc[-1]

    # ── 1. VOLATILITY FILTER (highest priority) ───────────────────────
    atr_ratio = safe_float(last.get("atr_ratio", 1.0))
    if atr_ratio > ATR_RATIO_MAX:
        return False, [f"HIGH-VOL ATR x{atr_ratio:.2f} (max {ATR_RATIO_MAX:.1f}x baseline)"]

    _rsi = rsi_thresh if rsi_thresh is not None else RSI_BUY
    _bb  = bb_thresh  if bb_thresh  is not None else BB_ENTRY

    at_lower_bb  = safe_float(last["bb_pct"]) < _bb
    rsi_dip      = safe_float(last["rsi"]) < _rsi
    macd_bullish = safe_float(last["macd_hist"]) > 0

    reasons = []
    if trend_ok:     reasons.append("15m-UP")
    if at_lower_bb:  reasons.append(f"BB={last['bb_pct']:.2f}")
    if rsi_dip:      reasons.append(f"RSI={last['rsi']:.1f}")
    if macd_bullish: reasons.append("MACD+")

    ok = trend_ok and at_lower_bb and rsi_dip and macd_bullish
    return ok, reasons


def is_panic_exit(rsi_val, price, sl_price, trend_ok):
    """
    Returns (should_exit, reason) for any hard-exit condition.
    Checked BEFORE take-profit to ensure safety always wins.
    """
    if rsi_val < PANIC_RSI:
        return True, f"panic_rsi({rsi_val:.1f})"
    if sl_price > 0 and price <= sl_price:
        return True, "hard_stop"
    if not trend_ok:
        return True, "trend_break"
    return False, ""


def is_take_profit(price, avg_entry, tp_pct=TAKE_PROFIT_PCT):
    """Returns True if price has reached the TP target above avg entry."""
    return avg_entry > 0 and price >= avg_entry * (1 + tp_pct)


def is_bb_exit(price, bb_mid_at_entry):
    """Price crossed the BB midline that was recorded at initial entry."""
    return bb_mid_at_entry > 0 and price >= bb_mid_at_entry


def is_indicator_sell(df, trend_ok):
    """RSI overbought, MACD turns bearish, or BB upper zone."""
    last = df.iloc[-1]
    prev = df.iloc[-2]
    rsi_high      = safe_float(last["rsi"]) > RSI_SELL
    macd_bearish  = prev["macd_hist"] > 0 and last["macd_hist"] < 0
    bb_upper_zone = safe_float(last["bb_pct"]) >= BB_EXIT
    return rsi_high or macd_bearish or bb_upper_zone


# ================================================================
#  LEARNING ENGINE (DCA-specific)
# ================================================================
class DcaLearningEngine:
    """
    Tracks DCA trade history and adapts parameters.
    Learns:
      1. Symbols with bad win rates → cooldown
      2. Whether DCA levels help or hurt → tune drop thresholds
      3. Hour avoidance — same as scalp bot
    Stored in dca_learning.json (separate from scalp bot).
    """

    MAX_RAW_TRADES    = 50
    CONSEC_LOSS_PAUSE = 3
    COOLDOWN_MINUTES  = 60

    def __init__(self):
        self.trades       = []
        self.symbol_stats = {s: {"wins": 0, "losses": 0, "consec_losses": 0}
                             for s in SYMBOLS}
        self.avoid_hours  = []
        self.cooldowns    = {}   # symbol -> datetime expiry
        # Learnable thresholds (start at defaults)
        self.dca_drop1    = DCA_DROPS[1]   # 0.015
        self.dca_drop2    = DCA_DROPS[2]   # 0.030
        self.tp_pct       = TAKE_PROFIT_PCT # 0.02
        self._load()

    # ── Persistence ──────────────────────────────────────────────
    def _save(self):
        data = {
            "trades"       : self.trades[-self.MAX_RAW_TRADES:],
            "symbol_stats" : self.symbol_stats,
            "avoid_hours"  : self.avoid_hours,
            "cooldowns"    : {s: t.isoformat() for s, t in self.cooldowns.items()},
            "dca_drop1"    : self.dca_drop1,
            "dca_drop2"    : self.dca_drop2,
            "tp_pct"       : self.tp_pct,
        }
        with open(LEARN_FILE, "w") as f:
            json.dump(data, f, indent=2)

    def _load(self):
        if not os.path.exists(LEARN_FILE):
            return
        try:
            with open(LEARN_FILE) as f:
                data = json.load(f)
            self.trades       = data.get("trades",       [])
            self.symbol_stats = data.get("symbol_stats", self.symbol_stats)
            self.avoid_hours  = data.get("avoid_hours",  [])
            self.dca_drop1    = data.get("dca_drop1",    DCA_DROPS[1])
            self.dca_drop2    = data.get("dca_drop2",    DCA_DROPS[2])
            self.tp_pct       = data.get("tp_pct",       TAKE_PROFIT_PCT)
            now = datetime.now()
            for s, iso in data.get("cooldowns", {}).items():
                expiry = datetime.fromisoformat(iso)
                if expiry > now:
                    self.cooldowns[s] = expiry
            log(f"DCA-LEARN  {len(self.trades)} trades loaded | "
                f"TP={self.tp_pct*100:.1f}%  "
                f"DCA1=-{self.dca_drop1*100:.1f}%  "
                f"DCA2=-{self.dca_drop2*100:.1f}%", "WARN")
        except Exception as e:
            log(f"DCA-LEARN  Could not load: {e}", "WARN")

    # ── Record a completed trade ──────────────────────────────────
    def record(self, symbol, dca_levels_used, exit_reason, pnl_pct,
               entry_rsi, entry_bb):
        win = pnl_pct > 0
        self.trades.append({
            "time"           : datetime.now().isoformat(),
            "symbol"         : symbol,
            "dca_levels_used": dca_levels_used,
            "exit_reason"    : exit_reason,
            "pnl_pct"        : round(pnl_pct, 4),
            "win"            : win,
            "hour"           : datetime.now().hour,
            "entry_rsi"      : round(entry_rsi, 2),
            "entry_bb"       : round(entry_bb, 3),
        })
        self.trades = self.trades[-self.MAX_RAW_TRADES:]

        ss = self.symbol_stats.setdefault(
            symbol, {"wins": 0, "losses": 0, "consec_losses": 0})
        if win:
            ss["wins"]         += 1
            ss["consec_losses"] = 0
        else:
            ss["losses"]       += 1
            ss["consec_losses"] = ss.get("consec_losses", 0) + 1

        self._save()

        if len(self.trades) % LEARN_EVERY == 0:
            self._adapt()

    # ── Adapt parameters ─────────────────────────────────────────
    def _adapt(self):
        recent = self.trades
        if len(recent) < LEARN_EVERY:
            return

        wins   = [t for t in recent if t["win"]]
        losses = [t for t in recent if not t["win"]]
        wr     = len(wins) / len(recent)
        changes = []

        # 1. TP% — if most losses exit via hard_stop or panic before TP,
        #    lower TP slightly to capture more wins
        stop_exits = [t for t in losses
                      if t["exit_reason"] in ("hard_stop", "panic_rsi", "trend_break")]
        if len(stop_exits) > len(losses) * 0.6 and self.tp_pct > 0.012:
            new_tp = round(self.tp_pct - 0.002, 3)
            new_tp = max(new_tp, 0.012)   # floor at 1.2%
            if new_tp != self.tp_pct:
                changes.append(f"TP {self.tp_pct*100:.1f}%→{new_tp*100:.1f}% "
                                f"(too many stop exits)")
                self.tp_pct = new_tp

        # 2. If TP is consistently hit and WR > 70%, raise TP to capture more
        tp_exits = [t for t in wins if t["exit_reason"] == "take_profit"]
        if wr > 0.70 and len(recent) >= 10 and self.tp_pct < 0.035:
            new_tp = round(self.tp_pct + 0.002, 3)
            new_tp = min(new_tp, 0.035)   # cap at 3.5%
            if new_tp != self.tp_pct:
                changes.append(f"TP {self.tp_pct*100:.1f}%→{new_tp*100:.1f}% "
                                f"(WR={wr:.0%} strong)")
                self.tp_pct = new_tp

        # 3. DCA drops — if level-2 trades (3 buys) mostly lose, widen the
        #    drop trigger so we only add when price has fallen far enough
        level2_trades = [t for t in recent if t["dca_levels_used"] >= 3]
        if len(level2_trades) >= 3:
            level2_wr = sum(1 for t in level2_trades if t["win"]) / len(level2_trades)
            if level2_wr < 0.40 and self.dca_drop2 < 0.05:
                new_drop2 = round(self.dca_drop2 + 0.005, 3)
                changes.append(f"DCA2 drop -{self.dca_drop2*100:.1f}%"
                                f"→-{new_drop2*100:.1f}% "
                                f"(level-2 WR={level2_wr:.0%})")
                self.dca_drop2 = new_drop2

        # 4. Hour avoidance
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
                    changes.append(f"Avoid hours {bad_hours} (WR<25%)")
                    self.avoid_hours = new_avoid
            recovered = [h for h in self.avoid_hours
                         if (s := hour_stats.get(h)) and
                            s["total"] >= 3 and s["wins"] / s["total"] >= 0.50]
            if recovered:
                self.avoid_hours = [h for h in self.avoid_hours
                                    if h not in recovered]
                changes.append(f"Restored hours {recovered} (WR≥50%)")

        self._save()

        if changes:
            log("─" * 70)
            log(f"DCA-LEARN  {len(recent)} trades | WR={wr:.0%} | "
                f"{len(changes)} adjustments:", "WARN")
            for c in changes:
                log(f"DCA-LEARN    • {c}", "WARN")
            log("─" * 70)
        else:
            log(f"DCA-LEARN  WR={wr:.0%} over {len(recent)} trades — stable", "WARN")

    # ── Symbol cooldown ───────────────────────────────────────────
    def is_symbol_banned(self, symbol):
        s   = self.symbol_stats.get(symbol, {})
        net = s.get("losses", 0) - s.get("wins", 0)
        cl  = s.get("consec_losses", 0)

        needs_cooldown = net >= 5 or cl >= self.CONSEC_LOSS_PAUSE
        if not needs_cooldown:
            self.cooldowns.pop(symbol, None)
            return False

        now    = datetime.now()
        expiry = self.cooldowns.get(symbol)
        if expiry is None:
            self.cooldowns[symbol] = now + timedelta(minutes=self.COOLDOWN_MINUTES)
            self._save()
            log(f"  {symbol}: DCA cooldown started — "
                f"pausing {self.COOLDOWN_MINUTES}min", "WARN")
            return True

        if now >= expiry:
            log(f"  {symbol}: DCA cooldown over — resetting", "WARN")
            s["consec_losses"] = 0
            self.cooldowns.pop(symbol, None)
            self._save()
            return False

        mins_left = int((expiry - now).total_seconds() / 60)
        log(f"  {symbol:<10} DCA COOLDOWN {mins_left}min left", "WARN")
        return True

    def should_skip_hour(self):
        return datetime.now().hour in self.avoid_hours

    def summary(self):
        parts = []
        if self.trades:
            recent = self.trades[-20:]
            wr  = sum(1 for t in recent if t["win"]) / len(recent)
            avg = sum(t["pnl_pct"] for t in recent) / len(recent)
            parts.append(f"Recent {len(recent)}: WR={wr:.0%} AvgPnL={avg:+.2f}%")
        parts.append(
            f"TP={self.tp_pct*100:.1f}%  "
            f"DCA1=-{self.dca_drop1*100:.1f}%  "
            f"DCA2=-{self.dca_drop2*100:.1f}%")
        if self.avoid_hours:
            parts.append(f"AvoidHrs:{self.avoid_hours}")
        return " | ".join(parts)


# ================================================================
#  STARTUP SWEEP
# ================================================================
def sweep_coins_to_usdt(protected_coins=None):
    """Sell any leftover non-USDT coins back to USDT at startup."""
    protected_coins = protected_coins or set()
    MIN_SWEEP_USD   = 1.0

    log("SWEEP  Checking for non-USDT balances...", "WARN")
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
        if coin in protected_coins:
            log(f"SWEEP  {coin:<6} — skipped (active DCA position)", "WARN")
            continue
        symbol    = f"{coin}USDT"
        price     = get_price(symbol)
        if price <= 0:
            log(f"SWEEP  {coin:<6} — skipped (no price)", "WARN")
            continue
        usd_value = bal * price
        if usd_value < MIN_SWEEP_USD:
            log(f"SWEEP  {coin:<6} ~${usd_value:.4f} — dust, skipping", "WARN")
            continue
        sell_qty = round_qty(symbol, bal)
        if sell_qty <= 0:
            log(f"SWEEP  {coin:<6} rounds to 0 — skipping", "WARN")
            continue
        log(f"SWEEP  {coin:<6} {bal:.4f} (~${usd_value:.2f}) → selling", "WARN")
        try:
            resp2 = session.place_order(
                category="spot", symbol=symbol, side="Sell",
                orderType="Market", qty=str(sell_qty),
            )
            log(f"SWEEP  {coin:<6} sold | "
                f"order {resp2['result']['orderId']}", "WARN")
            swept_any = True
            time.sleep(0.4)
        except Exception as e:
            log(f"SWEEP  {coin:<6} sell failed: {e}", "ERR")

    if swept_any:
        time.sleep(1.5)
        log(f"SWEEP  Done. Balance: ${get_balance():.2f}", "WARN")
    else:
        log("SWEEP  Nothing to sweep — wallet clean.", "WARN")


# ================================================================
#  MAIN LOOP
# ================================================================
def run_bot():
    init_trade_log()
    learner = DcaLearningEngine()

    mode_label = "MAINNET ⚠️  REAL MONEY" if IS_MAINNET else "TESTNET"
    log("=" * 70)
    log(f"  DCA TRADING BOT — Bybit {mode_label}")
    log(f"  Pairs      : {', '.join(SYMBOLS)}")
    log(f"  DCA Ladder : ${DCA_SIZES[0]:.0f} → ${DCA_SIZES[1]:.0f} → "
        f"${DCA_SIZES[2]:.0f}  (max ${sum(DCA_SIZES):.0f} per symbol)")
    log(f"  Triggers   : Entry→signal  "
        f"DCA1→-{DCA_DROPS[1]*100:.1f}%  DCA2→-{DCA_DROPS[2]*100:.1f}%")
    log(f"  Hard Stop  : -{HARD_STOP_PCT*100:.1f}% below initial entry")
    log(f"  Take Profit: +{TAKE_PROFIT_PCT*100:.1f}% above avg entry")
    log(f"  Panic RSI  : < {PANIC_RSI}")
    log(f"  Max Pos    : {MAX_POSITIONS}")
    log(f"  State file : {STATE_FILE}")
    log("=" * 70)

    # Per-symbol DCA state
    # dca_levels[s] = list of {"price": float, "usdt": float, "coins": float}
    dca_levels      = {s: [] for s in SYMBOLS}
    sl_prices       = {s: 0.0 for s in SYMBOLS}  # hard stop at initial entry - 5%
    highest_price   = {s: 0.0 for s in SYMBOLS}  # for trailing stop
    trail_sl        = {s: 0.0 for s in SYMBOLS}  # trailing stop level
    bb_mid_at_entry = {s: 0.0 for s in SYMBOLS}  # BB midline at initial entry
    # For learning
    entry_rsi       = {s: 0.0 for s in SYMBOLS}
    entry_bb        = {s: 0.0 for s in SYMBOLS}
    entry_macd      = {s: 0.0 for s in SYMBOLS}
    entry_trend_gap = {s: 0.0 for s in SYMBOLS}

    trade_count  = 0
    win_count    = 0
    total_pnl    = 0.0
    daily_pnl    = 0.0
    error_streak = 0

    # ── Recover state from previous run ──────────────────────────
    saved = load_state()
    if saved:
        trade_count = saved.get("trade_count", 0)
        win_count   = saved.get("win_count",   0)
        total_pnl   = saved.get("total_pnl",   0.0)
        for s, pos in saved.get("positions", {}).items():
            if s not in SYMBOLS:
                continue
            dca_levels[s]      = pos.get("levels", [])
            sl_prices[s]       = pos.get("sl_price", 0.0)
            highest_price[s]   = pos.get("highest_price", 0.0)
            trail_sl[s]        = pos.get("trail_sl", 0.0)
            bb_mid_at_entry[s] = pos.get("bb_mid_at_entry", 0.0)
            entry_rsi[s]       = pos.get("entry_rsi", 0.0)
            entry_bb[s]        = pos.get("entry_bb", 0.0)
            entry_macd[s]      = pos.get("entry_macd", 0.0)
            entry_trend_gap[s] = pos.get("entry_trend_gap", 0.0)
            avg = calc_avg_entry(dca_levels[s])
            log(f"  Recovered DCA pos: {s} | Levels={len(dca_levels[s])} | "
                f"AvgEntry=${avg:.4f} | SL=${sl_prices[s]:.4f}", "WARN")

    # Sweep leftover coins (skip symbols with active DCA positions)
    protected = {s.replace("USDT", "") for s in SYMBOLS if dca_levels[s]}
    sweep_coins_to_usdt(protected_coins=protected)

    starting_bal = get_balance()
    daily_limit  = starting_bal * DAILY_LOSS_LIMIT
    log(f"  Starting balance: ${starting_bal:.2f} USDT")
    log("=" * 70)

    while True:
        try:
            usdt_bal       = get_usdt_balance()
            total_bal      = get_balance()
            open_positions = sum(1 for s in SYMBOLS if dca_levels[s])

            # ── Circuit Breaker ───────────────────────────────────
            if daily_pnl <= -daily_limit:
                log(f"CIRCUIT BREAKER: Daily loss ${daily_pnl:.2f} "
                    f"hit ${daily_limit:.2f} limit. Pausing 1h.", "WARN")
                time.sleep(3600)
                daily_pnl   = 0.0
                daily_limit = get_balance() * DAILY_LOSS_LIMIT
                continue

            wr = int(win_count / trade_count * 100) if trade_count else 0
            log(f"Balance: ${total_bal:.2f}  |  USDT: ${usdt_bal:.2f}  |  "
                f"Open: {open_positions}/{MAX_POSITIONS}  |  "
                f"Trades: {trade_count}  |  Win: {wr}%  |  "
                f"PnL: {'+'if total_pnl>=0 else ''}${total_pnl:.2f}")
            log(f"DCA-LEARN  {learner.summary()}", "WARN")
            log("─" * 70)

            for symbol in SYMBOLS:
                try:
                    if learner.is_symbol_banned(symbol):
                        log(f"  {symbol:<10} SKIPPED — DCA cooldown active", "WARN")
                        continue

                    df = add_indicators(get_candles(symbol, INTERVAL_5M))

                    min_rows = BB_PERIOD + RSI_PERIOD + 5
                    if len(df) < min_rows:
                        log(f"  {symbol:<10} skipping — only {len(df)} candles", "WARN")
                        continue

                    last = df.iloc[-1]

                    # Validate indicators
                    rsi_val    = safe_float(last["rsi"], default=-1)
                    bb_pct_val = safe_float(last["bb_pct"], default=-1)
                    if math.isnan(last["rsi"]) or math.isnan(last["bb_pct"]) \
                            or rsi_val < 5:
                        log(f"  {symbol:<10} skipping — invalid indicators", "WARN")
                        continue

                    price      = get_price(symbol)
                    macd_hist  = safe_float(last["macd_hist"])
                    macd_sign  = "+" if macd_hist > 0 else ""

                    trend_ok, ema_f, ema_s = get_15m_trend(symbol)
                    if trend_ok is None:
                        log(f"  {symbol:<10} skipping — not enough 15m candles", "WARN")
                        continue

                    trend_gap  = ema_f - ema_s
                    trend_icon = "▲" if trend_ok else "▼"

                    # Is this symbol currently in a DCA position?
                    coin_qty = get_position_qty(symbol)
                    in_pos   = (len(dca_levels[symbol]) > 0 and
                                (coin_qty * price) >= MIN_TRADE_USDT)

                    # ── Handle stale state (position disappeared externally) ─
                    if not in_pos and dca_levels[symbol]:
                        log(f"  {symbol}: DCA position gone from wallet — "
                            f"clearing state", "WARN")
                        # Estimate PnL from last known data
                        avg = calc_avg_entry(dca_levels[symbol])
                        tot = calc_total_usdt(dca_levels[symbol])
                        pnl = (price * (1 - TAKER_FEE) -
                               avg * (1 + TAKER_FEE)) * calc_total_coins(dca_levels[symbol]) \
                              if avg > 0 else 0.0
                        total_pnl   += pnl
                        trade_count += 1
                        if pnl > 0:
                            win_count += 1
                        dca_levels[symbol]      = []
                        sl_prices[symbol]       = 0.0
                        highest_price[symbol]   = 0.0
                        trail_sl[symbol]        = 0.0
                        bb_mid_at_entry[symbol] = 0.0
                        save_state(dca_levels, sl_prices, highest_price,
                                   trail_sl, bb_mid_at_entry,
                                   entry_rsi, entry_bb, entry_macd,
                                   entry_trend_gap, trade_count,
                                   win_count, total_pnl)
                        continue

                    # ── Status display ────────────────────────────
                    if in_pos:
                        avg_entry    = calc_avg_entry(dca_levels[symbol])
                        total_coins  = calc_total_coins(dca_levels[symbol])
                        total_usdt_i = calc_total_usdt(dca_levels[symbol])
                        # Fee-adjusted P&L: buy paid avg*(1+fee), sell gets price*(1-fee)
                        pnl_pct      = (price * (1 - TAKER_FEE) /
                                        (avg_entry * (1 + TAKER_FEE)) - 1) \
                                       if avg_entry > 0 else 0.0
                        pnl_usd      = (price * (1 - TAKER_FEE) -
                                        avg_entry * (1 + TAKER_FEE)) * total_coins
                        pnl_sign     = "+" if pnl_usd >= 0 else ""
                        pnl_icon     = "▲" if pnl_usd >= 0 else "▼"
                        lv_count     = len(dca_levels[symbol])

                        log(f"  {symbol:<10} ${price:>12,.4f}  "
                            f"RSI={rsi_val:4.1f}  BB={bb_pct_val:.2f}  "
                            f"MACD={macd_sign}{macd_hist:.4f}  "
                            f"[IN DCA Lv{lv_count}]  {trend_icon}")
                        exit_label = (f"Trail=${trail_sl[symbol]:.4f} 🎯"
                                      if trail_sl[symbol] > 0
                                      else f"TP=${avg_entry*(1+learner.tp_pct):.4f}")
                        log(f"           AvgEntry=${avg_entry:.4f}  "
                            f"SL=${sl_prices[symbol]:.4f}  "
                            f"{exit_label}  "
                            f"Invested=${total_usdt_i:.2f}  "
                            f"P&L={pnl_sign}${abs(pnl_usd):.2f} "
                            f"({pnl_pct*100:+.2f}%) {pnl_icon}")
                    else:
                        # Not in position — show entry conditions
                        L_RSI = learner.symbol_stats.get(symbol, {})
                        log(f"  {symbol:<10} ${price:>12,.4f}  "
                            f"RSI={rsi_val:4.1f}  BB={bb_pct_val:.2f}  "
                            f"MACD={macd_sign}{macd_hist:.4f}  "
                            f"[waiting]  {trend_icon}")

                    # ── UPDATE TRAILING HIGH ──────────────────────
                    if in_pos and price > highest_price[symbol]:
                        highest_price[symbol] = price

                    # ═══════════════════════════════════════════════
                    #  MANAGE OPEN POSITION
                    # ═══════════════════════════════════════════════
                    if in_pos:
                        avg_entry   = calc_avg_entry(dca_levels[symbol])
                        total_coins = calc_total_coins(dca_levels[symbol])

                        # Dust guard
                        sellable = round_qty(symbol, coin_qty)
                        if sellable <= 0:
                            log(f"  {symbol}: Dust position — clearing state", "WARN")
                            dca_levels[symbol]      = []
                            sl_prices[symbol]       = 0.0
                            highest_price[symbol]   = 0.0
                            trail_sl[symbol]        = 0.0
                            bb_mid_at_entry[symbol] = 0.0
                            save_state(dca_levels, sl_prices, highest_price,
                                       trail_sl, bb_mid_at_entry,
                                       entry_rsi, entry_bb, entry_macd,
                                       entry_trend_gap, trade_count,
                                       win_count, total_pnl)
                            continue

                        def close_position(reason):
                            """Sell all coins and record the trade."""
                            nonlocal trade_count, win_count, total_pnl, daily_pnl
                            actual_qty = get_position_qty(symbol)
                            sell_qty   = round_qty(symbol, actual_qty)
                            if sell_qty <= 0:
                                log(f"    {reason.upper()} | wallet dust "
                                    f"({actual_qty:.6f}) — clearing state", "WARN")
                                dca_levels[symbol]      = []
                                sl_prices[symbol]       = 0.0
                                highest_price[symbol]   = 0.0
                                trail_sl[symbol]        = 0.0
                                bb_mid_at_entry[symbol] = 0.0
                                save_state(dca_levels, sl_prices, highest_price,
                                           trail_sl, bb_mid_at_entry,
                                           entry_rsi, entry_bb, entry_macd,
                                           entry_trend_gap, trade_count,
                                           win_count, total_pnl)
                                return
                            if place_sell(symbol, sell_qty, reason=reason):
                                avg   = calc_avg_entry(dca_levels[symbol])
                                tot_u = calc_total_usdt(dca_levels[symbol])
                                # Fee-adjusted: buy paid avg*(1+fee), sell nets price*(1-fee)
                                pnl   = (price * (1 - TAKER_FEE) -
                                         avg * (1 + TAKER_FEE)) * sell_qty \
                                        if avg > 0 else 0.0
                                ppct  = (price * (1 - TAKER_FEE) /
                                         (avg * (1 + TAKER_FEE)) - 1) * 100 \
                                        if avg > 0 else 0.0
                                total_pnl   += pnl
                                daily_pnl   += pnl
                                trade_count += 1
                                if pnl > 0:
                                    win_count += 1
                                sign = "+" if pnl >= 0 else ""
                                log(f"    {reason.upper()} | "
                                    f"AvgEntry=${avg:.4f}  "
                                    f"ExitPrice=${price:.4f}  "
                                    f"P&L: {sign}${pnl:.2f} ({ppct:+.1f}%)",
                                    "SELL")
                                log_trade(symbol, "SELL",
                                          len(dca_levels[symbol]), price,
                                          tot_u, avg, pnl, reason)
                                learner.record(
                                    symbol           = symbol,
                                    dca_levels_used  = len(dca_levels[symbol]),
                                    exit_reason      = reason,
                                    pnl_pct          = ppct,
                                    entry_rsi        = entry_rsi[symbol],
                                    entry_bb         = entry_bb[symbol],
                                )
                                dca_levels[symbol]      = []
                                sl_prices[symbol]       = 0.0
                                highest_price[symbol]   = 0.0
                                trail_sl[symbol]        = 0.0
                                bb_mid_at_entry[symbol] = 0.0
                                save_state(dca_levels, sl_prices, highest_price,
                                           trail_sl, bb_mid_at_entry,
                                           entry_rsi, entry_bb, entry_macd,
                                           entry_trend_gap, trade_count,
                                           win_count, total_pnl)

                        # ── 1. PANIC / HARD STOP / TREND BREAK ───
                        # These are non-negotiable — exit immediately
                        should_panic, panic_reason = is_panic_exit(
                            rsi_val, price, sl_prices[symbol], trend_ok)
                        if should_panic:
                            log(f"    !! EMERGENCY EXIT [{panic_reason}] "
                                f"@ ${price:.4f}", "SELL")
                            close_position(panic_reason)

                        # ── 2. TRAILING STOP ──────────────────────────
                        # Update: only raise the trail when price is above TP
                        elif avg_entry > 0 and highest_price[symbol] > 0:
                            if price >= avg_entry * (1 + learner.tp_pct):
                                new_trail = highest_price[symbol] * (1 - TRAIL_STOP_PCT)
                                if trail_sl[symbol] == 0.0:
                                    trail_sl[symbol] = new_trail
                                    log(f"    Trailing stop activated @ "
                                        f"${trail_sl[symbol]:.4f}", "INFO")
                                elif new_trail > trail_sl[symbol]:
                                    trail_sl[symbol] = new_trail
                                    log(f"    Trail SL → ${trail_sl[symbol]:.4f}",
                                        "INFO")
                            # Trigger: check ALWAYS once trail is set, regardless of TP
                            if trail_sl[symbol] > 0 and price <= trail_sl[symbol]:
                                log(f"    TRAIL STOP hit @ ${price:.4f} "
                                    f"(trail=${trail_sl[symbol]:.4f})", "SELL")
                                close_position("trail_stop")

                        # ── 3. TAKE PROFIT ────────────────────────
                        elif is_take_profit(price, avg_entry,
                                            learner.tp_pct):
                            log(f"    TAKE PROFIT @ ${price:.4f} "
                                f"(AvgEntry=${avg_entry:.4f} "
                                f"+{learner.tp_pct*100:.1f}%)", "SELL")
                            close_position("take_profit")

                        # ── 4. BB MIDLINE TARGET ──────────────────
                        elif is_bb_exit(price, bb_mid_at_entry[symbol]):
                            log(f"    BB MIDLINE hit @ ${price:.4f} "
                                f"(target ${bb_mid_at_entry[symbol]:.4f})", "SELL")
                            close_position("bb_midline")

                        # ── 5. DCA — ADD TO POSITION ──────────────
                        # Checked BEFORE sell signal: if DCA conditions are
                        # met we add to position rather than exiting. Sell
                        # signal only fires when there is no DCA opportunity.
                        elif dca_levels[symbol]:
                            current_level = len(dca_levels[symbol]) - 1
                            next_level    = current_level + 1

                            if next_level <= MAX_DCA_LEVEL:
                                required_drop = learner.dca_drop1 \
                                    if next_level == 1 else learner.dca_drop2
                                required_rsi  = DCA_RSI_MIN[next_level]
                                drop_from_avg = (avg_entry - price) / avg_entry \
                                    if avg_entry > 0 else 0.0

                                if (drop_from_avg >= required_drop and
                                        rsi_val >= required_rsi and
                                        trend_ok):
                                    dca_size = DCA_SIZES[next_level]
                                    if usdt_bal < dca_size:
                                        log(f"  {symbol}: DCA Level {next_level} "
                                            f"signal but insufficient USDT "
                                            f"(need ${dca_size:.0f}, "
                                            f"have ${usdt_bal:.2f})", "WARN")
                                    else:
                                        log(f"  {symbol}: DCA Level {next_level} "
                                            f"triggered | Drop={drop_from_avg*100:.2f}% "
                                            f"RSI={rsi_val:.1f}", "BUY")
                                        # Estimate coins received (approx — actual
                                        # balance check happens at sell time)
                                        coins_est = dca_size / price
                                        if place_buy(symbol, dca_size,
                                                     next_level,
                                                     avg_entry, sl_prices[symbol]):
                                            dca_levels[symbol].append({
                                                "price": price,
                                                "usdt" : dca_size,
                                                "coins": coins_est,
                                            })
                                            usdt_bal -= dca_size
                                            new_avg   = calc_avg_entry(
                                                dca_levels[symbol])
                                            log(f"    New avg entry: "
                                                f"${new_avg:.4f}  "
                                                f"Total invested: "
                                                f"${calc_total_usdt(dca_levels[symbol]):.2f}",
                                                "INFO")
                                            log_trade(symbol, "BUY",
                                                      next_level, price,
                                                      dca_size, new_avg,
                                                      reason=f"dca_level_{next_level}")
                                            save_state(
                                                dca_levels, sl_prices,
                                                highest_price, trail_sl,
                                                bb_mid_at_entry,
                                                entry_rsi, entry_bb,
                                                entry_macd, entry_trend_gap,
                                                trade_count, win_count,
                                                total_pnl)
                                        # Small delay to let fill register
                                        time.sleep(0.5)
                                else:
                                    drop_pct = drop_from_avg * 100
                                    log(f"           DCA-{next_level} waiting: "
                                        f"drop={drop_pct:.2f}% "
                                        f"(need {required_drop*100:.1f}%)  "
                                        f"RSI={rsi_val:.1f} "
                                        f"(need >{required_rsi})", "INFO")
                                    # No DCA opportunity — now check sell signal
                                    if is_indicator_sell(df, trend_ok):
                                        log(f"    SELL SIGNAL (no DCA pending) | "
                                            f"RSI={rsi_val:.1f}  BB={bb_pct_val:.2f}",
                                            "SELL")
                                        close_position("signal")

                            else:
                                # Already at max DCA level — allow sell signal
                                if is_indicator_sell(df, trend_ok):
                                    log(f"    SELL SIGNAL (max DCA reached) | "
                                        f"RSI={rsi_val:.1f}  BB={bb_pct_val:.2f}",
                                        "SELL")
                                    close_position("signal")

                    # ═══════════════════════════════════════════════
                    #  LOOK FOR NEW ENTRY
                    # ═══════════════════════════════════════════════
                    elif open_positions < MAX_POSITIONS:
                        if learner.should_skip_hour():
                            log(f"  {symbol}: skip — "
                                f"hour {datetime.now().hour:02d}:xx avoided", "WARN")
                        else:
                            signal_ok, reasons = entry_signal(df, trend_ok)
                            if not signal_ok and reasons and \
                                    reasons[0].startswith("HIGH-VOL"):
                                log(f"  {symbol}: SKIP — {reasons[0]}", "WARN")
                            if signal_ok:
                                # Ensure enough USDT for initial buy
                                init_size = DCA_SIZES[0]
                                if usdt_bal < init_size:
                                    log(f"  {symbol}: signal but need "
                                        f"${init_size:.0f} USDT "
                                        f"(have ${usdt_bal:.2f})", "WARN")
                                else:
                                    initial_sl = price * (1 - HARD_STOP_PCT)
                                    tp_target  = price * (1 + TAKE_PROFIT_PCT)
                                    coins_est  = init_size / price
                                    log(f"    DCA ENTRY [{' | '.join(reasons)}] | "
                                        f"${init_size:.2f}  "
                                        f"SL=${initial_sl:.4f}  "
                                        f"TP≈${tp_target:.4f}", "BUY")
                                    if place_buy(symbol, init_size,
                                                 0, price, initial_sl):
                                        dca_levels[symbol] = [{
                                            "price": price,
                                            "usdt" : init_size,
                                            "coins": coins_est,
                                        }]
                                        sl_prices[symbol]       = initial_sl
                                        highest_price[symbol]   = price
                                        trail_sl[symbol]        = 0.0
                                        bb_mid_at_entry[symbol] = safe_float(
                                            last["bb_mid"])
                                        entry_rsi[symbol]       = rsi_val
                                        entry_bb[symbol]        = bb_pct_val
                                        entry_macd[symbol]      = macd_hist
                                        entry_trend_gap[symbol] = trend_gap
                                        open_positions         += 1
                                        usdt_bal               -= init_size
                                        log_trade(symbol, "BUY", 0, price,
                                                  init_size, price,
                                                  reason="dca_level_0")
                                        save_state(
                                            dca_levels, sl_prices,
                                            highest_price, trail_sl,
                                            bb_mid_at_entry,
                                            entry_rsi, entry_bb,
                                            entry_macd, entry_trend_gap,
                                            trade_count, win_count, total_pnl)
                                        # Small delay to let fill register
                                        time.sleep(0.5)

                except Exception as e:
                    log(f"  {symbol}: {e}", "ERR")
                    import traceback
                    traceback.print_exc()
                    continue

            log("─" * 70)
            error_streak = 0

        except KeyboardInterrupt:
            log("\nDCA Bot stopped by user.")
            log(f"Final: Trades={trade_count} | Wins={win_count} | "
                f"PnL=${total_pnl:.2f}")
            break
        except Exception as e:
            error_streak += 1
            wait = min(30 * (2 ** error_streak), 300)
            log(f"Error (streak={error_streak}): {e} — retry in {wait}s", "ERR")
            time.sleep(wait)
            continue

        any_open = any(dca_levels[s] for s in SYMBOLS)
        time.sleep(SLEEP_IN_TRADE if any_open else SLEEP_SEC)


# ================================================================
#  ENTRY POINT
# ================================================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Bybit DCA Bot — Spot")
    parser.add_argument("--mainnet", action="store_true",
                        help="Connect to Bybit MAINNET (requires BYBIT_API_KEY / "
                             "BYBIT_API_SECRET env vars)")
    parser.add_argument("--reset", action="store_true",
                        help="Clear saved state and trade log before starting")
    args = parser.parse_args()

    # ── Initialise globals that depend on --mainnet flag ──────────────
    IS_MAINNET = args.mainnet

    if IS_MAINNET:
        key    = os.environ.get("BYBIT_API_KEY")
        secret = os.environ.get("BYBIT_API_SECRET")
        if not key or not secret:
            print("ERROR: --mainnet requires BYBIT_API_KEY and BYBIT_API_SECRET "
                  "environment variables to be set.")
            raise SystemExit(1)
        API_KEY    = key
        API_SECRET = secret
        SYMBOLS.append("BTCUSDT")

    session = HTTP(testnet=not IS_MAINNET, api_key=API_KEY, api_secret=API_SECRET)

    log_filename = "dca_mainnet.log" if IS_MAINNET else "dca_testnet.log"
    _log_fh = open(log_filename, "a", encoding="utf-8")

    if args.reset:
        clear_state()
        if os.path.exists(TRADE_LOG):
            os.remove(TRADE_LOG)
        log("Reset done: DCA state + trade log cleared.", "WARN")

    try:
        run_bot()
    finally:
        _log_fh.close()
