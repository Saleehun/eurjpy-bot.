# Mean-reversion bot for SPY and QQQ on an Alpaca PAPER account (your Strategy 1).
# 15-minute candles, 20-period average and standard deviation.
#   BUY   when the last finished candle closes more than K standard deviations BELOW the average
#   SHORT when it closes more than K standard deviations ABOVE the average
#   EXIT  when the close comes back to the average (or the safety stop is hit)
# K = 1.5 for SPY, 1.8 for QQQ. Runs every 15 minutes during US market hours from GitHub Actions.
# Safety: paper endpoint only, fixed size, safety stop, daily loss limit, entry limit per day.
import os, json, time, math
import requests
import pandas as pd
import numpy as np

PAPER_URL = "https://paper-api.alpaca.markets"      # paper only, there is no live URL in this file
DATA_URL = "https://data.alpaca.markets"
SYMBOLS = {"SPY": 1.5, "QQQ": 1.8}                  # symbol: number of standard deviations
PERIOD = 20
POSITION_USD = float(os.environ.get("POSITION_USD", "5000"))
SAFETY_STOP_PCT = float(os.environ.get("SAFETY_STOP_PCT", "2"))   # not in your spec: protects against runaway moves
MAX_DAILY_LOSS_PCT = float(os.environ.get("MAX_DAILY_LOSS_PCT", "2"))
MAX_ENTRIES_PER_DAY = int(os.environ.get("MAX_ENTRIES_PER_DAY", "6"))
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"
STATE_FILE = "stocks_state.json"


