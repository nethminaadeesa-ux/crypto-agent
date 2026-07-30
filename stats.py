"""
Counts what the bot has actually done, and publishes it for the page.

It reads data/history.json (written by agent.py) and data/lab.json (written by
learn.py) and writes docs/stats.json. It touches nothing else, so it cannot
break the note or the lab.

Runs every hour alongside the watcher. Costs nothing - no network calls.
"""

import json, datetime as dt
from pathlib import Path

ROOT = Path(__file__).parent
HIST = ROOT / "data" / "history.json"
LAB = ROOT / "data" / "lab.json"
STARTED = ROOT / "data" / "started.txt"
OUT = ROOT / "docs" / "stats.json"


def read(path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def start_date(runs):
    """The day this thing came alive. Written down once, then never guessed again."""
    if STARTED.exists():
        try:
            return dt.date.fromisoformat(STARTED.read_text().strip())
        except ValueError:
            pass
    first = runs[0]["date"] if runs else dt.date.today().isoformat()
    STARTED.parent.mkdir(parents=True, exist_ok=True)
    STARTED.write_text(first)
    return dt.date.fromisoformat(first)


def main():
    h = read(HIST, {})
    runs = h.get("runs", [])
    forecasts = h.get("forecasts", [])
    lab = read(LAB, {})

    began = start_date(runs)
    days = (dt.date.today() - began).days + 1

    scored = [f for f in forecasts if f.get("actual") is not None]
    hits = sum(1 for f in scored if f.get("hit"))

    # how many hours of price history the lab is holding right now
    trail = lab.get("trail", {})
    readings = sum(len(v) for v in trail.values())

    stats = {
        "updated": dt.datetime.now(dt.timezone.utc).isoformat(),
        "started": began.isoformat(),
        "days_running": days,
        "notes_written": len(runs),
        "forecasts_made": len(forecasts),
        "forecasts_scored": len(scored),
        "forecasts_hit": hits,
        "lab_tests_scored": lab.get("resolved", 0),
        "lab_awaiting": len(lab.get("pending", [])),
        "lab_coins": len(trail),
        "lab_readings_held": readings,
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(stats, indent=1))

    print(f"  stats: day {days}, {len(runs)} notes, "
          f"{stats['lab_tests_scored']} lab tests, {len(scored)} forecasts scored")


if __name__ == "__main__":
    main()
