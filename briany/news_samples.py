"""Capture real Google News RSS responses (public) to verify the parser. Used once.

  python -m briany.news_samples
"""
import os
import time
import urllib.parse
import urllib.request

from briany.news import HEADERS, RSS_URL, build_query, parse_feed

QUERIES = [("Reliance Industries Limited", "RELIANCE"), ("Tempsens Instruments (India) Limited", "TEMPSENS"),
           ("KRN Heat Exchanger and Refrigeration Limited", "KRN"), ("Nippon India ETF Gold BeES", "GOLDBEES"),
           ("Zzqx Nonexistent Holdings Limited", "ZZQX")]


def main(out_dir="news_samples", pause=2.0):
    os.makedirs(out_dir, exist_ok=True)
    for name, symbol in QUERIES:
        query = build_query(name)
        url = RSS_URL + "?" + urllib.parse.urlencode({"q": query, "hl": "en-IN", "gl": "IN", "ceid": "IN:en"})
        print(f"\n== {name} ({symbol})  query: {query}")
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=30) as r:
                body = r.read().decode("utf-8", "replace")
                print(f"   HTTP {r.status}, {len(body)} chars, content-type: {r.headers.get('Content-Type')}")
        except Exception as e:  # noqa: BLE001
            print("   request failed:", type(e).__name__)
            continue
        with open(os.path.join(out_dir, symbol + ".xml"), "w", encoding="utf-8") as fh:
            fh.write(body[:80000])
        try:
            raw = parse_feed(body)
        except Exception as e:  # noqa: BLE001
            print("   parse failed:", type(e).__name__, e)
            print("   start of body:", body[:300].replace("\n", " "))
            continue
        print(f"   parsed items: {len(raw)}")
        for r in raw[:6]:
            print(f"   - {r['pub']} | {r['source']!r} | {r['title'][:100]}")
        time.sleep(pause)


if __name__ == "__main__":
    main()
