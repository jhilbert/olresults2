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
import certifi
import html as html_mod
import json
import re
import ssl
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

from sportsoftware_common import (
    CAT_LINE_RE, CLUB_LINK_ALLOWLIST, COLUMN_ALIASES, MANUAL_ATTACHMENT_SKIP,
    STATUS_TAIL_RE,
    category_starter_count,
    classify_championship_text, detect_list_type, aggregate_team_status,
    expand_pair_result, is_expected_source_failure, is_junk_name, is_ooc_status,
    parse_champion_annotation, parse_course_info, find_trailing_club, load_clubs,
    looks_like_person,
    parse_status, parse_time, parse_time_loose, number_team_results,
    repair_official_club_status_overflow, team_results_from_pairs,
)
from sync_selection import select_jobs

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
LEGACY_HEADERLESS_CAT_RE = re.compile(
    r"^(?P<name>.+?)\s+\((?P<shown>\d+)"
    r"(?:\s*/\s*(?P<entered>\d+))?\)?\s+Ergebnis\b",
    re.I)

CLUBS = load_clubs()


# The 2014 Krems school championship stored some two-person starts in one
# fixed-width Name cell without any separator.  The source column truncates
# a handful of second given names, so this deliberately remains a
# source-specific transcription instead of a risky global four-token rule.
# Spellings completed below are corroborated by the following year's result.
KREMS_2014_SCHOOL_PAIRS = {
    "Kirchberger Lisa Schöller Luis": ("Kirchberger Lisa", "Schöller Luis"),
    "Koller Luise Preyser Salome": ("Koller Luise", "Preyser Salome"),
    "Jascha Denise Dullinger Andrea": ("Jascha Denise", "Dullinger Andrea"),
    "Schabasser Stephanie Kretz Lau": ("Schabasser Stephanie", "Kretz Lau"),
    "Sladek Nina Kuderna Johanna": ("Sladek Nina", "Kuderna Johanna"),
    "Schmid Jasmina Ecker Theresa": ("Schmid Jasmina", "Ecker Theresa"),
    "Studeregger Sophie Fischer Ann": ("Studeregger Sophie", "Fischer Anna"),
    "Wagner Christiane Trü�mml Nico": ("Wagner Christiane", "Trümml Nico"),
    "Latzenhofer Johannes Mörx Morr": ("Latzenhofer Johannes", "Mörx Morr"),
    "Dietl Alexander Winkler Armin": ("Dietl Alexander", "Winkler Armin"),
    "Aufreiter Stefan Schiegl Phili": ("Aufreiter Stefan", "Schiegl Philipp"),
    "Bauer Immanuel Hirsch Jakob": ("Bauer Immanuel", "Hirsch Jakob"),
    "Ruhofer Christoph Rudischer B.": ("Ruhofer Christoph", "Rudischer B."),
    "Fuchs Tobias Seitl Stefan": ("Fuchs Tobias", "Seitl Stefan"),
    "Ganic Manuel Hofer Alexander": ("Ganic Manuel", "Hofer Alexander"),
    "Unger Elias Eilmberger Ph.": ("Unger Elias", "Eilmberger Ph."),
    "Posch Jakob Mü�ller Martin": ("Posch Jakob", "Müller Martin"),
    "Dörr David Preisinger N.": ("Dörr David", "Preisinger N."),
    "Hofmann Manuel Schiefer Fabian": ("Hofmann Manuel", "Schiefer Fabian"),
    "Klein Michael Mueayprom Piya": ("Klein Michael", "Mueayprom Piya"),
    "Danniel Paleskic Sebastian Him": ("Danniel Paleskic", "Sebastian Him"),
}


