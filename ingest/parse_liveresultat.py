#!/usr/bin/env python3
"""Fetch results for events whose organizer publishes only on
liveresultat.orientering.se (a Swedish live-timing service some Austrian
events use instead of a SportSoftware export, e.g. Vienna O Challenge) -
ANNE has no attachment pointing there at all for these, only a link to the
organizer's own homepage, so the comp ids are found by hand from the
organizer's own results archive and recorded in MANUAL_LIVERESULTAT_COMPS.

liveresultat exposes a small public JSON API
(https://liveresults.github.io/documentation/api.html); this hits it
directly rather than scraping the JS-rendered results page.
"""
import argparse
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sportsoftware_common import MANUAL_LIVERESULTAT_COMPS  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw" / "anne"
OUT = ROOT / "data" / "normalized"
API = "https://liveresultat.orientering.se/api.php"
HEADERS = {"User-Agent": "olresults-sync/0.1 (+https://github.com/josefhilbert/olresults)"}

# liveresultat's numeric status code -> our vocabulary (matches the IOF/MeOS
# convention this and similar Nordic live-timing services follow)
STATUS_MAP = {
    0: "ok", 1: "dns", 2: "dnf", 3: "mp", 4: "dsq",
    # LiveResults documents 5 as OT (over maximum time). OLRESULTS2 uses the
    # same normalized classification as SportSoftware's Zeitüberschreitung.
    5: "dsq",
    # 9/10 are "Not Started Yet" and 11 is "Walk Over". In OLRESULTS2's
    # archived, completed-event result model all three mean no start/result.
    9: "dns", 10: "dns", 11: "dns",
}


def get(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def format_time(seconds):
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fetch_comp(comp):
    classes = get(f"{API}?method=getclasses&comp={comp}").get("classes") or []
    categories = []
    for c in classes:
        name = (c.get("className") or "").strip()
        if not name:
            continue
        d = get(f"{API}?comp={comp}&method=getclassresults&unformattedTimes=true"
                f"&class={urllib.parse.quote(name)}")
        results = []
        for r in d.get("results") or []:
            rname = re.sub(r"\s+", " ", (r.get("name") or "")).strip()
            if not rname:
                continue
            status = STATUS_MAP.get(r.get("status", 0), "unknown")
            res = {"name": rname, "club": re.sub(r"\s+", " ", (r.get("club") or "")).strip(),
                   "timeText": "", "status": status,
                   "rawStatusCode": r.get("status", 0)}
            place = str(r.get("place") or "").strip()
            if place.isdigit():
                res["rank"] = int(place)
            secs = str(r.get("result") or "0")
            if status == "ok" and secs.isdigit() and int(secs) > 0:
                res["timeS"] = int(secs) // 100
                res["timeText"] = format_time(res["timeS"])
            results.append(res)
        if results:
            categories.append({"name": name, "declaredStarters": len(results), "results": results})
        time.sleep(0.1)
    return categories


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--event-id", type=int, help="only process one ANNE event")
    ap.add_argument("--force-download", action="store_true",
                    help="refresh the selected live-results snapshot")
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    ok = empty = failed = 0
    for eid, comps in MANUAL_LIVERESULTAT_COMPS.items():
        if args.event_id is not None and eid != args.event_id:
            continue
        out_path = OUT / f"{eid}-0.json"
        if out_path.exists() and not args.force_download:
            continue  # already have results for this event from some source
        categories = []
        try:
            for comp in comps:
                categories.extend(fetch_comp(comp))
        except Exception as e:
            failed += 1
            print(f"  FAIL {eid} comps={comps}: {e}", file=sys.stderr)
            continue
        if not categories:
            empty += 1
            continue
        out_path.write_text(json.dumps({
            "eventId": eid,
            "source": "liveresultat",
            "sourceUrl": f"https://liveresultat.orientering.se/followfull.php?comp={comps[0]}",
            "fileName": f"liveresultat-comp-{'-'.join(map(str, comps))}",
            "listType": "race",
            "categories": categories,
        }, ensure_ascii=False))
        ok += 1
    print(f"parsed: {ok}, empty: {empty}, failed: {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
