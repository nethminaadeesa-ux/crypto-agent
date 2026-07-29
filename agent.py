"""
Daily crypto agent.

Runs once a day on GitHub Actions. Each run it:
  1. reads everything it has said before          (memory)
  2. checks whether its old forecasts were right  (scoring)
  3. widens or narrows its ranges accordingly     (learning)
  4. collects today's prices and candles          (data)
  5. writes today's note                          (Claude)
  6. saves it all back to the repo                (memory grows)
"""

import json, math, os, time, datetime as dt
from pathlib import Path
import requests
from brain import ask
import news

CG = "https://api.coingecko.com/api/v3"
# claude-sonnet-5 is the balance of quality and price.
# Swap to claude-haiku-4-5-20251001 to cut the bill by about half.
MODEL = os.environ.get("MODEL", "claude-sonnet-5")
COINS = os.environ.get("COINS", "bitcoin,ethereum,solana").split(",")
HORIZON = int(os.environ.get("HORIZON_DAYS", "7"))

ROOT = Path(__file__).parent
HIST = ROOT / "data" / "history.json"
EVENTS = ROOT / "data" / "events.json"
OUT = ROOT / "docs" / "data.json"


# ── small helpers ──────────────────────────────────────────
def get(url, **params):
    for attempt in range(3):
        r = requests.get(url, params=params, timeout=30)
        if r.status_code == 200:
            return r.json()
        time.sleep(5 * (attempt + 1))          # free tier rate limit
    raise RuntimeError(f"CoinGecko failed: {url} -> {r.status_code}")


def load_history():
    if HIST.exists():
        return json.loads(HIST.read_text())
    return {"runs": [], "forecasts": [], "width_factor": 1.0}


def daily_vol(coin_id):
    """How much this coin moves on a normal day, from 90 days of closes."""
    px = [p[1] for p in get(f"{CG}/coins/{coin_id}/market_chart",
                            vs_currency="usd", days=90, interval="daily")["prices"]]
    rets = [math.log(px[i] / px[i - 1]) for i in range(1, len(px)) if px[i - 1] > 0]
    mean = sum(rets) / len(rets)
    sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1))
    return sd


def price_on(coin_id, when_ms):
    """What the price actually was on a past date."""
    days = min(365, int((time.time() * 1000 - when_ms) / 86_400_000) + 2)
    px = get(f"{CG}/coins/{coin_id}/market_chart",
             vs_currency="usd", days=days, interval="daily")["prices"]
    return min(px, key=lambda p: abs(p[0] - when_ms))[1]


# ── 2 + 3. score old forecasts, then adjust ────────────────
def score_and_learn(h):
    now = time.time() * 1000
    newly_scored = 0

    for f in h["forecasts"]:
        if f.get("actual") is not None:
            continue
        due = f["made_ms"] + f["days"] * 86_400_000
        if now < due:
            continue
        try:
            actual = price_on(f["coin"], due)
        except Exception as e:
            print("  could not score:", e)
            continue
        f["actual"] = actual
        f["hit"] = f["lo"] <= actual <= f["hi"]
        newly_scored += 1
        print(f"  scored {f['coin']} {'HIT' if f['hit'] else 'MISS'} -> {actual:,.2f}")
        time.sleep(2)

    scored = [f for f in h["forecasts"] if f.get("actual") is not None]
    recent = scored[-20:]
    if len(recent) >= 6:
        rate = sum(f["hit"] for f in recent) / len(recent)
        old = h["width_factor"]
        # target is 68% — the band should be right about 2 times in 3
        if rate < 0.55:
            h["width_factor"] = min(2.0, old * 1.10)   # too confident, widen
        elif rate > 0.85:
            h["width_factor"] = max(0.8, old * 0.96)   # too vague, tighten
        print(f"  hit rate {rate:.0%} over {len(recent)} -> width {old:.2f} to {h['width_factor']:.2f}")

    return newly_scored