class API:
    def __init__(self):
        key, secret = os.environ.get("ALPACA_KEY"), os.environ.get("ALPACA_SECRET")
        if not key or not secret:
            raise SystemExit("Missing ALPACA_KEY / ALPACA_SECRET secrets")
        self.h = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}

    def _req(self, method, url, **kw):
        last = None
        for _ in range(3):
            try:
                r = requests.request(method, url, headers=self.h, timeout=20, **kw)
                if r.status_code == 404 and method == "GET":
                    return None
                r.raise_for_status()
                return r.json()
            except Exception as e:
                last = e
                time.sleep(2)
        raise RuntimeError(f"Alpaca request failed: {str(last)[:150]}")

    def clock(self):
        return self._req("GET", f"{PAPER_URL}/v2/clock")

    def bars(self, now):
        start = (now - pd.Timedelta(days=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
        out, token = {s: [] for s in SYMBOLS}, None
        for _ in range(6):
            params = {"symbols": ",".join(SYMBOLS), "timeframe": "15Min", "start": start, "limit": 1000,
                      "sort": "asc", "feed": "iex", "adjustment": "raw"}
            if token:
                params["page_token"] = token
            d = self._req("GET", f"{DATA_URL}/v2/stocks/bars", params=params) or {}
            for s, rows in (d.get("bars") or {}).items():
                out.setdefault(s, []).extend(rows)
            token = d.get("next_page_token")
            if not token:
                break
        res = {}
        for s, rows in out.items():
            df = pd.DataFrame(rows)
            if df.empty:
                continue
            df.index = pd.to_datetime(df["t"], utc=True)
            df = df.rename(columns={"o": "Open", "h": "High", "l": "Low", "c": "Close", "v": "Volume"})
            res[s] = df[["Open", "High", "Low", "Close", "Volume"]].astype(float)
        return res

    def account(self):
        return self._req("GET", f"{PAPER_URL}/v2/account")

    def positions(self):
        return {p["symbol"]: p for p in (self._req("GET", f"{PAPER_URL}/v2/positions") or [])}

    def open_orders(self):
        return self._req("GET", f"{PAPER_URL}/v2/orders", params={"status": "open"}) or []

    def market_order(self, symbol, side, qty):
        body = {"symbol": symbol, "side": side, "type": "market", "time_in_force": "day", "qty": str(qty)}
        o = self._req("POST", f"{PAPER_URL}/v2/orders", json=body)
        for _ in range(10):
            if o and o.get("status") == "filled":
                break
            time.sleep(2)
            o = self._req("GET", f"{PAPER_URL}/v2/orders/{o['id']}") or o
        return o


def send(text):
    print(text, flush=True)
    token, chat = os.environ.get("TG_TOKEN"), os.environ.get("TG_CHAT_ID")
    if token and chat:
        try:
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          data={"chat_id": chat, "text": text}, timeout=15)
        except Exception as e:
            print("Telegram error:", e)


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(st):
    with open(STATE_FILE, "w") as f:
        json.dump(st, f, indent=1)


def regular_session(df):
    """Keep only 9:30-16:00 New York time candles."""
    ny = df.copy()
    ny.index = ny.index.tz_convert("America/New_York")
    return ny.between_time("09:30", "15:59").tz_convert("UTC")


def decide(df, k, qty_held, entry_price):
    """Return (action, info). action: 'buy', 'short', 'exit', 'stop' or None."""
    close = df.Close
    sma, sd = close.rolling(PERIOD).mean().iloc[-1], close.rolling(PERIOD).std().iloc[-1]
    px = float(close.iloc[-1])
    info = {"px": px, "sma": float(sma), "sd": float(sd), "z": (px - sma) / sd if sd else 0.0}
    if np.isnan(sma) or not sd:
        return None, info
    if qty_held == 0:
        if px < sma - k * sd:
            return "buy", info
        if px > sma + k * sd:
            return "short", info
        return None, info
    sign = 1 if qty_held > 0 else -1
    pnl_pct = 100 * sign * (px / entry_price - 1)
    info["pnl_pct"] = pnl_pct
    if pnl_pct <= -SAFETY_STOP_PCT:
        return "stop", info
    if (sign > 0 and px >= sma) or (sign < 0 and px <= sma):
        return "exit", info
    return None, info


def run_once(api, now=None):
    now = now or pd.Timestamp.now(tz="UTC")
    clock = api.clock()
    if not clock.get("is_open"):
        print(f"{now:%Y-%m-%d %H:%M} UTC | market closed, nothing to do", flush=True)
        return
    st = load_state()
    acct = api.account()
    equity = float(acct["equity"])
    day = now.strftime("%Y-%m-%d")
    if st.get("day") != day:
        st["day"], st["day_equity"], st["entries_today"] = day, equity, 0
    loss = 100 * (equity / st["day_equity"] - 1)
    bars = api.bars(now)
    positions = api.positions()
    busy = {o.get("symbol") for o in api.open_orders()}
    for sym, k in SYMBOLS.items():
        df = bars.get(sym)
        if df is None:
            print(f"{sym}: no data", flush=True)
            continue
        df = regular_session(df[df.index + pd.Timedelta(minutes=15) <= now])    # finished candles only
        if len(df) < PERIOD + 1:
            print(f"{sym}: not enough candles", flush=True)
            continue
        age = (now - (df.index[-1] + pd.Timedelta(minutes=15))).total_seconds() / 60
        p = positions.get(sym)
        qty_held, entry = 0.0, 0.0
        if p:
            qty_held = abs(float(p["qty"])) * (-1 if (p.get("side") == "short" or float(p["qty"]) < 0) else 1)
            entry = float(p["avg_entry_price"])
        action, info = decide(df, k, qty_held, entry)
        state_txt = "LONG" if qty_held > 0 else "SHORT" if qty_held < 0 else "flat"
        if "pnl_pct" in info:
            state_txt += " " + format(info["pnl_pct"], "+.2f") + "%"
        print(f"{now:%H:%M} UTC | {sym} close {info['px']:.2f} avg {info['sma']:.2f} "
              f"({info['z']:+.2f} std, trigger {k}) | {state_txt} | candle age {age:.0f} min", flush=True)
        if age > 20:
            print(f"{sym}: data is {age:.0f} min old, skipping", flush=True)
            continue
        if action is None or sym in busy:
            continue
        if action in ("exit", "stop"):
            side = "sell" if qty_held > 0 else "buy"
            if DRY_RUN:
                print(f"DRY RUN: would {side} {abs(qty_held):g} {sym} ({action})", flush=True)
                continue
            o = api.market_order(sym, side, int(abs(qty_held)) if float(abs(qty_held)).is_integer() else abs(qty_held))
            out = float(o.get("filled_avg_price") or info["px"])
            pnl = 100 * (1 if qty_held > 0 else -1) * (out / entry - 1)
            why = "back at the average" if action == "exit" else f"safety stop {SAFETY_STOP_PCT}%"
            send(f"🔴 PAPER CLOSE {sym} @ {out:.2f}\n{why}\nResult {pnl:+.2f}%")
        else:
            if loss <= -MAX_DAILY_LOSS_PCT:
                print(f"{sym}: signal but daily loss limit reached ({loss:.1f}%)", flush=True)
                continue
            if st["entries_today"] >= MAX_ENTRIES_PER_DAY:
                print(f"{sym}: signal but max entries per day reached", flush=True)
                continue
            qty = max(1, int(POSITION_USD // info["px"]))
            side = "buy" if action == "buy" else "sell"
            if DRY_RUN:
                print(f"DRY RUN: would {side} {qty} {sym} ({action})", flush=True)
                continue
            o = api.market_order(sym, side, qty)
            price = float(o.get("filled_avg_price") or info["px"])
            st["entries_today"] += 1
            send(f"{'🟢 PAPER BUY' if action == 'buy' else '🔻 PAPER SHORT'} {qty} {sym} @ {price:.2f}\n"
                 f"{abs(info['z']):.1f} std {'below' if action == 'buy' else 'above'} the 20-candle average {info['sma']:.2f}")
    save_state(st)


if __name__ == "__main__":
    run_once(API())
