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
import ssl
import sys
import time
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

import certifi

from sportsoftware_common import (
    CAT_LINE_RE, COLUMN_ALIASES, COURSE_RE, MANUAL_ATTACHMENT_INDEX_SKIP,
    MANUAL_ATTACHMENT_SKIP, MANUAL_CATEGORY_SKIP,
    MANUAL_DOC_DATE_OVERRIDES,
    STATUS_TAIL_RE, TIME_TOKEN_RE, category_starter_count, classify_championship_text,
    detect_list_type,
    expand_pair_result, extract_html_title,
    aggregate_team_status, guess_doc_date, is_junk_name, is_ooc_status, is_ooc_time,
    parse_champion_annotation, parse_course_info, parse_status, parse_time,
    parse_time_loose, number_team_results, repair_official_club_status_overflow,
    team_results_from_pairs, is_auxiliary_attachment_name,
)
from sync_selection import select_jobs

ANNOT_RANK_RE = re.compile(r"(?i)meister|sieger")

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw" / "anne"
FILES = RAW / "files"
SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
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
            oevent_category = re.match(
                r"^(?P<name>.+?)\s+\(\d+(?:[.,]\d+)?\s*(?:m|km)\s*,", first, re.I)
            if oevent_category and len(row) == 1:
                # OEvent prints one global header followed by one-cell course
                # boundaries. Preserve that header across categories.
                current = {
                    "name": oevent_category.group("name").strip(),
                    "sourceCategory": first.strip(),
                    "declaredStarters": None, "results": [],
                }
                current.update(parse_course_info(first))
                categories.append(current)
                pending_rank = pending_championship = None
                team_counts = defaultdict(int)
                continue
            # Some compact/custom HTML exports omit ``(starter count)`` but
            # keep an unambiguous course row (``Damen A | 5,0 km | 19
            # Posten`` or ``Mittel | 1,7 km | 11 Posten``). It is a category
            # boundary, not a title, only when distance and controls occur in
            # the same short table row.
            joined_header = " ".join(row)
            if (len(row) >= 2 and first and not first.isdigit()
                    and re.search(r"\d+(?:[.,]\d+)?\s*km\b", joined_header, re.I)
                    and re.search(r"\d+\s*(?:P|Posten)\b", joined_header, re.I)):
                current = {
                    "name": first.strip(), "sourceCategory": first.strip(),
                    "declaredStarters": None, "results": [],
                }
                current.update(parse_course_info(" ".join(row[1:])))
                categories.append(current)
                columns = None
                pending_rank = pending_championship = None
                team_counts = defaultdict(int)
                continue
            aliased_columns = [COLUMN_ALIASES.get(cell, cell) for cell in row]
            if aliased_columns[0] == "Pl" or (current and "Name" in aliased_columns):
                # OE's English export uses Time/Club/YB/Stno while the
                # parser's canonical field names are German.  Keeping the
                # raw labels made every ranked row look time-less and also
                # dropped unranked MP/DNF rows completely, because neither
                # rank nor the nominal ``Zeit`` field then existed.
                columns = aliased_columns
                continue
            if (current is not None and columns is None and len(row) >= 5
                    and (row[0].strip().isdigit() or row[0].strip() in {"-", "AK"})
                    and (parse_time_loose(row[4].split()[0].strip("()")) is not None
                         or parse_status(row[4]))):
                # Small club-generated tables often have no explicit column
                # header but consistently use Pl | SI | Name | Verein | Zeit.
                columns = ["Pl", "Stnr", "Name", "Verein", "Zeit"]
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
            # Several classic OE exports print the winner's placement in a
            # separate championship-announcement row.  The following winner
            # row then physically omits the ``Pl`` cell instead of leaving it
            # blank, so every value would otherwise shift one column left
            # (bib becomes Pl, name becomes bib, year becomes name) and the
            # real winner is discarded as a numeric "name".  Restore the
            # missing leading cell before aligning the row.  A trailing empty
            # header cell is deliberately ignored in the size comparison.
            populated_column_count = sum(bool(column) for column in columns)
            if (pending_rank is not None and columns and columns[0] == "Pl"
                    and len(row) == populated_column_count - 1):
                row = [""] + row
            rec = dict(zip([c or f"col{i}" for i, c in enumerate(columns)], row))
            if rec.get("Pl"):
                rec["Pl"] = rec["Pl"].strip().rstrip(".")
            time_text = (rec.get("Zeit") or rec.get("Gesamt") or "").strip()
            if parse_time_loose(time_text) is None and not parse_status(time_text):
                # Colspan-heavy international exports can shift Country into
                # the nominal Zeit cell on unranked rows. Older OE tables can
                # also leave that cell empty and put N Ang/Disqu into a later
                # column. Recover the actual trailing value in both cases.
                recovered = next((cell.strip() for cell in reversed(row)
                                  if parse_time_loose(cell.strip()) is not None
                                  or parse_status(cell.strip())), "")
                if recovered:
                    time_text = recovered
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
            team_member_columns = sum(
                bool(re.fullmatch(r"Name(?:\s*\d+)?", (header or "").strip(), re.I))
                for header in columns
            )

            if family_row and member_values:
                # The same Name-1/2/3 report layout is also used for Family,
                # but these arbitrary combinations must not create person
                # identities. Keep exactly one result unit and its displayed
                # source names; build_db intentionally leaves it personless.
                # A named row with a blank result cell is still present in
                # the source and must count toward completeness.
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
                if team_member_columns >= 2:
                    # Count the physical team row even when its only member
                    # text is a non-person placeholder (``Ben, und andere``)
                    # or all member cells are blank. Such a row is one source
                    # start, but must not fabricate a person identity.
                    current["sourceUnitCount"] = current.get("sourceUnitCount", 0) + 1
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
                if team_member_columns >= 2:
                    # Team-only rows can contain no usable people at all
                    # (blank Name columns or placeholders such as ``Ben, und
                    # andere``). The team/rank/time is nevertheless a real
                    # result unit and must stay visible without creating a
                    # fake person from the placeholder text.
                    team_counts[club] += 1
                    team_name = f"{club} {team_counts[club]}" if club else (
                        f"Mannschaft {team_counts[club]}")
                    rank_text = rec.get("Pl", "").strip()
                    seconds = parse_time_loose(time_text)
                    status = "ok" if seconds is not None else (
                        parse_status(time_text) or "unknown")
                    result = {
                        "name": "", "club": club, "timeText": time_text,
                        "resultKind": "team", "memberlessTeam": True,
                        "note": f"Mannschaft: {team_name} · keine vollständigen Teilnehmernamen in der Quelle",
                        "status": status, "individualStatus": None,
                        "teamStatus": status,
                        "teamNumber": (rec.get("Stnr") or rec.get("Stno") or "").strip() or None,
                        "teamName": team_name, "teamTimeText": time_text,
                    }
                    if rank_text.isdigit():
                        result["rank"] = int(rank_text)
                    if seconds is not None:
                        result["timeS"] = seconds
                        result["teamTimeS"] = seconds
                    if is_ooc_status(rank_text) or is_ooc_time(time_text):
                        result["outOfCompetition"] = True
                    current["results"].append(result)
                    continue

            name = rec.get("Name", "").strip()
            if not name and (rec.get("Nachname") or rec.get("Vorname")):
                name = f"{rec.get('Nachname') or ''} {rec.get('Vorname') or ''}".strip()
            if not name and individual_member_layout and member_values:
                name = member_values[0]
            if is_junk_name(name):
                continue
            # A valid Name cell in an active result table is itself enough to
            # preserve the source row. Some exports explicitly list a runner
            # while leaving rank/time/status blank. Keeping it as unknown is
            # preferable to turning a visible source entry into a false
            # completeness gap; the audit model reports the blank value.
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
            repair_official_club_status_overflow(result)
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


