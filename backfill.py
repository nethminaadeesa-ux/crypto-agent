"""
Backfill. Run this ONCE.

The problem: a bot that starts today knows nothing, and needs months
before it has enough cases to say anything useful.

But the past is already there. This goes back a full year, finds every
day a coin moved unusually hard, and records what happened next -
one day later and one week later.

You end up with 100+ real cases on day one instead of waiting until October.

What it CANNOT do: say why those old moves happened. Free news feeds only
carry the last day or two. So these cases are filed as "sharp drop" and
"sharp rise" rather than "regulation" or "hack".

That is still worth a lot. "After a 6% one-day fall, what usually happened
next?" is a real question with a real answer, and it needs no news at all.
"""

import json, math, os, statistics, time, datetime as dt
from pathlib import Path
import requests

CG = "https://api.coingecko.com/api/v3"
COINS = os.environ.get("COINS", "bitcoin,ethereum,solana").split(",")
DAYS = int(os.environ.get("BACKFILL_DAYS", "365"))
SENSITIVITY = float(os.environ.get("SENSITIVITY", "2.5"))

ROOT = Path(__file__).parent
EVENTS = ROOT / "data" / "events.json"
PUBLIC = ROOT / "docs" / "events.json"


def get(url, **params):
    waits = [20, 40, 60, 90, 120]
    for attempt, wait in enumerate(waits, 1):
        r = requests.get(url, params=params, timeout=30)
        if r.status_code == 200:
            return r.json()
        wait = int(r.headers.get("Retry-After", wait))
        print(f"    got {r.status_code}, waiting {wait}s (try {attempt})")
        time.sleep(wait)
    raise RuntimeError(f"CoinGecko failed: {url} -> {r.status_code}")


def mine(coin_id):
    """Find the unusual days in this coin's past, and what followed each one."""
    print(f"  reading {DAYS} days of {coin_id}...")
    px = get(f"{CG}/coins/{coin_id}/market_chart",
             vs_currency="usd", days=DAYS, interval="daily")["prices"]
    if len(px) < 40:
        print("    not enough history")
        return []

    moves = []
    for i in range(1, len(px)):
        if px[i - 1][1] > 0:
            moves.append((i, (px[i][1] - px[i - 1][1]) / px[i - 1][1] * 100))

    normal = statistics.median(abs(m) for _, m in moves) or 1.0
    threshold = max(3.0, normal * SENSITIVITY)
    print(f"    a normal day moves {normal:.2f}%, so unusual means beyond {threshold:.2f}%")

    found = []
    for i, move in moves:
        if abs(move) < threshold:
            continue
        # what happened after? need at least one more day of data
        if i + 1 >= len(px):
            continue
        price_then = px[i][1]
        after_1 = (px[i + 1][1] - price_then) / price_then * 100
        after_7 = ((px[i + 7][1] - price_then) / price_then * 100) if i + 7 < len(px) else None

        found.append({
            "t": int(px[i][0]),
            "when": dt.datetime.utcfromtimestamp(px[i][0] / 1000).strftime("%Y-%m-%d"),
            "coin": coin_id,
            "name": coin_id.replace("-", " ").title(),
            "price": round(price_then, 6),
            "move_pct": round(move, 2),
            "category": "sharp_drop" if move < 0 else "sharp_rise",
            "headline": f"moved {move:+.1f}% in one day, about {abs(move)/normal:.1f} times normal",
            "detail": "Found by looking back through price history. The reason is not known.",
            "source": "price history only",
            "confidence": "low",
            "after_24h": round(after_1, 2),
            "after_7d": round(after_7, 2) if after_7 is not None else None,
        })
    print(f"    found {len(found)} unusual days")
    return found


def summarise(events):
    out = {}
    for cat in sorted({e["category"] for e in events}):
        rows = [e for e in events if e["category"] == cat and e.get("after_24h") is not None]
        if len(rows) < 3:
            continue
        d1 = [e["after_24h"] for e in rows]
        d7 = [e["after_7d"] for e in rows if e.get("after_7d") is not None]
        out[cat] = {
            "cases": len(rows),
            "next_day_median": round(statistics.median(d1), 2),
            "next_day_range": [round(min(d1), 2), round(max(d1), 2)],
            "fell_next_day": sum(1 for x in d1 if x < 0),
            "week_median": round(statistics.median(d7), 2) if d7 else None,
            "up_after_week": sum(1 for x in d7 if x > 0) if d7 else None,
        }
    return out


def main():
    db = {"events": [], "trail": {}}
    if EVENTS.exists():
        db = json.loads(EVENTS.read_text())
        print(f"library already has {len(db['events'])} events")

    already = {(e["coin"], e.get("when")) for e in db["events"]}
    added = 0

    for coin in COINS:
        try:
            for ev in mine(coin.strip()):
                if (ev["coin"], ev["when"]) not in already:
                    db["events"].append(ev)
                    added += 1
        except Exception as e:
            print(f"  skipping {coin}: {e}")
        time.sleep(10)

    db["events"].sort(key=lambda e: e["t"])
    db["events"] = db["events"][-2000:]
    pat = summarise(db["events"])

    EVENTS.parent.mkdir(parents=True, exist_ok=True)
    PUBLIC.parent.mkdir(parents=True, exist_ok=True)
    EVENTS.write_text(json.dumps(db, indent=1))
    PUBLIC.write_text(json.dumps({
        "updated": dt.datetime.utcnow().isoformat() + "Z",
        "total_events": len(db["events"]),
        "patterns": pat,
        "recent": db["events"][-25:],
    }, indent=1))

    print(f"\nadded {added} historic cases. library now has {len(db['events'])}.")
    print("\nWHAT THE PAST SAYS:")
    for cat, p in pat.items():
        word = "fell" if p["next_day_median"] < 0 else "rose"
        print(f"\n  {cat.replace('_',' ')} - {p['cases']} cases")
        print(f"    next day: typically {word} {abs(p['next_day_median']):.2f}%, "
              f"{p['fell_next_day']} of {p['cases']} were negative")
        if p["week_median"] is not None:
            print(f"    a week later: median {p['week_median']:+.2f}%, "
                  f"{p['up_after_week']} of {p['cases']} were higher")
    if not pat:
        print("  not enough cases found. Try raising BACKFILL_DAYS or lowering SENSITIVITY.")


if __name__ == "__main__":
    main()
