"""
The watcher. Runs every hour. This is the part that learns.

Most hours it does almost nothing: it checks prices and goes back to sleep.
That costs nothing.

When a price moves more than normal, it wakes up properly:
  - searches the news to find out WHY
  - puts the reason into a category (regulation, hack, rates, ETF flows...)
  - writes it into a library with the price at that moment

Then 24 hours and 7 days later it comes back and records what happened AFTER.

Over months this builds real evidence: "we have seen 9 hacks. The day after,
the coin fell between 3% and 11%. 8 of the 9 recovered within a week."

That is learning. Not the model changing - the evidence piling up.
"""

import json, os, statistics, time, datetime as dt
from pathlib import Path
import requests
from brain import ask
import news

CG = "https://api.coingecko.com/api/v3"
# claude-sonnet-5 is the balance of quality and price.
# Swap to claude-haiku-4-5-20251001 to cut the bill by about half.
MODEL = os.environ.get("MODEL", "claude-sonnet-5")
COINS = os.environ.get("COINS", "bitcoin,ethereum,solana").split(",")

# how big a move has to be before it counts as an event
MIN_MOVE = float(os.environ.get("MIN_MOVE_PCT", "1.5"))   # never react below this
SENSITIVITY = float(os.environ.get("SENSITIVITY", "2.5"))  # or this many times normal

ROOT = Path(__file__).parent
EVENTS = ROOT / "data" / "events.json"
PUBLIC = ROOT / "docs" / "events.json"

CATEGORIES = [
    "regulation", "etf_flows", "interest_rates_macro", "hack_or_exploit",
    "exchange_news", "large_holder_selling", "liquidation_cascade",
    "network_upgrade", "adoption_or_partnership", "no_clear_cause",
    "sharp_drop", "sharp_rise",   # filled in by backfill.py from past price history
]


def get(url, **params):
    for i in range(3):
        r = requests.get(url, params=params, timeout=30)
        if r.status_code == 200:
            return r.json()
        time.sleep(5 * (i + 1))
    raise RuntimeError(f"{url} -> {r.status_code}")


def load():
    if EVENTS.exists():
        return json.loads(EVENTS.read_text())
    return {"events": [], "trail": {}}


# ── 1. is anything actually happening? ─────────────────────
def check_for_events(db):
    """Compare now against the last 48 hours we have recorded."""
    mkt = get(f"{CG}/coins/markets", vs_currency="usd", ids=",".join(COINS))
    now = int(time.time() * 1000)
    found = []

    for c in mkt:
        cid = c["id"]
        price = c["current_price"]
        trail = db["trail"].setdefault(cid, [])

        if trail:
            last = trail[-1]
            move = (price - last["p"]) / last["p"] * 100
            hours = (now - last["t"]) / 3.6e6

            # what counts as normal for this coin, from its own recent hours
            if len(trail) >= 8:
                steps = [abs((trail[i]["p"] - trail[i-1]["p"]) / trail[i-1]["p"] * 100)
                         for i in range(1, len(trail))]
                normal = statistics.median(steps) or 0.3
            else:
                normal = 0.5

            threshold = max(MIN_MOVE, normal * SENSITIVITY)
            if abs(move) >= threshold and hours <= 6:
                found.append({"coin": cid, "name": c["name"], "price": price,
                              "move_pct": round(move, 2), "over_hours": round(hours, 1),
                              "normal_hourly": round(normal, 2)})
                print(f"  EVENT {c['name']} {move:+.2f}% (normal is {normal:.2f}%)")

        trail.append({"t": now, "p": price})
        db["trail"][cid] = trail[-48:]

    return found