def parse_simple_global_results(html_text):
    """Parse tiny category-per-table exports without table-bound headings.

    WOLV's Kids-Cup export prints ``<b>Winter (10)</b>`` outside the result
    table. The number is the control count, not a starter count. Keep this
    adapter narrow: every accepted table needs Pos/Name/Zeit/Posten columns.
    """
    categories = []
    section_re = re.compile(
        r"<b\b[^>]*>(?P<title>.*?)</b>\s*<br\s*/?>\s*"
        r"(?P<table><table\b.*?</table>)",
        re.I | re.S,
    )
    for section in section_re.finditer(html_text):
        title = re.sub(
            r"\s+", " ",
            html_mod.unescape(re.sub(r"<[^>]+>", "", section.group("title"))),
        ).strip()
        title_match = re.fullmatch(
            r"(?P<name>.+?)\s*\((?P<controls>\d+)\)", title)
        if not title_match:
            continue
        extractor = TableExtractor()
        extractor.feed(section.group("table"))
        if len(extractor.tables) != 1 or not extractor.tables[0]:
            continue
        rows = extractor.tables[0]
        header = [cell.casefold() for cell in rows[0]]
        required = {"pos", "name", "zeit", "posten"}
        if not required.issubset(set(header)):
            continue
        positions = {name: header.index(name) for name in required}
        results = []
        for row in rows[1:]:
            if max(positions.values()) >= len(row):
                continue
            rank_text = row[positions["pos"]].strip().rstrip(".")
            name = row[positions["name"]].strip()
            value = row[positions["zeit"]].strip()
            controls_text = row[positions["posten"]].strip()
            if not rank_text.isdigit() or not name:
                continue
            seconds = parse_time_loose(value)
            result = {
                "name": name,
                "club": "",
                "rank": int(rank_text),
                "timeText": value,
                "rankingBasis": "score",
                "status": ("ok" if seconds is not None
                           else (parse_status(value) or "unknown")),
            }
            if seconds is not None:
                result["timeS"] = seconds
            if name.isdigit():
                # Preserve a card number accidentally entered in the source's
                # Name cell, but never mint a fake runner identity from it.
                result["identityExcluded"] = True
            if controls_text.isdigit():
                result["sourceControls"] = int(controls_text)
                result["scoreText"] = controls_text
            results.append(result)
        if results:
            categories.append({
                "name": title_match.group("name").strip(),
                "sourceCategory": title,
                "declaredStarters": len(results),
                "sourceUnitCount": len(results),
                "courseControls": int(title_match.group("controls")),
                "rankingBasis": "score",
                "results": results,
            })
    return categories


