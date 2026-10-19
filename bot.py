# EUR/USD 2-minute signal bot -> Telegram (runs on GitHub Actions)
# Posts each signal (action + entry time) and its result with win/loss counts.
# PRE_ALERT_SECONDS > 0: the signal is posted that many seconds BEFORE the entry time,
# based on the candle that is still forming. Set it to 0 for the old behaviour.
import os, json, time
import requests
import pandas as pd

# ------------------ SETTINGS ------------------
PAIR = "EUR/USD"
DERIV_SYMBOL = "frxEURUSD"   # Deriv's public real-time price feed (true candles, no login)
DERIV_URL = "wss://api.derivws.com/trading/v1/options/ws/public"
SYMBOL = "EURUSD=X"      # Yahoo feed, only used as a backup if Deriv's feed fails
PRICE_DEC = 5            # decimals when printing prices
PRE_ALERT_SECONDS = 30   # post the signal this many seconds before entry (0 = after the candle closes)
PREVIEW_RULES = "ABCD"   # show the live signals of these rule sets side by side, each with its own score ("" = only rule ACTIVE)
TF = 2                   # chart timeframe in minutes
EXPIRY = 2               # trade time in minutes (= one 2M candle)
MIN_GAP = 3              # minimum minutes between two trade entries
TZ_HOURS = 1             # your hours from UTC (UTC+1)
RUN_MINUTES = int(os.environ.get("RUN_MINUTES", "330"))  # stop cleanly before the 6h limit
STATE_FILE = "state.json"
# ----------------------------------------------


def send(text):
    print(text, "\n", flush=True)
    if os.environ.get("BOT_QUIET") == "1":
        return
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




LAST_RAW = None
_source_logged = [False]


def deriv_candles(count):
    """Real OHLC candles from Deriv's public feed. The last candle is the one still forming.
    Asks for older pages until `count` candles are collected (the server may cap one answer)."""
    from websockets.sync.client import connect
    rows = []
    with connect(DERIV_URL, open_timeout=15) as ws:
        end = "latest"
        for page in range(1, 9):
            want = count - len(rows)
            if want <= 0:
                break
            req = {"ticks_history": DERIV_SYMBOL, "adjust_start_time": 1, "count": min(want, 5000),
                   "end": end, "style": "candles", "granularity": TF * 60, "req_id": page}
            ws.send(json.dumps(req))
            stop = time.time() + 20
            d = None
            while time.time() < stop:
                msg = json.loads(ws.recv(timeout=max(0.5, stop - time.time())))
                if msg.get("req_id") == page or "candles" in msg or "error" in msg:
                    d = msg
                    break
            if d is None or "error" in d:
                if rows:
                    break
                raise RuntimeError(str((d or {}).get("error", "no reply"))[:120])
            got = d.get("candles") or []
            if rows:   # drop an overlap with the newer page
                got = [r for r in got if int(r["epoch"]) < int(rows[0]["epoch"])]
            if not got:
                break
            rows = got + rows
            end = str(int(rows[0]["epoch"]) - 1)
    if len(rows) < 60:
        raise RuntimeError(f"only {len(rows)} candles")
    c = pd.DataFrame(rows)
    c.index = pd.to_datetime(c["epoch"].astype("int64"), unit="s", utc=True)
    c = c.rename(columns={"open": "Open", "high": "High", "low": "Low", "close": "Close"})
    c = c[["Open", "High", "Low", "Close"]].astype(float).sort_index()
    return c[~c.index.duplicated(keep="last")]


def yahoo_candles():
    global LAST_RAW
    import yfinance as yf
    d = yf.download(SYMBOL, period="5d", interval="1m", progress=False, auto_adjust=False)
    if d is None or d.empty:
        return None
    if isinstance(d.columns, pd.MultiIndex):
        d.columns = d.columns.get_level_values(0)
    d.index = pd.to_datetime(d.index, utc=True)
    LAST_RAW = d.index[-1]
    return d.resample(f"{TF}min").agg(
        {"Open": "first", "High": "max", "Low": "min", "Close": "last"}).dropna()


