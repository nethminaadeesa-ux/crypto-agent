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
TOP_N = int(os.environ.get("TOP_N", "50"))     # 0 = use the COINS list instead

# pegged coins never move, and wrapped/staked ones just mirror their parent.
# neither tells us anything, and both would pad the coin count dishonestly.
SKIP = {
    "tether", "usd-coin", "dai", "first-digital-usd", "ethena-usde", "usds",
    "paypal-usd", "binance-peg-busd", "trueusd", "frax", "usdd", "pyusd",
    "wrapped-bitcoin", "wrapped-steth", "staked-ether", "weth", "wrapped-eeth",
    "coinbase-wrapped-btc", "rocket-pool-eth", "mantle-staked-ether", "wbeth",
    "binance-staked-sol", "jito-staked-sol", "solv-btc", "lombard-staked-btc",
    "kelp-dao-restaked-eth", "renzo-restaked-eth", "bridged-wrapped-steth",
    "tether-gold", "pax-gold", "leveraged-token",
}
MIN_SIGMA = 0.05        # below this a coin is effectively pegged - ignore it

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


def baseline(sc):
    """What doing nothing scores on this coin. The only honest yardstick."""
    rec = sc.get("always_flat", {})
    n = rec.get("tested", 0)
    return (rec.get("right", 0) / n) if n >= 50 else None


def inv_norm(q):
    """Inverse normal CDF - how many standard deviations for a given tail."""
    if q <= 0 or q >= 1:
        return 8.0
    a = [-39.6968302866538, 220.946098424521, -275.928510446969,
         138.357751867269, -30.6647980661472, 2.50662827745924]
    b = [-54.4760987982241, 161.585836858041, -155.698979859887,
         66.8013118877197, -13.2806815528857]
    c = [-0.00778489400243029, -0.322396458041136, -2.40075827716184,
         -2.54973253934373, 4.37466414146497, 2.93816398269878]
    d = [0.00778469570904146, 0.32246712907004, 2.445134137143, 3.75440866190742]
    pl, ph = 0.02425, 1 - 0.02425
    if q < pl:
        t = math.sqrt(-2 * math.log(q))
        return (((((c[0]*t+c[1])*t+c[2])*t+c[3])*t+c[4])*t+c[5]) / \
               ((((d[0]*t+d[1])*t+d[2])*t+d[3])*t+1)
    if q > ph:
        t = math.sqrt(-2 * math.log(1 - q))
        return -(((((c[0]*t+c[1])*t+c[2])*t+c[3])*t+c[4])*t+c[5]) / \
                ((((d[0]*t+d[1])*t+d[2])*t+d[3])*t+1)
    t = q - 0.5
    r = t * t
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*t / \
           (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)


def luck_z(n_coins):
    """The more coins you test, the higher the bar - or luck hands you winners."""
    return inv_norm(1 - (0.05 / max(1, n_coins)) / 2)


def noise_pp(p, n, z=1.96):
    """How far a score can drift on luck alone, in percentage points."""
    if n < 2:
        return 100.0
    return z * math.sqrt(2 * p * (1 - p) / n) * 100


def weights(st, coin):
    """How much each theory has earned the right to be heard."""
    sc = st["scores"].get(coin, {})
    base = baseline(sc)
    w = {}
    for name in THEORIES:
        if name == "ensemble":
            continue
        rec = sc.get(name, {})
        n = rec.get("tested", 0)
        if n < 50 or base is None:
            w[name] = 1.0                      # too early to judge - equal say
        else:
            acc = rec.get("right", 0) / n
            # only credit what it beats the do-nothing baseline by
            w[name] = max(0.0, acc - base) * 40 + 0.1
    return w