def parse_oe_multistage_html(html_text, stage_specs):
    """Extract stage columns from one OE ``Gesamt-Ergebnis`` HTML export.

    ``stage_specs`` contains logical output stages and source-column indexes.
    Ordinary stages use their own time/rank columns.  A pursuit/final stage
    may set ``overallTimeIndex`` and ``overallRankIndex`` so its published
    finish order and accumulated time remain authoritative while the stage
    split is retained in the note.
    """
    extractor = TableExtractor()
    extractor.feed(html_text)
    stage_documents = []
    stage_categories = [[] for _ in stage_specs]
    category_re = re.compile(
        r"^(?P<name>.+?)\s*\((?P<count>\d+)\)(?:\s+.*)?$")

    tables = extractor.tables
    for index in range(len(tables) - 2):
        category_table, header_table, result_table = (
            tables[index:index + 3])
        if (len(category_table) != 1 or not category_table[0]
                or len(header_table) != 1 or not header_table[0]):
            continue
        match = category_re.fullmatch(category_table[0][0].strip())
        header = header_table[0]
        if not match or "Name" not in header or not any(
                re.fullmatch(r"E\d+", cell.strip()) for cell in header):
            continue
        category_name = match.group("name").strip()
        declared = int(match.group("count"))
        annulled = {
            f"E{number}" for number in re.findall(
                r"Annulliert\s+E(\d+)", category_table[0][0], re.I)
        }
        per_stage = [
            None if spec.get("sourceColumn") in annulled else {
                "name": category_name,
                "sourceCategory": category_table[0][0].strip(),
                "declaredStarters": declared,
                "sourceUnitCount": 0,
                "rankingBasis": "time",
                "_hasObservedStageValue": False,
                "results": [],
            }
            for spec in stage_specs
        ]
        for row in result_table:
            if len(row) < 6:
                continue
            name = row[2].strip() if len(row) > 2 else ""
            club = row[4].strip() if len(row) > 4 else ""
            if not name or not club or is_junk_name(name):
                continue
            for spec_index, spec in enumerate(stage_specs):
                if per_stage[spec_index] is None:
                    continue
                value_index = spec["timeIndex"]
                if value_index >= len(row):
                    continue
                stage_value = row[value_index].strip()
                stage_seconds = parse_time_loose(stage_value)
                stage_status = (
                    "ok" if stage_seconds is not None
                    else parse_status(stage_value)
                )
                inferred_dns = bool(
                    stage_status is None
                    and spec.get("blankStageMeansDns")
                    and not stage_value
                )
                if inferred_dns:
                    stage_status = "dns"
                    stage_value = "DNS"
                elif stage_status is None:
                    continue
                if not inferred_dns:
                    per_stage[spec_index]["_hasObservedStageValue"] = True

                displayed_value = stage_value
                displayed_seconds = stage_seconds
                rank_text = ""
                if spec.get("overallTimeIndex") is not None:
                    overall_index = spec["overallTimeIndex"]
                    overall_value = (
                        row[overall_index].strip()
                        if overall_index < len(row) else ""
                    )
                    overall_seconds = parse_time_loose(overall_value)
                    if stage_status == "ok" and overall_seconds is not None:
                        displayed_value = overall_value
                        displayed_seconds = overall_seconds
                    rank_index = spec.get("overallRankIndex")
                    rank_text = (
                        row[rank_index].strip().rstrip(".")
                        if rank_index is not None and rank_index < len(row)
                        else ""
                    )
                else:
                    rank_index = spec.get("rankIndex")
                    rank_text = (
                        row[rank_index].strip().rstrip(".")
                        if rank_index is not None and rank_index < len(row)
                        else ""
                    )

                result = {
                    "name": name, "club": club,
                    "timeText": displayed_value,
                    "status": stage_status,
                }
                if displayed_seconds is not None:
                    result["timeS"] = displayed_seconds
                if rank_text.isdigit() and stage_status == "ok":
                    result["rank"] = int(rank_text)
                elif is_ooc_status(rank_text):
                    result["outOfCompetition"] = True
                if spec.get("overallTimeIndex") is not None:
                    result["note"] = (
                        f"{spec.get('sourceLabel', 'Etappenzeit')}: "
                        f"{'leer (als DNS)' if inferred_dns else stage_value}"
                    )
                    # A pursuit/overall export can contain a valid final-stage
                    # split but deliberately no overall placement or total
                    # when an earlier stage was MP/DNS.  Preserve the split,
                    # but identify the row as outside the published overall
                    # classification instead of presenting it as a lost rank.
                    if (spec.get("unrankedStageIsOoc")
                            and stage_status == "ok"
                            and displayed_value == stage_value
                            and not rank_text):
                        result["outOfCompetition"] = True
                yob = row[3].strip() if len(row) > 3 else ""
                if yob.isdigit():
                    year = int(yob)
                    result["yearOfBirth"] = (
                        year + (2000 if year <= 26 else 1900)
                        if year < 100 else year
                    )
                per_stage[spec_index]["results"].append(result)

        for spec_index, category in enumerate(per_stage):
            if (category is not None and category["results"]
                    and category.pop("_hasObservedStageValue", False)):
                category["sourceUnitCount"] = len(category["results"])
                stage_categories[spec_index].append(category)

    for spec, categories in zip(stage_specs, stage_categories):
        if not categories:
            continue
        stage_documents.append({
            "stageNumber": spec["stageNumber"],
            "stageDate": spec.get("stageDate"),
            "stageTitle": spec.get("stageTitle"),
            "listType": "race",
            "categories": categories,
        })
    return stage_documents


def _school_excel_duration(value):
    """Interpret Excel's accidental ``minutes:seconds:00`` display."""
    match = re.fullmatch(r"(\d+):([0-5]\d):00", (value or "").strip())
    if not match:
        return None
    return int(match.group(1)) * 60 + int(match.group(2))