def repair_krems_2014_school_pairs(categories):
    """Expand the source's delimiter-less pair starts into two people."""
    for category in categories or []:
        expanded = []
        for source_index, result in enumerate(category.get("results") or [], 1):
            members = KREMS_2014_SCHOOL_PAIRS.get(result.get("name"))
            if not members:
                expanded.append(result)
                continue
            team_number = f"school-pair-{source_index}"
            for member in members:
                row = dict(result)
                row.update({
                    "name": member,
                    "resultKind": "pair",
                    "teamNumber": team_number,
                    "note": "Partner: " + next(m for m in members if m != member),
                })
                expanded.append(row)
        category["results"] = expanded
    return categories


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


def split_trailing_result_value(line):
    """Split a fixed-width row into identity text and its final result value."""
    status = STATUS_TAIL_RE.search(line)
    if status and not line[status.end():].strip():
        return line[:status.start()].rstrip(), status.group(0).strip()
    clock = re.search(r"(\d{1,3}:\d{2}(?::\d{2})?)\s*$", line)
    if clock:
        return line[:clock.start()].rstrip(), clock.group(1)
    return line.rstrip(), ""


def parse_legacy_team_text(text):
    """Parse old ``<pre>`` relay and Mannschaft exports.

    These reports have one team header followed by indented roster lines and
    therefore cannot be interpreted as ordinary individual fixed-width rows.
    Team/status/rank are copied to every real member; a registered DNS team
    without a roster remains one explicitly personless result unit.
    """
    expanded = text.expandtabs()
    header = next((line for line in expanded.splitlines()
                   if re.search(r"\bPl\b.*\bStnr\b.*\b(?:Staffel|Verein)\b", line)), "")
    if not header:
        return []
    team_mode = bool(re.search(r"\bVerein\b", header))
    kind = "team" if team_mode else "relay"
    label = "Mannschaft" if team_mode else "Staffel"
    categories, current, pending = [], None, None

    def flush():
        nonlocal pending
        if not pending or current is None:
            pending = None
            return
        current["sourceUnitCount"] = current.get("sourceUnitCount", 0) + 1
        members = pending["members"]
        member_statuses = []
        for member in members:
            seconds = parse_time_loose(member["timeText"])
            member_statuses.append(
                "ok" if seconds is not None else
                (parse_status(member["timeText"]) or "unknown"))
        team_status = aggregate_team_status(pending["status"], member_statuses)
        common = {
            "club": pending["name"], "resultKind": kind,
            "status": team_status, "teamStatus": team_status,
            "teamNumber": pending["number"], "teamName": pending["name"],
            "teamTimeText": pending["timeText"],
        }
        if pending.get("rank") is not None:
            common["rank"] = pending["rank"]
        if pending.get("outOfCompetition"):
            common["outOfCompetition"] = True
        if pending.get("timeS") is not None:
            common["teamTimeS"] = pending["timeS"]
        if not members:
            row = dict(common)
            row.update({
                "name": "", "timeText": "", "individualStatus": None,
                "memberlessTeam": True,
                "note": f"{label}: {pending['name']} · keine Teilnehmernamen in der Quelle",
            })
            current["results"].append(row)
            pending = None
            return
        names = [member["name"] for member in members]
        for index, member in enumerate(members, 1):
            row = dict(common)
            seconds = parse_time_loose(member["timeText"])
            own_status = ("ok" if seconds is not None else
                          (parse_status(member["timeText"]) or "unknown"))
            mates = list(dict.fromkeys(name for name in names if name != member["name"]))
            note = [f"{label}: {pending['name']}"]
            if not team_mode:
                note.append(f"Leg {index}/{len(members)}")
            if mates:
                note.append("Team: " + ", ".join(mates))
            row.update({
                "name": member["name"], "timeText": member["timeText"],
                "individualStatus": own_status, "note": " · ".join(note),
            })
            if not team_mode:
                row.update({"leg": index, "legCount": len(members)})
            if seconds is not None:
                row["timeS"] = seconds
            current["results"].append(row)
        pending = None

    for raw_line in expanded.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        category = CAT_LINE_RE.match(stripped)
        course_category = COURSE_CAT_LINE_RE.match(stripped)
        if category or course_category:
            flush()
            match = category or course_category
            name = match.group("name").strip()
            current = {
                "name": name,
                "declaredStarters": (category_starter_count(category)
                                     if category else None),
                "sourceUnitCount": 0, "results": [],
            }
            if category:
                current.update(parse_course_info(category.group("rest")))
            categories.append(current)
            continue
        if current is None or re.match(r"^(?:Pl\b|Name\b)", stripped):
            continue
        body, value = split_trailing_result_value(stripped)
        team = re.match(r"^(?:(?P<place>AK|\d+)\s+)?(?P<number>\d+)\s+(?P<name>.+)$",
                        body, re.I)
        if team and value:
            flush()
            place = team.group("place") or ""
            seconds = parse_time_loose(value)
            pending = {
                "rank": int(place) if place.isdigit() else None,
                "outOfCompetition": is_ooc_status(place),
                "number": team.group("number"), "name": team.group("name").strip(),
                "timeText": value, "timeS": seconds,
                "status": "ok" if seconds is not None else
                          (parse_status(value) or "unknown"),
                "members": [],
            }
            continue
        if pending is None:
            continue
        member_text = body
        member_text = re.sub(r"\s+(?:\d{2}|\d{4})\s*$", "", member_text).strip()
        if (not value and not team_mode) or is_junk_name(member_text):
            continue
        if looks_like_person(member_text):
            pending["members"].append({"name": member_text, "timeText": value})
    flush()
    return [category for category in categories if category["results"]]


