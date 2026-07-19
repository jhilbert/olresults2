#!/usr/bin/env python3
"""Parse SportSoftware fixed-width plain-text result reports.

This is the third SportSoftware export format after HTML tables and PDFs:
space-aligned fixed-width columns, either served as a `.txt` attachment on
the ANNE CDN or embedded in a <pre> block on a club website (linked from
ANNE as a text/link "results" attachment). Example:

       Pl Name                            Verein                          Zeit
    Damen A  (11)                    3.6 km   20 P
        1 Denise Hlosta                   NF Wien                        27:12
        2 Anika Gassner                   NF Wien                        29:12

Columns are read from the header row's word positions (they vary between
files - some carry Stnr/Jg/Nat, some don't). Left-aligned fields (Name,
Verein) are sliced [start_i : start_{i+1}]; the trailing right-aligned time
column is widened leftward so longer times (H:MM:SS) that overflow their
header width are still captured.

text/link sources are gated to a domain allowlist of club sites verified to
embed this format - we do not blindly crawl all ~500 external result links
(many are dead, live-timing systems, or unrelated formats).
"""
import argparse
from collections import defaultdict
import html as html_mod
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

from sportsoftware_common import (
    CAT_LINE_RE, CLUB_LINK_ALLOWLIST, COLUMN_ALIASES, category_starter_count,
    classify_championship_text, detect_list_type,
    expand_pair_result, is_expected_source_failure, is_junk_name, is_ooc_status,
    parse_champion_annotation, parse_course_info,
    parse_status, parse_time, parse_time_loose, number_team_results,
    team_results_from_pairs,
)

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw" / "anne"
FILES = RAW / "files"
OUT = ROOT / "data" / "normalized"

HEADERS = {"User-Agent": "olresults-sync/0.1 (+https://github.com/josefhilbert/olresults)"}
LINK_DOMAIN_ALLOWLIST = CLUB_LINK_ALLOWLIST

HEADER_RE = re.compile(r"^\s*Pl\b")
TIME_COL_PAD = 6  # chars to widen the trailing time column leftward
CHAMPION_MARKER_RE = re.compile(r"(?i)\b(?:staats?)?meister(?:in(?:nen)?)?\b")
CHAMPION_TRAILING_RANK_RE = re.compile(
    r"(?i)\b(?:staats?)?meister(?:in(?:nen)?)?\s+(\d+)\s*$")
SCORE_CAT_LINE_RE = re.compile(
    r"^(?P<name>(?!\d).+?)\s+\d+(?::\d{2})?\s*min\b.*\b(?:P|Pkt)\b", re.I)
COURSE_CAT_LINE_RE = re.compile(
    r"^(?P<name>(?!\d).+?)\s+\d+(?:[.,]\d+)?\s*km\b", re.I)


def extract_pre_blocks(text):
    """Return SportSoftware fixed-width text from a document: the raw text
    itself, or the concatenation of any <pre> blocks it contains."""
    if "<pre" not in text.lower():
        return text
    blocks = re.findall(r"<pre[^>]*>(.*?)</pre>", text, re.S | re.I)
    out = []
    for b in blocks:
        b = re.sub(r"<[^>]+>", "", b)      # strip inner <font>/<b> tags
        out.append(html_mod.unescape(b))
    return "\n".join(out)


def parse_columns(header_line):
    cols = [(m.group(), m.start()) for m in re.finditer(r"\S+", header_line)]
    return [c[0] for c in cols], [c[1] for c in cols]


def column_bounds(labels, starts):
    """Slice boundaries from header word positions. The trailing right-aligned
    time column is pulled left (longer times overflow their header width); the
    preceding column's right edge is pulled left to match so the time's
    leading digits don't leak into it. ``Zeit`` is not always the final column:
    older exports append a Bundesland/country column (``BL``). Moving that
    final column instead split ``39:08`` into club="... 3", time="9:" and
    BL="08 ST", silently dropping every normally timed row while retaining
    only trailing DNS/MP rows."""
    bounds = list(starts)
    time_index = next(
        (i for i, label in enumerate(labels) if label in ("Zeit", "Gesamt")),
        len(bounds) - 1,
    )
    if time_index > 0:
        bounds[time_index] = max(
            bounds[time_index - 1] + 1, bounds[time_index] - TIME_COL_PAD)
    return bounds


