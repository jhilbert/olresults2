#!/usr/bin/env python3
"""Parse SportSoftware (Stephan Krämer OE2003/OE12/OEScore) PDF result exports.

Unlike the HTML exports, PDFs carry no table markup - columns only exist as
word x-positions on the page. Structure, from real samples:

    Pl Stnr Name       Verein          Nat  Zeit      <- column header,
                                                          repeats once per page
    H17-Wien (21) 7.8 km  280 Hm  27 P                <- category + course info,
                                                          one line, repeats per category
    1  105  Simkovics Erik  OLC Wienerwald  W  47:01  <- data row
    106      Lang Gerhard   HSV Pinkafeld   B  Fehlst <- unclassified (no Pl)

When a category continues onto a new page, the header repeats, a "(Forts.)"
marker appears, then the *same* category line repeats before further rows -
those rows are appended to the existing category rather than starting a new
one.

Column boundaries are derived from the header row's word x-positions using
the midpoint between consecutive headers, which correctly handles narrower
data (e.g. a right-aligned time) sitting left of its header's own x0.
"""
import argparse
import bisect
import json
import re
import sys
import time
import urllib.parse
import urllib.request
import warnings
from pathlib import Path

from sportsoftware_common import (
    CAT_LINE_RE, detect_list_type, is_junk_name, parse_course_info,
    parse_status, parse_time,
)

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw" / "anne"
FILES = RAW / "files"
OUT = ROOT / "data" / "normalized"

HEADERS = {"User-Agent": "olresults-sync/0.1 (+https://github.com/josefhilbert/olresults)"}

CONTINUATION_RE = re.compile(r"^\(Forts\.?\)$", re.I)
RANK_LEAK_RE = re.compile(r"^(\d{1,3})\s+(\S.*)$")
# split-times ("Zwischenzeiten") reports: a different, per-control layout that
# puts the club on its own line and interleaves dozens of split times into each
# row. They duplicate the plain results list, so we skip them rather than
# mis-parse them. Detected by the header word or a run of "N(controlcode)" tokens.
SPLITS_RE = re.compile(r"Zwischenzeiten|\d+\(\d+\)\s+\d+\(\d+\)")
# SportSoftware repeats the event title + full date as a running page header on
# every page; it leaks in as a bogus result row ("AC Mitteldistanz"). A real
# result row never carries a full dd.mm.yyyy date, so skip any line that does.
DATE_HEADER_RE = re.compile(r"\d{1,2}\.\d{1,2}\.\d{4}")
TIME_TOKEN_RE = re.compile(r"\d{1,3}:\d{2}(?::\d{2})?")
LINE_TOLERANCE = 3  # px, for clustering words into the same visual line


def group_lines(words):
    """Cluster words sharing (approximately) the same vertical position."""
    lines = {}
    for w in words:
        key = round(w["top"] / LINE_TOLERANCE)
        lines.setdefault(key, []).append(w)
    for key in sorted(lines):
        yield sorted(lines[key], key=lambda w: w["x0"])


def assign_columns(line_words, headers):
    """headers: [(label, x0), ...] sorted by x0. Assign each word to the
    header column whose x0 is closest without going over the midpoint to
    the next column, then join words per column in reading order."""
    xs = [h[1] for h in headers]
    midpoints = [(xs[i] + xs[i + 1]) / 2 for i in range(len(xs) - 1)]
    rec = {}
    for w in line_words:
        idx = bisect.bisect_right(midpoints, w["x0"])
        label = headers[idx][0]
        rec[label] = (rec.get(label, "") + " " + w["text"]).strip()
    return rec


