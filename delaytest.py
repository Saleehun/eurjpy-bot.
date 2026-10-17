# One-time test: what happens to each rule if you enter LATER than the candle close?
# Signal = finished 2M candle (as in the earlier comparison). Prices come from 1-minute bars.
import json, math
import pandas as pd
import bot

PAYOUT = 0.82                   # assumed IQ Option payout
WIN_START, WIN_END = 7, 17      # London + New York hours (UTC)
SCHEMES = {                     # name: (minutes after the signal, trade minutes)
    "now": (0, 2),              # enter at once, 2 min trade
    "d1": (1, 1),               # enter 1 min later, 1 min trade
    "d2": (1, 2),               # enter 1 min later, 2 min trade
}
# Price at entry = open of the 1M bar that starts then; price at expiry = open of the 1M bar
# that starts when the trade ends. (Yahoo's own 1M bars often have open = close.)


def raw_1m():
    import yfinance as yf
    d = yf.download(bot.SYMBOL, period="5d", interval="1m", progress=False, auto_adjust=False)
    if d is None or d.empty:
        return None
    if isinstance(d.columns, pd.MultiIndex):
        d.columns = d.columns.get_level_values(0)
    d.index = pd.to_datetime(d.index, utc=True)
    return d


def run(c, m1, v):
    T = pd.Timedelta(minutes=1)
    out = {k: [] for k in list(SCHEMES) + ["bot"]}
    sig_times = []
    last = None
    for i in range(60, len(c) - 2):
        s = bot.signal_variant(v, c, i)
        if not s:
            continue
        t = c.index[i] + pd.Timedelta(minutes=bot.TF)      # the finished candle's end
        if last is not None and (t - last) < pd.Timedelta(minutes=bot.MIN_GAP):
            continue
        last = t
        sig_times.append(t)
        for k, (delay, mins) in SCHEMES.items():
            a, e = t + delay * T, t + (delay + mins) * T
            if a in m1.index and e in m1.index:
                out[k].append(bot.outcome(s, m1.at[a, "Open"], m1.at[e, "Open"]))
        b = t + T      # how the live bot scores: open of first bar vs close of the last bar
        if t in m1.index and b in m1.index:
            out["bot"].append(bot.outcome(s, m1.at[t, "Open"], m1.at[b, "Close"]))
    return out, sig_times


def pct(lst):
    w, l = lst.count("WIN"), lst.count("LOSS")
    return f"{100 * w / (w + l):.0f}%" if w + l else "-"


def report():
    m1 = raw_1m()
    if m1 is None:
        print("delay test: no data", flush=True)
        return
    m1 = m1[m1.index + pd.Timedelta(minutes=1) <= bot.utcnow()]
    c = m1.resample(f"{bot.TF}min").agg(
        {"Open": "first", "High": "max", "Low": "min", "Close": "last"}).dropna()
    c = c[c.index + pd.Timedelta(minutes=bot.TF) <= bot.utcnow()]
    c = bot.add_indicators(c)
    mask = (c.index.hour >= WIN_START) & (c.index.hour < WIN_END) & (c.index.dayofweek < 5)
    hours = mask.sum() * bot.TF / 60
    same = 100 * (m1.Open == m1.Close).mean()
    cont = 100 * (m1.Open == m1.Close.shift(1)).mean()
    lines = [f"📊 Delayed-entry test 2 · {bot.PAIR}",
             f"Data check: 1m bars with open = close: {same:.0f}% | open = previous close: {cont:.0f}%",
             f"Break-even about {100 / (1 + PAYOUT):.0f}% (payout {int(PAYOUT * 100)}%)",
             "bot = scored as the live bot does",
             "now = enter at close, price 2 min later",
             "+1m/1m, +1m/2m = enter 1 min later, trade 1 or 2 min",
             "Same 5 days the rules were picked on: treat as optimistic", ""]
    for v in bot.RULES:
        out, times = run(c, m1, v)
        n = len([r for r in out["now"] if r != "DRAW"])
        inwin = sum(WIN_START <= t.hour < WIN_END and t.dayofweek < 5 for t in times)
        gap = f"1 every {hours * 60 / inwin:.0f} min" if inwin else "none"
        lines.append(f"{v}) {n} trades, {gap}\n   bot {pct(out['bot'])} | now {pct(out['now'])} | +1m/1m {pct(out['d1'])} | +1m/2m {pct(out['d2'])}")
    bot.send("\n".join(lines))


def run_once():
    """Send the report only the first time (remembered in state.json)."""
    st = bot.load_state()
    if st.get("delay_test2_done"):
        return
    report()
    st["delay_test2_done"] = True
    bot.save_state(st)