# ── 4. today's data ────────────────────────────────────────
def collect():
    mkt = get(f"{CG}/coins/markets", vs_currency="usd", ids=",".join(COINS),
              price_change_percentage="1h,24h,7d")
    out = []
    for c in mkt:
        time.sleep(2)
        ohlc = get(f"{CG}/coins/{c['id']}/ohlc", vs_currency="usd", days=7)
        step = max(1, len(ohlc) // 12)
        out.append({
            "id": c["id"], "name": c["name"], "price": c["current_price"],
            "h24": c["price_change_percentage_24h"],
            "d7": c.get("price_change_percentage_7d_in_currency"),
            "mcap": c["market_cap"],
            "turnover": round(c["total_volume"] / c["market_cap"], 3) if c["market_cap"] else None,
            "candles": [[dt.datetime.utcfromtimestamp(x[0] / 1000).strftime("%d %b %H:%M"),
                         x[1], x[2], x[3], x[4]] for x in ohlc[::step]],
        })
    return out


# ── 5. the note ────────────────────────────────────────────
def load_lessons():
    """What the watcher has learned about news and its effect on prices."""
    if not EVENTS.exists():
        return {"patterns": {}, "recent": []}
    db = json.loads(EVENTS.read_text())
    import statistics as st
    pat = {}
    for e in db.get("events", []):
        if e.get("after_24h") is None:
            continue
        pat.setdefault(e["category"], []).append(e["after_24h"])
    lessons = {k: {"cases": len(v), "next_day_median": round(st.median(v), 2),
                   "fell": sum(1 for x in v if x < 0)}
               for k, v in pat.items() if len(v) >= 2}
    recent = [{"when": e["when"], "coin": e["name"], "move": e["move_pct"],
               "why": e["headline"], "category": e["category"]}
              for e in db.get("events", [])[-8:]]
    return {"patterns": lessons, "recent": recent}


def write_note(h, today, ranges):
    lessons = load_lessons()
    past = [{"date": r["date"], "one_line": r.get("one_line", "")} for r in h["runs"][-7:]]
    scored = [f for f in h["forecasts"] if f.get("actual") is not None][-10:]
    record = [{"coin": f["coin"], "expected": [round(f["lo"], 2), round(f["hi"], 2)],
               "actual": round(f["actual"], 2), "hit": f["hit"]} for f in scored]

    prompt = f"""You are a crypto analyst writing a short daily note. Today is {dt.date.today()}.
Plain, simple English. Short sentences. No hype, no emoji.

TODAY'S DATA: {json.dumps(today)}

YOUR RANGES FOR THE NEXT {HORIZON} DAYS (two-in-three confidence, already calibrated): {json.dumps(ranges)}

WHAT YOU SAID THE LAST FEW DAYS: {json.dumps(past)}

HOW YOUR PAST RANGES ACTUALLY DID: {json.dumps(record)}

EVENTS THE WATCHER CAUGHT RECENTLY: {json.dumps(lessons["recent"])}

WHAT YOU HAVE LEARNED SO FAR about what follows each kind of news
(next_day_median is the typical move the day after; "fell" is how many of those cases went down):
{json.dumps(lessons["patterns"])}
Use this evidence where it applies. Say how many past cases you are drawing on.
If a category has fewer than 3 cases, say the evidence is still thin.

RECENT HEADLINES: {json.dumps(news.headlines(hours=36, limit=25))}
Use these headlines to explain the moves. If none of them explain a move, say so honestly.

Reply with ONE JSON object, no markdown, no backticks:
{{
 "one_line": "today in one sentence",
 "moved": ["3 to 5 bullets on what moved and the real news reason"],
 "traded": "how the candles looked - was it one steady push or a spike that got sold back. Explain any term you use.",
 "versus_yesterday": "what changed since your last note. If something is now on a run of several days, say so.",
 "scenarios": [{{"name":"Higher","prob":30,"text":"the conditions, under 30 words"}},
               {{"name":"Roughly flat","prob":45,"text":"..."}},
               {{"name":"Lower","prob":25,"text":"..."}}],
 "breaker": "the one event that would make all of this wrong",
 "learned": "what your own event library says about today, and how many past cases that is based on. Say 'not enough cases yet' if it is thin.",
 "honesty": "one line on how your past ranges have performed and what that means for trusting today's"
}}"""

    return ask(prompt, want_json=True, tokens=2000)


# ── main ───────────────────────────────────────────────────
def main():
    h = load_history()
    print("1. memory:", len(h["runs"]), "past runs,", len(h["forecasts"]), "forecasts")

    print("2. scoring old forecasts...")
    score_and_learn(h)

    print("3. collecting today's data...")
    today = collect()

    print("4. working out ranges...")
    now_ms = int(time.time() * 1000)
    ranges = []
    for c in today:
        time.sleep(2)
        sd = daily_vol(c["id"]) * math.sqrt(HORIZON) * h["width_factor"]
        lo, hi = c["price"] * math.exp(-sd), c["price"] * math.exp(sd)
        ranges.append({"coin": c["name"], "low": round(lo, 4), "high": round(hi, 4)})
        h["forecasts"].append({"coin": c["id"], "name": c["name"], "made_ms": now_ms,
                               "days": HORIZON, "price_then": c["price"],
                               "lo": lo, "hi": hi, "actual": None})

    print("5. writing the note...")
    note = write_note(h, today, ranges)

    run = {"date": dt.date.today().isoformat(), "prices": {c["name"]: c["price"] for c in today},
           "ranges": ranges, "width_factor": round(h["width_factor"], 3), **note}
    h["runs"].append(run)
    h["runs"] = h["runs"][-180:]
    h["forecasts"] = h["forecasts"][-300:]

    HIST.parent.mkdir(parents=True, exist_ok=True)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    HIST.write_text(json.dumps(h, indent=1))

    scored = [f for f in h["forecasts"] if f.get("actual") is not None]
    OUT.write_text(json.dumps({
        "updated": dt.datetime.utcnow().isoformat() + "Z",
        "latest": run,
        "width_factor": round(h["width_factor"], 3),
        "record": {"scored": len(scored), "hits": sum(f["hit"] for f in scored)},
        "recent_runs": h["runs"][-14:],
        "recent_forecasts": [f for f in h["forecasts"][-30:]],
    }, indent=1))
    print("6. done -", run["one_line"])


if __name__ == "__main__":
    main()