def parse_noe_school_team_html(html_text):
    """Parse the 2016 NÖ school Mannschaftswertung Excel-HTML export.

    Only ranked three-person teams are results in this source. The later
    ``Qualifikation für Bundesmeisterschaft`` block is a duplicate subset.
    """
    extractor = TableExtractor()
    extractor.feed(html_text)
    extractor.close()
    if not extractor.tables:
        return []

    categories = []
    current = None
    pending = None
    category_names = {
        "Unterstufe männlich", "Unterstufe weiblich",
        "Oberstufe männlich", "Oberstufe weiblich",
    }

    def flush():
        nonlocal pending
        if current is None or pending is None:
            pending = None
            return
        members = pending["members"]
        if len(members) != 3:
            pending = None
            return
        team_number = f"{current['name']}-{pending['rank']}"
        for index, member in enumerate(members, 1):
            result = {
                "name": member["name"], "club": pending["club"],
                "rank": pending["rank"], "status": "ok",
                "individualStatus": member["status"],
                "timeText": member["timeText"],
                "resultKind": "team", "teamNumber": team_number,
                "teamName": pending["club"],
                "teamTimeText": pending["teamTimeText"],
                "note": f"Mannschaft {pending['club']} · Mitglied {index}/3",
            }
            if member.get("timeS") is not None:
                result["timeS"] = member["timeS"]
            if pending.get("teamTimeS") is not None:
                result["teamTimeS"] = pending["teamTimeS"]
            current["results"].append(result)
        current["sourceUnitCount"] += 1
        pending = None

    for row in extractor.tables[0]:
        cells = [re.sub(r"\s+", " ", cell or "").strip() for cell in row]
        first = cells[0] if cells else ""
        if first.startswith("Qualifikation für Bundesmeisterschaft"):
            flush()
            break
        if first in category_names:
            flush()
            current = {
                "name": first, "declaredStarters": None,
                "sourceUnitCount": 0, "rankingBasis": "time", "results": [],
            }
            categories.append(current)
            continue
        if current is None or first == "Rang":
            continue

        if first.isdigit() and len(cells) >= 6:
            flush()
            club, surname, given, raw_time, team_time = cells[1:6]
            if not all((club, surname, given, raw_time, team_time)):
                continue
            seconds = _school_excel_duration(raw_time)
            status = "ok" if seconds is not None else parse_status(raw_time)
            team_seconds = _school_excel_duration(team_time)
            if status is None or team_seconds is None:
                continue
            pending = {
                "rank": int(first), "club": club,
                "teamTimeText": team_time, "teamTimeS": team_seconds,
                "members": [{
                    "name": f"{surname} {given}", "timeText": raw_time,
                    "timeS": seconds, "status": status,
                }],
            }
            continue

        if pending is not None and len(cells) >= 4:
            club, surname, given, raw_time = cells[:4]
            if not all((club, surname, given, raw_time)):
                continue
            seconds = _school_excel_duration(raw_time)
            status = "ok" if seconds is not None else parse_status(raw_time)
            if status is None:
                continue
            pending["members"].append({
                "name": f"{surname} {given}", "timeText": raw_time,
                "timeS": seconds, "status": status,
            })
            if len(pending["members"]) == 3:
                flush()
    flush()

    for category in categories:
        category["declaredStarters"] = category["sourceUnitCount"]
    return [category for category in categories if category["results"]]


def parse_os2003_relay_pre_html(html_text):
    """Parse OS2003's fixed-width relay report embedded in ``<pre>``."""
    pre_match = re.search(r"<pre\b[^>]*>(.*?)</pre>", html_text, re.I | re.S)
    if not pre_match:
        return []
    fixed = html_mod.unescape(re.sub(r"<[^>]+>", "", pre_match.group(1)))
    categories = []
    current = None
    pending = None
    category_re = re.compile(r"^(?P<name>.+?)\s+\((?P<count>\d+)\)\s*$")

    def result_value(value):
        seconds = parse_time_loose(value)
        return seconds, ("ok" if seconds is not None
                         else (parse_status(value) or "unknown"))

    def flush():
        nonlocal pending
        if current is None or pending is None:
            pending = None
            return
        member_statuses = [member["status"] for member in pending["members"]]
        team_status = aggregate_team_status(pending["declaredStatus"], member_statuses)
        team_number = pending["teamNumber"]
        members = pending["members"]
        if not members:
            current["results"].append({
                "name": "", "club": "", "status": team_status,
                "teamStatus": team_status, "timeText": pending["teamTimeText"],
                "resultKind": "relay", "teamNumber": team_number,
                "teamName": pending["teamName"], "memberlessTeam": True,
                "teamTimeText": pending["teamTimeText"],
            })
        for leg, member in enumerate(members, 1):
            result = {
                "name": member["name"], "club": "",
                "status": team_status, "teamStatus": team_status,
                "individualStatus": member["status"],
                "timeText": member["timeText"],
                "resultKind": "relay", "teamNumber": team_number,
                "teamName": pending["teamName"],
                "teamTimeText": pending["teamTimeText"],
                "leg": leg, "legCount": len(members),
                "note": (
                    f"Staffel {pending['teamName']} · "
                    f"Leg {leg}/{len(members)}"
                ),
            }
            if pending.get("rank") is not None:
                result["rank"] = pending["rank"]
            if pending["outOfCompetition"]:
                result["outOfCompetition"] = True
            if member.get("timeS") is not None:
                result["timeS"] = member["timeS"]
            if pending.get("teamTimeS") is not None:
                result["teamTimeS"] = pending["teamTimeS"]
            current["results"].append(result)
        current["sourceUnitCount"] += 1
        pending = None

    for raw_line in fixed.splitlines():
        line = raw_line.rstrip()
        category_match = category_re.fullmatch(line.strip())
        if category_match and not line.lstrip().startswith(("Pl ", "Name ")):
            flush()
            current = {
                "name": category_match.group("name").strip(),
                "declaredStarters": int(category_match.group("count")),
                "sourceUnitCount": 0, "rankingBasis": "time", "results": [],
            }
            categories.append(current)
            continue
        if current is None or not line.strip():
            continue

        # Fixed columns: Pl[0:6], Stnr[6:13], Staffel[13:50], Zeit[50:].
        team_number = line[6:13].strip() if len(line) >= 13 else ""
        team_name = line[13:50].strip() if len(line) >= 50 else ""
        team_value = line[50:].strip() if len(line) > 50 else ""
        if team_number.isdigit() and team_name and team_value:
            flush()
            rank_text = line[:6].strip()
            team_seconds, declared_status = result_value(team_value)
            pending = {
                "rank": int(rank_text) if rank_text.isdigit() else None,
                "outOfCompetition": rank_text.casefold() == "ak",
                "teamNumber": team_number, "teamName": team_name,
                "teamTimeText": team_value, "teamTimeS": team_seconds,
                "declaredStatus": declared_status, "members": [],
            }
            continue

        if pending is None or len(line) < 48:
            continue
        member_name = line[13:42].strip()
        member_value = line[47:].strip()
        if not member_name or not member_value:
            continue
        seconds, status = result_value(member_value)
        pending["members"].append({
            "name": member_name, "timeText": member_value,
            "timeS": seconds, "status": status,
        })
    flush()
    return [category for category in categories if category["results"]]


