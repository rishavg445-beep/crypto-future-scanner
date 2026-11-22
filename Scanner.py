#!/usr/bin/env python3
"""CoinDCX INR-M futures pump scanner. Runs hourly on GitHub Actions. Standard library only."""
import json, os, random, sys, time, traceback, urllib.request, urllib.parse
from datetime import datetime, timezone, timedelta

BASE = "https://api.coindcx.com"
PUB = "https://public.coindcx.com"
DATA = "data"
IST = timezone(timedelta(hours=5, minutes=30))
BIG = {"BTC","ETH","BNB","XRP","SOL","DOGE","ADA","TRX","LINK","LTC","BCH","DOT","AVAX","TON","SHIB","XLM","HBAR","SUI","UNI","ATOM"}
DEFAULT = {"version": 1,
           "weights": {"vol": 10.0, "squeeze": 25.0, "mom": 1.0, "fund": 1.0},
           "max_chg24": 20.0, "stop_atr": 3.0, "t1_r": 2.0, "t2_r": 4.0, "top_n": 10}
LOGS = []
MIN_POS = 8

def now(): return int(time.time())
def log(m):
    s = "%s %s" % (datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"), m)
    print(s); LOGS.append(s)

def get(url, tries=3):
    err = ""
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            err = str(e); time.sleep(1.5 * (i + 1))
    log("FAILED %s -> %s" % (url[:140], err))
    return None

def coin_of(pair):
    return pair.split("-", 1)[-1].split("_")[0].upper()

def get_instruments():
    d = get(BASE + "/exchange/v1/derivatives/futures/data/active_instruments?margin_currency_short_name[]=INR")
    if d is None: return []
    if isinstance(d, dict):
        log("instruments came as dict, keys: %s" % list(d)[:8])
        d = d.get("instruments") or d.get("data") or []
    out = []
    for x in d:
        p = x if isinstance(x, str) else (x.get("pair") or x.get("symbol") or x.get("instrument") or x.get("name"))
        if p: out.append(p)
    log("instruments: %d, sample %s" % (len(out), out[:3]))
    return out

def get_prices():
    """Returns {pair: {'fr': funding or None}}; logs one raw sample so the format can be checked."""
    d = get(PUB + "/market_data/v3/current_prices/futures/rt")
    res = {}
    if not isinstance(d, dict): return res
    node = d.get("prices") or d.get("vs") or d
    for k, v in node.items():
        if isinstance(v, dict):
            fr = None
            for key in ("fr", "funding_rate", "efr"):
                if v.get(key) is not None:
                    try: fr = float(v[key])
                    except Exception: pass
                    break
            res[k] = {"fr": fr}
    if node:
        k0 = next(iter(node)); log("price sample %s: %s" % (k0, str(node[k0])[:250]))
    return res

def parse_rows(rows):
    out = []
    for r in rows or []:
        try:
            if isinstance(r, dict):
                t = float(r.get("time") or r.get("t") or r.get("open_time"))
                o, h, l, c = (float(r[k]) for k in ("open", "high", "low", "close"))
                v = float(r.get("volume") or r.get("v") or 0)
            else:
                t, o, h, l, c, v = (float(x) for x in r[:6])
            if t > 1e12: t /= 1000
            out.append({"t": int(t), "o": o, "h": h, "l": l, "c": c, "v": v})
        except Exception:
            continue
    out.sort(key=lambda x: x["t"])
    return out

def get_candles(pair, hours=170):
    to = now(); frm = to - hours * 3600
    for res in ("60", "1h"):
        d = get("%s/market_data/candlesticks?pair=%s&from=%d&to=%d&resolution=%s&pcode=f" %
                (PUB, urllib.parse.quote(pair), frm, to, res), tries=2)
        rows = d.get("data") if isinstance(d, dict) else d
        c = parse_rows(rows)
        if len(c) >= 30: return c
    return []

def mean(a): return sum(a) / len(a) if a else 0.0

def feats(c):
    n = len(c)
    if n < 60: return None
    v = [x["v"] for x in c]
    base = sum(v[:-4]) / (n - 4) * 4
    vr = sum(v[-4:]) / base if base > 0 else 0.0
    def rng(seg):
        last = seg[-1]["c"]
        return (max(x["h"] for x in seg) - min(x["l"] for x in seg)) / last if last > 0 else 0.0
    prev = [rng(c[n - 24 * (d + 1): n - 24 * d]) for d in range(1, 7) if n - 24 * (d + 1) >= 0]
    pm = mean(prev)
    sq = rng(c[-24:]) / pm if pm > 0 else 1.0
    mom4 = (c[-1]["c"] / c[-5]["c"] - 1) * 100 if c[-5]["c"] > 0 else 0.0
    chg24 = (c[-1]["c"] / c[-25]["c"] - 1) * 100 if n >= 25 and c[-25]["c"] > 0 else 0.0
    atr = mean([(x["h"] - x["l"]) / x["c"] for x in c[-24:] if x["c"] > 0])
    vol24 = sum(v[-24:])
    return {"vr": round(vr, 3), "sq": round(sq, 3), "mom4": round(mom4, 2), "chg24": round(chg24, 2),
            "atr": round(atr, 5), "vol24": vol24}

def peak_event(c, i):
    """Pump = a 50%+ rise from a low to a high inside the 24 candles after position i. Returns (gain %, start index) or None."""
    w = c[i + 1:i + 25]
    if len(w) < 24: return None
    pi = max(range(24), key=lambda k: w[k]["h"])
    li = min(range(pi + 1), key=lambda k: w[k]["l"])
    low = w[li]["l"]
    if low <= 0: return None
    return (w[pi]["h"] / low - 1) * 100, i + 1 + li

def collect(c, positions, p_neg, rnd):
    """Find real pumps (features taken just BEFORE the low) and sample quiet non-pump moments."""
    ev, ng, skip = [], [], -1
    for i in positions:
        if i < skip: continue
        r = peak_event(c, i)
        if not r: continue
        gain, s = r
        if gain >= 50:
            f = feats(c[:s])
            if f: ev.append((c[s]["t"], round(gain, 1), f))
            skip = i + 24
        elif gain < 15 and rnd.random() < p_neg:
            f = feats(c[:i + 1])
            if f: ng.append((c[i]["t"], f))
    return ev, ng

def score(f, fund, w):
    s = min(f["vr"], 6) * w["vol"] + max(0, 1 - f["sq"]) * w["squeeze"] + min(abs(f["mom4"]), 5) * w["mom"]
    if fund is not None: s += min(abs(fund) * 1e4, 10) * w["fund"]
    return s

def levels(price, atr, low12, m, r1, r2):
    lo = price * (1 - 0.5 * atr); mid = (lo + price) / 2
    stop = min(low12, lo * (1 - m * atr))
    risk = mid - stop
    return {"entry_lo": lo, "entry_hi": price, "mid": mid, "stop": stop, "t1": mid + r1 * risk,
            "t2": mid + r2 * risk, "risk_pct": risk / mid * 100 if mid > 0 else 0}

def leverage(risk_pct):
    rf = max(risk_pct, 0.1) / 100
    return max(1, min(30, int(0.2 / rf))), max(1, min(30, int(0.7 / rf)))

def simulate(price, lv, r1, path):
    risk = lv["mid"] - lv["stop"]
    for hi, lo in path:
        if price * (1 + lo / 100) <= lv["stop"]: return "STOP", -1.0
        if price * (1 + hi / 100) >= lv["t1"]: return "T1", float(r1)
    return "OPEN", 0.0

def grade(pick, candles):
    t0 = pick["t"]; start = t0 - t0 % 3600 + 3600
    win = [x for x in candles if start <= x["t"] < start + 24 * 3600]
    if len(win) < 22: return None
    p = pick["p"]
    path = [[round((x["h"] / p - 1) * 100, 1), round((x["l"] / p - 1) * 100, 1)] for x in win]
    peak_i = max(range(len(win)), key=lambda i: win[i]["h"])
    peak = (win[peak_i]["h"] / p - 1) * 100
    dip = (min(x["l"] for x in win[:peak_i + 1]) / p - 1) * 100
    end = (win[-1]["c"] / p - 1) * 100
    return {"peak": round(peak, 1), "dip": round(dip, 1), "end": round(end, 1), "path": path,
            "grade": "HIT" if peak >= 50 else "PARTIAL" if peak >= 15 else "MISS"}

def auc(pos, neg, w):
    sp = [score(f, None, w) for f in pos]; sn = [score(f, None, w) for f in neg]
    if not sp or not sn: return 0.5
    wins = sum(1.0 if a > b else 0.5 if a == b else 0.0 for a in sp for b in sn)
    return wins / (len(sp) * len(sn))

def effect(pos, neg, fn):
    a = [fn(f) for f in pos]; b = [fn(f) for f in neg]
    if len(a) < 2 or len(b) < 2: return 0.0
    def sd(x):
        m = mean(x); return (sum((i - m) ** 2 for i in x) / (len(x) - 1)) ** 0.5
    pooled = ((sd(a) ** 2 + sd(b) ** 2) / 2) ** 0.5
    return (mean(a) - mean(b)) / pooled if pooled > 0 else 0.0

def review(st, S):
    """Nightly self-improvement with guardrails. Returns a short text for the history."""
    notes = []
    ex = sorted(S["ex"], key=lambda e: e[0])
    pos = [e for e in ex if e[4] == 1]; neg = [e for e in ex if e[4] == 0]
    notes.append("learning data: %d pumps, %d quiet samples" % (len(pos), len(neg)))
    if len(pos) >= MIN_POS and len(neg) >= 40:
        cp, cn = int(len(pos) * 0.7), int(len(neg) * 0.7)
        fd = lambda e: {"vr": e[1], "sq": e[2], "mom4": e[3]}
        tp, hp = [fd(e) for e in pos[:cp]], [fd(e) for e in pos[cp:]]
        tn, hn = [fd(e) for e in neg[:cn]], [fd(e) for e in neg[cn:]]
        if len(hp) >= 3 and len(hn) >= 20:
            base = DEFAULT["weights"]; cur = st["weights"]
            fns = {"vol": lambda f: min(f["vr"], 6), "squeeze": lambda f: max(0, 1 - f["sq"]), "mom": lambda f: min(abs(f["mom4"]), 5)}
            new = dict(cur)
            for k, fn in fns.items():
                d = effect(tp, tn, fn)
                new[k] = round(max(0.3 * base[k], min(2 * base[k], base[k] * (1 + 0.5 * d))), 2)
            a_old, a_new = auc(hp, hn, cur), auc(hp, hn, new)
            if a_new >= a_old + 0.03:
                st["weights"] = new; st["version"] += 1
                notes.append("weights changed to %s (test score %.2f -> %.2f)" % (new, a_old, a_new))
            else:
                notes.append("weights kept (test score old %.2f, new %.2f; need +0.03)" % (a_old, a_new))
        else:
            notes.append("weights kept (test set too small)")
    else:
        notes.append("weights kept (need %d pumps and 40 quiet samples)" % MIN_POS)
    outs = [o for o in S["outcomes"] if o.get("path") and o.get("atr")]
    if len(outs) >= 20:
        def total(m, r1):
            tot = 0.0
            for o in outs:
                lv = levels(o["p"], o["atr"], o["low12"], m, r1, r1 * 2)
                tot += simulate(o["p"], lv, r1, o["path"])[1]
            return tot
        cur_t = total(st["stop_atr"], st["t1_r"])
        best = max(((m, r), total(m, r)) for m in (2, 3, 4, 5) for r in (1.5, 2, 3, 4))
        if best[1] > cur_t * 1.1 + 1:
            st["stop_atr"], st["t1_r"] = best[0]; st["t2_r"] = best[0][1] * 2; st["version"] += 1
            notes.append("stop/target changed to stop %.0f x ATR, T1 %.1f R (total R %.1f -> %.1f)" % (best[0][0], best[0][1], cur_t, best[1]))
        else:
            notes.append("stop/target kept (total R now %.1f, best %.1f)" % (cur_t, best[1]))
    else:
        notes.append("stop/target kept (need 20 graded picks, have %d)" % len(outs))
    return "; ".join(notes)

def load():
    try:
        S = json.load(open(DATA + "/state.json"))
    except Exception:
        S = {}
    S.setdefault("strategy", json.loads(json.dumps(DEFAULT)))
    for k in ("picks", "outcomes", "movers", "history", "ex"): S.setdefault(k, [])
    S["movers"] = [m for m in S["movers"] if "gain" in m]
    S.setdefault("backfilled", False)
    S.setdefault("last_review", "")
    return S

def stats(outs, since=0):
    o = [x for x in outs if x["t"] >= since]
    n = len(o)
    g = lambda k: sum(1 for x in o if x["grade"] == k)
    stopped = [x for x in o if x.get("plan") == "STOP"]
    shake = sum(1 for x in stopped if x["peak"] >= 15)
    return {"n": n, "hit": g("HIT"), "partial": g("PARTIAL"), "miss": g("MISS"),
            "plan_t1": sum(1 for x in o if x.get("plan") == "T1"), "plan_stop": len(stopped), "shakeout": shake,
            "avg_peak": round(mean([x["peak"] for x in o]), 1), "avg_dip": round(mean([x["dip"] for x in o]), 1)}

def main():
    S = load(); st = S["strategy"]; t = now()
    for k, v in DEFAULT.items(): st.setdefault(k, v)
    pairs = get_instruments()
    prices = get_prices()
    cache, universe = {}, []
    for p in pairs:
        c = get_candles(p)
        if not c and p.endswith("_INR"):
            c = get_candles(p[:-4] + "_USDT")
        time.sleep(0.15)
        if not c: continue
        cache[p] = c
        f = feats(c)
        if f: universe.append((p, c, f))
    log("coins with usable candles: %d of %d" % (len(universe), len(pairs)))
    # 1. real pumps: backfill once (last 7 days), then check the newest finished 24h window every hour
    S.setdefault("first_scan", t)
    rnd = random.Random(t)
    def add(p, ev, ng):
        for ts, gain, f in ev:
            if any(m["pair"] == p and abs(m["t"] - ts) < 24 * 3600 for m in S["movers"]): continue
            tracked = ts >= S["first_scan"]
            was = any(x["pair"] == p and ts - 24 * 3600 <= x["t"] <= ts + 3600 for x in S["picks"]) if tracked else None
            S["movers"].append({"t": ts, "pair": p, "coin": coin_of(p), "gain": gain, "f": f, "was_picked": was})
            S["ex"].append([ts, f["vr"], f["sq"], f["mom4"], 1])
            log("REAL PUMP %s +%.0f%% (picked first: %s)" % (coin_of(p), gain, was))
        for ts, f in ng:
            S["ex"].append([ts, f["vr"], f["sq"], f["mom4"], 0])
    just_filled = False
    if not S["backfilled"]:
        allneg = []
        for p, c, f in universe:
            ev, ng = collect(c, range(59, len(c) - 24, 2), 0.08, rnd)
            add(p, ev, [])
            allneg += [(p, x) for x in ng]
        if len(allneg) > 1500: allneg = rnd.sample(allneg, 1500)
        for p, x in allneg: S["ex"].append([x[0], x[1]["vr"], x[1]["sq"], x[1]["mom4"], 0])
        S["backfilled"] = True; just_filled = True
        log("backfill done: %d pumps found in the last ~7 days, %d quiet samples" % (len(S["movers"]), len(allneg)))
    else:
        for p, c, f in universe:
            if len(c) >= 85:
                ev, ng = collect(c, [len(c) - 25], 0.03, rnd)
                add(p, ev, ng)
    pos = [e for e in S["ex"] if e[4] == 1][-800:]; neg = [e for e in S["ex"] if e[4] == 0][-2500:]
    S["ex"] = pos + neg
    # 2. grade old picks
    done = {o["id"] for o in S["outcomes"]}
    for pk in S["picks"]:
        if pk["id"] in done or t - pk["t"] < 25 * 3600: continue
        c = cache.get(pk["pair"]) or get_candles(pk["pair"], 200)
        g = grade(pk, c) if c else None
        if g is None:
            if t - pk["t"] > 150 * 3600:
                S["outcomes"].append({"id": pk["id"], "t": pk["t"], "pair": pk["pair"], "coin": pk["coin"], "grade": "NODATA", "peak": 0, "dip": 0, "end": 0})
            continue
        lv = levels(pk["p"], pk["atr"], pk["low12"], pk["m"], pk["r1"], pk["r2"])
        res, r = simulate(pk["p"], lv, pk["r1"], g["path"])
        S["outcomes"].append({"id": pk["id"], "t": pk["t"], "pair": pk["pair"], "coin": pk["coin"], "p": pk["p"], "atr": pk["atr"],
                              "low12": pk["low12"], "f": pk["f"], "plan": res, "R": r, "score": pk["score"], **g})
        log("graded %s: %s peak %.1f%% dip %.1f%% plan %s" % (pk["coin"], g["grade"], g["peak"], g["dip"], res))
    S["outcomes"] = [o for o in S["outcomes"] if o["grade"] != "NODATA"][-1500:]
    # 3. rank candidates
    cands = []
    for p, c, f in universe:
        coin = coin_of(p)
        if coin in BIG or abs(f["chg24"]) >= st["max_chg24"] or f["vol24"] <= 0: continue
        fund = prices.get(p, {}).get("fr")
        sc = score(f, fund, st["weights"])
        low12 = min(x["l"] for x in c[-12:]); price = c[-1]["c"]
        cands.append({"pair": p, "coin": coin, "p": price, "f": f, "fund": fund, "score": sc, "low12": low12,
                      "cur": "INR" if p.endswith("_INR") else "USDT"})
    cands.sort(key=lambda x: -x["score"])
    top = cands[:st["top_n"]]
    picks_now = []
    for rank, x in enumerate(top, 1):
        lv = levels(x["p"], x["f"]["atr"], x["low12"], st["stop_atr"], st["t1_r"], st["t2_r"])
        sug, mx = leverage(lv["risk_pct"])
        picks_now.append({"rank": rank, "coin": x["coin"], "pair": x["pair"], "cur": x["cur"], "price": x["p"], "score": round(x["score"], 1),
                          "vr": x["f"]["vr"], "sq": x["f"]["sq"], "mom4": x["f"]["mom4"], "chg24": x["f"]["chg24"], "fund": x["fund"],
                          "entry_lo": lv["entry_lo"], "entry_hi": lv["entry_hi"], "stop": lv["stop"], "t1": lv["t1"], "t2": lv["t2"],
                          "risk_pct": round(lv["risk_pct"], 2), "lev_suggest": sug, "lev_max": mx})
        if not any(k["pair"] == x["pair"] and t - k["t"] < 24 * 3600 for k in S["picks"]):
            S["picks"].append({"id": "%s-%d" % (x["coin"], t), "t": t, "pair": x["pair"], "coin": x["coin"], "p": x["p"], "f": x["f"],
                               "score": round(x["score"], 1), "atr": x["f"]["atr"], "low12": x["low12"], "m": st["stop_atr"],
                               "r1": st["t1_r"], "r2": st["t2_r"], "v": st["version"]})
    S["picks"] = [k for k in S["picks"] if t - k["t"] < 5 * 24 * 3600]
    S["movers"] = S["movers"][-400:]
    # 4. nightly review (after 23:00 IST, once a day)
    ist = datetime.now(IST); today = ist.strftime("%Y-%m-%d")
    if just_filled or (ist.hour >= 23 and S["last_review"] != today):
        old = st["version"]; txt = review(st, S)
        S["history"].append({"t": t, "date": today, "version": st["version"], "changed": st["version"] != old, "text": txt})
        S["last_review"] = today; log("review: " + txt)
    S["history"] = S["history"][-60:]
    # 5. write files
    day = 24 * 3600
    rec = [m for m in S["movers"] if t - m["t"] < 7 * day]
    trk = [m for m in rec if m["was_picked"] is not None]
    latest = {"updated": t, "updated_ist": ist.strftime("%d %b %Y %H:%M IST"), "coins_scanned": len(universe), "pairs_listed": len(pairs),
              "strategy": {"version": st["version"], "weights": st["weights"], "stop_atr": st["stop_atr"], "t1_r": st["t1_r"], "t2_r": st["t2_r"]},
              "picks": picks_now,
              "movers": [{"coin": m["coin"], "gain": m["gain"], "t": m["t"], "was_picked": m["was_picked"]} for m in sorted(rec, key=lambda m: -m["t"])[:40]],
              "movers_7d": {"total": len(rec), "tracked": len(trk), "caught": sum(1 for m in trk if m["was_picked"])},
              "learn": {"pos": sum(1 for e in S["ex"] if e[4] == 1), "neg": sum(1 for e in S["ex"] if e[4] == 0), "need_pos": MIN_POS},
              "stats": {"all": stats(S["outcomes"]), "d7": stats(S["outcomes"], t - 7 * day), "d30": stats(S["outcomes"], t - 30 * day)},
              "report": [{k: o.get(k) for k in ("coin", "t", "grade", "peak", "dip", "end", "plan")} for o in S["outcomes"][-25:][::-1]],
              "history": S["history"][::-1][:15],
              "errors": [l for l in LOGS if "FAILED" in l][:5]}
    json.dump(S, open(DATA + "/state.json", "w"), separators=(",", ":"))
    json.dump(latest, open(DATA + "/latest.json", "w"), separators=(",", ":"))

if __name__ == "__main__":
    os.makedirs(DATA, exist_ok=True)
    try:
        main()
    except Exception:
        log("CRASH " + traceback.format_exc()[-600:])
    try:
        old = open(DATA + "/log.txt").read().splitlines()[-250:]
    except Exception:
        old = []
    open(DATA + "/log.txt", "w").write("\n".join(old + LOGS) + "\n")
