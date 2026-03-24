"""
================================================================
  SWEEP TO USDT — Manual cleanup utility
  Converts all leftover coin balances back to USDT.

  Run this WHILE THE BOT IS STOPPED to clear dust/remainders.
  Usage:
      python sweep_to_usdt.py          # dry-run (shows what it would do)
      python sweep_to_usdt.py --sell   # actually sells everything
================================================================
"""

import sys
import math
import time
from pybit.unified_trading import HTTP
from datetime import datetime

# ── Config — must match bybit_bot.py ──────────────────────────
API_KEY    = "YndDHQr6Mpx2i3fElS"
API_SECRET = "oGjioa9ZVih7rw1b4dBVMJJ2UkGQZ4LrF5cR"
TESTNET    = True

# Minimum USD value to bother selling (below this it's unsellable dust)
MIN_VALUE_USD = 1.0

# Coins to sweep — add any other coins that appear in your wallet
COINS = ["ETH", "SOL", "XRP", "ADA", "TRX", "BTC", "BNB", "DOGE"]

DRY_RUN = "--sell" not in sys.argv
# ──────────────────────────────────────────────────────────────

session = HTTP(testnet=TESTNET, api_key=API_KEY, api_secret=API_SECRET)


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}]  {msg}")


def get_price(symbol):
    try:
        resp = session.get_tickers(category="spot", symbol=symbol)
        return float(resp["result"]["list"][0]["lastPrice"])
    except Exception:
        return 0.0


def get_coin_balance(coin):
    try:
        resp  = session.get_wallet_balance(accountType="UNIFIED", coin=coin)
        coins = resp["result"]["list"][0]["coin"]
        for c in coins:
            if c["coin"] == coin:
                return float(c.get("availableBalance") or c.get("walletBalance") or 0)
    except Exception:
        pass
    return 0.0


def floor_qty(coin, qty, price):
    """Floor qty to exchange-safe precision based on price magnitude."""
    if price >= 1000:  return math.floor(qty * 10000) / 10000  # 4 dp  (ETH $2000+)
    if price >= 100:   return math.floor(qty * 1000)  / 1000   # 3 dp  (SOL $100+)
    if price >= 1:     return math.floor(qty * 10)    / 10     # 1 dp  (XRP, TRX, ADA)
    return             float(math.floor(qty))                   # int   (very cheap)


def sell_coin(coin, qty_str, symbol):
    try:
        resp = session.place_order(
            category  = "spot",
            symbol    = symbol,
            side      = "Sell",
            orderType = "Market",
            qty       = qty_str,
        )
        order_id = resp["result"]["orderId"]
        log(f"  SOLD  {qty_str} {coin} → USDT  |  order {order_id}")
        return True
    except Exception as e:
        log(f"  FAIL  {coin}: {e}")
        return False


def main():
    mode = "DRY RUN (no orders placed)" if DRY_RUN else "LIVE — SELLING NOW"
    log("=" * 60)
    log(f"  SWEEP TO USDT  |  {mode}")
    log(f"  Testnet: {TESTNET}  |  Min value: ${MIN_VALUE_USD:.2f}")
    log("=" * 60)

    usdt_before = get_coin_balance("USDT")
    log(f"  USDT before sweep: ${usdt_before:.4f}")
    log("")

    total_est_usdt = 0.0
    actions = []

    for coin in COINS:
        symbol = f"{coin}USDT"
        bal    = get_coin_balance(coin)
        if bal <= 0:
            continue

        price = get_price(symbol)
        if price <= 0:
            log(f"  {coin:<6}  bal={bal:.6f}  — could not get price, skipping")
            continue

        value_usd = bal * price
        sellable  = floor_qty(coin, bal, price)
        sell_val  = sellable * price
        dust_val  = (bal - sellable) * price

        if value_usd < MIN_VALUE_USD:
            log(f"  {coin:<6}  bal={bal:.6f}  ~${value_usd:.4f}  — below minimum, skipping (dust)")
            continue

        if sellable <= 0:
            log(f"  {coin:<6}  bal={bal:.6f}  ~${value_usd:.4f}  — rounds to 0, unsellable dust")
            continue

        log(f"  {coin:<6}  bal={bal:.6f}  price=${price:.4f}  "
            f"value=${value_usd:.4f}  sellable={sellable}  "
            f"(~${sell_val:.4f})  dust~${dust_val:.4f}")

        total_est_usdt += sell_val
        actions.append((coin, symbol, sellable, sell_val))

    log("")
    if not actions:
        log("  Nothing to sweep — all balances are dust or already USDT.")
        return

    log(f"  Will convert ~${total_est_usdt:.4f} USDT worth of coins")
    log("")

    if DRY_RUN:
        log("  [DRY RUN] Re-run with --sell to actually place orders.")
        log("  Example: python sweep_to_usdt.py --sell")
        return

    # ── Actually sell ─────────────────────────────────────────
    for coin, symbol, qty, est_val in actions:
        qty_str = str(qty)
        log(f"  Selling {qty_str} {coin} (~${est_val:.4f}) ...")
        sell_coin(coin, qty_str, symbol)
        time.sleep(0.5)   # small pause between orders

    # ── Final balance ─────────────────────────────────────────
    time.sleep(2)
    usdt_after = get_coin_balance("USDT")
    gained     = usdt_after - usdt_before
    log("")
    log(f"  USDT before: ${usdt_before:.4f}")
    log(f"  USDT after : ${usdt_after:.4f}")
    log(f"  Gained     : +${gained:.4f}")

    # Show any remaining dust
    log("")
    log("  Remaining balances (dust that could not be sold):")
    for coin in COINS:
        bal = get_coin_balance(coin)
        if bal > 0:
            price = get_price(f"{coin}USDT")
            log(f"    {coin:<6}  {bal:.6f}  (~${bal*price:.4f})")

    log("=" * 60)
    log("  Done.")


if __name__ == "__main__":
    main()
