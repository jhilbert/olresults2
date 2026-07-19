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
from collections import defaultdict
import html as html_mod
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

from sportsoftware_common import (
    CAT_LINE_RE, COLUMN_ALIASES, COURSE_RE, MANUAL_ATTACHMENT_SKIP, MANUAL_CATEGORY_SKIP,
    MANUAL_DOC_DATE_OVERRIDES,
    TIME_TOKEN_RE, category_starter_count, classify_championship_text, detect_list_type,
    expand_pair_result, extract_html_title,
    aggregate_team_status, guess_doc_date, is_junk_name, is_ooc_status, is_ooc_time,
    parse_champion_annotation, parse_course_info, parse_status, parse_time,
    parse_time_loose, number_team_results, team_results_from_pairs,
)
from sync_selection import select_jobs

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
        self._row_buf = None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._flush_row()
            self._rows = []
            self.tables.append(self._rows)
        elif tag == "tr" and self._rows is not None:
            self._flush_row()
            self._cells = []
            self._row_buf = []
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
        elif self._row_buf is not None:
            self._row_buf.append(data)

    def _flush_cell(self):
        if self._buf is not None and self._cells is not None:
            text = re.sub(r"\s+", " ", "".join(self._buf)).strip().strip("\xa0").strip()
            self._cells.append(text)
        self._buf = None

    def _flush_row(self):
        self._flush_cell()
        if self._cells is not None and self._rows is not None:
            # Some old OE exports put the championship announcement directly
            # inside <tr><b>...</b></tr>, without any td/th at all. Preserve
            # that malformed but meaningful row so rank 1 can be carried to
            # the winner row that follows with a blank placement cell.
            row_text = re.sub(
                r"\s+", " ", "".join(self._row_buf or [])).strip().strip("\xa0").strip()
            if not self._cells and row_text:
                self._cells.append(row_text)
            self._rows.append(self._cells)
        self._cells = None
        self._row_buf = None


