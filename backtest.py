# Compares the rule sets in bot.py on the last ~5 days of real market data for the pair set in bot.py.
# NOTE: it scores finished candles; the 30-second early alert in bot.py decides on an unfinished candle, so live results can be lower.
import math
import pandas as pd
import bot

SEND_TO_TELEGRAM = True   # posts the comparison once to your channel
WIN_START, WIN_END = 7, 17  # London + New York hours (UTC) used for signals-per-hour
PAYOUT = 0.82             # IQ Option payout shown for the pair (82% -> break-even about 55%)


def wilson(w, n, z=1.96):
    if n == 0:
        return 0.0, 0.0
    p = w / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return 100 * (centre - half), 100 * (centre + half)


def run(c, v):
    trades, last = [], None
    for i in range(60, len(c) - 1):
        s = bot.signal_variant(v, c, i)
        if not s:
            continue
        t = c.index[i] + pd.Timedelta(minutes=bot.TF)
        if last is not None and (t - last) < pd.Timedelta(minutes=bot.MIN_GAP):
            continue
        last = t
        trades.append((t, bot.outcome(s, c.Open.iloc[i + 1], c.Close.iloc[i + 1])))
    return trades


def report():
    c = bot.get_candles()
    if c is None:
        print("backtest: no data", flush=True)
        return
    c = c[c.index + pd.Timedelta(minutes=bot.TF) <= bot.utcnow()]
    idx = c.index
    in_win = (idx.hour >= WIN_START) & (idx.hour < WIN_END) & (idx.dayofweek < 5)
    hours = in_win.sum() * bot.TF / 60
    lines = [f"📊 {bot.PAIR} {bot.TF}M rule comparison",
             f"Data: {idx[0]:%d %b} to {idx[-1]:%d %b}, {WIN_START}:00-{WIN_END}:00 UTC = {hours:.0f} hours",
             f"Break-even is about {100 / (1 + PAYOUT):.0f}% (at {PAYOUT * 100:.0f}% payout)",
             "Finished-candle timing, same 5 days the rule was picked on: treat as optimistic", ""]
    for v in bot.RULES:
        tr = run(c, v)
        w = sum(r == "WIN" for _, r in tr)
        l = sum(r == "LOSS" for _, r in tr)
        n = w + l
        rate = 100 * w / n if n else 0
        lo, hi = wilson(w, n)
        inwin = sum(WIN_START <= t.hour < WIN_END and t.dayofweek < 5 for t, _ in tr)
        gap = f"1 every {hours * 60 / inwin:.0f} min" if inwin else "none"
        lines.append(f"{v}) {bot.RULES[v]}\n   {w}W-{l}L, win {rate:.0f}% (true rate likely {lo:.0f}-{hi:.0f}%), signals: {gap}")
    text = "\n".join(lines)
    print(text, flush=True)
    if SEND_TO_TELEGRAM:
        bot.send(text)
