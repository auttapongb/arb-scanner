from safety import SafeBybitAPI
import os

BYBIT_API_KEY = os.environ.get("BYBIT_API_KEY", "") or "0ToflNv3n1mFpnkY3r"
BYBIT_PRIV_KEY_PATH = "/root/.bybit/private.pem"

api = SafeBybitAPI("https://api.bybit.com", BYBIT_API_KEY, BYBIT_PRIV_KEY_PATH)

# Check SOL spot orders
r = api.get("/v5/order/realtime", {"category": "spot", "symbol": "SOLUSDT"})
orders = r.get("result", {}).get("list", [])
print(f"Total orders returned: {len(orders)}")
for o in orders:
    side = o.get("side", "?")
    qty = o.get("qty", "?")
    price = o.get("price", "?")
    status = o.get("orderStatus", "?")
    exec_qty = o.get("cumExecQty", "0")
    exec_val = o.get("cumExecValue", "0")
    print(f"  {side} qty={qty} price=${price} status={status} execQty={exec_qty} execVal={exec_val}")

# Also check USDT balance in unified account
r2 = api.get("/v5/account/wallet-balance", {"accountType": "UNIFIED", "coin": "USDT"})
for coin in r2.get("result", {}).get("list", [{}])[0].get("coin", []):
    if coin.get("coin") == "USDT":
        wb = coin.get("walletBalance", "?")
        avail = coin.get("availableToWithdraw", "?")
        print(f"\nUSDT walletBalance={wb} availableToWithdraw={avail}")
