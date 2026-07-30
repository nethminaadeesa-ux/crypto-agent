"""
Makes sure today's note gets written, even when GitHub skips a run.

The problem this solves: the daily note used to depend on ONE scheduled run
at 7am. GitHub gives free accounts spare capacity only, so that run gets
skipped fairly often - and when it does, there is no note that day.

The watcher already runs hourly, so it gets many chances instead of one.
This script is the check it does each time:

    is it 7am or later in Sri Lanka?      no  -> do nothing
    has today's note already been written? yes -> do nothing
    otherwise                                  -> write it now

So the first run of the day that actually happens produces the note. If
that is the 7:23am run, fine. If GitHub skips until 11am, the note appears
at 11am instead of not at all.

A small file, data/last_note.txt, records which day was last written.
It is only updated after agent.py succeeds, so a failure just means the
next hour tries again.
"""

import subprocess, sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).parent
MARKER = HERE / "data" / "last_note.txt"
AGENT = HERE / "agent.py"

TZ = ZoneInfo("Asia/Colombo")
WRITE_AFTER_HOUR = 7          # no note before 7am local


def main():
    now = datetime.now(TZ)
    today = now.strftime("%Y-%m-%d")

    if now.hour < WRITE_AFTER_HOUR:
        print(f"  note check: {now:%H:%M} in Colombo, too early - waiting")
        return

    done = MARKER.read_text().strip() if MARKER.exists() else ""
    if done == today:
        print(f"  note check: today's note ({today}) is already written")
        return

    if done:
        print(f"  note check: last note was {done}, today is {today} - writing now")
    else:
        print(f"  note check: no note recorded yet - writing {today}")

    r = subprocess.run([sys.executable, str(AGENT)], cwd=str(HERE))
    if r.returncode != 0:
        print("  note check: agent.py failed - leaving it for the next hour")
        sys.exit(0)              # don't fail the whole watcher run

    MARKER.parent.mkdir(parents=True, exist_ok=True)
    MARKER.write_text(today)
    print(f"  note check: wrote the note for {today}")


if __name__ == "__main__":
    main()
