name: Watch the market

on:
  schedule:
    - cron: "5 * * * *"        # every hour, 5 minutes past
  workflow_dispatch:

permissions:
  contents: write

concurrency:
  group: watcher              # never let two runs collide
  cancel-in-progress: false

jobs:
  watch:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pip install requests

      - name: Check the market
        env:
          GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}
          COINS: bitcoin,ethereum,solana
          MIN_MOVE_PCT: "1.5"
          SENSITIVITY: "2.5"
        run: python watch.py

      - name: Save what it learned
        run: |
          git config user.name "crypto-agent"
          git config user.email "agent@users.noreply.github.com"
          git pull --rebase --autostash || true
          git add data/events.json docs/events.json
          git diff --staged --quiet || git commit -m "watch $(date -u +%Y-%m-%dT%H:%M)"
          git push