def parse_vienna_sprint_relay_html(html_text):
    """Parse Vienna O Challenge's compact team/member relay table."""
    extractor = TableExtractor()
    extractor.feed(html_text)
    extractor.close()
    if not extractor.tables:
        return []
    categories = []
    current = None
    pending = None

    def value_status(value):
        seconds = parse_time_loose(value)
        return seconds, ("ok" if seconds is not None
                         else (parse_status(value) or "unknown"))

    def flush():
        nonlocal pending
        if current is None or pending is None:
            pending = None
            return
        member_statuses = [member["status"] for member in pending["members"]]
        team_status = aggregate_team_status(pending["declaredStatus"], member_statuses)
        members = pending["members"]
        if not members:
            current["results"].append({
                "name": "", "club": "",
                "status": team_status, "teamStatus": team_status,
                "timeText": pending["teamTimeText"],
                "resultKind": "relay", "teamNumber": pending["teamNumber"],
                "teamName": pending["teamName"],
                "teamTimeText": pending["teamTimeText"],
                "memberlessTeam": True,
                "note": f"Staffel {pending['teamName']} · keine Mitgliedsnamen",
            })
        for member in members:
            result = {
                "name": member["name"], "club": "",
                "status": team_status, "teamStatus": team_status,
                "individualStatus": member["status"],
                "timeText": member["timeText"],
                "resultKind": "relay", "teamNumber": pending["teamNumber"],
                "teamName": pending["teamName"],
                "teamTimeText": pending["teamTimeText"],
                "leg": member["leg"], "legCount": len(members),
                "note": (
                    f"Staffel {pending['teamName']} · "
                    f"Leg {member['leg']}/{len(members)}"
                ),
            }
            if pending.get("rank") is not None:
                result["rank"] = pending["rank"]
            if member.get("timeS") is not None:
                result["timeS"] = member["timeS"]
            if pending.get("teamTimeS") is not None:
                result["teamTimeS"] = pending["teamTimeS"]
            current["results"].append(result)
        current["sourceUnitCount"] += 1
        pending = None

    for row in extractor.tables[0]:
        cells = [re.sub(r"\s+", " ", cell or "").strip() for cell in row]
        while cells and not cells[-1]:
            cells.pop()
        if cells and not cells[0]:
            cells = cells[1:]
        if len(cells) >= 3 and cells[-2:] == ["", "Time"]:
            # Defensive path for a differently retained blank cell.
            cells = [cell for cell in cells if cell]
        if len(cells) >= 2 and cells[-1] == "Time":
            flush()
            current = {
                "name": cells[0], "declaredStarters": None,
                "sourceUnitCount": 0, "rankingBasis": "time", "results": [],
            }
            categories.append(current)
            continue
        if current is None or not cells:
            continue

        # Members always have leg, name, leg time and cumulative time.
        expected_leg = (
            len(pending["members"]) + 1 if pending is not None else None)
        if (pending is not None and len(cells) >= 4
                and cells[0] == f"{expected_leg}."
                and expected_leg <= 4):
            leg = int(cells[0].rstrip("."))
            seconds, status = value_status(cells[2])
            pending["members"].append({
                "leg": leg, "name": cells[1], "timeText": cells[2],
                "timeS": seconds, "status": status,
            })
            if leg == 4:
                flush()
            continue

        flush()
        if len(cells) >= 3 and re.fullmatch(r"\d+\.", cells[0]):
            rank = int(cells[0].rstrip("."))
            team_name, team_value = cells[1], cells[2]
        elif len(cells) >= 2:
            rank = None
            team_name, team_value = cells[0], cells[1]
        else:
            continue
        team_seconds, declared_status = value_status(team_value)
        if not team_name or declared_status == "unknown":
            continue
        pending = {
            "rank": rank,
            "teamNumber": f"{current['name']}-{current['sourceUnitCount'] + 1}",
            "teamName": team_name,
            "teamTimeText": team_value, "teamTimeS": team_seconds,
            "declaredStatus": declared_status, "members": [],
        }
    flush()
    for category in categories:
        category["declaredStarters"] = category["sourceUnitCount"]
    return [category for category in categories if category["results"]]


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
            # ``parse_status`` deliberately recognizes status tokens inside
            # decorated report text, so it also sees the standalone word OK
            # in perfectly real club names such as ``OK Jihlava``, ``Rold
            # Skov OK`` and ``OK Älgen``.  Only a complete status cell may
            # prove that the nominal club column actually contains the result
            # value.  Otherwise dozens of international rows lose both their
            # club and the real elapsed time that follows in a later column.
            value_in_club_column = (
                parse_time_loose(club) is not None
                or (parse_status(club) is not None
                    and STATUS_TAIL_RE.fullmatch(club) is not None))
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
        if not pending_team:
            pending_team = None
            return
        if not pending_team["members"]:
            # A relay entry can be printed as only one team header, most
            # commonly ``74 HSV OL Wiener Neustadt 3 N Ang``.  It is still a
            # real result-list unit even though the source supplies no people
            # to attach to it.  Keep a deliberately personless team row so it
            # remains visible without inventing a runner or a leg.
            team_status = pending_team.get("status") or "unknown"
            if team_status != "unknown" or pending_team.get("rank") is not None:
                result = {
                    "name": "", "club": pending_team["name"],
                    "timeText": pending_team.get("timeText") or "",
                    "resultKind": "relay", "memberlessTeam": True,
                    "note": f"Staffel: {pending_team['name']} · keine Teilnehmernamen in der Quelle",
                    "status": team_status, "individualStatus": None,
                    "teamStatus": team_status,
                    "teamNumber": pending_team.get("number"),
                    "teamName": pending_team["name"],
                    "teamTimeText": pending_team.get("timeText") or "",
                }
                if pending_team.get("timeS") is not None:
                    result["teamTimeS"] = pending_team["timeS"]
                if pending_team.get("outOfCompetition"):
                    result["outOfCompetition"] = True
                if pending_team.get("rank") is not None:
                    result["rank"] = pending_team["rank"]
                current["results"].append(result)
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
            if not m:
                nonempty = [cell.strip() for cell in row
                            if cell.strip() not in ("", "&nbsp")]
                # Nested SportSoftware category tables put the heading in
                # the middle cell, surrounded only by blanks. Never scan a
                # populated member/team row for arbitrary ``(N)`` text: leg
                # times in parentheses otherwise become phantom categories.
                if len(nonempty) == 1:
                    m = CAT_LINE_RE.match(nonempty[0])
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
            nested_stnr_idx = staffel_idx + 2
            stnr_marks_team = has_stnr and (
                (len(row) > 1 and row[1].strip().isdigit())
                or (staffel_idx >= 3 and len(row) > nested_stnr_idx
                    and row[nested_stnr_idx].strip().isdigit()))
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
                team_number = ""
                if has_stnr:
                    team_number = next((
                        row[position].strip()
                        for position in ((1, nested_stnr_idx)
                                         if staffel_idx >= 3 else (1,))
                        if len(row) > position and row[position].strip().isdigit()
                    ), "")
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
        urllib.request.Request(safe_url, headers=HEADERS), timeout=30,
        context=SSL_CONTEXT).read()
    dest.write_bytes(data)
    time.sleep(0.15)
    return data


