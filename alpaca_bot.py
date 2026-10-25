# BTC/USD breakout bot for an Alpaca PAPER account (long only). Runs once per hour from GitHub Actions.
# Rules (your Strategy 2, long side only because Alpaca cannot short crypto):
#   BUY  when the last finished 1-hour candle closes above the previous 20-candle high
#        AND its volume is at least 1.5x the previous 20-candle average.
#   SELL when price touches a trailing stop (highest high since entry minus 2 x ATR14),
#        or when a candle closes below the previous 20-candle low with the same volume confirmation.
# Safety: paper endpoint only, one position, fixed size, daily loss limit, trade-count limit.
import os, json, time
import requests
import pandas as pd
import numpy as np

PAPER_URL = "https://paper-api.alpaca.markets"      # paper only, there is no live URL in this file
DATA_URL = "https://data.alpaca.markets"
SYMBOL = "BTC/USD"
POS_SYMBOL = "BTCUSD"
LOOKBACK, VOL_MULT, ATR_N, ATR_MULT = 20, 1.5, 14, 2.0
POSITION_USD = float(os.environ.get("POSITION_USD", "1000"))
MAX_DAILY_LOSS_PCT = float(os.environ.get("MAX_DAILY_LOSS_PCT", "2"))
MAX_TRADES_PER_DAY = int(os.environ.get("MAX_TRADES_PER_DAY", "4"))
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"
STATE_FILE = "alpaca_state.json"


