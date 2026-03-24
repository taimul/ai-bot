# Bybit Spot Trading Bot — Project Notes

## Overview
Multi-pair Bollinger Band mean-reversion scalp bot running on Bybit **Testnet** (spot).
Strategy: dual-timeframe (15m trend + 5m entry). No leverage.

**Pairs:** ETHUSDT, SOLUSDT, XRPUSDT, ADAUSDT, TRXUSDT
**Files:**
- `bybit_bot.py` — main bot
- `learning.json` — learned parameters + daily summaries (auto-managed, stays ~35KB forever)
- `bot_state.json` — open position recovery (auto-managed)
- `trades.csv` — full trade log
- `sweep_to_usdt.py` — manual sweep utility (run anytime outside bot)

---

## Trading Style
Moderately aggressive entry (BB_ENTRY=0.50, RSI_BUY=55), conservative risk management.
- Risk per trade: 2% of balance
- Stop loss: 1.5% | Take profit: 3% | Trailing stop: 0.8%
- Max simultaneous positions: 6
- Daily loss circuit breaker: 5%
- Max order size: $200 USDT hard cap
- Spot only — no leverage, max loss capped at position size

---

## What Has Been Implemented

### 1. Learning Engine v2 (upgraded from v1)
**File:** `bybit_bot.py` — `LearningEngine` class

**What it learns:**
| # | What | How | Trigger |
|---|------|-----|---------|
| 1 | RSI threshold | Tightens when losses happen at higher RSI than wins | Every 5 trades |
| 2 | BB threshold | Tightens when losses happen mid-band; relaxes on win streaks | Every 5 trades |
| 3 | MACD minimum | Raises floor when weak-MACD entries keep losing | Every 5 trades |
| 4 | Trend gap minimum | Raises when wins have much stronger EMA separation than losses | Every 5 trades |
| 5 | Per-symbol RSI/BB | Each pair tuned independently from its own trade history | Every 5 trades |
| 6 | Hour avoidance | Flags hours with <25% WR over 3+ trades; auto-recovers when WR >= 50% | Every 5 trades (needs 20+) |
| 7 | Consecutive losses | Symbol paused after 3 straight losses; resets on next win | Every trade |
| 8 | Cross-day pattern | Extra tightening after multiple bad days in a row | Every 5 trades (needs 3+ days) |

**File size permanently bounded:**
- 50 raw trades × ~200B = ~10KB
- 60 daily summaries × ~400B = ~24KB
- params + stats = ~1KB
- **Total max: ~35KB forever** (old system grew unbounded)

**Daily compression:** At first trade after midnight, previous day's raw trades are compressed into one summary row stored in `daily_summaries[]`. Raw trades trimmed to last 50.

**Bug fixed:** Old `buy_signal()` used hardcoded config constants — learning was adjusting params but actual entries still used the original values. Now `buy_signal()` accepts `rsi_thresh`, `bb_thresh`, `macd_min` and the main loop passes learned params directly.

---

### 2. Per-Symbol Parameters
Each symbol gets its own RSI/BB thresholds tuned from its own trade history.
Main loop uses `learner.get_symbol_params(symbol)` instead of global params.
Falls back to global learned params if symbol has insufficient data (<5 trades).

---

### 3. Trend Gap Filter (new parameter)
New learned param: `TREND_GAP_MIN` (EMA20 - EMA50 minimum separation).
If trend is technically "up" but EMA gap is tiny (weak trend), entry is skipped.
Displayed as `✓ Gap` / `✗ Gap` in the terminal status lines.

---

### 4. Hour-of-Day Avoidance
Bot tracks win rate per hour of day.
Hours with <25% WR over 3+ trades are added to `avoid_hours` list.
Hours are removed from the list when WR recovers to ≥50%.
Entries (not position management) are skipped during avoided hours.

---

### 5. Sell Quantity Bug Fix
**Problem:** `close_trade()` was calculating sell qty as `entry_usdt / entry_price` mathematically. But Bybit deducts fees from the coins received on buy, so the actual wallet balance is slightly less than the math says. This caused "Insufficient balance" errors on sells.

**Fix:** `close_trade()` now calls `get_position_qty(symbol)` fresh right before selling, then floors it with `round_qty()`. Sells exactly what the wallet has.

---

### 6. `round_qty()` Precision Fix
Old version used 2 decimal places for coins above $100, which caused SOL ($91) and ETH small balances to round to 0.

**New tiers:**
```
price >= 1000  →  4 decimal places  (ETH at $2000+)
price >= 10    →  3 decimal places  (SOL, BNB)
price >= 1     →  1 decimal place   (XRP, ADA, TRX)
< 1            →  integer           (very cheap coins)
```

