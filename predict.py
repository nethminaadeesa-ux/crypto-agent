"""
The forecaster. This is the part that predicts, marks its own homework,
and corrects itself.

The loop:
  1. predict     - up / flat / down, with a percentage on each
  2. compare     - when the date arrives, check what really happened
  3. measure     - score it properly, and against a baseline that needs no brain
  4. correct     - if it is beating the baseline, trust it more.
                   If not, pull it back towards the baseline automatically.
  5. review      - every 10 results, read its worst misses and write down lessons.
                   Those lessons go into the next prediction.

The baseline matters more than anything else here.
The baseline is simply: "how often has this coin gone up over a week, historically?"
That takes no intelligence at all.

If the bot cannot beat that, the bot is adding nothing - and this file
will say so plainly instead of hiding it.
"""

import json, math, os, statistics, time, datetime as dt
from pathlib import Path
import requests
from brain import ask as think
import news

CG = "https://api.coingecko.com/api/v3"
# claude-sonnet-5 is the balance of quality and price.
# Swap to claude-haiku-4-5-20251001 to cut the bill by about half.
MODEL = os.environ.get("MODEL", "claude-sonnet-5")
COINS = os.environ.get("COINS", "bitcoin,ethereum,solana").split(",")
HORIZON = int(os.environ.get("HORIZON_DAYS", "7"))

ROOT = Path(__file__).parent
STATE = ROOT / "data" / "predictions.json"
PUBLIC = ROOT / "docs" / "predictions.json"
EVENTS = ROOT / "data" / "events.json"


def get(url, **p):
    """CoinGecko's free service is strict about speed. Wait and try again."""
    for attempt, wait in enumerate([20, 40, 60, 90, 120], 1):
        r = requests.get(url, params=p, timeout=30)
        if r.status_code == 200:
            return r.json()
        wait = int(r.headers.get("Retry-After", wait))
        print(f"    got {r.status_code}, waiting {wait}s (try {attempt})")
        time.sleep(wait)
    raise RuntimeError(f"{url} -> {r.status_code}")


def load():
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {"predictions": [], "blend_weight": 0.5, "lessons": [], "reviewed_at": 0}


def closes(coin, days=180):
    return get(f"{CG}/coins/{coin}/market_chart",
               vs_currency="usd", days=days, interval="daily")["prices"]


# ── the baseline: no intelligence, just history ────────────
def baseline(px, horizon, threshold_pct):
    """How often did this coin go up, sit flat, or go down over this many days?"""
    p = [x[1] for x in px]
    up = flat = down = 0
    for i in range(len(p) - horizon):
        move = (p[i + horizon] - p[i]) / p[i] * 100
        if move > threshold_pct:
            up += 1
        elif move < -threshold_pct:
            down += 1
        else:
            flat += 1
    n = up + flat + down or 1
    return {"up": round(up / n, 3), "flat": round(flat / n, 3), "down": round(down / n, 3)}


def vol(px):
    p = [x[1] for x in px]
    r = [math.log(p[i] / p[i - 1]) for i in range(1, len(p)) if p[i - 1] > 0]
    m = sum(r) / len(r)
    return math.sqrt(sum((x - m) ** 2 for x in r) / (len(r) - 1))


def brier(probs, actual):
    """0 is perfect, 2 is as wrong as possible. Lower is better."""
    return sum((probs[k] - (1.0 if k == actual else 0.0)) ** 2 for k in ("up", "flat", "down"))


def classify(move, threshold):
    return "up" if move > threshold else "down" if move < -threshold else "flat"


# ── 2 + 3. compare and measure ─────────────────────────────
def score_pending(st):
    now = time.time() * 1000
    done = 0
    for pr in st["predictions"]:
        if pr.get("outcome") or done >= 4:
            continue
        due = pr["made_ms"] + pr["horizon"] * 86.4e6
        if now < due:
            continue
        try:
            days = min(365, int((now - pr["made_ms"]) / 86.4e6) + 2)
            px = closes(pr["coin"], days)
            actual_price = min(px, key=lambda x: abs(x[0] - due))[1]
            move = (actual_price - pr["price_then"]) / pr["price_then"] * 100
            pr["actual_move"] = round(move, 2)
            pr["outcome"] = classify(move, pr["threshold"])
            pr["brier"] = round(brier(pr["final_probs"], pr["outcome"]), 4)
            pr["brier_baseline"] = round(brier(pr["baseline_probs"], pr["outcome"]), 4)
            done += 1
            print(f"  {pr['name']}: said {max(pr['final_probs'], key=pr['final_probs'].get)}, "
                  f"got {pr['outcome']} ({move:+.2f}%)")
            time.sleep(10)
        except Exception as e:
            print("  scoring failed:", e)
    return done