CHARSET_UTF8_RE = re.compile(r'charset=["\']?utf-8', re.I)
POSITIONED_ELEMENT_RE = re.compile(
    r"<(?P<tag>div|h[1-6])\b(?P<attrs>[^>]*)>(?P<body>.*?)</(?P=tag)>",
    re.I | re.S)


def positioned_rows(html_text):
    """Return rows from MeOS' absolute-positioned, table-less HTML export."""
    rows = defaultdict(list)
    for match in POSITIONED_ELEMENT_RE.finditer(html_text):
        style = re.search(r'style=["\']([^"\']+)', match.group("attrs"), re.I)
        if not style:
            continue
        left = re.search(r"\bleft\s*:\s*(\d+)px", style.group(1), re.I)
        top = re.search(r"\btop\s*:\s*(\d+)px", style.group(1), re.I)
        if not left or not top:
            continue
        value = html_mod.unescape(re.sub(r"<[^>]+>", "", match.group("body")))
        value = re.sub(r"\s+", " ", value).strip().strip("\xa0")
        if value:
            rows[int(top.group(1))].append((int(left.group(1)), value))
    return [sorted(cells) for _, cells in sorted(rows.items())]


def parse_positioned_document(html_text, relay=False):
    """Parse table-less MeOS individual or relay result HTML.

    The export encodes semantic columns exclusively as CSS ``left/top``
    coordinates. Reconstructing those rows is both more exact and safer than
    flattening the whole document into one stream of words.
    """
    rows = positioned_rows(html_text)
    if not rows:
        return []
    categories, current, pending = [], None, None

    def cell(cells, left):
        return next((value for x, value in cells if x == left), "")

    def flush_team():
        nonlocal pending
        if not relay or not pending or current is None:
            pending = None
            return
        current["sourceUnitCount"] = current.get("sourceUnitCount", 0) + 1
        members = pending["members"]
        own_statuses = []
        for member in members:
            seconds = parse_time_loose(member["timeText"])
            own_statuses.append("ok" if seconds is not None else
                                (parse_status(member["timeText"]) or "unknown"))
        team_status = aggregate_team_status(pending["status"], own_statuses)
        common = {
            "club": pending["name"], "resultKind": "relay",
            "status": team_status, "teamStatus": team_status,
            "teamName": pending["name"], "teamNumber": pending.get("number"),
            "teamTimeText": pending["timeText"],
        }
        if pending.get("rank") is not None:
            common["rank"] = pending["rank"]
        if pending.get("outOfCompetition"):
            common["outOfCompetition"] = True
        if pending.get("timeS") is not None:
            common["teamTimeS"] = pending["timeS"]
        if not members:
            result = dict(common)
            result.update({
                "name": "", "timeText": "", "individualStatus": None,
                "memberlessTeam": True,
                "note": (f"Staffel: {pending['name']} · "
                         "keine Teilnehmernamen in der Quelle"),
            })
            current["results"].append(result)
        else:
            names = [member["name"] for member in members]
            for index, member in enumerate(members, 1):
                result = dict(common)
                seconds = parse_time_loose(member["timeText"])
                mates = list(dict.fromkeys(name for name in names if name != member["name"]))
                note = [f"Staffel: {pending['name']}", f"Leg {index}/{len(members)}"]
                if mates:
                    note.append("Team: " + ", ".join(mates))
                result.update({
                    "name": member["name"], "timeText": member["timeText"],
                    "individualStatus": own_statuses[index - 1],
                    "leg": index, "legCount": len(members),
                    "note": " · ".join(note),
                })
                if seconds is not None:
                    result["timeS"] = seconds
                current["results"].append(result)
        pending = None

    count_left = 384 if relay else 280
    for cells in rows:
        first, count = cell(cells, 48), cell(cells, count_left)
        count_match = re.fullmatch(r"\((\d+)\s*/\s*(\d+)\)", count)
        if first and count_match:
            flush_team()
            current = {
                "name": first.strip(), "declaredStarters": int(count_match.group(1)),
                "results": [],
            }
            if relay:
                current["sourceUnitCount"] = 0
            categories.append(current)
            continue
        if current is None:
            continue
        if relay:
            member_name = cell(cells, 102)
            if member_name:
                if pending is not None and not is_junk_name(member_name):
                    pending["members"].append({
                        "name": member_name, "timeText": cell(cells, 384),
                    })
                continue
            team_name, team_value = cell(cells, 78), cell(cells, 528)
            if not team_name or not (parse_time_loose(team_value) is not None
                                     or parse_status(team_value)):
                continue
            flush_team()
            rank_text = first.rstrip(".")
            seconds = parse_time_loose(team_value)
            pending = {
                "name": team_name, "number": None, "timeText": team_value,
                "timeS": seconds,
                "status": "ok" if seconds is not None else
                          (parse_status(team_value) or "unknown"),
                "rank": int(rank_text) if rank_text.isdigit() else None,
                "outOfCompetition": is_ooc_status(first), "members": [],
            }
            continue

        name, club, value = cell(cells, 78), cell(cells, 280), cell(cells, 483)
        if not name or is_junk_name(name):
            continue
        seconds = parse_time_loose(value)
        # A dash is a real, named source row with an unspecified result
        # state.  Dropping it turns the row into a false completeness gap.
        # Keep it as ``unknown`` so it remains visible and reviewable.
        status = "ok" if seconds is not None else (
            parse_status(value) or "unknown")
        result = {
            "name": name, "club": club, "timeText": value, "status": status,
        }
        rank_text = first.rstrip(".")
        if rank_text.isdigit():
            result["rank"] = int(rank_text)
        elif is_ooc_status(first):
            result["outOfCompetition"] = True
        if seconds is not None:
            result["timeS"] = seconds
        current["results"].append(result)
    flush_team()
    return [category for category in categories if category["results"]]