def get_candles(count=400):
    global LAST_RAW
    try:
        c = deriv_candles(count)
        now = pd.Timestamp(time.time(), unit="s", tz="UTC")
        # LAST_RAW = "how fresh is the newest data": now if the forming candle is there
        LAST_RAW = now.floor("1min") if c.index[-1] >= now.floor(f"{TF}min") \
            else c.index[-1] + pd.Timedelta(minutes=TF - 1)
        if not _source_logged[0]:
            print("data source: Deriv real-time candles", flush=True)
            _source_logged[0] = True
    except Exception as e:
        print("Deriv feed failed, using Yahoo backup:", e, flush=True)
        c = yahoo_candles()
        if c is None:
            return None
    return add_indicators(c)


ACTIVE = "D"   # which rule set the live bot uses (A is the strict one tested first)

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


SCHEDULE_UTC = [(6, 30), (12, 0)]   # weekday session starts; keep in sync with the cron lines in bot.yml


def next_session_text():
    now = utcnow()
    for d in range(0, 9):
        day = (now + pd.Timedelta(days=d)).normalize()
        if day.dayofweek > 4:
            continue
        for h, m in SCHEDULE_UTC:
            t = day + pd.Timedelta(hours=h, minutes=m)
            if t <= now - pd.Timedelta(minutes=15):
                continue
            mins = int((t - now).total_seconds() // 60)
            if mins <= 5:
                return "Next session starts now"
            hrs, mm = divmod(mins, 60)
            if hrs >= 24:
                when = f"{hrs // 24}d {hrs % 24}h"
            else:
                when = f"{hrs}h {mm}m"
            return f"Next session in {when} ({hhmm(t)})"
    return "Next session: Monday"


def sig_text(side, entry, early):
    icon, word = ("🟢", "BUY") if side == "CALL" else ("🔴", "SELL")
    t = f"{icon} {word} {PAIR} {hhmm(entry)} · {EXPIRY} min"
    if early:
        t += f"\n⏰ Entry in {PRE_ALERT_SECONDS} seconds"
    return t


def res_text(res, side, entry, st):
    icon = {"WIN": "✅", "LOSS": "❌", "DRAW": "➖"}[res]
    word = "BUY" if side == "CALL" else "SELL"
    return f"{icon} {res} {PAIR} · {word} {hhmm(entry)} · Today {st['day']['W']}W-{st['day']['L']}L"


def load_state():
    try:
        with open(STATE_FILE) as f:
            st = json.load(f)
    except Exception:
        st = {}
    if st.get("pair") != PAIR:   # new pair -> start the win/loss count from zero
        st = {"pair": PAIR, "tot": {"W": 0, "L": 0}, "day": {"W": 0, "L": 0}, "day_key": ""}
    st.setdefault("tot", {"W": 0, "L": 0})
    st.setdefault("day", {"W": 0, "L": 0})
    st.setdefault("day_key", "")
    return st


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
    print(f"session started. Total so far: {st['tot']['W']}W - {st['tot']['L']}L", flush=True)
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
              f"| data lag {lag:.0f}s | close {c.Close.iloc[-1]:.{PRICE_DEC}f} | RSI {c.rsi.iloc[-1]:.1f}", flush=True)

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
            send(res_text(res, pending["side"], pending["entry"], st))
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
        send(sig_text(s, entry, False))
    print(f"session ended. Total: {st['tot']['W']}W - {st['tot']['L']}L", flush=True)


def early_check(boundary):
    """~PRE_ALERT_SECONDS before the 2M candle closes: judge it from the candle that is still forming."""
    c = get_candles()
    if c is None or len(c) < 60:
        return None, "no data"
    bucket = pd.Timestamp(boundary - TF * 60, unit="s", tz="UTC")
    if c.index[-1] != bucket:
        return None, f"stale data (last bar {c.index[-1]:%H:%M})"
    if boundary - LAST_RAW.timestamp() > 65:
        return None, f"feed lag (newest 1m bar {LAST_RAW:%H:%M})"
    s = signal(c, len(c) - 1)
    return s, f"forming close {c.Close.iloc[-1]:.{PRICE_DEC}f} | RSI {c.rsi.iloc[-1]:.1f}"