def slice_row(line, labels, bounds):
    rec = {}
    for i, label in enumerate(labels):
        s = bounds[i]
        e = bounds[i + 1] if i + 1 < len(bounds) else len(line)
        rec[label] = line[s:e].strip()
    return rec


def parse_text(text):
    categories = []
    current = None
    labels = starts = None
    pending_rank = pending_championship = None  # from a champion-announcement
    team_counts = defaultdict(int)
                     # line ("1. und Österr.Meister 2022"), which - unlike the
                     # HTML/PDF layouts - sits on its own line rather than
                     # merged into the winner's row; fixed-width column
                     # slicing would garble it anyway since the announcement
                     # text overflows the narrow "Pl" column, so it's matched
                     # against the raw line before any column slicing and
                     # carried forward onto the very next data row

    # SportSoftware indents the champion row with a tab; expand tabs so the
    # fixed-width columns line up with the header again
    text = text.expandtabs()
    for line in text.split("\n"):
        if not line.strip():
            continue
        if HEADER_RE.match(line) and "Name" in line:
            labels, starts = parse_columns(line)
            labels = [COLUMN_ALIASES.get(l, l) for l in labels]
            starts = column_bounds(labels, starts)
            continue

        cm = CAT_LINE_RE.match(line.strip())
        if cm:
            name = cm.group("name").strip()
            if current and current["name"] == name:
                continue
            current = {"name": name,
                       "declaredStarters": category_starter_count(cm),
                       "results": []}
            current.update(parse_course_info(cm.group("rest")))
            categories.append(current)
            pending_rank = pending_championship = None
            team_counts = defaultdict(int)
            continue

        # OEScore sometimes omits ``(N)`` for a class while still printing a
        # structural score-course heading such as ``Herren Oberstufe 40 min
        # 29 P 590 Pkt``. Without this boundary its rows leak into the
        # preceding class and create an apparent +N parser mismatch.
        score_category = SCORE_CAT_LINE_RE.match(line.strip())
        if score_category:
            name = score_category.group("name").strip()
            if not current or current["name"] != name:
                current = {"name": name, "declaredStarters": None, "results": []}
                categories.append(current)
            pending_rank = pending_championship = None
            team_counts = defaultdict(int)
            continue

        # Truncated historic exports often lose the whole ``(N)`` field but
        # retain an unambiguous course heading (``Herren C Erwachsene 2.5 km
        # 12 Posten``). Treat it as a new class with unknown declared size;
        # otherwise all following rows leak into the previous counted class.
        course_category = COURSE_CAT_LINE_RE.match(line.strip())
        if course_category:
            name = re.sub(r"\s*\($", "", course_category.group("name")).strip()
            if not current or current["name"] != name:
                current = {"name": name, "declaredStarters": None, "results": []}
                categories.append(current)
            pending_rank = pending_championship = None
            team_counts = defaultdict(int)
            continue

        if current is None or labels is None:
            continue

        stripped = line.strip()
        annot_rank, annot_championship = parse_champion_annotation(stripped)
        if annot_rank is not None:
            pending_rank, pending_championship = annot_rank, annot_championship
            continue

        # Some fixed-width exports separate the Austrian-champion marker
        # from the actual result row even more aggressively than the common
        # annotation forms: either only ``und österreichischer Meister`` is
        # printed (the numeric rank remains on the following row), or the
        # rank is appended to the marker (``... Meisterin 2``) while the
        # following row starts with the bib number. Keep both pieces as
        # pending state and repair that following row below.
        if (CHAMPION_MARKER_RE.search(stripped)
                and not re.search(r"\d{1,3}:\d{2}", stripped)):
            championship = classify_championship_text(stripped)
            if championship:
                trailing = CHAMPION_TRAILING_RANK_RE.search(stripped)
                pending_rank = int(trailing.group(1)) if trailing else None
                pending_championship = championship
                continue

        parse_line = line
        repaired_rank = repaired_bib = None
        if pending_championship and "Name" in labels:
            # OE2010 occasionally tab-indents the row following a champion
            # marker, shifting every fixed-width column. Recover its leading
            # rank/bib tokens and anchor the actual name at the Name column.
            # Two numbers mean rank+bib; one number with a pending rank means
            # bib only. This is deliberately limited to the immediately
            # following champion row, so ordinary unranked/status rows retain
            # their original fixed-column interpretation.
            two = re.match(r"^\s*(\d+)\s+(\d+)\s+(.+)$", line)
            one = re.match(r"^\s*(\d+)\s+(.+)$", line)
            name_start = starts[labels.index("Name")]
            if two:
                repaired_rank, repaired_bib, remainder = two.groups()
                parse_line = " " * name_start + remainder
            elif pending_rank is not None and one:
                repaired_bib, remainder = one.groups()
                parse_line = " " * name_start + remainder

        pairs = [(labels[i],
                  parse_line[starts[i]:(starts[i + 1] if i + 1 < len(starts) else len(parse_line))].strip())
                 for i in range(len(labels))]
        rec = slice_row(parse_line, labels, starts)
        if repaired_rank is not None:
            rec["Pl"] = repaired_rank
        if repaired_bib is not None:
            rec["Stnr"] = repaired_bib
        time_text = (rec.get("Zeit") or rec.get("Gesamt") or "").strip()
        rank_text = (rec.get("Pl") or "").strip().rstrip(".")

        # A Mannschaft report can switch to compact individual classes near
        # the end while retaining the document-wide
        # Name/Läufer2/Läufer3/Zeit header.  Those shorter rows place their
        # elapsed time or status in the Läufer2 slice and leave Zeit empty.
        # Treat that value as the result column and do not manufacture a
        # two-person team from ``Name + 34:45``.
        compact_value = ""
        if not time_text:
            compact_value = next((
                (rec.get(label) or "").strip()
                for label in ("Läufer2", "Laeufer2", "Runner2", "Läufer3", "Laeufer3", "Runner3")
                if parse_time_loose((rec.get(label) or "").strip()) is not None
                or parse_status((rec.get(label) or "").strip())
            ), "")
            if compact_value:
                time_text = compact_value

        # team (Mannschaft) fixed-width lists: members across Name/Läufer2/… cols
        if (rank_text.isdigit() or time_text) and not compact_value:
            club = (rec.get("Verein") or "").strip()
            team = team_results_from_pairs(pairs, club, rec.get("Pl", ""), time_text)
            if team is not None:
                team_counts[club] += 1
                current["results"].extend(
                    number_team_results(team, club, team_counts[club]))
                continue

        name = (rec.get("Name") or "").strip()
        if is_junk_name(name):
            continue
        if not rank_text.isdigit() and not time_text and not parse_status(line):
            continue

        result = {
            "name": name,
            "club": (rec.get("Verein") or rec.get("Verein/Schule") or "").strip(),
            "timeText": time_text,
        }
        if "famil" in current["name"].casefold():
            # Family combinations stay visible as one source result but must
            # never create or attach to a person identity.
            result["resultKind"] = "family"
        elif compact_value:
            result["resultKind"] = "individual"
        # AK replaces the numeric placement in old fixed-width exports.  It
        # is orthogonal to the finish classification: both ``AK 48:12`` and
        # ``AK Fehlst`` are valid source combinations.
        if is_ooc_status(rank_text):
            result["outOfCompetition"] = True
        if rank_text.isdigit():
            # this row has its own rank after all - it wasn't the one the
            # pending announcement belonged to, so drop the pending state
            # rather than misattaching the title to an unrelated rank
            result["rank"] = int(rank_text)
            if pending_championship:
                result["championship"] = pending_championship
        elif pending_rank is not None:
            result["rank"] = pending_rank
            if pending_championship:
                result["championship"] = pending_championship
        pending_rank = pending_championship = None
        seconds = parse_time_loose(time_text)
        explicit_status = parse_status(line)
        if explicit_status == "ok":
            explicit_status = None
        score_mode = (rank_text.isdigit()
                      and any(rec.get(label) for label in ("Pkt", "Erg", "Punkte")))
        if seconds is not None:
            result["timeS"] = seconds
            result["status"] = explicit_status or "ok"
        elif score_mode:
            # OEScore uses Zeit for whole elapsed minutes (``40``) and ranks
            # by the final score in Erg/Pkt. This is a classified result even
            # though that source value deliberately is not an HH:MM time.
            result["status"] = explicit_status or "ok"
            result["scoreText"] = next(
                (rec[label].strip() for label in ("Erg", "Pkt", "Punkte")
                 if rec.get(label)), "")
        else:
            status = explicit_status or parse_status(time_text)
            if status is None:
                # no valid time and no recognized status keyword: not a
                # genuine result row
                continue
            result["status"] = status
        yob = (rec.get("Jg") or "").strip()
        if yob.isdigit():
            y = int(yob)
            result["yearOfBirth"] = y + (2000 if y <= 26 else 1900) if y < 100 else y
        current["results"].extend(expand_pair_result(result, current.get("name")))

    parsed = [c for c in categories if c["results"]]
    for category in parsed:
        results = category["results"]
        if not results or any(
                (result.get("resultKind") or "individual") not in {"individual", "family"}
                for result in results):
            continue
        ranks = [result["rank"] for result in results if result.get("rank") is not None]
        if (ranks and category.get("declaredStarters") == max(ranks)
                and len(results) > category["declaredStarters"]):
            category["declaredStarters"] = len(results)
    return parsed