---

### 7. Total Portfolio Balance (`get_balance()`)
Old `get_balance()` only fetched USDT. If ADA/SOL/ETH were sitting in wallet, they were invisible to position sizing and risk calculations.

**Fix:** `get_balance()` now fetches all coins, converts each to USD via live price, and returns true total portfolio value. Position sizing is based on real capital.

---

### 8. Startup Sweep (auto)
On every bot startup, before trading begins:
1. Fetches all non-USDT coin balances
2. Skips coins with active recovered positions (bot crashed mid-trade)
3. Skips dust under $1
4. Sells everything else to USDT in chunks ≤ MAX_ORDER_USDT (respects per-order exchange limits)
5. Waits 1.5s for fills to settle, then starts trading with 100% liquid USDT

This ensures all capital is liquid before the main loop begins. Handles chunking automatically for coins with per-order quantity limits (e.g. ADA max 1900 per order).

---

### 9. Manual Sweep Utility (`sweep_to_usdt.py`)
Standalone script to check and convert leftover coins to USDT at any time (outside of bot).

```bash
python sweep_to_usdt.py          # dry-run — shows what it would do
python sweep_to_usdt.py --sell   # actually sells
```

Shows full wallet breakdown with USD values, identifies what needs sweeping, sells in exchange-safe chunks.

---

## Known Dust Issue
After every sell, a tiny amount of coin remains due to fee deduction on buy + floor rounding.
For example: buy 615.69 TRX, receive 615.07 after fee, sell 615, leave 0.07 TRX.
This accumulates over many trades. The startup sweep and manual sweep both handle it.
Amounts below ~$1 are genuinely unsellable (below exchange minimum order value).

---

## What Could Be Done Next

### High Priority
- [ ] **Mainnet migration** — BTCUSDT excluded from testnet (static price), add it back on mainnet. All major pairs work on mainnet.
- [ ] **USDC detection** — bot doesn't trade USDC pairs; if USDC ends up in wallet (e.g. from a transfer), startup sweep converts it to USDT automatically now, but worth monitoring.

### Medium Priority
- [ ] **Exit learning** — currently only entry conditions are learned. Could also learn optimal exit: e.g. if `bb_midline` exits are consistently outperforming `take_profit`, adjust exit strategy.
- [ ] **Volatility-based position sizing** — scale position size down during high-volatility periods (large MACD swings, wide BB bands).
- [ ] **Session time awareness** — bot runs 24/7 but crypto has quieter hours. Hour avoidance partially handles this but a more explicit session filter could help.
- [ ] **Trade duration tracking** — currently not recorded. Knowing average winning trade duration vs losing trade duration would help refine trailing stop settings.

### Nice to Have
- [ ] **Claude API integration** — for natural-language explanation of why trades are winning/losing, market regime detection (trending vs ranging), parameter suggestions based on accumulated daily summaries. Would require `anthropic` package and API key.
- [ ] **Telegram/Discord alerts** — notify on trade open/close, daily PnL summary, circuit breaker triggers.
- [ ] **Backtesting harness** — replay historical candles through the strategy to validate learned params before running live.

---

## Running the Bot

```bash
# Normal start (sweeps coins, then trades)
python bybit_bot.py

# Manual wallet sweep (run while bot is stopped)
python sweep_to_usdt.py          # check only
python sweep_to_usdt.py --sell   # convert to USDT
```

---

## Config Constants (top of `bybit_bot.py`)

| Constant | Value | Purpose |
|---|---|---|
| `RISK_PCT` | 0.02 | 2% of balance risked per trade |
| `STOP_LOSS_PCT` | 0.015 | 1.5% stop loss |
| `TAKE_PROFIT_PCT` | 0.03 | 3% take profit |
| `TRAIL_STOP_PCT` | 0.008 | 0.8% trailing stop |
| `MAX_POSITIONS` | 6 | Max simultaneous open positions |
| `DAILY_LOSS_LIMIT` | 0.05 | 5% daily drawdown circuit breaker |
| `MAX_ORDER_USDT` | 200 | Hard cap per order (exchange safety) |
| `LEARN_EVERY` | 5 | Adapt params after every N completed trades |
| `RSI_BUY` | 55 | Starting RSI threshold (learned over time) |
| `BB_ENTRY` | 0.50 | Starting BB% threshold (learned over time) |

---

## Learning Parameter Limits (hard bounds, never exceeded)

| Param | Min | Max |
|---|---|---|
| `RSI_BUY` | 35 | 60 |
| `BB_ENTRY` | 0.20 | 0.70 |
| `MACD_MIN` | 0.0 | 0.01 |
| `TREND_GAP_MIN` | 0.0 | 0.005 |