def settle(st, pending):
    """Score a finished trade. Returns True when it was settled (or given up)."""
    # Yahoo's 1-minute bars hold one price each (open = close), so the price when the trade
    # ends is the OPEN of the candle that starts at expiry (not the close of the entry candle).
    c = get_candles()
    nxt = pending["entry"] + pd.Timedelta(minutes=TF)
    if c is None or pending["entry"] not in c.index or nxt not in c.index:
        return False
    if LAST_RAW is None or LAST_RAW < nxt:
        return False   # the feed has not delivered the expiry price yet
    row = c.loc[pending["entry"]]
    res = outcome(pending["side"], row.Open, c.at[nxt, "Open"])
    k = str((pending["entry"] + pd.Timedelta(hours=TZ_HOURS)).date())
    if k != st["day_key"]:
        st["day_key"], st["day"] = k, {"W": 0, "L": 0}
    if res == "WIN":
        st["tot"]["W"] += 1; st["day"]["W"] += 1
    elif res == "LOSS":
        st["tot"]["L"] += 1; st["day"]["L"] += 1
    save_state(st)
    send(res_text(res, pending["side"], pending["entry"], st))
    return True


def live_early():
    started = time.time()
    st = load_state()
    save_state(st)
    pending = None
    last_entry = None
    secs = TF * 60
    print(f"session started (pre-alert {PRE_ALERT_SECONDS}s). Total so far: {st['tot']['W']}W - {st['tot']['L']}L", flush=True)
    while True:
        t = time.time()
        elapsed = t - started
        stopping = elapsed > RUN_MINUTES * 60
        if (stopping and not pending) or elapsed > (RUN_MINUTES + 10) * 60:
            break
        boundary = t - (t % secs) + secs          # next candle start = entry time
        alert_at = boundary - PRE_ALERT_SECONDS
        if alert_at <= t + 1:
            boundary += secs
            alert_at += secs
        wake = alert_at
        if pending and pending["settle_at"] < wake:
            wake = pending["settle_at"]
        time.sleep(max(0, wake - time.time()))

        # 1) settle the open trade once its candle has closed
        if pending and time.time() >= pending["settle_at"]:
            try:
                done = settle(st, pending)
            except Exception as e:
                print("settle error:", e, flush=True)
                done = False
            if done:
                pending = None
            else:
                pending["tries"] += 1
                if pending["tries"] >= 8:
                    print(f"result for {hhmm(pending['entry'])} {pending['side']} not available (price feed). Not counted.", flush=True)
                    pending = None
                else:
                    pending["settle_at"] = time.time() + 12
            continue

        if time.time() < alert_at - 1:
            continue
        if stopping or pending:
            continue

        # 2) look at the forming candle and alert BEFORE the entry time
        if last_entry is not None and (boundary - last_entry) < MIN_GAP * 60:
            continue
        if time.time() > boundary - 8:
            print("pre-alert skipped: too close to entry time", flush=True)
            continue
        try:
            s, note = early_check(boundary)
        except Exception as e:
            print("data error:", e, flush=True)
            continue
        entry = pd.Timestamp(boundary, unit="s", tz="UTC")
        print(f"{utcnow():%H:%M:%S} UTC | check for entry {hhmm(entry)} | {s or 'no signal'} | {note}", flush=True)
        if not s or time.time() > boundary - 8:
            continue
        pending = {"side": s, "entry": entry, "settle_at": boundary + secs + 4, "tries": 0}
        last_entry = boundary
        send(sig_text(s, entry, True))
    send(f"⏸ Session ended · {PAIR}\n"
         f"Rule {ACTIVE}: {st['day']['W']}W-{st['day']['L']}L today · {st['tot']['W']}W-{st['tot']['L']}L total\n"
         f"{next_session_text()}")


def early_data(boundary):
    """Candles incl. the one still forming, if the feed is fresh enough ~PRE_ALERT_SECONDS before entry."""
    c = get_candles()
    if c is None or len(c) < 60:
        return None, "no data"
    bucket = pd.Timestamp(boundary - TF * 60, unit="s", tz="UTC")
    if c.index[-1] != bucket:
        return None, f"stale data (last bar {c.index[-1]:%H:%M})"
    if boundary - LAST_RAW.timestamp() > 65:
        return None, f"feed lag (newest 1m bar {LAST_RAW:%H:%M})"
    return c, f"forming close {c.Close.iloc[-1]:.{PRICE_DEC}f} | RSI {c.rsi.iloc[-1]:.1f}"