def call_theories(st, coin, price, trail, vol_ratio):
    """Every theory makes its call for the next hour. Sees only the past."""
    if len(trail) < MIN_TRAIL:
        return None

    rets = returns(trail + [{"t": 0, "p": price}])
    if len(rets) < MIN_TRAIL - 1:
        return None

    sigma = statistics.pstdev(rets[-48:]) if len(rets) > 2 else 0.0
    if sigma < MIN_SIGMA:
        return None                        # pegged or dead - nothing to predict

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
    """Score every theory against doing nothing on the same coin."""
    out = {}
    z = luck_z(len(st["scores"]))
    for coin, sc in st["scores"].items():
        base = baseline(sc)
        if base is None:
            continue

        th = {}
        for name, rec in sc.items():
            n = rec.get("tested", 0)
            if n < 50:
                continue
            acc = rec["right"] / n
            th[name] = {
                "tested": n,
                "right": round(acc * 100, 1),
                "edge_over_doing_nothing": round((acc - base) * 100, 1),
                "margin_of_luck": round(noise_pp(acc, n, z), 1),
                "real": (acc - base) * 100 > noise_pp(acc, n, z),
            }
        if not th:
            continue

        rivals = {k: v for k, v in th.items() if k != "always_flat"}
        best = max(rivals or th, key=lambda k: th[k]["edge_over_doing_nothing"])
        out[coin] = {
            "doing_nothing_scores": round(base * 100, 1),
            "theories": th,
            "best": best,
            "beats_doing_nothing": th[best]["real"],
        }
    return out


def verdict(rep):
    if not rep:
        return "Not enough tests yet."

    tested = sum(v["theories"][v["best"]]["tested"] for v in rep.values())
    if tested < 400:
        return "Too few tests so far to say anything. Keep it running."

    winners = {}
    for coin, r in rep.items():
        if r["beats_doing_nothing"]:
            winners.setdefault(r["best"], []).append(coin)

    if not winners:
        return ("Nothing beats simply assuming the price stays put. Every theory "
                "is inside the margin of luck, or below it. On this evidence "
                "hourly crypto direction is not predictable from these signals. "
                "That is a real answer, not a failure.")

    name, coins = max(winners.items(), key=lambda x: len(x[1]))
    need = max(3, round(0.2 * len(rep)))
    if len(coins) < need:
        return (f"{name} edges past doing nothing on {len(coins)} of {len(rep)} coins. "
                f"Below the {need} needed to rule out luck across this many coins. "
                "Not a finding - crypto moves together, so a handful of winners is "
                "what a coincidence looks like.")
    return (f"{name} beats doing nothing on {len(coins)} of {len(rep)} coins, past the "
            "margin of luck on each. That is worth taking seriously. Keep watching it "
            "on hours that have not happened yet.")


def print_table(rep):
    """Every score shown against the do-nothing baseline for that coin."""
    for coin, r in rep.items():
        base = r["doing_nothing_scores"]
        print(f"\n  {coin}   (doing nothing scores {base}%)")
        rows = sorted(r["theories"].items(),
                      key=lambda x: -x[1]["edge_over_doing_nothing"])
        for name, v in rows:
            e = v["edge_over_doing_nothing"]
            if name == "always_flat":
                tag = "  the baseline"
            elif v["real"]:
                tag = "  <-- BEATS IT"
            elif e > 0:
                tag = f"  (inside luck, +/-{v['margin_of_luck']})"
            else:
                tag = ""
            print(f"    {name:<15} {v['right']:>5.1f}%   {e:>+5.1f} vs nothing"
                  f"   over {v['tested']} tests{tag}")


# ── the hourly run ────────────────────────────────────────────────────────

def main():
    st = load()
    now_ms = int(time.time() * 1000)

    if TOP_N > 0:
        print(f"checking the top {TOP_N} coins by market cap")
        rows = get(f"{CG}/coins/markets", vs_currency="usd",
                   order="market_cap_desc", per_page=min(250, TOP_N), page=1)
    else:
        print(f"checking {len(COINS)} named coins")
        rows = get(f"{CG}/coins/markets", vs_currency="usd",
                   ids=",".join(COINS), per_page=250, page=1)

    live = {r["id"]: r for r in rows
            if r.get("current_price") and r["id"] not in SKIP}
    skipped = len(rows) - len(live)
    if skipped:
        print(f"  ignoring {skipped} pegged or wrapped coins")

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

    print("\nEVERY THEORY AGAINST SIMPLY DOING NOTHING:")
    print_table(rep)
    print(f"\n  {verdict(rep)}")


if __name__ == "__main__":
    main()
