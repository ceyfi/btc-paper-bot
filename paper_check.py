#!/usr/bin/env python3
"""
Paper-trading provera za 4h trend strategiju (EMA21/55 + EMA200 + ADX20, SL 2xATR, TP 4xATR).

Dva nacina rada:
  python paper_check.py --fetch          # sam povuce podatke (GitHub Actions / lokalno)
  python paper_check.py <fajl> [...]     # parsira vec sacuvane odgovore API-ja

Obradjuje SVE nove zatvorene 4h svece od poslednje provere (prvo SL/TP unutar
svece, pa signal na zatvorenoj sveci), pa azurira:
  paper_state.json  (kapital, pozicija)
  paper_log.csv     (istorija dogadjaja)
Linije "EVENT:" u izlazu su promene (ulaz/izlaz). Cista simulacija, bez para.
"""
import json
import math
import os
import re
import sys
import csv
from datetime import datetime, timezone

# parametri strategije (isti kao backtest 4h varijanta)
EMA_FAST, EMA_SLOW, EMA_TREND = 21, 55, 200
ADX_LEN, ADX_MIN = 14, 20.0
ATR_LEN, SL_ATR, TP_ATR = 14, 2.0, 4.0
RISK_PCT = 0.01
ALLOW_SHORT = False
START_EQUITY = 100.0
COST = 0.0012  # spot taker 0.1% + slippage 0.02%, po strani

TF_MS = 4 * 3600 * 1000
BASE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(BASE, "paper_state.json")
LOG = os.path.join(BASE, "paper_log.csv")

CANDLE_RE = re.compile(
    r'\[(\d{13}),([\d.eE+-]+),([\d.eE+-]+),([\d.eE+-]+),([\d.eE+-]+),([\d.eE+-]+)\]')


def fetch_rows():
    import requests
    url = ("https://api-pub.bitfinex.com/v2/candles/"
           "trade%3A1h%3AtBTCUSD/hist?limit=2000")
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    data = r.json()  # [mts, open, close, high, low, volume], najnovije prvo
    rows = {int(x[0]): (float(x[1]), float(x[3]), float(x[4]), float(x[2]), float(x[5]))
            for x in data}
    return dict(sorted(rows.items()))


def parse_files(paths):
    rows = {}
    for p in paths:
        raw = open(p, errors="ignore").read()
        for m in CANDLE_RE.finditer(raw):
            ts = int(m.group(1))
            o, c, h, l, v = (float(m.group(i)) for i in range(2, 7))
            rows[ts] = (o, h, l, c, v)
    return dict(sorted(rows.items()))


def to_4h(rows1h):
    out = {}
    for ts, (o, h, l, c, v) in rows1h.items():
        b = ts // TF_MS
        if b not in out:
            out[b] = [b * TF_MS, o, h, l, c, v, 1]
        else:
            r = out[b]
            r[2] = max(r[2], h); r[3] = min(r[3], l); r[4] = c; r[5] += v; r[6] += 1
    # zadrzi samo kompletne bucket-e (4 x 1h)
    return [r[:6] for r in out.values() if r[6] == 4]


def ema_series(vals, n):
    k = 2 / (n + 1)
    out, e = [], None
    for v in vals:
        e = v if e is None else v * k + e * (1 - k)
        out.append(e)
    return out


def rma(vals, n):
    out, e = [], None
    a = 1 / n
    for v in vals:
        e = v if e is None else v * a + e * (1 - a)
        out.append(e)
    return out


def indicators(c4):
    o = [r[1] for r in c4]; h = [r[2] for r in c4]
    l = [r[3] for r in c4]; c = [r[4] for r in c4]
    n = len(c4)
    ef, es, et = ema_series(c, EMA_FAST), ema_series(c, EMA_SLOW), ema_series(c, EMA_TREND)
    tr, pdm, mdm = [], [], []
    for i in range(n):
        if i == 0:
            tr.append(h[i] - l[i]); pdm.append(0.0); mdm.append(0.0); continue
        tr.append(max(h[i] - l[i], abs(h[i] - c[i-1]), abs(l[i] - c[i-1])))
        up, dn = h[i] - h[i-1], l[i-1] - l[i]
        pdm.append(up if (up > dn and up > 0) else 0.0)
        mdm.append(dn if (dn > up and dn > 0) else 0.0)
    atr = rma(tr, ATR_LEN)
    atr_a = rma(tr, ADX_LEN)
    pdi = [100 * x / y if y else 0 for x, y in zip(rma(pdm, ADX_LEN), atr_a)]
    mdi = [100 * x / y if y else 0 for x, y in zip(rma(mdm, ADX_LEN), atr_a)]
    dx = [100 * abs(p - m) / (p + m) if (p + m) else 0 for p, m in zip(pdi, mdi)]
    adx = rma(dx, ADX_LEN)
    return o, h, l, c, ef, es, et, atr, adx