def settle_group(st, g):
    """Score every rule that signalled for one entry time. True when done."""
    c = get_candles()
    nxt = g["entry"] + pd.Timedelta(minutes=TF)
    if c is None or g["entry"] not in c.index or nxt not in c.index:
        return False
    if LAST_RAW is None or LAST_RAW < nxt:
        return False
    row = c.loc[g["entry"]]
    lines = []
    for v, side in g["calls"].items():
        res = outcome(side, row.Open, c.at[nxt, "Open"])
        if res == "WIN":
            st["rules"][v]["W"] += 1
        elif res == "LOSS":
            st["rules"][v]["L"] += 1
        icon = {"WIN": "✅", "LOSS": "❌", "DRAW": "➖"}[res]
        word = "BUY" if side == "CALL" else "SELL"
        lines.append(f"{icon} {res} {PAIR} · Rule {v} · {word} {hhmm(g['entry'])} · {st['rules'][v]['W']}W-{st['rules'][v]['L']}L")
    save_state(st)
    send("\n".join(lines))
    return True


def live_preview():
    started = time.time()
    st = load_state()
    st.setdefault("rules", {})
    for v in PREVIEW_RULES:
        st["rules"].setdefault(v, {"W": 0, "L": 0})
    save_state(st)
    secs = TF * 60
    groups = []        # open trades, one group per entry time
    last_entry = {}    # last entry time per rule (epoch seconds)
    board = lambda: "\n".join(f"Rule {v}: {st['rules'][v]['W']}W-{st['rules'][v]['L']}L" for v in PREVIEW_RULES)
    print(f"preview started (rules {', '.join(PREVIEW_RULES)}). Scores:\n{board()}", flush=True)
    while True:
        t = time.time()
        elapsed = t - started
        stopping = elapsed > RUN_MINUTES * 60
        if (stopping and not groups) or elapsed > (RUN_MINUTES + 10) * 60:
            break
        boundary = t - (t % secs) + secs          # next candle start = entry time
        alert_at = boundary - PRE_ALERT_SECONDS
        if alert_at <= t + 1:
            boundary += secs
            alert_at += secs
        wake = min([alert_at] + [g["settle_at"] for g in groups])
        time.sleep(max(0, wake - time.time()))

        # 1) score finished trades
        due = [g for g in groups if time.time() >= g["settle_at"]]
        if due:
            for g in due:
                try:
                    done = settle_group(st, g)
                except Exception as e:
                    print("settle error:", e, flush=True)
                    done = False
                if done:
                    groups.remove(g)
                else:
                    g["tries"] += 1
                    if g["tries"] >= 8:
                        print(f"result for entry {hhmm(g['entry'])} not available (price feed). Not counted.", flush=True)
                        groups.remove(g)
                    else:
                        g["settle_at"] = time.time() + 12
            continue

        if time.time() < alert_at - 1 or stopping:
            continue

        # 2) judge the forming candle with every rule and alert BEFORE the entry time
        if time.time() > boundary - 8:
            print("pre-alert skipped: too close to entry time", flush=True)
            continue
        try:
            c, note = early_data(boundary)
        except Exception as e:
            print("data error:", e, flush=True)
            continue
        entry = pd.Timestamp(boundary, unit="s", tz="UTC")
        calls = {}
        if c is not None:
            for v in PREVIEW_RULES:
                s = signal_variant(v, c, len(c) - 1)
                if s and not (v in last_entry and boundary - last_entry[v] < MIN_GAP * 60):
                    calls[v] = s
        print(f"{utcnow():%H:%M:%S} UTC | check for entry {hhmm(entry)} | signals: {calls or 'none'} | {note}", flush=True)
        if not calls or time.time() > boundary - 8:
            continue
        for v in calls:
            last_entry[v] = boundary
        groups.append({"entry": entry, "calls": calls, "settle_at": boundary + secs + 4, "tries": 0})
        lines = [("🟢 BUY" if sd == "CALL" else "🔴 SELL") + f" {PAIR} {hhmm(entry)} · {EXPIRY} min · Rule {v}" for v, sd in calls.items()]
        send("\n".join(lines) + f"\n⏰ Entry in {PRE_ALERT_SECONDS} seconds")
    send(f"⏸ Session ended · {PAIR}\nScores:\n{board()}\n{next_session_text()}")


if __name__ == "__main__":
    st0 = load_state()
    first = not st0.get("real_candle_report2_done")
    try:
        import backtest
        os.environ["BOT_QUIET"] = "0" if first else "1"   # posted to Telegram once, then log only
        backtest.report()
        if first:
            st0 = load_state()
            st0["real_candle_report2_done"] = True
            save_state(st0)
    except Exception as e:
        print("backtest skipped:", e, flush=True)
    os.environ["BOT_QUIET"] = "0"
    if PREVIEW_RULES:
        live_preview()
    elif PRE_ALERT_SECONDS > 0:
        live_early()
    else:
        live()