def report_card(st):
    done = [p for p in st["predictions"] if p.get("outcome")]
    if not done:
        return {"scored": 0}
    recent = done[-40:]
    mine = statistics.mean(p["brier"] for p in recent)
    base = statistics.mean(p["brier_baseline"] for p in recent)
    skill = (base - mine) / base if base else 0
    right = sum(1 for p in recent
                if max(p["final_probs"], key=p["final_probs"].get) == p["outcome"])

    # calibration: when it sounded confident, was it?
    bins = {}
    for p in recent:
        top = max(p["final_probs"].values())
        b = "50-60%" if top < .6 else "60-70%" if top < .7 else "70-80%" if top < .8 else "80%+"
        bins.setdefault(b, {"claimed": [], "right": 0, "n": 0})
        bins[b]["claimed"].append(top)
        bins[b]["n"] += 1
        bins[b]["right"] += max(p["final_probs"], key=p["final_probs"].get) == p["outcome"]
    calib = {k: {"said": round(statistics.mean(v["claimed"]) * 100),
                 "actually_right": round(v["right"] / v["n"] * 100), "cases": v["n"]}
             for k, v in bins.items()}

    return {"scored": len(done), "on_recent": len(recent),
            "top_pick_right": right, "hit_rate": round(right / len(recent) * 100),
            "brier": round(mine, 4), "brier_baseline": round(base, 4),
            "skill_vs_baseline": round(skill, 3),
            "beating_baseline": skill > 0, "calibration": calib}


# ── 4. correct itself ──────────────────────────────────────
def adjust(st, card):
    """Blend the bot's view with the baseline. How much depends on its record."""
    if card.get("scored", 0) < 8:
        return "Not enough results yet — still leaning half on the baseline."
    old = st["blend_weight"]
    s = card["skill_vs_baseline"]
    if s < -0.02:
        st["blend_weight"] = min(0.9, old + 0.08)   # worse than baseline: trust it less
        why = "It did worse than the baseline, so it now leans harder on the baseline."
    elif s > 0.05:
        st["blend_weight"] = max(0.05, old - 0.06)  # genuinely better: let it speak
        why = "It beat the baseline, so it is allowed more of its own opinion."
    else:
        why = "About level with the baseline. No change."
    print(f"  blend weight {old:.2f} -> {st['blend_weight']:.2f}. {why}")
    return why


# ── 5. learn from its worst misses ─────────────────────────
def review(st, card):
    done = [p for p in st["predictions"] if p.get("outcome")]
    if len(done) < st["reviewed_at"] + 10:
        return
    worst = sorted(done[-30:], key=lambda p: -p["brier"])[:5]
    rows = [{"coin": p["name"], "said": p["final_probs"], "happened": p["outcome"],
             "move": p["actual_move"], "why_it_said_so": p.get("reasoning", "")[:220]}
            for p in worst]
    try:
        out = think(f"""You are reviewing your own worst crypto predictions so you can do better.

Your five biggest misses: {json.dumps(rows)}
Your record: top pick right {card['hit_rate']}% of the time. Skill against a
no-brain baseline: {card['skill_vs_baseline']} (above 0 means you are adding value).
Calibration - when you sounded this confident, how often you were actually right:
{json.dumps(card.get('calibration', {}))}

Be hard on yourself. Look for a repeated habit, not one-off bad luck.

Reply with ONE JSON object, no markdown:
{{"lessons": ["3 short rules for your future self, each under 20 words, specific and testable"]}}""", tokens=800)
        st["lessons"] = (out.get("lessons", []) + st["lessons"])[:6]
        st["reviewed_at"] = len(done)
        print("  new lessons:", st["lessons"][:3])
    except Exception as e:
        print("  review failed:", e)


