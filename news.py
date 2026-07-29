"""
Free crypto news.

The paid web search costs 1 cent per search, and the bot searches a lot.
This replaces it with RSS feeds, which are free, need no key, and have no limit.

RSS is just a list of headlines a news site publishes for anyone to read.
Every crypto news site has one. We read four of them, take the recent
headlines, and hand those to the bot instead of letting it search.

Slightly less thorough than real search. Costs nothing.
"""

import time, datetime as dt
from xml.etree import ElementTree as ET
import requests

FEEDS = {
    "CoinDesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "Cointelegraph": "https://cointelegraph.com/rss",
    "Decrypt": "https://decrypt.co/feed",
    "The Block": "https://www.theblock.co/rss.xml",
}

GOOGLE = "https://news.google.com/rss/search"
UA = {"User-Agent": "Mozilla/5.0 (compatible; crypto-agent/1.0)"}


def _parse_date(s):
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z",
                "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            d = dt.datetime.strptime(s.strip(), fmt)
            return d.replace(tzinfo=None) - dt.timedelta(seconds=(d.utcoffset() or dt.timedelta()).total_seconds())
        except (ValueError, AttributeError):
            continue
    return None


def _read(url, source, hours):
    try:
        r = requests.get(url, headers=UA, timeout=20)
        r.raise_for_status()
        root = ET.fromstring(r.content)
    except Exception as e:
        print(f"    {source} feed unavailable: {e}")
        return []

    cutoff = dt.datetime.utcnow() - dt.timedelta(hours=hours)
    out = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        pub = _parse_date(item.findtext("pubDate") or "")
        if not title:
            continue
        if pub and pub < cutoff:
            continue
        out.append({"title": title[:180], "source": source,
                    "when": pub.strftime("%d %b %H:%M") if pub else "recent"})
    return out


def headlines(hours=24, limit=30):
    """Recent crypto headlines from the main news sites."""
    out = []
    for source, url in FEEDS.items():
        out += _read(url, source, hours)
        time.sleep(0.5)
    seen, clean = set(), []
    for h in out:
        k = h["title"].lower()[:60]
        if k not in seen:
            seen.add(k)
            clean.append(h)
    return clean[:limit]


def about(topic, hours=24, limit=12):
    """Headlines about one specific coin or subject."""
    url = f"{GOOGLE}?q={requests.utils.quote(topic + ' crypto when:1d')}&hl=en-US&gl=US&ceid=US:en"
    found = _read(url, "Google News", hours)
    if len(found) < 3:
        found += [h for h in headlines(hours, 20)
                  if topic.lower() in h["title"].lower()]
    return found[:limit]


if __name__ == "__main__":
    print("--- general ---")
    for h in headlines(24, 8):
        print(f"  [{h['source']}] {h['when']} - {h['title']}")
    print("\n--- about bitcoin ---")
    for h in about("bitcoin", 24, 5):
        print(f"  {h['when']} - {h['title']}")
