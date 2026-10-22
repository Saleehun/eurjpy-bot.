# One-time tests (posted to Telegram once):
#   1) five new rule families on REAL 2M EUR/USD candles from Deriv (2-minute trades, IQ Option style)
#   2) your three strategies (mean reversion SPY/QQQ, breakout BTC, trend GLD/USO) on Yahoo history
import math, random
import numpy as np
import pandas as pd
import bot

PAYOUT = 0.82


def wilson(w, n, z=1.96):
    if n == 0:
        return 0.0, 0.0
    p = w / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return 100 * (centre - half), 100 * (centre + half)


# ------------------------------------------------------------------ part 1: EUR/USD 2M rules
FAMILIES = {
    "M1": "Bollinger fade: close 2 std below/above the 20-average, bet on a return",
    "M2": "RSI extreme fade: RSI below 25 / above 75, bet on a return",
    "M3": "Streak fade: 3 same-colour candles in a row, bet on a reversal",
    "M4": "Breakout: close beyond the 20-candle high/low, bet on continuation",
    "M5": "Trend + band dip: in a trend, price 1 std against it, bet on the trend",
}


def family_signal(v, c, i):
    if i < 60:
        return None
    r = c.iloc[i]
    if v == "M1":
        if r.Close < r.sma - 2 * r.sd:
            return "CALL"
        if r.Close > r.sma + 2 * r.sd:
            return "PUT"
    elif v == "M2":
        if r.rsi < 25:
            return "CALL"
        if r.rsi > 75:
            return "PUT"
    elif v == "M3":
        a, b = c.iloc[i - 2], c.iloc[i - 1]
        if all(x.Close < x.Open for x in (a, b, r)):
            return "CALL"
        if all(x.Close > x.Open for x in (a, b, r)):
            return "PUT"
    elif v == "M4":
        if r.Close > r.hh:
            return "CALL"
        if r.Close < r.ll:
            return "PUT"
    elif v == "M5":
        if r.fast > r.ema and r.Close < r.sma - 1 * r.sd:
            return "CALL"
        if r.fast < r.ema and r.Close > r.sma + 1 * r.sd:
            return "PUT"
    return None


def run_family(c, v):
    out, last = [], None
    for i in range(60, len(c) - 1):
        s = family_signal(v, c, i)
        if not s:
            continue
        t = c.index[i] + pd.Timedelta(minutes=bot.TF)
        if last is not None and (t - last) < pd.Timedelta(minutes=bot.MIN_GAP):
            continue
        last = t
        out.append((t, bot.outcome(s, c.Open.iloc[i + 1], c.Close.iloc[i + 1])))
    return out


def part1():
    c = bot.get_candles(count=5000)
    if c is None:
        return "EUR/USD rule families: no data"
    c = c[c.index + pd.Timedelta(minutes=bot.TF) <= bot.utcnow()].copy()
    c["sma"] = c.Close.rolling(20).mean()
    c["sd"] = c.Close.rolling(20).std()
    c["hh"] = c.High.rolling(20).max().shift(1)
    c["ll"] = c.Low.rolling(20).min().shift(1)
    cut = c.index[int(len(c) * 2 / 3)]
    lines = [f"📊 New EUR/USD 2M rule ideas · real Deriv candles",
             f"Data: {c.index[0]:%d %b} to {c.index[-1]:%d %b}. Break-even about {100 / (1 + PAYOUT):.0f}% ({int(PAYOUT * 100)}% payout)",
             f"Early = before {cut:%d %b}, Late = after (the rule was not tuned on either)", ""]
    for v, name in FAMILIES.items():
        tr = run_family(c, v)
        def stat(sel):
            w = sum(r == "WIN" for t, r in sel)
            l = sum(r == "LOSS" for t, r in sel)
            return w, l
        w, l = stat(tr)
        we, le = stat([x for x in tr if x[0] < cut])
        wl, ll_ = stat([x for x in tr if x[0] >= cut])
        n = w + l
        lo, hi = wilson(w, n)
        pc = lambda a, b: f"{100 * a / (a + b):.0f}%" if a + b else "-"
        lines.append(f"{v}) {name}\n   {n} trades, win {pc(w, l)} (likely {lo:.0f}-{hi:.0f}%) | early {pc(we, le)} | late {pc(wl, ll_)}")
    return "\n".join(lines)