# ------------------------------------------------------------------ Alpaca access
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
            except Exception as e:   # retry network and server errors
                last = e
                time.sleep(2)
        raise RuntimeError(f"Alpaca request failed: {str(last)[:150]}")

    def bars(self, now):
        start = (now - pd.Timedelta(days=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows, token = [], None
        for _ in range(6):
            params = {"symbols": SYMBOL, "timeframe": "1Hour", "start": start, "limit": 1000, "sort": "asc"}
            if token:
                params["page_token"] = token
            d = self._req("GET", f"{DATA_URL}/v1beta3/crypto/us/bars", params=params) or {}
            rows += (d.get("bars") or {}).get(SYMBOL, [])
            token = d.get("next_page_token")
            if not token:
                break
        df = pd.DataFrame(rows)
        if df.empty:
            return df
        df.index = pd.to_datetime(df["t"], utc=True)
        df = df.rename(columns={"o": "Open", "h": "High", "l": "Low", "c": "Close", "v": "Volume"})
        return df[["Open", "High", "Low", "Close", "Volume"]].astype(float)

    def last_price(self):
        d = self._req("GET", f"{DATA_URL}/v1beta3/crypto/us/latest/trades", params={"symbols": SYMBOL})
        try:
            return float(d["trades"][SYMBOL]["p"])
        except Exception:
            return None

    def account(self):
        return self._req("GET", f"{PAPER_URL}/v2/account")

    def position(self):
        for p in self._req("GET", f"{PAPER_URL}/v2/positions") or []:
            if p.get("symbol", "").replace("/", "") == POS_SYMBOL:
                return p
        return None

    def open_orders(self):
        d = self._req("GET", f"{PAPER_URL}/v2/orders", params={"status": "open", "symbols": SYMBOL})
        return d or []

    def market_order(self, side, notional=None, qty=None):
        body = {"symbol": SYMBOL, "side": side, "type": "market", "time_in_force": "gtc"}
        if notional is not None:
            body["notional"] = f"{notional:.2f}"
        else:
            body["qty"] = str(qty)
        o = self._req("POST", f"{PAPER_URL}/v2/orders", json=body)
        for _ in range(10):       # wait briefly for the fill
            if o and o.get("status") == "filled":
                break
            time.sleep(2)
            o = self._req("GET", f"{PAPER_URL}/v2/orders/{o['id']}") or o
        return o


# ------------------------------------------------------------------ helpers
def send(text):
    print(text, flush=True)
    token, chat = os.environ.get("TG_TOKEN"), os.environ.get("TG_CHAT_ID")
    if token and chat:
        try:
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          data={"chat_id": chat, "text": text}, timeout=15)
        except Exception as e:
            print("Telegram error:", e)


def external_volume():
    """Hourly BTC volume for the whole market, from Coinbase (or Kraken). Alpaca's own volume is tiny
    and patchy, so only the RATIO of the last candle to the 20-candle average is used from here."""
    hdr = {"User-Agent": "Mozilla/5.0 (alpaca-paper-bot)"}
    try:
        r = requests.get("https://api.exchange.coinbase.com/products/BTC-USD/candles",
                         params={"granularity": 3600}, headers=hdr, timeout=20)
        r.raise_for_status()
        s = pd.Series({pd.Timestamp(x[0], unit="s", tz="UTC"): float(x[5]) for x in r.json()}).sort_index()
        if len(s) >= LOOKBACK + 2:
            return s, "Coinbase"
    except Exception as e:
        print("Coinbase volume failed:", str(e)[:100], flush=True)
    try:
        r = requests.get("https://api.kraken.com/0/public/OHLC", params={"pair": "XBTUSD", "interval": 60},
                         headers=hdr, timeout=20)
        r.raise_for_status()
        res = r.json()["result"]
        rows = next(v for k, v in res.items() if k != "last")
        s = pd.Series({pd.Timestamp(int(x[0]), unit="s", tz="UTC"): float(x[6]) for x in rows}).sort_index()
        if len(s) >= LOOKBACK + 2:
            return s, "Kraken"
    except Exception as e:
        print("Kraken volume failed:", str(e)[:100], flush=True)
    return None, "Alpaca"


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(st):
    with open(STATE_FILE, "w") as f:
        json.dump(st, f, indent=1)


def atr(df, n=ATR_N):
    pc = df.Close.shift(1)
    tr = pd.concat([df.High - df.Low, (df.High - pc).abs(), (df.Low - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def signals(df):
    """Look at the last finished candle."""
    prev = df.iloc[-1 - LOOKBACK:-1]
    last = df.iloc[-1]
    vol_ok = last.Volume >= VOL_MULT * prev.Volume.mean()
    return {"close": last.Close, "low": last.Low, "high": last.High,
            "hh": prev.High.max(), "ll": prev.Low.min(), "vol_ok": bool(vol_ok),
            "vol_ratio": last.Volume / prev.Volume.mean() if prev.Volume.mean() else 0.0,
            "atr": float(atr(df).iloc[-1])}


def fmt(x):
    return f"{x:,.2f}"


# ------------------------------------------------------------------ one hourly run
def run_once(api, now=None):
    now = now or pd.Timestamp.now(tz="UTC")
    st = load_state()
    df = api.bars(now)
    if df.empty or len(df) < LOOKBACK + ATR_N + 2:
        print("not enough candles yet", flush=True)
        return
    df = df[df.index + pd.Timedelta(hours=1) <= now]          # finished candles only
    vol, vsrc = external_volume()
    if vol is not None:
        v = vol.reindex(df.index)
        if v.iloc[-(LOOKBACK + 1):].notna().all():
            df = df.copy()
            df["Volume"] = v
        else:
            vsrc = "Alpaca"
    else:
        vsrc = "Alpaca"
    s = signals(df)
    acct = api.account()
    equity = float(acct["equity"])
    day = now.strftime("%Y-%m-%d")
    if st.get("day") != day:
        st["day"], st["day_equity"], st["trades_today"] = day, equity, 0
    pos = api.position()

    print(f"{now:%Y-%m-%d %H:%M} UTC | candle {df.index[-1]:%H:%M} close {fmt(s['close'])} | "
          f"20-high {fmt(s['hh'])} 20-low {fmt(s['ll'])} | volume {s['vol_ratio']:.1f}x ({vsrc}) | "
          f"{'IN POSITION' if pos else 'flat'} | equity {fmt(equity)}", flush=True)

    if pos is None:
        st.pop("entry_price", None)
        loss = 100 * (equity / st["day_equity"] - 1)
        if s["close"] > s["hh"] and s["vol_ok"]:
            if loss <= -MAX_DAILY_LOSS_PCT:
                print(f"breakout seen but daily loss limit reached ({loss:.1f}%), no trade", flush=True)
            elif st["trades_today"] >= MAX_TRADES_PER_DAY:
                print("breakout seen but max trades per day reached, no trade", flush=True)
            elif api.open_orders():
                print("breakout seen but an order is still open, no trade", flush=True)
            elif DRY_RUN:
                print("DRY RUN: would buy now", flush=True)
            else:
                o = api.market_order("buy", notional=POSITION_USD)
                price = float(o.get("filled_avg_price") or s["close"])
                stop = price - ATR_MULT * s["atr"]
                st.update({"entry_time": now.isoformat(), "entry_price": price,
                           "high": max(s["high"], price), "stop": stop, "checked": ""})
                st["trades_today"] += 1
                send(f"🟢 PAPER BUY BTC/USD ${POSITION_USD:,.0f} @ {fmt(price)}\n"
                     f"Breakout above {fmt(s['hh'])}, volume {s['vol_ratio']:.1f}x\nStop {fmt(stop)}")
    else:
        entry = float(pos["avg_entry_price"])
        qty = float(pos["qty"])
        if "stop" not in st:     # position opened outside the bot: start a stop from now
            st.update({"entry_price": entry, "high": s["high"], "stop": entry - ATR_MULT * s["atr"]})
        if st.get("checked"):                       # candles finished since the last run
            newer = df[df.index > pd.Timestamp(st["checked"])]
        else:
            newer = df[df.index >= pd.Timestamp(st.get("entry_time", df.index[-1].isoformat())).floor("h")]
        low_since = float(newer.Low.min()) if len(newer) else s["low"]
        price = api.last_price() or s["close"]
        why = None
        stop_before = st["stop"]
        if min(low_since, price) <= stop_before:
            why = f"trailing stop {fmt(stop_before)} touched"
        if len(newer):
            st["high"] = max(st["high"], float(newer.High.max()))
        st["stop"] = max(st["stop"], st["high"] - ATR_MULT * s["atr"])
        st["checked"] = df.index[-1].isoformat()
        if not why and s["close"] < s["ll"] and s["vol_ok"]:
            why = f"closed below the 20-candle low {fmt(s['ll'])} on high volume"
        print(f"holding {qty:.6f} BTC from {fmt(entry)} | stop {fmt(st['stop'])} | price {fmt(price)}", flush=True)
        if why and DRY_RUN:
            print(f"DRY RUN: would sell now ({why})", flush=True)
        elif why:
            o = api.market_order("sell", qty=pos["qty"])
            out = float(o.get("filled_avg_price") or price)
            pnl = 100 * (out / entry - 1)
            send(f"🔴 PAPER SELL BTC/USD @ {fmt(out)}\n{why}\nResult {pnl:+.2f}% (${(out - entry) * qty:+,.2f})")
            st["last_result"] = round(pnl, 3)
            for k in ("entry_time", "entry_price", "high", "stop", "checked"):
                st.pop(k, None)
    save_state(st)


if __name__ == "__main__":
    run_once(API())
