"""
Lab backfill. Run this ONCE, by hand.

The lab tests six theories one hour at a time, which gives about 190 tests a
day. A week to get anywhere.

But those hours already happened. CoinGecko keeps 90 days of hourly prices,
so replay them.

This walks forward through 90 days, hour by hour, exactly as the live lab
would: at each hour it sees only what was knowable at that moment, makes all
six calls, then checks the very next hour to see who was right.

  8 coins x roughly 2,100 hours = about 17,000 scored tests, in a few minutes.

It uses learn.py's own functions, so the numbers line up exactly with what the
live lab produces from tonight onward.

IMPORTANT: it cannot peek. At each step only the past is visible. A backtest
that cheats tells you nothing.
"""

import json, os, statistics, time
from pathlib import Path
import requests

import learn

CG = "https://api.coingecko.com/api/v3"
COINS = [c.strip() for c in os.environ.get(
    "COINS", "bitcoin,ethereum,solana").split(",") if c.strip()]
DAYS = min(90, int(os.environ.get("LAB_DAYS", "90")))

ROOT = Path(__file__).parent
STATE = ROOT / "data" / "lab.json"
PUBLIC = ROOT / "docs" / "lab.json"


def replay(st, coin):
    print(f"  fetching {DAYS} days of hourly {coin}...")
    d = learn.get(f"{CG}/coins/{coin}/market_chart", vs_currency="usd", days=DAYS)
    px = d.get("prices", [])
    vols = {int(t): v for t, v in d.get("total_volumes", [])}

    if len(px) < 60:
        print("    not enough hourly history")
        return 0

    print(f"    replaying {len(px)} hours...")
    trail = []
    made = 0
    before = st["resolved"]

    for i in range(len(px) - 1):
        t, price = int(px[i][0]), px[i][1]
        nt, nprice = int(px[i + 1][0]), px[i + 1][1]
        if not price or not nprice:
            continue

        vol = vols.get(t, 0)
        recent = [x.get("v", 0) for x in trail[-24:] if x.get("v")]
        vol_ratio = (vol / statistics.median(recent)) if recent and vol else 1.0

        # make the calls using only what was knowable at time t
        r = learn.call_theories(st, coin, price, trail, vol_ratio)
        if r:
            st["pending"].append({"coin": coin, "made_ms": t, "price": price,
                                  "band": r["band"], "calls": r["calls"]})
            made += 1

        trail.append({"t": t, "p": price, "v": vol})
        trail = trail[-learn.TRAIL_KEEP:]

        # now the next hour arrives and the calls get marked
        learn.resolve(st, {coin: nprice}, nt)

    st["trail"][coin] = trail
    st["pending"] = [p for p in st["pending"] if p["coin"] != coin]
    scored = st["resolved"] - before
    print(f"    {made} calls made, {scored} marked")
    return scored


def main():
    st = learn.load()
    total = 0

    for coin in COINS:
        try:
            total += replay(st, coin)
        except Exception as e:
            print(f"    {coin} failed: {e}")
        time.sleep(8)          # be polite to the free API
        learn.save(st)

    rep = learn.report(st)
    PUBLIC.parent.mkdir(parents=True, exist_ok=True)
    PUBLIC.write_text(json.dumps({
        "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "tests_resolved": st["resolved"],
        "awaiting_marking": len(st["pending"]),
        "note": "Right by chance is about 33%.",
        "verdict": learn.verdict(rep),
        "findings": rep,
    }, indent=1))

    print("\n" + "=" * 62)
    print(f"WHAT {DAYS} DAYS OF HISTORY ACTUALLY SAYS")
    print(f"{total} scored tests. The yardstick is always_flat - doing nothing.")
    print("=" * 62)

    learn.print_table(rep)

    print("\n" + "-" * 62)
    print("VERDICT")
    print("  " + learn.verdict(rep))
    print("-" * 62)
    print("\nThe live lab keeps scoring from tonight, on hours that have not")
    print("happened yet. That is the test that really counts.")


if __name__ == "__main__":
    main()
