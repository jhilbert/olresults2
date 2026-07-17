#!/usr/bin/env python3
"""Parse SportSoftware (Stephan Krämer OE/OS/OE12) HTML result exports.

Reads the attachment index produced by anne_sync.py, downloads text/html
result files (cached under data/raw/anne/files/), parses them into the
normalized results shape and writes data/normalized/{eventId}-{n}.json.

SportSoftware HTML uses HTML4 with unclosed <td>/<tr> tags. Structure per
category:
    <a id="D14"></a>
    <table><tr><td id=c00>D14  (1)<td id=c01>2,3 km  130 Hm<td id=c02>8 P ...
    <table><thead><tr><th>Pl</th><th>Name</th>...</thead>...
    <table><tbody><tr><td>1<td><nobr>Name</nobr><td>...
"""
import argparse
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

from sportsoftware_common import (
    CAT_LINE_RE, COURSE_RE, MANUAL_ATTACHMENT_SKIP, MANUAL_CATEGORY_SKIP, MANUAL_DOC_DATE_OVERRIDES,
    TIME_TOKEN_RE, classify_championship_text, detect_list_type, expand_pair_result, extract_html_title,
    guess_doc_date, is_junk_name, parse_champion_annotation, parse_course_info, parse_status, parse_time,
    parse_time_loose, team_results_from_pairs,
)

ANNOT_RANK_RE = re.compile(r"(?i)meister|sieger")

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw" / "anne"
FILES = RAW / "files"
OUT = ROOT / "data" / "normalized"

# A descriptive bot UA is polite, but some organizer sites (e.g.
# viennaochallenge.com) sit behind Cloudflare's basic bot check and 403 it
# outright; a normal desktop-browser UA gets through without triggering an
# actual JS challenge, so it's used everywhere rather than just per-domain.
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}


