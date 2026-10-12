# EUR/JPY 2-minute signal bot -> Telegram (runs on GitHub Actions)
# Posts each signal (action + entry time) and its result with win/loss counts.
import os, json, time
import requests
import pandas as pd

# ------------------ SETTINGS ------------------
PAIR = "EUR/JPY"
SYMBOL = "EURJPY=X"      # free Yahoo price feed (can differ slightly from IQ Option)
TF = 2                   # chart timeframe in minutes
EXPIRY = 2               # trade time in minutes (= one 2M candle)
MIN_GAP = 3              # minimum minutes between two trade entries
TZ_HOURS = 1             # your hours from UTC (UTC+1)
RUN_MINUTES = int(os.environ.get("RUN_MINUTES", "330"))  # stop cleanly before the 6h limit
STATE_FILE = "state.json"
# ----------------------------------------------


def send(text):
    print(text, "\n", flush=True)
    token, chat = os.environ.get("TG_TOKEN"), os.environ.get("TG_CHAT_ID")
    if not token or not chat:
        print("(Telegram secrets missing - not sent)")
        return
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      data={"chat_id": chat, "text": text}, timeout=15)
    except Exception as e:
        print("Telegram error:", e)


def utcnow():
    return pd.Timestamp(time.time(), unit="s", tz="UTC")


def add_indicators(c):
    c = c.copy()
    c["ema"] = c.Close.ewm(span=50, adjust=False).mean()
    c["fast"] = c.Close.ewm(span=20, adjust=False).mean()
    c["slope"] = c.ema - c.ema.shift(5)
    delta = c.Close.diff()
    up = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    dn = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    c["rsi"] = 100 - 100 / (1 + up / dn)
    return c


def get_candles():
    import yfinance as yf
    d = yf.download(SYMBOL, period="5d", interval="1m", progress=False, auto_adjust=False)
    if d is None or d.empty:
        return None
    if isinstance(d.columns, pd.MultiIndex):
        d.columns = d.columns.get_level_values(0)
    d.index = pd.to_datetime(d.index, utc=True)
    c = d.resample(f"{TF}min").agg(
        {"Open": "first", "High": "max", "Low": "min", "Close": "last"}).dropna()
    return add_indicators(c)


ACTIVE = "A"   # which rule set the live bot uses (A is the strict one tested first)

RULES = {
    "A": "Strict: RSI crosses back through 45/55 in the trend direction",
    "B": "RSI crosses back through 50 in the trend direction",
    "C": "Trend bounce: opposite candle, then a candle in the trend direction",
    "D": "Trend breakout: candle in trend direction closing beyond the previous high/low",
    "E": "Trend bounce (like C) but skips moves that are already stretched (RSI 65/35)",
}


def signal_variant(v, c, i):
    """Trend = EMA20 vs EMA50 with EMA50 sloping the same way. All variants trade with the trend."""
    if i < 60:
        return None
    r, p = c.iloc[i], c.iloc[i - 1]
    up = r.fast > r.ema and r.slope > 0
    dn = r.fast < r.ema and r.slope < 0
    green, red = r.Close > r.Open, r.Close < r.Open
    if v == "A":
        if up and p.rsi < 45 <= r.rsi and green:
            return "CALL"
        if dn and p.rsi > 55 >= r.rsi and red:
            return "PUT"
    elif v == "B":
        if up and p.rsi < 50 <= r.rsi and green:
            return "CALL"
        if dn and p.rsi > 50 >= r.rsi and red:
            return "PUT"
    elif v == "C":
        if up and p.Close < p.Open and green:
            return "CALL"
        if dn and p.Close > p.Open and red:
            return "PUT"
    elif v == "D":
        if up and green and r.Close > p.High:
            return "CALL"
        if dn and red and r.Close < p.Low:
            return "PUT"
    elif v == "E":
        if up and p.Close < p.Open and green and r.rsi < 65:
            return "CALL"
        if dn and p.Close > p.Open and red and r.rsi > 35:
            return "PUT"
    return None


def signal(c, i):
    return signal_variant(ACTIVE, c, i)


def outcome(side, entry, close):
    if close == entry:
        return "DRAW"
    win = close > entry if side == "CALL" else close < entry
    return "WIN" if win else "LOSS"


def hhmm(ts):
    return (ts + pd.Timedelta(hours=TZ_HOURS)).strftime("%H:%M")


