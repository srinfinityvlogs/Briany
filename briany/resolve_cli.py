"""Try the resolver on REAL data (stdlib only).

  python -m briany.resolve_cli --stats reliance relience goldbees tata nifty
"""
import argparse
import io
import urllib.request
import zipfile
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from briany.symbols import Resolver, format_choices, parse_bhavcopy

UA = {"User-Agent": "Mozilla/5.0"}
URL = "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{ymd}_F_0000.csv.zip"


def fetch_latest(date=None):
    day = datetime.strptime(date, "%Y-%m-%d") if date else datetime.now(ZoneInfo("Asia/Kolkata"))
    for _ in range(8):
        if day.weekday() < 5:
            ymd = day.strftime("%Y%m%d")
            try:
                req = urllib.request.Request(URL.format(ymd=ymd), headers=UA)
                with urllib.request.urlopen(req, timeout=30) as r:
                    z = zipfile.ZipFile(io.BytesIO(r.read()))
                    return day.strftime("%Y-%m-%d"), z.read(z.namelist()[0]).decode("utf-8")
            except Exception:  # noqa: BLE001 - try the previous day
                pass
        day -= timedelta(days=1)
    raise SystemExit("could not fetch a bhavcopy in the last 8 days")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("queries", nargs="*")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--date")
    a = ap.parse_args()
    date, text = fetch_latest(a.date)
    instruments, stats = parse_bhavcopy(text)
    print(f"file date {date}: {stats['rows']} rows, {len(instruments)} tradable instruments")
    if a.stats:
        print("series counts:", dict(stats["series"].most_common(12)))
        print("instrument types:", dict(stats["instrument_types"]))
        gb = [i for i in instruments if i.symbol == "GOLDBEES"]
        print("GOLDBEES in universe:", gb[0] if gb else "NOT FOUND")
    res = Resolver(instruments)
    for q in a.queries:
        r = res.resolve(q)
        top = ", ".join(f"{m.instrument.symbol}={m.score}" for m in r.matches)
        print(f"{q!r:28} -> {r.status:9} {top}\n{'':31}{format_choices(r)}")


if __name__ == "__main__":
    main()
