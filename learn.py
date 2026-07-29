"""
The lab. Runs every hour. Uses no AI and costs nothing.

Six simple theories about what a price does in the next hour. Every hour each
one makes a call: up, down, or flat. An hour later all six get marked against
what the price actually did.

Nothing here is shown on the public page. This is the bot's own notebook.

Being right by chance is about 33%, because the flat band is set so that
roughly a third of hours land in it. Anything holding above that over
hundreds of tests is a real, measured finding.
"""

import json, math, os, statistics, time
from pathlib import Path
import requests

CG = "https://api.coingecko.com/api/v3"
COINS = [c.strip() for c in os.environ.get(
    "COINS", "bitcoin,ethereum,solana").split(",") if c.strip()]

ROOT = Path(__file__).parent
STATE = ROOT / "data" / "lab.json"
PUBLIC = ROOT / "docs" / "lab.json"

TRAIL_KEEP = 72          # hours of history kept per coin
MIN_TRAIL = 12           # need this many hours before making any call
RESOLVE_MIN_MS = 45 * 60 * 1000
RESOLVE_MAX_MS = 150 * 60 * 1000

THEORIES = ["momentum_1h", "reversion_1h", "momentum_24h",
            "breakout_fade", "volume_push", "always_flat", "ensemble"]


def blank():
    return {"trail": {}, "pending": [], "scores": {}, "resolved": 0}


def load():
    if STATE.exists():
        try:
            st = json.loads(STATE.read_text())
            for k in ("trail", "pending", "scores"):
                st.setdefault(k, {} if k != "pending" else [])
            st.setdefault("resolved", 0)
            return st
        except Exception as e:
            print(f"  state unreadable ({e}) - starting fresh")
    return blank()


def save(st):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st))


def get(url, **params):
    last = None
    for wait in (15, 30, 60, 90):
        r = requests.get(url, params=params, timeout=45)
        if r.status_code == 200:
            return r.json()
        last = r.status_code
        wait = int(r.headers.get("Retry-After", wait))
        print(f"    got {last}, waiting {wait}s")
        time.sleep(wait)
    raise RuntimeError(f"CoinGecko failed: {last}")


# ── the six theories ──────────────────────────────────────────────────────

def returns(trail):
    """Hour-to-hour percentage changes from the trail."""
    out = []
    for a, b in zip(trail, trail[1:]):
        if a["p"]:
            out.append((b["p"] - a["p"]) / a["p"] * 100.0)
    return out


def classify(pct, band):
    if pct > band:
        return "up"
    if pct < -band:
        return "down"
    return "flat"


def flip(call):
    return {"up": "down", "down": "up", "flat": "flat"}[call]


def weights(st, coin):
    """How much each theory has earned the right to be heard."""
    sc = st["scores"].get(coin, {})
    w = {}
    for name in THEORIES:
        if name == "ensemble":
            continue
        rec = sc.get(name, {})
        n = rec.get("tested", 0)
        if n < 20:
            w[name] = 1.0                      # too early to judge - equal say
        else:
            acc = rec.get("right", 0) / n
            w[name] = max(0.0, acc - 0.33) * 10 + 0.1
    return w


def call_theories(st, coin, price, trail, vol_ratio):
    """Every theory makes its call for the next hour. Sees only the past."""
    if len(trail) < MIN_TRAIL:
        return None

    rets = returns(trail + [{"t": 0, "p": price}])
    if len(rets) < MIN_TRAIL - 1:
        return None

    sigma = statistics.pstdev(rets[-48:]) if len(rets) > 2 else 0.0
    if sigma <= 0:
        return None

    # band chosen so roughly a third of hours are 'flat' -> chance is ~33%
    band = 0.43 * sigma

    r1 = rets[-1]
    old = trail[-24] if len(trail) >= 24 else trail[0]
    r24 = (price - old["p"]) / old["p"] * 100.0 if old["p"] else 0.0
    band24 = band * math.sqrt(min(24, len(trail)))

    calls = {}
    calls["momentum_1h"] = classify(r1, band)
    calls["reversion_1h"] = flip(calls["momentum_1h"])
    calls["momentum_24h"] = classify(r24, band24)
    calls["breakout_fade"] = flip(classify(r1, 2 * sigma)) if abs(r1) > 2 * sigma else "flat"
    calls["volume_push"] = classify(r1, band) if vol_ratio > 1.5 else "flat"
    calls["always_flat"] = "flat"

    w = weights(st, coin)
    tally = {"up": 0.0, "down": 0.0, "flat": 0.0}
    for name, c in calls.items():
        tally[c] += w.get(name, 1.0)
    calls["ensemble"] = max(tally, key=tally.get)

    return {"band": band, "calls": calls}