class TableExtractor(HTMLParser):
    """Flatten the document into a list of tables, each a list of row-cell-lists."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables = []
        self._rows = None
        self._cells = None
        self._buf = None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._flush_row()
            self._rows = []
            self.tables.append(self._rows)
        elif tag == "tr" and self._rows is not None:
            self._flush_row()
            self._cells = []
        elif tag in ("td", "th") and self._cells is not None:
            self._flush_cell()
            self._buf = []

    def handle_endtag(self, tag):
        if tag == "table":
            self._flush_row()
            self._rows = None
        elif tag == "tr":
            self._flush_row()
        elif tag in ("td", "th"):
            self._flush_cell()

    def handle_data(self, data):
        if self._buf is not None:
            self._buf.append(data)

    def _flush_cell(self):
        if self._buf is not None and self._cells is not None:
            text = re.sub(r"\s+", " ", "".join(self._buf)).strip().strip("\xa0").strip()
            self._cells.append(text)
        self._buf = None

    def _flush_row(self):
        self._flush_cell()
        if self._cells is not None and self._rows is not None:
            self._rows.append(self._cells)
        self._cells = None


def parse_document(html_text):
    ex = TableExtractor()
    ex.feed(html_text)

    categories = []
    current = None
    columns = None
    pending_rank = pending_championship = None
    for table in ex.tables:
        for row in table:
            if not row or all(c in ("", "&nbsp") for c in row):
                continue
            first = row[0]
            m = CAT_LINE_RE.match(first)
            if m and len(row) >= 2:
                # category header: "D14  (1)" | "2,3 km  130 Hm" | "8 P"
                current = {
                    "name": m.group("name").strip(),
                    "declaredStarters": int(m.group("starters")),
                    "results": [],
                }
                current.update(parse_course_info(" ".join(row[1:])))
                categories.append(current)
                columns = None
                pending_rank = pending_championship = None
                continue
            if first == "Pl" or (current and "Name" in row):
                columns = row
                continue
            if current is None or columns is None:
                continue
            # champion-announcement rows come in several shapes - the whole
            # phrase in one cell ('<td colspan=3>1. und Staatsmeister 2025'),
            # a clean rank cell plus one wide cell for the rest ('<td>1<td
            # colspan=100>und Österreichische Meisterin'), or a clean rank
            # with the phrase landing in whatever header the layout's next
            # column happens to be (Name if there's no Stnr column, Stnr if
            # there is) - none of which line up with the real Pl/Stnr/Name/
            # Verein/Zeit header. Rather than enumerate every cell-count
            # shape, join the whole row and match it as one: a genuine
            # announcement carries no time value of its own (guarded inside
            # parse_champion_annotation), so a hybrid row that also has the
            # winner's real name/time in a later cell correctly falls
            # through to the normal per-cell handling below instead.
            joined = " ".join(c for c in row if c and c != "&nbsp").strip()
            annot_rank, annot_championship = parse_champion_annotation(joined)
            if annot_rank is not None:
                pending_rank, pending_championship = annot_rank, annot_championship
                continue
            # data row: align cells to columns
            rec = dict(zip([c or f"col{i}" for i, c in enumerate(columns)], row))
            time_text = (rec.get("Zeit") or rec.get("Gesamt") or "").strip()
            rank_ok = rec.get("Pl", "").strip().isdigit()
            club = (rec.get("Verein") or rec.get("Verein/Schule") or "").strip()

            # team (Mannschaft) tables: members across several columns
            # (Name 1/2/3, Name Läufer2 Läufer3, or repeated 'Name' headers)
            if rank_ok or time_text:
                team = team_results_from_pairs(list(zip(columns, row)),
                                               club, rec.get("Pl", ""), time_text)
                if team is not None:
                    current["results"].extend(team)
                    continue

            name = rec.get("Name", "").strip()
            if is_junk_name(name):
                continue
            # club/spacer rows in split-time lists carry neither rank nor time
            if not rank_ok and not time_text:
                continue
            result = {
                "name": name,
                "club": (rec.get("Verein") or rec.get("Verein/Schule") or "").strip(),
                "timeText": time_text,
            }
            rank_text = rec.get("Pl", "").strip()
            if rank_text.isdigit():
                # this row has its own rank after all - not the one the
                # pending announcement belonged to
                result["rank"] = int(rank_text)
                pending_rank = pending_championship = None
            else:
                annot_rank, championship = parse_champion_annotation(rank_text)
                if annot_rank is not None:
                    result["rank"] = annot_rank
                    if championship:
                        result["championship"] = championship
                elif pending_rank is not None:
                    result["rank"] = pending_rank
                    if pending_championship:
                        result["championship"] = pending_championship
                pending_rank = pending_championship = None
            seconds = parse_time_loose(time_text)
            if seconds is not None:
                result["timeS"] = seconds
                result["status"] = "ok"
            else:
                result["status"] = parse_status(time_text) or "unknown"
            yob = (rec.get("Jg") or "").strip()
            if yob.isdigit():
                y = int(yob)
                result["yearOfBirth"] = y + (2000 if y <= 26 else 1900) if y < 100 else y
            if rec.get("Pkt"):
                result["scoreText"] = rec["Pkt"].strip()
            current["results"].extend(expand_pair_result(result, current.get("name")))

    return [c for c in categories if c["results"]]


BRACKET_CAT_RE = re.compile(r"^\(\d+(?:\s*/\s*\d+)?\)$")


def parse_bracket_html(html_text):
    """Parse a liveresultat.orientering.se-exported results page (ANNE
    stores a saved snapshot of these for e.g. a KO-Sprint's heat brackets,
    rather than a SportSoftware export). One <table> holds a title/date row,
    then per-heat blocks: a header row ('Viertelfinal A' | '(6 / 6)' | 'Time'
    | 'Behind' | 'Time lost') and its data rows, all prefixed by a blank
    spacer cell from a merged first column. A winner's row sometimes merges
    the Time/Behind cells via colspan, but the cell *position* from the left
    (rank, name, club, time, ...) stays the same either way - only the
    trailing cell count varies, which parse_document()'s Pl/Name/Verein
    header-column matching can't handle at all since this layout has no
    header row of its own to match against."""
    ex = TableExtractor()
    categories, current = [], None
    ex.feed(html_text)
    for table in ex.tables:
        for row in table:
            cells = list(row)
            if cells and cells[0] in ("", "&nbsp"):
                cells = cells[1:]
            if not cells or all(c in ("", "&nbsp") for c in cells):
                continue
            if len(cells) >= 2 and BRACKET_CAT_RE.match(cells[1].strip()):
                name = cells[0].strip()
                if current and current["name"] == name:
                    continue
                m = re.search(r"\d+", cells[1])
                current = {"name": name, "declaredStarters": int(m.group()) if m else None,
                           "results": []}
                categories.append(current)
                continue
            if current is None or len(cells) < 2:
                continue
            rank = championship = None
            if cells[0].rstrip(".").isdigit():
                rank = int(cells[0].rstrip("."))
                cells = cells[1:]
            else:
                # the winner's rank cell sometimes carries the champion
                # announcement inline ("1. und Österreichische Meisterin
                # 2024") instead of a bare "1." - confirmed real (event
                # 4220, D45-): without this, the whole cell fails the bare-
                # digit check above, "name" below becomes the announcement
                # text instead of the real name, and is_junk_name() rejects
                # the row outright - the actual winner silently vanishes
                # rather than just losing their rank.
                annot_rank, annot_championship = parse_champion_annotation(cells[0])
                if annot_rank is not None:
                    rank, championship = annot_rank, annot_championship
                    cells = cells[1:]
            if len(cells) < 2:
                continue
            name, club = cells[0].strip(), cells[1].strip()
            if is_junk_name(name):
                continue
            # some organizers (e.g. Vienna O Challenge) add a "Country"
            # column between club and the time/status value, so the value
            # isn't reliably at a fixed position - scan for whichever of the
            # two it actually is, preferring a real time over a status word
            values = [c.strip().lstrip("+") for c in cells[2:]]
            time_text = next((v for v in values if TIME_TOKEN_RE.fullmatch(v)), None)
            status_text = None if time_text else next((v for v in values if parse_status(v)), None)
            result = {"name": name, "club": club, "timeText": time_text or status_text or ""}
            if rank is not None:
                result["rank"] = rank
            if championship:
                result["championship"] = championship
            seconds = parse_time(time_text) if time_text else None
            if seconds is not None:
                result["timeS"] = seconds
                result["status"] = "ok"
            else:
                result["status"] = parse_status(status_text or "") or "unknown"
            current["results"].append(result)
    return [c for c in categories if c["results"]]


def parse_relay_document(html_text):
    """Parse a clean (non-split-times) SportSoftware relay result table:

        Pl  Stnr  Staffel               Zeit    Zuschlag +
            Name                        EPl     Zeit    WPl   W Zeit
        1   und Österreichische Meister:in
            82    OC Fürstenfeld OCFF1  37:33
            Veitsberger Mateo           4       12:34   4     12:34
            Eibel-Lenane Tara           4       13:48   2     26:22
            Schmalhardt Matthias        1       11:11         37:33
        2   88    SU Klagenfurt 1       38:32
            ...

    A team row starts with a rank (digit) in the first cell; member rows below
    it have a blank first cell, the runner's name in the second, and their own
    leg time in the fourth (constant across the 'Jg'/'Zeit' and
    'EPl'/'Zeit'/'WPl'/'W Zeit' member-column variants seen). The champion is
    sometimes announced on its own annotation row ('1  und ... Meister:in'),
    which steals the rank from the real team row that follows with a blank
    first cell — detected and re-attached here rather than left for the
    generic per-category rank-1 fallback (which assumes individual, not team,
    times)."""
    ex = TableExtractor()
    ex.feed(html_text)

    categories = []
    current = None
    pending_team = None
    pending_rank = None
    pending_championship = None  # ÖM/ÖSTM classified from the champion
                     # annotation that stole pending_rank, carried onto the
                     # team it precedes and from there onto every member
    staffel_idx = 2  # column holding the team name; some layouts omit 'Stnr',
                     # shifting it from index 2 to 1 - detected from the header
    has_stnr = True  # whether this layout has a Stnr column; lets a
                     # DNF team row (blank rank, no champion annotation to
                     # steal it) be told apart from a member row, since Stnr
                     # is always numeric and a member's name never is
    team_row_len = member_row_len = None  # observed cell counts, captured from
                     # the first ranked team/member rows - a second signal for
                     # telling a rankless team row from a member row when
                     # there's no Stnr column to check (row length differs
                     # between the two even though both start with a blank cell)

    def flush():
        nonlocal pending_team
        if not pending_team or not pending_team["members"]:
            pending_team = None
            return
        names = [m["name"] for m in pending_team["members"]]
        for i, m in enumerate(pending_team["members"]):
            seconds = parse_time_loose(m["timeText"])
            status = "ok" if seconds is not None else (parse_status(m["timeText"]) or "unknown")
            # dedupe: a small team running multiple legs each (e.g. a 2-person
            # relay run twice) repeats the same teammate's name once per leg
            mates = list(dict.fromkeys(n for n in names if n != m["name"]))
            note_bits = [f"Staffel: {pending_team['name']}", f"Leg {i + 1}/{len(names)}"]
            if mates:
                note_bits.append("Team: " + ", ".join(mates))
            result = {"name": m["name"], "club": pending_team["name"],
                      "timeText": m["timeText"], "resultKind": "relay",
                      "note": " · ".join(note_bits), "status": status}
            if pending_team["rank"] is not None:
                result["rank"] = pending_team["rank"]
            if pending_team.get("championship"):
                result["championship"] = pending_team["championship"]
            if seconds is not None:
                result["timeS"] = seconds
            current["results"].append(result)
        pending_team = None

    for table in ex.tables:
        for row in table:
            if not row or all(c in ("", "&nbsp") for c in row):
                continue
            first = row[0].strip()
            m = CAT_LINE_RE.match(first)
            if m:
                flush()
                pending_rank = None
                pending_championship = None
                staffel_idx = 2
                has_stnr = True
                team_row_len = member_row_len = None
                current = {"name": m.group("name").strip(),
                           "declaredStarters": int(m.group("starters")), "results": []}
                current.update(parse_course_info(" ".join(row[1:])))
                categories.append(current)
                continue
            if current is None:
                continue
            if first == "Pl":
                if "Staffel" in row:
                    staffel_idx = row.index("Staffel")
                has_stnr = "Stnr" in row
                continue  # outer header row
            if not first and len(row) > 1 and row[1].strip() == "Name":
                continue  # inner (member) header row

            # champion annotation, split across cells - either the plain-digit
            # form ('1' | 'und ... Meister:in'), the trailing-period form
            # ('1.' | 'und Österreichische Staatsmeister', confirmed real:
            # event 4480 - a colspan on the second cell keeps the "1." rank
            # entirely on its own, so it never has trailing text for the
            # annot_m regex below to anchor on), or as one cell together
            # ('1. und österr. Staatsmeister 2016')
            first_rank_digit = first.rstrip(".").isdigit()
            annot_m = re.match(r"^(\d+)\.?\s", first) if not first_rank_digit else None
            joined = " ".join(row[1:]) if first_rank_digit else " ".join(row)
            if (first_rank_digit or annot_m) and ANNOT_RANK_RE.search(joined) \
                    and not TIME_TOKEN_RE.search(joined):
                flush()
                pending_rank = int(first.rstrip(".")) if first_rank_digit else int(annot_m.group(1))
                pending_championship = classify_championship_text(joined)
                continue

            # A non-finishing team ('Fehlst' as its total time) gets no rank at
            # all and no champion annotation precedes it, so a blank first cell
            # is ambiguous between "new (DNF) team" and "member of the current
            # team" - the two signals below tell them apart even without a
            # rank: a Stnr column is always numeric (unlike a member's name),
            # and otherwise the observed row length for team vs. member rows
            # (captured from the first unambiguous instance of each) usually
            # differs even though both layouts start with a blank cell.
            confident_rank = first.isdigit() or pending_rank is not None
            stnr_marks_team = has_stnr and len(row) > 1 and row[1].strip().isdigit()
            len_marks_team = (not has_stnr and team_row_len and member_row_len
                              and team_row_len != member_row_len and len(row) == team_row_len)

            if confident_rank or stnr_marks_team or len_marks_team:
                flush()
                if first.isdigit():
                    rank_val = int(first)
                elif pending_rank is not None:
                    rank_val = pending_rank
                else:
                    rank_val = None  # genuinely rankless (DNF) team
                championship_val = pending_championship
                pending_rank = None
                pending_championship = None
                if confident_rank and not has_stnr:
                    team_row_len = team_row_len or len(row)
                idx = staffel_idx if len(row) > staffel_idx else (len(row) - 1)
                team_name = row[idx].strip() if idx >= 0 and row[idx] else ""
                pending_team = {"rank": rank_val, "name": team_name,
                                 "championship": championship_val, "members": []}
                continue

            if pending_team is None:
                continue
            name = row[1].strip() if len(row) > 1 else ""
            if is_junk_name(name):
                continue
            member_row_len = member_row_len or len(row)
            # own leg time is the first time-like (or status) cell after Name -
            # column count varies (Name+Zeit only; Name+Jg+Zeit; Name+EPl+Zeit+
            # WPl+W Zeit, where the own time must not be confused with the
            # cumulative WZeit that follows it)
            leg_time = ""
            for cell in row[2:]:
                cell = cell.strip()
                if cell and (TIME_TOKEN_RE.search(cell) or parse_status(cell)):
                    leg_time = cell
                    break
            pending_team["members"].append({"name": name, "timeText": leg_time})

    flush()
    return [c for c in categories if c["results"]]


def fetch(url, dest):
    if dest.exists():
        return dest.read_bytes()
    safe_url = urllib.parse.quote(url, safe=":/?&=%")
    data = urllib.request.urlopen(
        urllib.request.Request(safe_url, headers=HEADERS), timeout=30).read()
    dest.write_bytes(data)
    time.sleep(0.15)
    return data


CHARSET_UTF8_RE = re.compile(r'charset=["\']?utf-8', re.I)


def decode(data):
    head = data[:600].decode("ascii", "ignore").lower()
    if CHARSET_UTF8_RE.search(head):
        return data.decode("utf-8", "replace")
    return data.decode("windows-1252", "replace")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="only process N files (0 = all)")
    args = ap.parse_args()

    FILES.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)
    attachments = json.loads((RAW / "attachments.json").read_text())

    jobs = []
    for eid, files in attachments.items():
        sole_attachment = len(files or []) == 1
        for n, f in enumerate(files or []):
            if f["mimeType"] == "text/html":
                jobs.append((int(eid), n, f, sole_attachment))
    if args.limit:
        jobs = jobs[: args.limit]
    print(f"html files to parse: {len(jobs)}")

    ok = empty = failed = 0
    for eid, n, f, sole_attachment in jobs:
        if (eid, f["fileName"]) in MANUAL_ATTACHMENT_SKIP:
            empty += 1
            continue
        out_path = OUT / f"{eid}-{n}.json"
        try:
            data = fetch(f["url"], FILES / f"{eid}-{n}.html")
            text = decode(data)
            list_type = detect_list_type(f["fileName"], text, sole_attachment)
            if list_type == "overall":
                empty += 1  # split-times / cumulative-standings report: redundant, skip
                out_path.unlink(missing_ok=True)  # stale output from an earlier, buggier run
                continue
            if list_type == "relay":
                cats = parse_relay_document(text)
            else:
                cats = parse_document(text)
                if not cats and "<pre" in text.lower():
                    # some SportSoftware HTML wraps a fixed-width report in <pre>
                    # instead of a table (e.g. team/Mannschaft lists) — parse it
                    # with the fixed-width text logic
                    from parse_sportsoftware_text import extract_pre_blocks, parse_text
                    cats = parse_text(extract_pre_blocks(text))
                if not cats:
                    cats = parse_bracket_html(text)
            skip_cats = MANUAL_CATEGORY_SKIP.get((eid, f["fileName"]))
            if skip_cats:
                cats = [c for c in cats if c["name"] not in skip_cats]
            if not cats:
                empty += 1
                out_path.unlink(missing_ok=True)  # stale output from an earlier, buggier run
                continue
            out_path.write_text(json.dumps({
                "eventId": eid,
                "source": "sportsoftware-html",
                "sourceUrl": f["url"],
                "fileName": f["fileName"],
                "listType": list_type,
                "docDate": MANUAL_DOC_DATE_OVERRIDES.get((eid, f["fileName"]))
                           or guess_doc_date(f["fileName"], text),
                "docTitle": extract_html_title(text),
                "categories": cats,
            }, ensure_ascii=False))
            ok += 1
        except Exception as e:
            failed += 1
            print(f"  FAIL {eid}-{n} {f['fileName']}: {e}", file=sys.stderr)
    print(f"parsed: {ok}, empty: {empty}, failed: {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