# ── 1. make today's predictions ────────────────────────────
def predict(st, card):
    mkt = get(f"{CG}/coins/markets", vs_currency="usd", ids=",".join(COINS),
              price_change_percentage="24h,7d")
    lessons = st["lessons"]
    ev = {}
    if EVENTS.exists():
        db = json.loads(EVENTS.read_text())
        ev = {"recent_events": [{"coin": e["name"], "move": e["move_pct"],
                                 "why": e["headline"], "type": e["category"]}
                                for e in db.get("events", [])[-6:]]}

    out = []
    for c in mkt:
        time.sleep(10)
        px = closes(c["id"])
        sd = vol(px) * math.sqrt(HORIZON)
        threshold = round(sd * 100 * 0.5, 2)          # what counts as a real move
        base = baseline(px, HORIZON, threshold)

        try:
            r = think(f"""Predict where {c['name']} goes over the next {HORIZON} days.

Price now: ${c['current_price']}
Last 24h: {c['price_change_percentage_24h']:+.2f}%
Last 7d: {c.get('price_change_percentage_7d_in_currency') or 0:+.2f}%
A move counts as UP if above +{threshold}%, DOWN if below -{threshold}%, otherwise FLAT.

The historical base rate for this coin over {HORIZON} days: {json.dumps(base)}
That is what happens with no analysis at all. Only move away from it if you have a real reason.

{json.dumps(ev) if ev else ""}
{"Lessons from your own past mistakes: " + json.dumps(lessons) if lessons else ""}

HEADLINES ABOUT THIS COIN:
{json.dumps(news.about(c["name"], hours=36))}

Use only these. Do not use anything you remember.

Reply with ONE JSON object, no markdown:
{{"up": 0.30, "flat": 0.45, "down": 0.25,
  "reasoning": "under 40 words, plain simple English, why you differ from the base rate - or why you do not"}}""", tokens=600)
        except Exception as e:
            print(f"  {c['name']} prediction failed: {e}")
            continue

        raw = {k: max(0.01, float(r.get(k, base[k]))) for k in ("up", "flat", "down")}
        tot = sum(raw.values())
        raw = {k: v / tot for k, v in raw.items()}

        # the correction: blend its view with the baseline
        w = st["blend_weight"]
        final = {k: round((1 - w) * raw[k] + w * base[k], 3) for k in raw}

        st["predictions"].append({
            "made_ms": int(time.time() * 1000),
            "made": dt.date.today().isoformat(),
            "coin": c["id"], "name": c["name"], "price_then": c["current_price"],
            "horizon": HORIZON, "threshold": threshold,
            "raw_probs": {k: round(v, 3) for k, v in raw.items()},
            "baseline_probs": base, "final_probs": final,
            "blend_weight": round(w, 2), "reasoning": r.get("reasoning", ""),
            "outcome": None, "actual_move": None,
        })
        pick = max(final, key=final.get)
        print(f"  {c['name']}: {pick} {final[pick]*100:.0f}% "
              f"(its own view said {max(raw, key=raw.get)} {max(raw.values())*100:.0f}%)")
        out.append(c["name"])
    return out


def main():
    st = load()
    print(f"{len(st['predictions'])} predictions on record")

    print("comparing old predictions to what really happened...")
    score_pending(st)

    card = report_card(st)
    print("measuring...")
    if card["scored"]:
        print(f"  top pick right {card['hit_rate']}% of {card['on_recent']}")
        print(f"  score {card['brier']} vs baseline {card['brier_baseline']} "
              f"-> {'BEATING' if card['beating_baseline'] else 'NOT beating'} the baseline")

    why = adjust(st, card)
    review(st, card)

    print("making today's predictions...")
    predict(st, card)

    st["predictions"] = st["predictions"][-400:]
    card = report_card(st)

    STATE.parent.mkdir(parents=True, exist_ok=True)
    PUBLIC.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, indent=1))
    PUBLIC.write_text(json.dumps({
        "updated": dt.datetime.utcnow().isoformat() + "Z",
        "report_card": card,
        "blend_weight": round(st["blend_weight"], 2),
        "adjustment": why,
        "lessons": st["lessons"],
        "open": [p for p in st["predictions"] if not p.get("outcome")][-12:],
        "settled": [p for p in st["predictions"] if p.get("outcome")][-20:],
        "verdict": (
            "Not enough results yet. Wait until at least 20 predictions have been scored."
            if card.get("scored", 0) < 20 else
            "It is beating the simple baseline. Modest, but real."
            if card["beating_baseline"] else
            "It is NOT beating the simple baseline. On this evidence its direction calls "
            "are not worth acting on, and it has pulled itself back towards the baseline."
        ),
    }, indent=1))
    print("\nverdict:", json.loads(PUBLIC.read_text())["verdict"])


if __name__ == "__main__":
    main()