def parse_meos_relay_table(html_text):
    """Parse MeOS' single-table relay layout (team row + numbered legs)."""
    extractor = TableExtractor()
    extractor.feed(html_text)
    if len(extractor.tables) != 1:
        return []
    categories, current, pending = [], None, None

    def clean_value(value):
        return (value or "").strip().strip("()")

    def flush():
        nonlocal pending
        if pending is None or current is None:
            pending = None
            return
        current["sourceUnitCount"] += 1
        members = pending["members"]
        member_statuses = []
        for member in members:
            seconds = parse_time_loose(member["timeText"])
            member_statuses.append("ok" if seconds is not None else
                                   (parse_status(member["timeText"]) or "unknown"))
        team_status = aggregate_team_status(pending["status"], member_statuses)
        common = {
            "club": pending["name"], "resultKind": "relay",
            "status": team_status, "teamStatus": team_status,
            "teamNumber": None, "teamName": pending["name"],
            "teamTimeText": pending["timeText"],
        }
        if pending.get("rank") is not None:
            common["rank"] = pending["rank"]
        if pending.get("outOfCompetition"):
            common["outOfCompetition"] = True
        if pending.get("timeS") is not None:
            common["teamTimeS"] = pending["timeS"]
        if not members:
            result = dict(common)
            result.update({
                "name": "", "timeText": "", "individualStatus": None,
                "memberlessTeam": True,
                "note": (f"Staffel: {pending['name']} · "
                         "keine Teilnehmernamen in der Quelle"),
            })
            current["results"].append(result)
        else:
            names = [member["name"] for member in members]
            for index, member in enumerate(members, 1):
                result = dict(common)
                seconds = parse_time_loose(member["timeText"])
                mates = list(dict.fromkeys(name for name in names if name != member["name"]))
                note = [f"Staffel: {pending['name']}", f"Leg {index}/{len(members)}"]
                if mates:
                    note.append("Team: " + ", ".join(mates))
                result.update({
                    "name": member["name"], "timeText": member["timeText"],
                    "individualStatus": member_statuses[index - 1],
                    "leg": index, "legCount": len(members),
                    "note": " · ".join(note),
                })
                if seconds is not None:
                    result["timeS"] = seconds
                current["results"].append(result)
        pending = None

    for row in extractor.tables[0]:
        if not row or all(not cell for cell in row):
            continue
        count = row[2].strip() if len(row) > 2 else ""
        category_count = re.fullmatch(r"\((\d+)\s*/\s*(\d+)\)", count)
        if category_count:
            flush()
            current = {
                "name": row[1].strip(),
                "declaredStarters": int(category_count.group(1)),
                "sourceUnitCount": 0, "results": [],
            }
            categories.append(current)
            continue
        if current is None:
            continue
        ordinal = row[1].strip().rstrip(".") if len(row) > 1 else ""
        is_member = bool(
            pending and ordinal.isdigit()
            and (len(row) >= 6
                 or (len(row) == 5 and not row[4].strip().startswith("+"))))
        if is_member:
            name = row[2].strip()
            if name and not is_junk_name(name):
                pending["members"].append({
                    "name": name, "timeText": clean_value(row[3]),
                })
            continue
        # Team rows have either rank|name|value[|behind] or, when unranked,
        # name|value. A valid result value prevents ordinary footer text from
        # opening a phantom team.
        if len(row) >= 4 and ordinal.isdigit():
            rank, name, raw_value = int(ordinal), row[2].strip(), row[3].strip()
        elif len(row) == 3:
            rank, name, raw_value = None, row[1].strip(), row[2].strip()
        else:
            continue
        value = clean_value(raw_value)
        seconds = parse_time_loose(value)
        status = "ok" if seconds is not None else (parse_status(value) or None)
        if not name or status is None:
            continue
        flush()
        ooc = raw_value.strip().startswith("(")
        pending = {
            "rank": None if ooc else rank, "name": name,
            "timeText": value, "timeS": seconds, "status": status,
            "outOfCompetition": ooc, "members": [],
        }
    flush()
    return [category for category in categories if category["results"]]


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
        primary_count = sum(
            not is_auxiliary_attachment_name(f.get("fileName") or "")
            for f in (files or []))
        sole_attachment = primary_count == 1
        for n, f in enumerate(files or []):
            if f["mimeType"] == "text/html":
                jobs.append((int(eid), n, f, sole_attachment))
    jobs = select_jobs(jobs, args.event_id, args.attachment_manifest)
    if args.limit:
        jobs = jobs[: args.limit]
    print(f"html files to parse: {len(jobs)}")

    ok = empty = failed = 0
    for eid, n, f, sole_attachment in jobs:
        if ((eid, n) in MANUAL_ATTACHMENT_INDEX_SKIP
                or (eid, f["fileName"]) in MANUAL_ATTACHMENT_SKIP):
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
            stage_documents = None
            if (eid, n) == (2430, 3):
                stage_documents = parse_oe_multistage_html(text, [
                    {
                        "stageNumber": 3, "stageDate": "2019-06-22",
                        "stageTitle": "-2. Einzelbewerb",
                        "sourceColumn": "E2", "timeIndex": 7, "rankIndex": 8,
                        "blankStageMeansDns": True,
                    },
                    {
                        "stageNumber": 4, "stageDate": "2019-06-23",
                        "stageTitle": "-3.Einzelbewerb",
                        "sourceColumn": "E3", "timeIndex": 9, "rankIndex": 10,
                        "blankStageMeansDns": True,
                    },
                ])
                cats = []
            elif (eid, n) == (3681, 1):
                stage_documents = parse_oe_multistage_html(text, [{
                    "stageNumber": 2, "stageDate": "2022-02-27",
                    "stageTitle": "8 AC Verfolgung",
                    "timeIndex": 7,
                    "overallTimeIndex": 8,
                    "overallRankIndex": 0,
                    "sourceLabel": "E2-Laufzeit",
                    "unrankedStageIsOoc": True,
                    "blankStageMeansDns": True,
                }])
                cats = []
            elif ("Bundesländer-Staffel Turracherhöhe 8.9.2013" in text
                    and "<pre" in text.lower()):
                cats = parse_os2003_relay_pre_html(text)
            elif "Results Vienna Sprint Relay 2022" in text:
                cats = parse_vienna_sprint_relay_html(text)
            elif "NÖ Schul MS - 19.04.2016 / Mannschaftswertungen" in text:
                cats = parse_noe_school_team_html(text)
            elif list_type == "overall":
                empty += 1  # split-times / cumulative-standings report: redundant, skip
                out_path.unlink(missing_ok=True)  # stale output from an earlier, buggier run
                continue
            elif list_type == "relay":
                cats = parse_relay_document(text)
                if not cats and re.search(r"Results\s+[–-].*\bTime lost\b", text, re.I | re.S):
                    cats = parse_meos_relay_table(text)
                if not cats and "position:absolute" in text:
                    cats = parse_positioned_document(text, relay=True)
                if not cats and "<pre" in text.lower():
                    from parse_sportsoftware_text import extract_pre_blocks, parse_legacy_pre_text
                    cats = parse_legacy_pre_text(extract_pre_blocks(text))
            else:
                cats = parse_document(text)
                if not cats and "position:absolute" in text:
                    cats = parse_positioned_document(text)
                if not cats and "<pre" in text.lower():
                    # some SportSoftware HTML wraps a fixed-width report in <pre>
                    # instead of a table (e.g. team/Mannschaft lists) — parse it
                    # with the fixed-width text logic
                    from parse_sportsoftware_text import extract_pre_blocks, parse_legacy_pre_text
                    cats = parse_legacy_pre_text(extract_pre_blocks(text))
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
                    cats = parse_simple_global_results(text)
                if not cats:
                    cats = parse_bracket_html(text)
            skip_cats = MANUAL_CATEGORY_SKIP.get((eid, f["fileName"]))
            if skip_cats and not stage_documents:
                cats = [c for c in cats if c["name"] not in skip_cats]
            if not cats and not stage_documents:
                empty += 1
                out_path.unlink(missing_ok=True)  # stale output from an earlier, buggier run
                continue
            if (not stage_documents
                    and re.search(r"\bOEScore(?:\d{4})?\b|\bSCORE[- ]?OL\b",
                                  text, re.I)):
                for category in cats:
                    for result in category.get("results") or []:
                        result["rankingBasis"] = "score"
            normalized_document = {
                "eventId": eid,
                "source": "sportsoftware-html",
                "sourceUrl": f["url"],
                "fileName": f["fileName"],
                "listType": "multi-stage" if stage_documents else list_type,
                "docDate": MANUAL_DOC_DATE_OVERRIDES.get((eid, f["fileName"]))
                           or guess_doc_date(f["fileName"], text),
                "docTitle": extract_html_title(text),
                "categories": cats,
            }
            if stage_documents:
                normalized_document["stageDocuments"] = stage_documents
            out_path.write_text(json.dumps(
                normalized_document, ensure_ascii=False))
            ok += 1
        except Exception as e:
            failed += 1
            print(f"  FAIL {eid}-{n} {f['fileName']}: {e}", file=sys.stderr)
    print(f"parsed: {ok}, empty: {empty}, failed: {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