# ------------------------------------------------------------------ part 2: strategies
def atr(df, n=14):
    pc = df.Close.shift(1)
    tr = pd.concat([df.High - df.Low, (df.High - pc).abs(), (df.Low - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def simulate(df, entry, mode, k=None):
    """entry[i] = +1/-1/0 decided at the close of bar i, executed at the open of bar i+1.
    mode 'sma'  : exit when the close returns to the moving average (df.sma)
    mode 'trail': trailing stop of k * ATR; an opposite entry flips the position.
    Returns the list of trade returns (fractions)."""
    o, h, lo, c = df.Open.values, df.High.values, df.Low.values, df.Close.values
    sma = df["sma"].values if mode == "sma" else None
    at = df["atr"].values
    trades, pos, ep, stop, extreme = [], 0, 0.0, None, 0.0
    pending, exit_next = 0, False
    for i in range(len(df)):
        if exit_next and pos != 0:
            trades.append(pos * (o[i] / ep - 1))
            pos = 0
        exit_next = False
        if pending != 0 and pos == 0:
            pos, ep, extreme = pending, o[i], o[i]
            a = at[i - 1] if i > 0 and not np.isnan(at[i - 1]) else at[i]
            if mode == "trail":
                stop = ep - pos * k * a
        pending = 0
        if pos != 0 and mode == "trail":
            if pos > 0 and lo[i] <= stop:
                trades.append(min(stop, o[i]) / ep - 1)
                pos = 0
            elif pos < 0 and h[i] >= stop:
                trades.append(-(max(stop, o[i]) / ep - 1))
                pos = 0
            elif pos > 0:
                extreme = max(extreme, h[i])
                stop = max(stop, extreme - k * at[i])
            else:
                extreme = min(extreme, lo[i])
                stop = min(stop, extreme + k * at[i])
        s = int(entry[i])
        if mode == "sma":
            if pos != 0 and ((pos > 0 and c[i] >= sma[i]) or (pos < 0 and c[i] <= sma[i])):
                exit_next = True
            elif pos == 0 and s != 0:
                pending = s
        else:
            if pos == 0 and s != 0:
                pending = s
            elif pos != 0 and s == -pos:
                exit_next, pending = True, s
    if pos != 0:
        trades.append(pos * (c[-1] / ep - 1))
    return trades


def summary(tr):
    if not tr:
        return None
    a = np.array(tr)
    wins, losses = a[a > 0].sum(), -a[a < 0].sum()
    return {"n": len(a), "win": 100 * (a > 0).mean(), "avg": 100 * a.mean(),
            "total": 100 * a.sum(), "pf": (wins / losses) if losses > 0 else float("inf")}


def random_entries(entry, rng):
    n_sig = int((np.asarray(entry) != 0).sum())
    ok = np.where(~np.isnan(np.asarray(entry, dtype=float)))[0]
    pick = rng.choice(ok, size=min(n_sig, len(ok)), replace=False)
    e = np.zeros(len(entry))
    e[pick] = rng.choice([-1, 1], size=len(pick))
    return e


def evaluate(label, df, entry, mode, k=None, runs=150):
    tr = simulate(df, entry, mode, k)
    s = summary(tr)
    bh = 100 * (df.Close.iloc[-1] / df.Close.iloc[0] - 1)
    if not s:
        return f"{label}: no trades in {len(df)} bars"
    rng = np.random.default_rng(1)
    beat = 0
    for _ in range(runs):
        rs = summary(simulate(df, random_entries(entry, rng), mode, k))
        if rs and rs["total"] >= s["total"]:
            beat += 1
    pf = "inf" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
    note = "  (too few trades to judge)" if s["n"] < 30 else ""
    return (f"{label}: {s['n']} trades, win {s['win']:.0f}%, avg {s['avg']:+.2f}%, total {s['total']:+.1f}%, "
            f"profit factor {pf}\n   buy&hold {bh:+.1f}% | random entries did as well {100 * beat / runs:.0f}% of the time{note}")


def yf_get(sym, interval, period):
    import yfinance as yf
    d = yf.download(sym, interval=interval, period=period, progress=False, auto_adjust=False)
    if d is None or d.empty:
        return None
    if isinstance(d.columns, pd.MultiIndex):
        d.columns = d.columns.get_level_values(0)
    d.index = pd.to_datetime(d.index, utc=True)
    return d[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["Open", "High", "Low", "Close"])


def to_4h(d):
    d = d.copy()
    d["day"] = d.index.date
    d["blk"] = d.groupby("day").cumcount() // 4
    g = d.groupby(["day", "blk"])
    out = g.agg({"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"})
    out.index = g.apply(lambda x: x.index[0]).values
    return out.sort_index()


def strat1(sym, k):
    d = yf_get(sym, "15m", "60d")
    if d is None or len(d) < 100:
        return f"S1 {sym}: no data"
    d["sma"] = d.Close.rolling(20).mean()
    sd = d.Close.rolling(20).std()
    d["atr"] = atr(d)
    e = np.zeros(len(d))
    e[(d.Close < d["sma"] - k * sd).values] = 1
    e[(d.Close > d["sma"] + k * sd).values] = -1
    e[np.isnan(d["sma"].values)] = 0
    return evaluate(f"S1 {sym} 15m ({k} std)", d, e, "sma")


def strat2(sym):
    d = yf_get(sym, "1h", "730d")
    if d is None or len(d) < 100:
        return f"S2 {sym}: no data"
    d["atr"] = atr(d)
    hh, ll = d.High.rolling(20).max().shift(1), d.Low.rolling(20).min().shift(1)
    vol_ok = d.Volume >= 1.5 * d.Volume.rolling(20).mean().shift(1)
    e = np.zeros(len(d))
    e[((d.Close > hh) & vol_ok).values] = 1
    e[((d.Close < ll) & vol_ok).values] = -1
    return evaluate(f"S2 {sym} 1h (2xATR stop)", d, e, "trail", k=2)


def strat3(sym):
    d = yf_get(sym, "1h", "730d")
    if d is None or len(d) < 100:
        return f"S3 {sym}: no data"
    d = to_4h(d)
    if len(d) < 260:
        return f"S3 {sym}: only {len(d)} four-hour bars, too short for a 200 EMA"
    d["atr"] = atr(d)
    f, s = d.Close.ewm(span=50, adjust=False).mean(), d.Close.ewm(span=200, adjust=False).mean()
    up = (f > s) & (f.shift(1) <= s.shift(1))
    dn = (f < s) & (f.shift(1) >= s.shift(1))
    e = np.zeros(len(d))
    e[up.values] = 1
    e[dn.values] = -1
    e[:200] = 0
    return evaluate(f"S3 {sym} 4h (3xATR stop)", d, e, "trail", k=3)


def part2():
    lines = ["📊 Your 3 strategies on Yahoo history (no costs, no slippage)", ""]
    for fn, args in [(strat1, ("SPY", 1.5)), (strat1, ("QQQ", 1.8)), (strat2, ("BTC-USD",)),
                     (strat3, ("GLD",)), (strat3, ("USO",))]:
        try:
            lines.append(fn(*args))
        except Exception as e:
            lines.append(f"{fn.__name__} {args[0]}: failed ({str(e)[:60]})")
    lines.append("")
    lines.append("15m stock data covers only ~60 days; 1h data 2 years. Small samples: treat as a first look.")
    return "\n".join(lines)


def run_once():
    st = bot.load_state()
    if st.get("moretest_done"):
        return
    for part in (part1, part2):
        try:
            bot.send(part())
        except Exception as e:
            print("moretest part failed:", e, flush=True)
    st = bot.load_state()
    st["moretest_done"] = True
    bot.save_state(st)
