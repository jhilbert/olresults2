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
STATUS_MAP = {0: "ok", 1: "dns", 2: "dnf", 3: "mp", 4: "dsq", 5: "nc"}


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
                   "timeText": "", "status": status}
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
    OUT.mkdir(parents=True, exist_ok=True)
    ok = empty = failed = 0
    for eid, comps in MANUAL_LIVERESULTAT_COMPS.items():
        out_path = OUT / f"{eid}-0.json"
        if out_path.exists():
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