def parse_headerless_result_text(text):
    """Parse OE fixed-width individual lists whose column header was omitted."""
    categories, current = [], None
    for raw_line in text.expandtabs().splitlines():
        stripped = raw_line.strip()
        category = LEGACY_HEADERLESS_CAT_RE.match(stripped)
        if category:
            current = {
                "name": category.group("name").strip(),
                "declaredStarters": int(category.group("shown")), "results": [],
            }
            categories.append(current)
            continue
        if current is None or not stripped:
            continue
        body, value = split_trailing_result_value(stripped)
        if not value:
            continue
        prefix = re.match(r"^(?:(?P<place>AK|\d+)\s+)?(?P<body>.+)$", body, re.I)
        if not prefix:
            continue
        identity = prefix.group("body").split()
        club, name_tokens = find_trailing_club(identity, CLUBS)
        yob = None
        if club is None:
            year_at = next((i for i, token in enumerate(identity)
                            if i >= 2 and re.fullmatch(r"\d{2}|\d{4}", token)), None)
            if year_at is not None:
                name_tokens, yob = identity[:year_at], identity[year_at]
                club = " ".join(identity[year_at + 1:])
            else:
                # These legacy <pre> reports remain fixed-width even when
                # they omit the column header. Unknown foreign/school clubs
                # cannot be found in the club dictionary, but the source's
                # physical fields are stable: placement [0:13], name [13:44],
                # optional birth year + club [44:time]. Preserve that row
                # rather than dropping the winner (e.g. Lessinia Orienteering).
                fixed_body, fixed_value = split_trailing_result_value(
                    raw_line.rstrip())
                fixed_name = fixed_body[13:44].strip() if len(fixed_body) > 44 else ""
                fixed_club = fixed_body[44:].strip() if len(fixed_body) > 44 else ""
                year_match = re.match(r"^(\d{2}|\d{4})\s+(.+)$", fixed_club)
                if year_match:
                    yob, fixed_club = year_match.groups()
                if not fixed_name or not fixed_club:
                    continue
                name_tokens = fixed_name.split()
                club = fixed_club
                value = fixed_value or value
        elif name_tokens and re.fullmatch(r"\d{2}|\d{4}", name_tokens[-1]):
            yob = name_tokens.pop()
        name = re.sub(r"^([^,]+),\s*(.+)$", r"\1 \2", " ".join(name_tokens)).strip()
        if not looks_like_person(name) or is_junk_name(name):
            continue
        seconds = parse_time_loose(value)
        row = {
            "name": name, "club": club or "", "timeText": value,
            "status": "ok" if seconds is not None else (parse_status(value) or "unknown"),
        }
        if "family" in current["name"].casefold() and "," not in " ".join(name_tokens):
            row["resultKind"] = "family"
        place = prefix.group("place") or ""
        if place.isdigit():
            row["rank"] = int(place)
        elif is_ooc_status(place):
            row["outOfCompetition"] = True
        if seconds is not None:
            row["timeS"] = seconds
        if yob:
            year = int(yob)
            row["yearOfBirth"] = year + (2000 if year <= 26 else 1900) if year < 100 else year
        current["results"].append(row)
    return [category for category in categories if category["results"]]