# ── 2. why did it happen? ──────────────────────────────────
def find_cause(ev):
    prompt = f"""A crypto price just moved and I need to know why.

Coin: {ev['name']}
Move: {ev['move_pct']:+.2f}% over the last {ev['over_hours']} hours
Price now: ${ev['price']}
Its normal hourly move is {ev['normal_hourly']}%, so this is unusual.
Time now: {dt.datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC.

HEADLINES FROM THE LAST 12 HOURS:
{json.dumps(news.about(ev["name"], hours=12))}

Use only these headlines. Do not use anything you remember.

If you cannot find a clear cause, say so honestly. Do not invent a reason.
Guessing would poison the library this goes into.

Reply with ONE JSON object, no markdown:
{{
 "category": "one of: {', '.join(CATEGORIES)}",
 "headline": "under 15 words, what happened, in your own words",
 "detail": "2 sentences, plain simple English, explaining it",
 "source": "the website name you found it on, or 'none found'",
 "confidence": "high, medium or low - how sure you are this caused the move"
}}"""
    try:
        return ask(prompt, want_json=True, tokens=800)
    except Exception as e:
        print("  could not find cause:", e)
        return {"category": "no_clear_cause", "headline": "cause not identified",
                "detail": "", "source": "none found", "confidence": "low"}


# ── 3. what happened afterwards? ───────────────────────────
def fill_in_outcomes(db):
    """Go back to old events and record what the price did after."""
    now = time.time() * 1000
    filled = 0

    for ev in db["events"]:
        for label, hours in (("after_24h", 24), ("after_7d", 168)):
            if ev.get(label) is not None:
                continue
            due = ev["t"] + hours * 3.6e6
            if now < due or filled >= 4:
                continue
            try:
                days = min(365, int((now - ev["t"]) / 86.4e6) + 2)
                px = get(f"{CG}/coins/{ev['coin']}/market_chart",
                         vs_currency="usd", days=days)["prices"]
                nearest = min(px, key=lambda p: abs(p[0] - due))[1]
                ev[label] = round((nearest - ev["price"]) / ev["price"] * 100, 2)
                filled += 1
                print(f"  outcome: {ev['name']} {ev['category']} {label} = {ev[label]:+.2f}%")
                time.sleep(2)
            except Exception as e:
                print("  outcome failed:", e)
    return filled


# ── 4. what has it learned so far? ─────────────────────────
def patterns(db):
    """Group past events by category and see what usually followed."""
    out = {}
    for cat in CATEGORIES:
        rows = [e for e in db["events"] if e["category"] == cat and e.get("after_24h") is not None]
        if len(rows) < 2:
            continue
        d1 = [e["after_24h"] for e in rows]
        d7 = [e["after_7d"] for e in rows if e.get("after_7d") is not None]
        out[cat] = {
            "cases": len(rows),
            "next_day_median": round(statistics.median(d1), 2),
            "next_day_range": [round(min(d1), 2), round(max(d1), 2)],
            "fell_next_day": sum(1 for x in d1 if x < 0),
            "week_median": round(statistics.median(d7), 2) if d7 else None,
            "recovered_in_week": sum(1 for x in d7 if x > 0) if d7 else None,
        }
    return out


def main():
    db = load()
    print(f"library: {len(db['events'])} events recorded")

    print("checking prices...")
    events = check_for_events(db)

    print("filling in what happened after old events...")
    fill_in_outcomes(db)

    if not events:
        print("nothing unusual. sleeping.")
    for ev in events:
        print(f"looking into {ev['name']}...")
        cause = find_cause(ev)
        db["events"].append({
            "t": int(time.time() * 1000),
            "when": dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
            "coin": ev["coin"], "name": ev["name"], "price": ev["price"],
            "move_pct": ev["move_pct"], **cause,
            "after_24h": None, "after_7d": None,
        })
        print(f"  -> {cause['category']}: {cause['headline']}")
        time.sleep(3)

    db["events"] = db["events"][-500:]
    pat = patterns(db)

    EVENTS.parent.mkdir(parents=True, exist_ok=True)
    PUBLIC.parent.mkdir(parents=True, exist_ok=True)
    EVENTS.write_text(json.dumps(db, indent=1))
    PUBLIC.write_text(json.dumps({
        "updated": dt.datetime.utcnow().isoformat() + "Z",
        "total_events": len(db["events"]),
        "patterns": pat,
        "recent": db["events"][-25:],
    }, indent=1))

    print(f"\nwhat it knows so far ({len(pat)} categories with enough cases):")
    for cat, p in pat.items():
        print(f"  {cat}: {p['cases']} cases, next day median {p['next_day_median']:+.2f}%, "
              f"{p['fell_next_day']} of {p['cases']} fell")


if __name__ == "__main__":
    main()