def load_state():
    if os.path.exists(STATE):
        return json.load(open(STATE))
    return {"equity": START_EQUITY, "pos": 0, "entry": 0.0, "entry_eff": 0.0,
            "sl": 0.0, "tp": 0.0, "qty": 0.0, "last_ts": 0}


def log_event(ts, event, direction, price, pnl, equity):
    new = not os.path.exists(LOG)
    with open(LOG, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["candle_utc", "event", "dir", "price", "pnl", "equity"])
        w.writerow([datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
                    event, direction, f"{price:.2f}", f"{pnl:.4f}", f"{equity:.2f}"])


def main():
    args = sys.argv[1:]
    if args and args[0] == "--fetch":
        rows1h = fetch_rows()
    elif args:
        rows1h = parse_files(args)
    else:
        print("Upotreba: paper_check.py --fetch | <fajlovi...>"); return
    c4 = to_4h(rows1h)
    if len(c4) < EMA_TREND + ADX_LEN + 5:
        print(f"UPOZORENJE: samo {len(c4)} kompletnih 4h sveca — treba bar "
              f"{EMA_TREND + ADX_LEN + 5} za pouzdane indikatore.")
    if len(c4) < 60:
        print("GRESKA: premalo podataka, prekid."); return
    o, h, l, c, ef, es, et, atr, adx = indicators(c4)
    st = load_state()
    events = 0

    for i in range(1, len(c4)):
        ts = c4[i][0]
        if ts <= st["last_ts"]:
            continue
        # 1) upravljanje otvorenom pozicijom unutar ove svece (konzervativno: prvo SL)
        if st["pos"] == 1:
            if l[i] <= st["sl"]:
                px = st["sl"] * (1 - COST)
                pnl = (px - st["entry_eff"]) * st["qty"]
                st["equity"] += pnl; st["pos"] = 0
                log_event(ts, "CLOSE(SL)", "LONG", st["sl"], pnl, st["equity"])
                print(f"EVENT: SL @ {st['sl']:.2f}  pnl={pnl:+.2f}  equity={st['equity']:.2f}")
                events += 1
            elif h[i] >= st["tp"]:
                px = st["tp"] * (1 - COST)
                pnl = (px - st["entry_eff"]) * st["qty"]
                st["equity"] += pnl; st["pos"] = 0
                log_event(ts, "CLOSE(TP)", "LONG", st["tp"], pnl, st["equity"])
                print(f"EVENT: TP @ {st['tp']:.2f}  pnl={pnl:+.2f}  equity={st['equity']:.2f}")
                events += 1
        # 2) signal na zatvorenoj sveci i
        cu = ef[i] > es[i] and ef[i-1] <= es[i-1]
        cd = ef[i] < es[i] and ef[i-1] >= es[i-1]
        if st["pos"] == 1 and cd:
            px = c[i] * (1 - COST)
            pnl = (px - st["entry_eff"]) * st["qty"]
            st["equity"] += pnl; st["pos"] = 0
            log_event(ts, "CLOSE(flip)", "LONG", c[i], pnl, st["equity"])
            print(f"EVENT: FLIP izlaz @ {c[i]:.2f}  pnl={pnl:+.2f}  equity={st['equity']:.2f}")
            events += 1
        if st["pos"] == 0 and cu and c[i] > et[i] and adx[i] > ADX_MIN \
                and math.isfinite(atr[i]) and atr[i] > 0:
            stop = SL_ATR * atr[i]
            qty = min(st["equity"] * RISK_PCT / stop, st["equity"] / c[i])
            st.update(pos=1, entry=c[i], entry_eff=c[i] * (1 + COST),
                      sl=c[i] - stop, tp=c[i] + TP_ATR * atr[i], qty=qty)
            log_event(ts, "OPEN", "LONG", c[i], 0.0, st["equity"])
            print(f"EVENT: LONG ulaz @ {c[i]:.2f}  qty={qty:.6f} BTC  "
                  f"SL={st['sl']:.2f}  TP={st['tp']:.2f}")
            events += 1
        st["last_ts"] = ts

    st["last_price"] = c[-1]
    st["updated_utc"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    json.dump(st, open(STATE, "w"), indent=1)
    last_t = datetime.fromtimestamp(st["last_ts"] / 1000, tz=timezone.utc)
    pos_txt = "LONG" if st["pos"] == 1 else "nema pozicije"
    print(f"---\nStanje: equity=${st['equity']:.2f} | {pos_txt}"
          + (f" (ulaz {st['entry']:.2f}, SL {st['sl']:.2f}, TP {st['tp']:.2f})" if st["pos"] else "")
          + f" | poslednja 4h sveca: {last_t:%Y-%m-%d %H:%M} UTC | cena: {c[-1]:.2f}")
    if events == 0:
        print("Bez promena.")


if __name__ == "__main__":
    main()