# ── marking ───────────────────────────────────────────────────────────────

def resolve(st, prices, now_ms):
    """Mark every call that is now due. Discard any whose hour was missed."""
    still = []
    for p in st["pending"]:
        age = now_ms - p["made_ms"]
        if age < RESOLVE_MIN_MS:
            still.append(p)
            continue
        if age > RESOLVE_MAX_MS or p["coin"] not in prices:
            continue                                   # gap - not comparable

        then, now = p["price"], prices[p["coin"]]
        if not then:
            continue
        actual = classify((now - then) / then * 100.0, p["band"])

        sc = st["scores"].setdefault(p["coin"], {})
        for name, call in p["calls"].items():
            rec = sc.setdefault(name, {"tested": 0, "right": 0})
            rec["tested"] += 1
            if call == actual:
                rec["right"] += 1
        st["resolved"] += 1

    st["pending"] = still


def report(st):
    out = {}
    for coin, sc in st["scores"].items():
        th = {}
        for name, rec in sc.items():
            n = rec.get("tested", 0)
            if n >= 10:
                th[name] = {"tested": n, "right": round(rec["right"] / n * 100, 1)}
        if th:
            real = {k: v for k, v in th.items() if k != "always_flat"}
            out[coin] = {"theories": th,
                         "best": max(real or th, key=lambda k: th[k]["right"])}
    return out


def verdict(rep):
    if not rep:
        return "Not enough tests yet."
    best = []
    for coin, r in rep.items():
        v = r["theories"][r["best"]]
        if v["tested"] >= 200:
            best.append((v["right"], coin, r["best"], v["tested"]))
    if not best:
        return "Too few tests so far to say anything. Keep it running."
    best.sort(reverse=True)
    top, coin, name, n = best[0]
    if top < 38:
        return ("Nothing clears chance by a meaningful margin. On this evidence "
                "hourly direction is not predictable from these simple signals. "
                "That is a real answer, not a failure.")
    return (f"{name} on {coin} is at {top}% over {n} tests, against 33% chance. "
            "Worth watching. Test seven theories and one looks good by luck, so "
            "the live scoring from here is the test that counts.")


# ── the hourly run ────────────────────────────────────────────────────────

def main():
    st = load()
    now_ms = int(time.time() * 1000)

    print(f"checking {len(COINS)} coins")
    rows = get(f"{CG}/coins/markets", vs_currency="usd",
               ids=",".join(COINS), per_page=250, page=1)
    live = {r["id"]: r for r in rows if r.get("current_price")}

    resolve(st, {k: v["current_price"] for k, v in live.items()}, now_ms)

    made = 0
    for coin, row in live.items():
        price = row["current_price"]
        vol = row.get("total_volume") or 0
        trail = st["trail"].setdefault(coin, [])

        vols = [t.get("v", 0) for t in trail[-24:] if t.get("v")]
        vol_ratio = (vol / statistics.median(vols)) if vols and vol else 1.0

        r = call_theories(st, coin, price, trail, vol_ratio)
        if r:
            st["pending"].append({"coin": coin, "made_ms": now_ms, "price": price,
                                  "band": r["band"], "calls": r["calls"]})
            made += 1

        trail.append({"t": now_ms, "p": price, "v": vol})
        st["trail"][coin] = trail[-TRAIL_KEEP:]

    save(st)

    rep = report(st)
    PUBLIC.parent.mkdir(parents=True, exist_ok=True)
    PUBLIC.write_text(json.dumps({
        "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "tests_resolved": st["resolved"],
        "awaiting_marking": len(st["pending"]),
        "note": "Right by chance is about 33%.",
        "verdict": verdict(rep),
        "findings": rep,
    }, indent=1))

    print(f"  {made} calls made, {st['resolved']} tests marked so far")

    if not rep:
        print(f"\n  no findings yet - need about {max(0, 10 - st['resolved'])} more hours")
        return

    print("\nWHAT IS ACTUALLY WORKING (chance would be ~33%):")
    for coin, r in rep.items():
        print(f"\n  {coin}")
        for name, v in sorted(r["theories"].items(), key=lambda x: -x[1]["right"]):
            flag = "  <-- best" if name == r["best"] else ""
            print(f"    {name:<15} {v['right']:>5.1f}% right over {v['tested']} tests{flag}")
    print(f"\n  {verdict(rep)}")


if __name__ == "__main__":
    main()