def fetch(url, dest):
    if dest.exists():
        return dest.read_bytes()
    safe_url = urllib.parse.quote(url, safe=":/?&=%#")
    data = urllib.request.urlopen(
        urllib.request.Request(safe_url, headers=HEADERS), timeout=30).read()
    dest.write_bytes(data)
    time.sleep(0.15)
    return data


def decode(data):
    head = data[:1000].decode("ascii", "ignore").lower()
    if "utf-8" in head:
        return data.decode("utf-8", "replace")
    return data.decode("windows-1252", "replace")


def domain_of(url):
    return urllib.parse.urlparse(url).netloc.lower().replace("www.", "")


def collect_jobs():
    attachments = json.loads((RAW / "attachments.json").read_text())
    jobs = []
    for eid, files in attachments.items():
        for n, f in enumerate(files or []):
            mime, url = f["mimeType"], f["url"]
            if mime == "text/plain":
                jobs.append((int(eid), n, f, "txt"))
            elif mime == "text/link" and domain_of(url) in LINK_DOMAIN_ALLOWLIST:
                jobs.append((int(eid), n, f, "html"))
    return jobs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--event-id", type=int, help="only process one ANNE event")
    args = ap.parse_args()

    FILES.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)
    jobs = collect_jobs()
    if args.event_id is not None:
        jobs = [job for job in jobs if job[0] == args.event_id]
    if args.limit:
        jobs = jobs[: args.limit]
    print(f"text/link+text/plain files to parse: {len(jobs)}")

    ok = empty = failed = expected_failed = 0
    for eid, n, f, kind in jobs:
        out_path = OUT / f"{eid}-{n}.json"
        try:
            data = fetch(f["url"], FILES / f"{eid}-{n}.{kind}")
            text = extract_pre_blocks(decode(data))
            cats = parse_text(text)
            if not cats:
                empty += 1
                out_path.unlink(missing_ok=True)  # stale output from an earlier, buggier run
                continue
            out_path.write_text(json.dumps({
                "eventId": eid,
                "source": "sportsoftware-text",
                "sourceUrl": f["url"],
                "fileName": f["fileName"] or f["url"],
                "listType": detect_list_type(f["fileName"] or f["url"], text),
                "categories": cats,
            }, ensure_ascii=False))
            ok += 1
        except Exception as e:
            if is_expected_source_failure("sportsoftware-text", eid, n):
                expected_failed += 1
                print(f"  EXPECTED UNAVAILABLE {eid}-{n} {f['url']}: {e}", file=sys.stderr)
            else:
                failed += 1
                print(f"  FAIL {eid}-{n} {f['url']}: {e}", file=sys.stderr)
    print(f"parsed: {ok}, empty: {empty}, expected unavailable: {expected_failed}, failed: {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