def parse_document(html_text):
    ex = TableExtractor()
    ex.feed(html_text)

    categories = []
    current = None
    columns = None
    pending_rank = pending_championship = None
    team_counts = defaultdict(int)
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
                    "sourceCategory": first.strip(),
                    "declaredStarters": category_starter_count(m),
                    "results": [],
                }
                current.update(parse_course_info(" ".join(row[1:])))
                categories.append(current)
                columns = None
                pending_rank = pending_championship = None
                team_counts = defaultdict(int)
                continue
            if first == "Pl" or (current and "Name" in row):
                # OE's English export uses Time/Club/YB/Stno while the
                # parser's canonical field names are German.  Keeping the
                # raw labels made every ranked row look time-less and also
                # dropped unranked MP/DNF rows completely, because neither
                # rank nor the nominal ``Zeit`` field then existed.
                columns = [COLUMN_ALIASES.get(cell, cell) for cell in row]
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
            if time_text and parse_time_loose(time_text) is None and not parse_status(time_text):
                # Colspan-heavy international exports can shift Country into
                # the nominal Zeit cell on unranked rows. Recover the actual
                # trailing DNS/MP/DSQ value from the row instead of accepting
                # a country (notably "Slovakia") as a time/status.
                time_text = next((cell.strip() for cell in reversed(row)
                                  if parse_time_loose(cell.strip()) is not None
                                  or parse_status(cell.strip())), "")
            rank_ok = rec.get("Pl", "").strip().isdigit()
            club = (rec.get("Verein") or rec.get("Verein/Schule") or "").strip()

            member_values = []
            for header, value in zip(columns, row):
                if re.fullmatch(r"Name(?:\s*\d+)?", (header or "").strip(), re.I):
                    value = re.sub(r"\s+", " ", (value or "").replace(",", " ")).strip()
                    if value and not is_junk_name(value):
                        member_values.append(value)
            family_row = "famil" in current["name"].casefold()
            individual_member_layout = "einzel" in current["name"].casefold()

            if family_row and member_values and (rank_ok or time_text):
                # The same Name-1/2/3 report layout is also used for Family,
                # but these arbitrary combinations must not create person
                # identities. Keep exactly one result unit and its displayed
                # source names; build_db intentionally leaves it personless.
                family = {
                    "name": " + ".join(member_values),
                    "club": club,
                    "timeText": time_text,
                    "resultKind": "family",
                }
                rank_text = rec.get("Pl", "").strip()
                if rank_text.isdigit():
                    family["rank"] = int(rank_text)
                if is_ooc_status(rank_text) or is_ooc_time(time_text):
                    family["outOfCompetition"] = True
                seconds = parse_time_loose(time_text)
                if seconds is not None:
                    family.update({"timeS": seconds, "status": "ok"})
                else:
                    family["status"] = parse_status(time_text) or "unknown"
                current["results"].append(family)
                continue

            # team (Mannschaft) tables: members across several columns
            # (Name 1/2/3, Name Läufer2 Läufer3, or repeated 'Name' headers)
            if (rank_ok or time_text) and not individual_member_layout:
                team = team_results_from_pairs(list(zip(columns, row)),
                                               club, rec.get("Pl", ""), time_text)
                if team is not None:
                    team_counts[club] += 1
                    numbered = number_team_results(team, club, team_counts[club])
                    if pending_rank is not None and not any(
                            result.get("rank") is not None for result in numbered):
                        for result in numbered:
                            result["rank"] = pending_rank
                            if pending_championship:
                                result["championship"] = pending_championship
                    pending_rank = pending_championship = None
                    current["results"].extend(numbered)
                    continue

            name = rec.get("Name", "").strip()
            if not name and individual_member_layout and member_values:
                name = member_values[0]
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
            if individual_member_layout:
                # Explicitly defeat the legacy DB fallback which otherwise
                # guesses any 3-token name inside a team event is a surname-
                # only Mannschaft roster (e.g. Tobler-Egger Gabriele).
                result["resultKind"] = "individual"
            rank_text = rec.get("Pl", "").strip()
            # SportSoftware writes AK in the rank cell, not the time cell.
            # It remains a perfectly valid result, but must never be counted
            # as a ranked competitor or considered for medals.
            if is_ooc_status(rank_text) or is_ooc_time(time_text):
                result["outOfCompetition"] = True
            # In some OE12 exports the winner's championship label is
            # appended directly to her name.  Keep the actual person and the
            # championship signal separately; otherwise the name validator in
            # build_db quite correctly rejects the title phrase as non-person
            # text and silently turns 53 source entries into 52 database rows.
            suffix = re.search(r"\s*(\([^)]*(?:meister|champion)[^)]*\))\s*$", name, re.I)
            if suffix:
                championship = classify_championship_text(suffix.group(1))
                if championship:
                    result["name"] = name[:suffix.start()].strip()
                    result["championship"] = championship
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
            explicit_status = next(
                (parse_status(cell) for cell in reversed(row)
                 if parse_status(cell) in {"dnf", "dns", "dsq", "mp"}),
                None,
            )
            if seconds is not None:
                result["timeS"] = seconds
                result["status"] = explicit_status or "ok"
            else:
                result["status"] = explicit_status or parse_status(time_text) or "unknown"
            yob = (rec.get("Jg") or "").strip()
            if yob.isdigit():
                y = int(yob)
                result["yearOfBirth"] = y + (2000 if y <= 26 else 1900) if y < 100 else y
            if rec.get("Pkt"):
                result["scoreText"] = rec["Pkt"].strip()
            current["results"].extend(expand_pair_result(result, current.get("name")))

    parsed = [c for c in categories if c["results"]]
    for category in parsed:
        results = category["results"]
        if not results or any(
                (result.get("resultKind") or "individual") not in {"individual", "family"}
                for result in results):
            continue
        ranks = [result["rank"] for result in results if result.get("rank") is not None]
        # Some classic OE HTMLs put only the highest classified placement in
        # ``(N)`` and print MP/DNF rows below it.  When N exactly equals that
        # highest rank, the visible result-unit count is unambiguous.
        if (ranks and category.get("declaredStarters") == max(ranks)
                and len(results) > category["declaredStarters"]):
            category["declaredStarters"] = len(results)
    return parsed


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
            out_of_competition = False
            if cells[0].rstrip(".").isdigit():
                rank = int(cells[0].rstrip("."))
                cells = cells[1:]
            elif is_ooc_status(cells[0]):
                # Saved liveresultat/SportSoftware snapshots also use AK in
                # the placement cell. It is a placement classification, not
                # the runner's name, so consume the cell and retain OOC as an
                # independent flag alongside the actual time/status.
                out_of_competition = True
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
            value_in_club_column = (
                parse_status(club) is not None
                or parse_time_loose(club) is not None)
            if value_in_club_column:
                # Club-less MeOS result tables collapse an unranked status or
                # a ranked time into the second remaining cell (the nominal
                # club slot). This also happens when later cells still hold
                # ``Behind``/``Time lost`` values: the first time is the real
                # finish, never a time-shaped club name (events 2440, 2633,
                # 5038 and seven more saved bracket exports).
                # DNS rows are printed below the ``(started / entered)``
                # count and therefore remain visible but are not part of the
                # declared competitor count.
                values = [club]
                club = ""
            # An AK result is written as ``(39:58)``.  A strict token-only
            # scan skipped it and then selected the later +24:00 difference
            # as the runner's time.  parse_time_loose accepts both normal and
            # bracketed elapsed times, so take the first actual time column.
            time_text = next((v for v in values if parse_time_loose(v) is not None), None)
            status_text = None if time_text else next((v for v in values if parse_status(v)), None)
            result = {"name": name, "club": club, "timeText": time_text or status_text or ""}
            if rank is not None:
                result["rank"] = rank
            if championship:
                result["championship"] = championship
            seconds = parse_time_loose(time_text) if time_text else None
            if seconds is not None:
                result["timeS"] = seconds
                result["status"] = "ok"
            else:
                result["status"] = parse_status(status_text or "") or "unknown"
            if value_in_club_column and result["status"] == "dns":
                result["excludedFromDeclaredCount"] = True
            if out_of_competition or is_ooc_status(status_text) or is_ooc_time(time_text):
                result["outOfCompetition"] = True
            current["results"].append(result)
    parsed = [c for c in categories if c["results"]]
    for category in parsed:
        declared = category.get("declaredStarters")
        results = category["results"]
        # MeOS has used both count conventions over time.  Some documents'
        # header includes printed DNS rows, others name only runners who
        # started and append DNS registrations below.  Infer the latter only
        # when the source number exactly equals the non-DNS rows; this keeps
        # every DNS visible without creating a false parser mismatch.
        if (declared is not None and len(results) > declared
                and sum(r.get("status") != "dns" for r in results) == declared):
            for result in results:
                if result.get("status") == "dns":
                    result["excludedFromDeclaredCount"] = True
    return parsed


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
    sprint_relay_mode = False  # English OS2010 layout: member rows start with
                     # an explicit numeric Leg instead of an empty Pl cell

    def flush():
        nonlocal pending_team
        if pending_team and current is not None:
            current["sourceUnitCount"] = current.get("sourceUnitCount", 0) + 1
        if not pending_team or not pending_team["members"]:
            pending_team = None
            return
        names = [m["name"] for m in pending_team["members"]]
        member_statuses = []
        for m in pending_team["members"]:
            seconds = parse_time_loose(m["timeText"])
            member_statuses.append(
                "ok" if seconds is not None else (parse_status(m["timeText"]) or "unknown"))
        team_status = aggregate_team_status(pending_team.get("status"), member_statuses)
        for i, m in enumerate(pending_team["members"]):
            seconds = parse_time_loose(m["timeText"])
            individual_status = member_statuses[i]
            # dedupe: a small team running multiple legs each (e.g. a 2-person
            # relay run twice) repeats the same teammate's name once per leg
            mates = list(dict.fromkeys(n for n in names if n != m["name"]))
            note_bits = [f"Staffel: {pending_team['name']}", f"Leg {i + 1}/{len(names)}"]
            if mates:
                note_bits.append("Team: " + ", ".join(mates))
            result = {"name": m["name"], "club": pending_team["name"],
                      "timeText": m["timeText"], "resultKind": "relay",
                      "note": " · ".join(note_bits), "status": team_status,
                      "individualStatus": individual_status,
                      "teamStatus": team_status,
                      "teamNumber": pending_team.get("number"),
                      "teamName": pending_team["name"],
                      "leg": i + 1, "legCount": len(names),
                      "teamTimeText": pending_team.get("timeText") or ""}
            if pending_team.get("timeS") is not None:
                result["teamTimeS"] = pending_team["timeS"]
            if pending_team.get("outOfCompetition"):
                result["outOfCompetition"] = True
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
                sprint_relay_mode = False
                current = {"name": m.group("name").strip(),
                           "declaredStarters": category_starter_count(m), "results": []}
                current.update(parse_course_info(" ".join(row[1:])))
                categories.append(current)
                continue
            if current is None:
                continue
            if first == "Pl":
                if "Staffel" in row:
                    staffel_idx = row.index("Staffel")
                elif "Team" in row:
                    staffel_idx = row.index("Team")
                    sprint_relay_mode = True
                has_stnr = "Stnr" in row or "Stno" in row
                continue  # outer header row
            if ((not first or first == "Leg") and len(row) > 1
                    and row[1].strip() == "Name"):
                if first == "Leg":
                    sprint_relay_mode = True
                continue  # inner (member) header row

            # champion annotation, split across cells - either the plain-digit
            # form ('1' | 'und ... Meister:in'), the trailing-period form
            # ('1.' | 'und Österreichische Staatsmeister', confirmed real:
            # event 4480 - a colspan on the second cell keeps the "1." rank
            # entirely on its own, so it never has trailing text for the
            # annot_m regex below to anchor on), or as one cell together
            # ('1. und österr. Staatsmeister 2016')
            annotation_text = " ".join(
                cell for cell in row if cell and cell != "&nbsp").strip()
            common_rank, common_championship = parse_champion_annotation(
                annotation_text)
            if common_rank is not None:
                flush()
                pending_rank = common_rank
                pending_championship = common_championship
                continue
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
            sprint_member = (sprint_relay_mode and first.isdigit()
                             and len(row) > 1 and not row[1].strip().isdigit())
            confident_rank = ((first.isdigit() and not sprint_member)
                              or pending_rank is not None)
            stnr_marks_team = has_stnr and len(row) > 1 and row[1].strip().isdigit()
            len_marks_team = (not has_stnr and team_row_len and member_row_len
                              and team_row_len != member_row_len and len(row) == team_row_len)

            if confident_rank or stnr_marks_team or len_marks_team:
                flush()
                if first.isdigit() and not sprint_member:
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
                team_number = row[1].strip() if has_stnr and len(row) > 1 else ""
                team_time_text = ""
                for cell in row[idx + 1:]:
                    cell = cell.strip()
                    if cell and (TIME_TOKEN_RE.search(cell) or parse_status(cell)):
                        team_time_text = cell
                        break
                team_time_s = parse_time_loose(team_time_text)
                if (rank_val is None and team_time_s is not None
                        and not current["results"]):
                    # A few nested-table exports drop the standalone champion
                    # annotation from the parser's table stream entirely. The
                    # first finishing team is nevertheless unambiguously rank 1.
                    rank_val = 1
                team_status = ("ok" if team_time_s is not None else
                               (parse_status(team_time_text) or "unknown"))
                pending_team = {"rank": rank_val, "name": team_name,
                                 "number": team_number or None,
                                 "timeText": team_time_text, "timeS": team_time_s,
                                 "status": team_status,
                                 "outOfCompetition": is_ooc_status(first),
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


def fetch(url, dest, force=False):
    if dest.exists() and not force:
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
    ap.add_argument("--event-id", type=int, help="only process one ANNE event")
    ap.add_argument("--cached", action="store_true",
                    help="reparse only already downloaded HTML files; never fetch")
    ap.add_argument("--attachment-manifest", type=Path,
                    help="only process attachments listed by the current incremental sync")
    ap.add_argument("--force-download", action="store_true",
                    help="re-download selected source files even when cached")
    args = ap.parse_args()
    if args.cached and args.force_download:
        ap.error("--cached and --force-download are mutually exclusive")

    FILES.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)
    attachments = json.loads((RAW / "attachments.json").read_text())

    jobs = []
    for eid, files in attachments.items():
        sole_attachment = len(files or []) == 1
        for n, f in enumerate(files or []):
            if f["mimeType"] == "text/html":
                jobs.append((int(eid), n, f, sole_attachment))
    jobs = select_jobs(jobs, args.event_id, args.attachment_manifest)
    if args.limit:
        jobs = jobs[: args.limit]
    print(f"html files to parse: {len(jobs)}")

    ok = empty = failed = 0
    for eid, n, f, sole_attachment in jobs:
        if (eid, f["fileName"]) in MANUAL_ATTACHMENT_SKIP:
            empty += 1
            continue
        out_path = OUT / f"{eid}-{n}.json"
        html_path = FILES / f"{eid}-{n}.html"
        try:
            if args.cached and not html_path.exists():
                empty += 1
                continue
            data = fetch(f["url"], html_path, args.force_download)
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
                if (not cats and re.search(r"<font\b", text, re.I)
                        and re.search(r"\bPl\s+.*\bName\s+.*\bVerein", text, re.I | re.S)):
                    # Older OE score exports are fixed-width reports wrapped
                    # only in inline <font>/<i>/<b> markup (no table and no
                    # <pre>).  Keep the physical source lines and remove just
                    # the formatting tags before using the text parser.  This
                    # also preserves explicit rank-cell AK markers which the
                    # historic cached JSON had lost.
                    from parse_sportsoftware_text import parse_text
                    fixed_text = html_mod.unescape(re.sub(r"<[^>]+>", "", text))
                    cats = parse_text(fixed_text)
                if not cats:
                    cats = parse_bracket_html(text)
            skip_cats = MANUAL_CATEGORY_SKIP.get((eid, f["fileName"]))
            if skip_cats:
                cats = [c for c in cats if c["name"] not in skip_cats]
            if not cats:
                empty += 1
                out_path.unlink(missing_ok=True)  # stale output from an earlier, buggier run
                continue
            if re.search(r"\bOEScore(?:\d{4})?\b|\bSCORE[- ]?OL\b", text, re.I):
                for category in cats:
                    for result in category.get("results") or []:
                        result["rankingBasis"] = "score"
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