def parse_pdf(path):
    import pdfplumber

    categories = []
    current = None
    headers = None
    head_text = ""

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pdfplumber.open(path) as pdf:
            if pdf.pages:
                head_text = pdf.pages[0].extract_text() or ""
            if SPLITS_RE.search(head_text):
                return [], head_text
            for page in pdf.pages:
                words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
                for line in group_lines(words):
                    if not line:
                        continue
                    text = " ".join(w["text"] for w in line)

                    if line[0]["text"] in ("Pl", "Platz") and len(line) >= 3:
                        headers = [(w["text"], w["x0"]) for w in line]
                        if current is None:
                            # some fun-run/app-based races have no age/gender
                            # classes at all: one flat ranking, no "(N)"
                            # category marker ever appears
                            current = {"name": "Ergebnis", "declaredStarters": None,
                                       "results": []}
                            categories.append(current)
                        continue
                    if CONTINUATION_RE.match(text):
                        continue
                    if DATE_HEADER_RE.search(text):
                        continue  # repeated page-header/title line

                    m = CAT_LINE_RE.match(text)
                    if m:
                        name = m.group("name").strip()
                        if current and current["name"] == name:
                            continue  # continuation of the same category
                        current = {"name": name,
                                   "declaredStarters": int(m.group("starters")),
                                   "results": []}
                        current.update(parse_course_info(m.group("rest")))
                        categories.append(current)
                        continue

                    if current is None or headers is None:
                        continue

                    rec = assign_columns(line, headers)
                    name = rec.get("Name", "").strip()
                    rank_text = (rec.get("Pl") or rec.get("Platz") or "").strip()
                    if not rank_text.isdigit():
                        # a narrow, right-aligned rank column can sit closer
                        # to the next header's x0 than its own, leaking the
                        # digit into the name field instead
                        leaked = RANK_LEAK_RE.match(name)
                        if leaked:
                            rank_text, name = leaked.group(1), leaked.group(2)
                    if is_junk_name(name):
                        continue
                    time_text = (rec.get("Zeit") or rec.get("Gesamt") or "").strip()
                    if not rank_text.isdigit() and not time_text:
                        continue

                    result = {
                        "name": name,
                        "club": (rec.get("Verein") or rec.get("Verein/Schule") or "").strip(),
                        "timeText": time_text,
                    }
                    if rank_text.isdigit():
                        result["rank"] = int(rank_text)
                    seconds = parse_time(time_text)
                    if seconds is None:
                        # a long club name can overflow into the time column
                        # ("Naturfreunde Villach - Oriente 21:06"); recover the
                        # time token rather than dropping a real finisher
                        tm = TIME_TOKEN_RE.search(time_text)
                        if tm:
                            seconds = parse_time(tm.group())
                    if seconds is not None:
                        result["timeS"] = seconds
                        result["status"] = "ok"
                    else:
                        result["status"] = parse_status(time_text) or "unknown"
                    yob = rec.get("Jg", "").strip()
                    if yob.isdigit():
                        y = int(yob)
                        result["yearOfBirth"] = y + (2000 if y <= 26 else 1900) if y < 100 else y
                    if rec.get("Pkt"):
                        result["scoreText"] = rec["Pkt"].strip()
                    current["results"].append(result)

    categories = [c for c in categories if c["results"]]
    for c in categories:
        if c["declaredStarters"] is None:
            c["declaredStarters"] = len(c["results"])
    return categories, head_text


def fetch(url, dest):
    if dest.exists():
        return dest.read_bytes()
    safe_url = urllib.parse.quote(url, safe=":/?&=%")
    data = urllib.request.urlopen(
        urllib.request.Request(safe_url, headers=HEADERS), timeout=30).read()
    dest.write_bytes(data)
    time.sleep(0.15)
    return data


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="only process N files (0 = all)")
    args = ap.parse_args()

    FILES.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)
    attachments = json.loads((RAW / "attachments.json").read_text())

    jobs = []
    for eid, files in attachments.items():
        for n, f in enumerate(files or []):
            if f["mimeType"] == "application/pdf":
                jobs.append((int(eid), n, f))
    if args.limit:
        jobs = jobs[: args.limit]
    print(f"pdf files to parse: {len(jobs)}")

    ok = empty = failed = 0
    for eid, n, f in jobs:
        out_path = OUT / f"{eid}-{n}.json"
        pdf_path = FILES / f"{eid}-{n}.pdf"
        try:
            fetch(f["url"], pdf_path)
            cats, head_text = parse_pdf(pdf_path)
            if not cats:
                empty += 1
                continue
            out_path.write_text(json.dumps({
                "eventId": eid,
                "source": "sportsoftware-pdf",
                "sourceUrl": f["url"],
                "fileName": f["fileName"],
                "listType": detect_list_type(f["fileName"], head_text),
                "categories": cats,
            }, ensure_ascii=False))
            ok += 1
        except Exception as e:
            failed += 1
            print(f"  FAIL {eid}-{n} {f['fileName']}: {e}", file=sys.stderr)
    print(f"parsed: {ok}, empty: {empty}, failed: {failed}")


if __name__ == "__main__":
    main()