def load_state():
    try:
        with open(STATE_FILE) as f:
            st = json.load(f)
        st.setdefault("tot", {"W": 0, "L": 0})
        st.setdefault("day", {"W": 0, "L": 0})
        st.setdefault("day_key", "")
        return st
    except Exception:
        return {"tot": {"W": 0, "L": 0}, "day": {"W": 0, "L": 0}, "day_key": ""}


def save_state(st):
    with open(STATE_FILE, "w") as f:
        json.dump(st, f)


def fetch_closed():
    """Fetch candles, retrying briefly until the candle that just closed is available."""
    c = None
    for _ in range(8):
        c = get_candles()
        if c is not None:
            now = utcnow()
            closed = c[c.index + pd.Timedelta(minutes=TF) <= now]
            if len(closed) and closed.index[-1] + pd.Timedelta(minutes=TF) >= now.floor(f"{TF}min"):
                return closed
        time.sleep(5)
    if c is None:
        return None
    return c[c.index + pd.Timedelta(minutes=TF) <= utcnow()]


def live():
    started = time.time()
    st = load_state()
    save_state(st)
    pending = None
    last_entry = None
    send(f"✅ {PAIR} {TF}M signal bot session started.\n"
         f"Total so far: {st['tot']['W']}W - {st['tot']['L']}L")
    while True:
        secs = TF * 60
        time.sleep(secs - (time.time() % secs) + 4)  # wake just after each candle closes
        elapsed = time.time() - started
        stopping = elapsed > RUN_MINUTES * 60
        if (stopping and not pending) or elapsed > (RUN_MINUTES + 10) * 60:
            break
        try:
            c = fetch_closed()
        except Exception as e:
            print("data error:", e, flush=True)
            continue
        if c is None or len(c) < 60:
            print("no data (market closed?)", flush=True)
            continue
        now = utcnow()
        lag = (now - (c.index[-1] + pd.Timedelta(minutes=TF))).total_seconds()
        print(f"{now:%H:%M:%S} UTC | last closed candle ends {(c.index[-1] + pd.Timedelta(minutes=TF)):%H:%M} "
              f"| data lag {lag:.0f}s | close {c.Close.iloc[-1]:.3f} | RSI {c.rsi.iloc[-1]:.1f}", flush=True)

        # 1) settle the open trade
        if pending and pending["entry"] in c.index:
            row = c.loc[pending["entry"]]
            res = outcome(pending["side"], row.Open, row.Close)
            k = str((pending["entry"] + pd.Timedelta(hours=TZ_HOURS)).date())
            if k != st["day_key"]:
                st["day_key"], st["day"] = k, {"W": 0, "L": 0}
            if res == "WIN":
                st["tot"]["W"] += 1; st["day"]["W"] += 1
            elif res == "LOSS":
                st["tot"]["L"] += 1; st["day"]["L"] += 1
            save_state(st)
            icon = {"WIN": "✅", "LOSS": "❌", "DRAW": "➖"}[res]
            send(f"{icon} RESULT: {res}\nPair: {PAIR}\n"
                 f"Action: {pending['side']}\nEntry time: {hhmm(pending['entry'])}\n"
                 f"Today: {st['day']['W']}W - {st['day']['L']}L\n"
                 f"Total: {st['tot']['W']}W - {st['tot']['L']}L")
            pending = None

        if stopping:
            continue

        # 2) look for a new signal on the candle that just closed
        i = len(c) - 1
        s = signal(c, i)
        entry = c.index[i] + pd.Timedelta(minutes=TF)
        if not s or pending:
            continue
        if (now - entry) > pd.Timedelta(seconds=45):
            print(f"signal {s} skipped: data arrived {(now - entry).total_seconds():.0f}s late", flush=True)
            continue  # data arrived too late, skip stale signal
        if last_entry is not None and (entry - last_entry) < pd.Timedelta(minutes=MIN_GAP):
            continue
        pending = {"side": s, "entry": entry}
        last_entry = entry
        arrow = "BUY ⬆️ (CALL)" if s == "CALL" else "SELL ⬇️ (PUT)"
        send(f"📢 SIGNAL\nPair: {PAIR}\nAction: {arrow}\n"
             f"Chart: {TF}M | Trade time: {EXPIRY} min\n"
             f"Entry time: {hhmm(entry)} (enter NOW)")
    send(f"⏸ Session ended. Total: {st['tot']['W']}W - {st['tot']['L']}L. "
         f"The next session starts at its scheduled time.")


if __name__ == "__main__":
    try:
        import backtest
        backtest.report()
    except Exception as e:
        print("backtest skipped:", e, flush=True)
    live()