def parse_legacy_pre_text(text):
    """Try the ordinary, team-block, then headerless historic text shapes."""
    return (parse_text(text) or parse_legacy_team_text(text)
            or parse_headerless_result_text(text))


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
            # In a Mannschaft table the post-annotation winner line starts
            # with bib + Verein, not bib + Name. Anchoring it at Name shifts
            # the club into the first roster slot and the third runner into
            # the time cell. Individual tables still anchor at Name.
            anchor_label = ("Verein" if "Verein" in labels and any(
                label.startswith("Läufer") for label in labels) else "Name")
            name_start = starts[labels.index(anchor_label)]
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
        if (ranks and category.get("declaredStarters") == max(ranks)
                and len(results) > category["declaredStarters"]):
            category["declaredStarters"] = len(results)
    return parsed


def fetch(url, dest, force=False):
    if dest.exists() and not force:
        return dest.read_bytes()
    safe_url = urllib.parse.quote(url, safe=":/?&=%#")
    data = urllib.request.urlopen(
        urllib.request.Request(safe_url, headers=HEADERS), timeout=30,
        context=ssl.create_default_context(cafile=certifi.where())).read()
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
            if (int(eid), f.get("fileName") or "") in MANUAL_ATTACHMENT_SKIP:
                continue
            if mime == "text/plain":
                jobs.append((int(eid), n, f, "txt"))
            elif mime == "text/link" and domain_of(url) in LINK_DOMAIN_ALLOWLIST:
                jobs.append((int(eid), n, f, "html"))
    return jobs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--event-id", type=int, help="only process one ANNE event")
    ap.add_argument("--attachment-manifest", type=Path,
                    help="only process attachments listed by the current incremental sync")
    ap.add_argument("--force-download", action="store_true",
                    help="re-download selected source files even when cached")
    args = ap.parse_args()

    FILES.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)
    jobs = collect_jobs()
    jobs = select_jobs(jobs, args.event_id, args.attachment_manifest)
    if args.limit:
        jobs = jobs[: args.limit]
    print(f"text/link+text/plain files to parse: {len(jobs)}")

    ok = empty = failed = expected_failed = 0
    for eid, n, f, kind in jobs:
        out_path = OUT / f"{eid}-{n}.json"
        try:
            data = fetch(f["url"], FILES / f"{eid}-{n}.{kind}", args.force_download)
            # A number of legacy ANNE records mislabel a direct PDF URL as
            # ``text/link``. The PDF parser owns those sources; importantly,
            # do not unlink the normalized output it may just have written
            # earlier in the same sync run.
            if data[:4] == b"%PDF":
                empty += 1
                continue
            text = extract_pre_blocks(decode(data))
            list_type = detect_list_type(f["fileName"] or f["url"], text)
            if list_type == "overall":
                empty += 1
                out_path.unlink(missing_ok=True)
                continue
            cats = parse_text(text)
            if eid == 1110 and n == 1:
                cats = repair_krems_2014_school_pairs(cats)
            if not cats:
                empty += 1
                out_path.unlink(missing_ok=True)  # stale output from an earlier, buggier run
                continue
            out_path.write_text(json.dumps({
                "eventId": eid,
                "source": "sportsoftware-text",
                "sourceUrl": f["url"],
                "fileName": f["fileName"] or f["url"],
                "listType": list_type,
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
