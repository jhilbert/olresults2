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
from collections import defaultdict
import json
import re
import ssl
import sys
import time
import urllib.parse
import urllib.request
import warnings
from pathlib import Path

import certifi

from sportsoftware_common import (
    CAT_LINE_RE, KAT_TOKEN_RE, MANUAL_ATTACHMENT_SKIP, MANUAL_DOC_DATE_OVERRIDES, STATUS_TAIL_RE,
    aggregate_team_status, category_starter_count, classify_championship_text,
    detect_list_type, find_trailing_club, guess_doc_date,
    expand_pair_result, is_junk_name, is_ooc_status, load_clubs, looks_like_person,
    split_by_kat, split_pair_names,
    parse_champion_annotation, parse_course_info, parse_flow_row, parse_status, parse_time,
    parse_time_loose, strip_champion_name_prefix,
)
from sync_selection import select_jobs

CLUBS = load_clubs()
CLUBS.update({
    # Full Hungarian spellings whose country code is interleaved with the
    # club/time glyphs in several old MTBO PDFs.
    "mom hegyvidék se-mom tájfutó szako":
        "MOM Hegyvidék SE-MOM Tájfutó Szako",
    "vhs veszprémi honvéd sportegyesüle":
        "VHS Veszprémi Honvéd Sportegyesüle",
})
CLUB_CANONICAL_KEYS = {club.casefold() for club in CLUBS.values()}


def looks_like_status_team_label(text):
    """Distinguish a rankless team label from a member name before a status.

    Historic relay PDFs omit rank and start number for some MP/DNF/DSQ teams.
    A duplicated club label (``HSV Ried HSV Ried``) or a canonical club plus
    its squad code (``SU Schöckl Orienteering SUSO``) can superficially pass
    ``looks_like_person``.  Those lines introduce a team; a genuine member
    such as ``Josef Hones 54 Fehlst`` must continue to belong to the pending
    team.
    """
    label = re.sub(r"\s+", " ", (text or "").strip()).casefold()
    if not label:
        return False
    tokens = label.split()
    middle = len(tokens) // 2
    if len(tokens) >= 2 and len(tokens) % 2 == 0 and tokens[:middle] == tokens[middle:]:
        return True
    if re.fullmatch(r"team\s+\d+", label, re.I):
        return True
    # Very short canonical codes (ARC, OLC, ...) are too weak as a prefix;
    # require a recognisable club spelling and an exact token boundary.
    return any(
        len(club) >= 5
        and (label == club or label.startswith(club + " ") or label.startswith(club + "-"))
        for club in CLUB_CANONICAL_KEYS
    )


def valid_flow(flow):
    """Only trust a text/club-dictionary parse when it's anchored by a known
    club (so title/header lines and school-cup formats don't masquerade as
    results). A pair additionally requires each side to be a clean two-token
    'Lastname Firstname' — otherwise it's not a genuine run-in-pairs row."""
    if not flow or not flow["club"]:
        return False
    names = flow["names"]
    if len(names) > 1:
        return all(len(n.split()) == 2 and looks_like_person(n) for n in names)
    return bool(names) and looks_like_person(names[0])


def flow_results(flow):
    """Build one normalized result per runner from a parse_flow_row() result.
    Two+ runners means a pair: each row carries the shared rank/time/club and a
    'Partner: …' note."""
    repaired_club, repaired_time = repair_result_club_and_value(
        flow.get("club") or "", flow.get("timeText") or "")
    flow = dict(flow)
    flow["club"], flow["timeText"] = repaired_club, repaired_time
    seconds = parse_time(flow["timeText"]) if flow.get("timeText") else None
    explicit_status = parse_status(flow.get("statusText") or "")
    status = (explicit_status if explicit_status not in (None, "ok") else
              "ok" if seconds is not None else explicit_status or "unknown")
    is_pair = len(flow["names"]) > 1
    out = []
    for nm in flow["names"]:
        name_ooc = bool(re.match(r"(?i)^A\.?\s*K\.?(?:\s+|$)", nm))
        if name_ooc:
            nm = re.sub(r"(?i)^A\.?\s*K\.?(?:\s+|$)", "", nm).strip()
        nm, championship = strip_champion_name_prefix(nm)
        if is_junk_name(nm):
            continue
        res = {"name": nm, "club": flow.get("club") or "",
               "timeText": flow.get("timeText") or flow.get("statusText") or ""}
        if championship:
            res["championship"] = championship
        if flow.get("rank") is not None:
            res["rank"] = flow["rank"]
        if seconds is not None:
            res["timeS"] = seconds
        res["status"] = status
        if flow.get("outOfCompetition"):
            res["outOfCompetition"] = True
        if name_ooc:
            res["outOfCompetition"] = True
        jg = flow.get("jg")
        if jg and jg.isdigit():
            y = int(jg)
            res["yearOfBirth"] = y + (2000 if y <= 26 else 1900) if y < 100 else y
        if is_pair:
            res["resultKind"] = "pair"
            res["note"] = "Partner: " + ", ".join(o for o in flow["names"] if o != nm)
        out.append(res)
    return out


def parse_course_class_result(text):
    """Parse ``Pl Stnr Name Jg Verein Klasse Zeit`` rows whose PDF header
    coordinates are horizontally compressed relative to the data font.

    The explicit Klasse column sits *after* the club, so the ordinary
    trailing-club parser cannot anchor it.  Locate the longest known club
    inside the row and keep the material on either side as person/year and
    class metadata respectively.
    """
    value_match = re.search(
        r"(?P<value>\d{1,3}:\d{2}(?::\d{2})?|Fehlst(?:empel)?|Aufg|Disq(?:u)?|"
        r"N\s*Ang|DNS|DNF|DSQ|MP)\s*$", text, re.I)
    if not value_match:
        return None
    prefix = text[:value_match.start()].strip()
    lead = re.match(r"^(?:(?P<place>AK|\d+)\s+)?(?P<stnr>\d+)\s+(?P<body>.+)$",
                    prefix, re.I)
    if not lead:
        return None
    body = lead.group("body").strip()
    folded = body.casefold()
    candidates = []
    for alias, canonical in CLUBS.items():
        start = folded.find(alias)
        while start >= 0:
            end = start + len(alias)
            if ((start == 0 or not folded[start - 1].isalnum())
                    and (end == len(folded) or not folded[end].isalnum())):
                candidates.append((len(alias), start, end, canonical))
            start = folded.find(alias, start + 1)
    if not candidates:
        return None
    _length, start, end, club = max(candidates)
    name = re.sub(r"\s+\d{2}$", "", body[:start].strip())
    if not name:
        return None
    place = lead.group("place")
    # With only one leading number it is the start number of an unranked
    # status row. Ranked rows carry both placement and start number.
    rank = int(place) if place and place.isdigit() else None
    return {
        "rank": rank,
        "names": [part.strip() for part in name.split(" / ") if part.strip()],
        "club": club,
        "jg": None,
        "timeText": value_match.group("value") if parse_time_loose(
            value_match.group("value")) is not None else None,
        "statusText": (None if parse_time_loose(value_match.group("value")) is not None
                       else value_match.group("value")),
        "outOfCompetition": bool(place and is_ooc_status(place)),
    }


def parse_multi_round_result(text):
    """Parse a flat ranking with several split-round times and ``Gesamt``.

    The final value is the result time; preceding Runde values are splits and
    must neither replace it nor leak into the runner/club fields.
    """
    match = re.match(
        r"^(?P<rank>\d+)\s+(?P<body>.+?)\s+"
        r"(?P<a>\d{1,3}:\d{2})\s+(?P<b>\d{1,3}:\d{2})\s+"
        r"(?P<c>\d{1,3}:\d{2})\s+(?P<total>\d{1,3}:\d{2})$", text)
    if not match:
        return None
    club, name_tokens = find_trailing_club(match.group("body").split(), CLUBS)
    name = " ".join(name_tokens).strip()
    if not club or is_junk_name(name):
        return None
    total = match.group("total")
    return {
        "name": name, "club": club, "timeText": total,
        "timeS": parse_time_loose(total), "status": "ok",
        "rank": int(match.group("rank")),
        "note": "Runden: " + ", ".join(
            match.group(key) for key in ("a", "b", "c")),
    }


def parse_score_course_result(text):
    """Parse ``Platz Nachname Vorname ... Zeit Club Bahn ... Ergebnis``.

    This report ranks each A/B/C course independently and commonly glues the
    left-aligned surname directly to the right-aligned placement digit.
    """
    match = re.match(
        r"^(?P<rank>\d+)\s*(?P<name>.+?)\s+"
        r"(?P<yg>\d{0,2}[MW])\s+"
        r"(?P<start>\d{1,3}:\d{2}(?::\d{2})?)\s+"
        r"(?P<finish>\d{1,3}:\d{2}(?::\d{2})?)\s+"
        r"(?P<time>\d{1,3}:\d{2}(?::\d{2})?)\s+"
        r"(?P<club>.+?)\s+(?P<course>[A-Z]-\d+min)\s+"
        r"(?P<points>-?\d+)\s+(?P<penalty>-?\d+)\s+(?P<score>-?\d+)$",
        text, re.I)
    if not match:
        return None
    name = match.group("name").strip()
    if is_junk_name(name) or not looks_like_person(name):
        return None
    time_text = match.group("time")
    result = {
        "name": name, "club": match.group("club").strip(),
        "timeText": time_text, "timeS": parse_time_loose(time_text),
        "status": "ok", "rank": int(match.group("rank")),
        "scoreText": match.group("score"),
        "sourceCourse": match.group("course").upper(),
    }
    yob = re.match(r"(\d{1,2})", match.group("yg"))
    if yob:
        year = int(yob.group(1))
        result["yearOfBirth"] = year + (2000 if year <= 26 else 1900)
    return result


def parse_points_cup_result(text):
    """Parse ``Platz (A) Name Verein Zeit Punkte`` spreadsheet exports.

    Their duration is formatted as ``minutes:seconds:00`` (an Excel duration
    accidentally rendered with a third, always-zero field), not as
    ``hours:minutes:seconds``.  Keeping the raw shape would turn 48:16 into a
    48-hour run.  Placements are commonly glued to the surname (``1Holper``).
    """
    match = re.match(
        r"^(?:(?P<rank>\d{1,3})\s*)?(?P<body>.+?)\s+"
        r"(?P<time>\d{1,3}:\d{2}:00)\s+(?P<points>-?\d+(?:[.,]\d+)?)$",
        text)
    if not match:
        return None
    club, name_tokens = find_trailing_club(match.group("body").split(), CLUBS)
    name = " ".join(name_tokens).strip()
    if not club or is_junk_name(name) or not looks_like_person(name):
        return None
    minutes, seconds, _zero = map(int, match.group("time").split(":"))
    result = {
        "name": name, "club": club,
        "timeText": f"{minutes}:{seconds:02d}",
        "timeS": minutes * 60 + seconds,
        "status": "ok", "scoreText": match.group("points"),
    }
    if match.group("rank"):
        result["rank"] = int(match.group("rank"))
    return result

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw" / "anne"
FILES = RAW / "files"
SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
OUT = ROOT / "data" / "normalized"

HEADERS = {"User-Agent": "olresults-sync/0.1 (+https://github.com/josefhilbert/olresults)"}

CONTINUATION_RE = re.compile(r"^\(Forts\.?\)$", re.I)
# Historic fixed-column exports use all of ``1``, ``1.`` and, when the first
# column is exceptionally narrow, ``1Holper Leo``.  Keep the rank separate
# before the name validator sees the glued form.
RANK_LEAK_RE = re.compile(r"^(\d{1,3})\.?\s*(\S.*)$")
RANK_TEXT_RE = re.compile(r"^(\d{1,3})\.?$")
# split-times ("Zwischenzeiten") reports: a different, per-control layout that
# puts the club on its own line and interleaves dozens of split times into each
# row. They duplicate the plain results list, so we skip them rather than
# mis-parse them. Detected by the header word or a run of "N(controlcode)" tokens.
SPLITS_RE = re.compile(r"Zwischenzeiten|\d+\(\d+\)\s+\d+\(\d+\)")
# SportSoftware repeats the event title + full date as a running page header on
# every page; it leaks in as a bogus result row ("AC Mitteldistanz"). A real
# result row never carries a full dd.mm.yyyy date, so skip any line that does.
# Older SportSoftware exports use a two-digit year in their repeated page
# header ("Sat 17.02.18 13:54").  Accept both variants: a result row never
# contains a dotted full date, while this blocks the header from becoming a
# synthetic runner such as "Annaberg" after a page break.
DATE_HEADER_RE = re.compile(
    r"\b(?:\d{1,2}\.\d{1,2}\.(?:\d{2}|\d{4})|"
    r"\d{1,2}/\d{1,2}/\d{4})\b")
PDF_PAGE_CHROME_RE = re.compile(
    r"(?:\bSeite\s+\d+\b|\bPage\s+\d+\s+of\s+\d+\b|"
    r"SportSoftware|Stephan\s+Kr[äa]mer)", re.I)
TIME_TOKEN_RE = re.compile(r"\d{1,3}:\d{2}(?::\d{2})?")
LINE_TOLERANCE = 3  # px, for clustering words into the same visual line
# see the "Text1" handling below assign_columns(): the word "Meister"/
# "Meisterin" spilling from a narrow Text1 announcement column into Name.
LEAKED_TITLE_WORD_RE = re.compile(r"(?i)^(?:staats)?meister(?:in)?\b\s*")

# Some clubs export a "flowing" PDF with no Pl/Stnr/Verein column headers at
# all: a numbered list "1. Name Club Zeit [Rückstand] [Zeit verloren]", with
# category lines like "Herren A (17 / 17) Zeit Rückstand Zeit verloren" or
# "Kategorie ULTIMATE" (no starter count at all). parse_pdf()'s column logic
# never gets going here since it never finds a "Pl"/"Platz" header; see
# parse_flowing_pdf() below.
FLOW_CAT_RE = re.compile(
    # Greedy name capture deliberately selects the final parenthesised count.
    # MeOS score lists put a duration before it: ``D OL-15 (45Min.) (6 / 6)``.
    # The earlier lazy capture reported 45 starts for every class.
    r"^(?P<name>.+)\s*\((?P<starters>\d+)"
    r"(?:\s*/\s*(?P<entered>\d+))?\)?\s*(?P<rest>.*)$")
FLOW_CAT_PLAIN_RE = re.compile(r"^Kategorie\s+(?P<name>.+)$", re.I)
FLOW_TIME_RE = re.compile(r"^\+?\d{1,3}:\d{2}(?::\d{2})?$")
RANK_PREFIX_RE = re.compile(r"^\d+\.?$")
# A few SportSoftware families do not print a starter count at all. Their
# category is still structurally explicit through a course/distance marker.
# Recognizing these lines prevents all following rows from leaking into the
# previous counted category.
COURSE_ONLY_CAT_RE = re.compile(
    r"^(?P<name>.+?)\s+\((?:\d+(?:[.,]\d+)?\s*(?:m|km))(?:\s*,[^)]*)?\)\s*$",
    re.I)
BAHN_CAT_RE = re.compile(
    r"^(?P<name>(?!\d)[A-Za-zÄÖÜäöü][^()]*)\s+Bahn\s*:?[\s\d,.-]*"
    r"(?:Kontrollposten|Hm|km|P)\b.*$", re.I)
BAHN_ONLY_CAT_RE = re.compile(r"^(?P<name>Bahn\s+(?:\d+|[A-Z]))\b.*$", re.I)
PLAIN_LETTER_CAT_RE = re.compile(r"^[A-ZÄÖÜ](?:\d+)?$")
PLAIN_AGE_CATEGORY_RE = re.compile(
    r"^(?P<name>(?:Herren|Damen)\s+(?:bis|ab)\s+\d+"
    r"(?:\s+(?:Elite|Kurz))?)$", re.I)
PLAIN_LEGACY_AGE_CATEGORY_RE = re.compile(
    r"^(?P<name>(?:Herren|Damen)\s+\d+[+-]?\s*(?:-\s*\w+)?)\s*:"
    r"(?:\s+\d.*)?$", re.I)
PLAIN_SPECIAL_CATEGORY_RE = re.compile(
    r"^(?P<name>Offen\s+(?:Lang|Kurz)|Neulinge|Familie|Family)$", re.I)
NUMBERED_COURSE_CAT_RE = re.compile(
    r"^(?P<number>\d+)\s+\((?P<starters>\d+)\)\s+"
    r"\d+(?:[.,]\d+)?\s*km\b", re.I)
UNCOUNTED_COURSE_CAT_RE = re.compile(
    r"^(?P<name>(?!\d).+?)\s*:?\s+\d+(?:[.,]\d+)?\s+km\b.*$", re.I)
PRELIMINARY_CAT_RE = re.compile(
    r"^(?P<name>(?!\d).+?)(?:\s+\([^)]*)?\s+"
    r"(?:Preliminary\s+results|Vorl[aä]ufiges\s+Ergebnis)\b.*$", re.I)
UNCOUNTED_STATUS_CAT_RE = re.compile(
    r"^(?P<name>(?!\d).+?)\s+\(\s*(?:Stand\s+von|Status)\s*:", re.I)
MEOS_PAGE_HEADER_RE = re.compile(r"^MeOS\s+\d{4}-\d{2}-\d{2}\b", re.I)
MEOS_CLASS_HEADER_RE = re.compile(
    r"^.+?\(\s*\d+\s*/\s*\d+\s*\)\s+(?:Time|Zeit)\s+"
    r"(?:Behind|R[uü]ckstand)\b", re.I | re.M)

PDF_HEADER_ALIASES = {
    "Místo": "Pl", "Jméno": "Name", "Oddíl": "Verein",
    "Čas": "Zeit", "Ztráta": "Diff",
    "Pos.": "Pl", "Pos": "Pl", "Club": "Verein",
    "Time": "Zeit", "YB": "Jg", "Stno": "Stnr",
    # ``Ergebnis`` must remain distinct: score races have both ``Zeit`` and
    # ``Ergebnis`` (final points). Tak-Soft school sheets using Ergebnis as
    # elapsed time are handled by their dedicated parser before this map.
    "Schule": "Verein", "Laufzeit": "Zeit",
    "l": "Pl", "Familienname": "Nachname",
}
# The team-row header's own column names vary too much to match literally -
# confirmed real across the corpus: "Pl Stnr Staffel Zeit" (plain), "Pl
# Text1 Staffel Zeit" (a champion announcement swaps "Stnr" for the narrow
# "Text1" column - event 3825, "Chicken Challenge ÖM/ÖStM Mixed-Sprint-
# Staffel"), "Pl Text1 Text2 Stnr Staffel Zeit" (event 794, two announcement
# columns), "Pl Staffel Zeit Diff." (no Stnr/Text at all - event 2022), "Pl
# Text1 Verein Bez Zeit" (team column literally called "Verein", no
# "Staffel" word anywhere - event 3824, "Chicken Challenge ÖM/ÖStM
# Staffel"). What's constant across every one of these two-tier team+member
# layouts, and never appears in a flat individual-row export, is the SECOND
# header line right below it - "[X] Name [Y] Zeit", where X/Y are whatever
# per-member stat that export includes: birth year "Jg" (after Name), leg
# number "Lnr" (confirmed real: event 3633, "ÖSTM und ÖM Staffel" 2022 -
# "Lnr Name Zeit", the ONE variant with its extra column BEFORE "Name"
# rather than after, which an earlier version of this regex - requiring
# "Name" to lead the line - missed entirely, dropping whole teams including
# Naturfreunde Wien's H-14 bronze relay out of the results), or nothing at
# all, just "Name Zeit". Matching on "Name" appearing somewhere on this
# second line with "Zeit" after it, an optional single token on either
# side, instead of enumerating every column-name spelling. Each earlier
# narrower version of this regex missed a real relay and silently mangled
# it into single bogus rows per category ("AK"/"OLC Wienerwald" as if it
# were one runner) via the flat individual parser instead.
RELAY_HEADER_RE = re.compile(
    r"^Pl(?:atz)?\b.*\n\s*(?:\S+\s+)?Name\s+"
    r"(?:(?:\S+\s+)?(?:Z\s*eit|Time)|Einzelzeit)\b", re.M)
RELAY_TITLE_RE = re.compile(r"Orientierungslauf[- ]Staffel", re.I)
MANNSCHAFT_HEADER_RE = re.compile(
    r"^Pl(?:atz)?\b.*\bVerein\b.*\bZeit\b\s*\n\s*Name\s+Jg\b", re.M)


def merge_category_continuations(categories):
    """Join non-adjacent ``(Forts.)`` sections of the same category.

    Some PDF page trees are not stored in the visual category order: a page
    can begin with the tail of DE, followed on the next page by DB/DC/DD and
    only then the beginning of DE.  The old adjacent-only check therefore
    emitted two result lists with the same name and starter count.  We merge
    only sections whose numeric ranks do not overlap, so two genuinely
    separate races using the same category label stay separate.
    """
    merged = []
    for category in categories:
        target = None
        ranks = {r.get("rank") for r in category.get("results", [])
                 if r.get("rank") is not None}
        for candidate in merged:
            candidate_name = candidate.get("name") or ""
            category_name = category.get("name") or ""
            same_or_clipped_name = (
                candidate_name == category_name
                or (min(len(candidate_name), len(category_name)) >= 15
                    and (candidate_name.startswith(category_name)
                         or category_name.startswith(candidate_name)))
            )
            if (not same_or_clipped_name
                    or candidate.get("declaredStarters") != category.get("declaredStarters")):
                continue
            candidate_ranks = {r.get("rank") for r in candidate.get("results", [])
                               if r.get("rank") is not None}
            if not ranks.intersection(candidate_ranks):
                target = candidate
                break
        if target is None:
            merged.append(category)
        else:
            if len(category.get("name") or "") > len(target.get("name") or ""):
                target["name"] = category["name"]
            target["results"].extend(category.get("results", []))
            if category.get("sourceUnitCount") is not None:
                target["sourceUnitCount"] = (target.get("sourceUnitCount", 0)
                                             + category["sourceUnitCount"])
            for key in ("courseLengthM", "courseClimbM", "courseControls"):
                if target.get(key) is None and category.get(key) is not None:
                    target[key] = category[key]
    for category in merged:
        ranks = [r.get("rank") for r in category.get("results", [])
                 if r.get("rank") is not None]
        units = category_competitor_unit_count(category)
        # A few exports put the number of classified/ranked competitors in
        # ``(N)`` and still print additional MP/DNS/DSQ rows below them. When
        # N is exactly the highest placement, those visible unranked rows are
        # real result entries and the source-list size is the visible unit
        # count, not merely the last rank.
        if (ranks and category.get("declaredStarters") == max(ranks)
                and units > category["declaredStarters"]):
            category["declaredStarters"] = units
    return merged


def category_competitor_unit_count(category):
    """Count individual rows, pairs, teams and relays like build_db does."""
    keys = []
    for index, result in enumerate(category.get("results") or []):
        kind = result.get("resultKind") or "individual"
        if kind in ("individual", "family"):
            key = ("row", index)
        elif kind == "pair" and result.get("teamNumber"):
            key = (kind, "number", result["teamNumber"])
        elif kind == "pair":
            key = (kind, result.get("rank"), result.get("status"),
                   result.get("timeS"), result.get("club"))
        else:
            key = (kind, result.get("teamNumber") or result.get("teamName")
                   or result.get("note"))
        keys.append(key)
    return len(set(keys))


def normalize_school_schnupper_pairs(categories):
    for category in categories:
        results = category.get("results") or []
        prepared = []
        for result in results:
            tokens = result.get("name", "").split()
            school_prefix = ""
            if (len(tokens) == 3
                    and re.fullmatch(r"(?:T?N?MS|HTL|MMS|BRG|Europagym)",
                                     tokens[-1], re.I)):
                school_prefix = tokens.pop()
            prepared.append((result, tokens, school_prefix))
        if ("schnupper" in category["name"].casefold()
                and category.get("declaredStarters") == 2 * len(results)
                and results
                and all((result.get("resultKind") or "individual") == "individual"
                        and len(names) in (2, 4)
                        for result, names, _school_prefix in prepared)):
            # Some school Schnupper lists state the number of participating
            # children (28) but rank 14 two-child teams, one row per pair.
            # Keep both supplied names separately addressable while comparing
            # the quality check against the 14 actual starts.
            paired = []
            for pair_number, (result, names, school_prefix) in enumerate(prepared, 1):
                pair_names = (names if len(names) == 2 else
                              [" ".join(names[:2]), " ".join(names[2:])])
                for name in pair_names:
                    pair = dict(result)
                    pair.update({
                        "name": name,
                        "club": f"{school_prefix} {result.get('club', '')}".strip(),
                        "resultKind": "pair",
                        "teamNumber": f"school-pair-{pair_number}",
                        "note": "Partner: " + next(
                            other for other in pair_names if other != name),
                    })
                    paired.append(pair)
            category["results"] = paired
            category["declaredStarters"] = len(results)
    return categories


def normalize_exact_time_ties(categories):
    for category in categories:
        previous = None
        for result in category.get("results") or []:
            if (previous and result.get("rank") is None
                    and result.get("status") == previous.get("status") == "ok"
                    and result.get("timeS") is not None
                    and result.get("timeS") == previous.get("timeS")
                    and previous.get("rank") is not None
                    and not result.get("outOfCompetition")
                    and not re.match(r"(?i)^A\.?\s*K\.?(?:\s+|$)",
                                     result.get("name") or "")):
                # SportSoftware leaves the placement cell empty for the
                # second athlete in an exact tie. Competition ranking keeps
                # the preceding placement; the next printed rank is skipped.
                result["rank"] = previous["rank"]
            previous = result
        results = category.get("results") or []
        for index in range(1, len(results) - 1):
            previous, result, following = results[index - 1:index + 2]
            if ((result.get("resultKind") or "individual") == "individual"
                    and result.get("rank") is None
                    and result.get("status") == "ok"
                    and result.get("timeS") is not None
                    and not result.get("outOfCompetition")
                    and previous.get("rank") is not None
                    and following.get("rank") == previous.get("rank") + 2):
                # A title announcement split over several visual PDF rows can
                # leave exactly one ordinary finisher between consecutive
                # printed placements without its own rank (5, blank, 7).
                result["rank"] = previous["rank"] + 1
    return categories


def normalize_qualitative_result_ranks(categories):
    """A qualitative ``gut`` participation result has no placement."""
    for category in categories or []:
        for result in category.get("results") or []:
            if re.search(
                    r"(?i)(?:(?:sehr\s+)?gut|teilg|(?:erfolgreich\s+)?teilgenommen)\s*$",
                    result.get("timeText") or ""):
                result.pop("rank", None)
    return categories

# A relay team row can carry its champion announcement inline, ahead of the
# real team name/time ("1 und ÖM Naturfreunde Wien 1 35:06") - unlike a
# plain announcement-only row, parse_champion_annotation() deliberately
# refuses this shape (a time token follows "und", its usual signal that the
# row is NOT a pure announcement - see TIME_TOKEN_IN_ANNOT_RE's docstring),
# so it has to be peeled off here before the rest of the line reaches
# parse_flow_row(), or "und ÖM" becomes stuck to the front of the team name.
RELAY_TEAM_ANNOT_RE = re.compile(
    r"^(?P<rank>\d+)\.?\s+und\s+(?P<title>ÖM|ÖSTM|"
    r"staats?meister(?:in)?|"
    r"öster(?:r|reich\w*)?\.?\s*(?:staats?)?meister(?:in)?)\s+(?=\S)", re.I)

# The "Pl Text1 Staffel Name Jg Zeit W Zeit" header (see RELAY_HEADER_RE)
# prints each member's OWN leg time and the team's cumulative time after
# that leg side by side ("Wolfgang Waldhäusl 71 11:40 11:40") - a second
# trailing time column parse_flow_row() was never built to expect (its
# original 'Hartberger Peter 13 13:28' shape - see this function's
# docstring - has only the one). Left alone, the extra token doesn't get
# peeled at all, so the own-leg-time stays stuck onto the name instead of
# being recognized as the time. Only the OWN leg time (first of the two) is
# kept - the cumulative running total isn't tracked anywhere in this schema.
MEMBER_TWO_TIME_RE = re.compile(
    r"^(?P<body>.+?\s\d{1,3}:\d{2}(?::\d{2})?)\s+\d{1,3}:\d{2}(?::\d{2})?$")

# A narrow Verein column can place the immediately following time directly
# against the final club word ("Orienteering Klosterneuburg45:35"). It is
# unambiguous only at the end of the club cell and only when the actual time
# cell is empty.
CLUB_TIME_SUFFIX_RE = re.compile(r"(?P<time>\d{1,3}:\d{2}(?::\d{2})?)$")

# A "Pl Staffel Zeit Diff." header (see RELAY_HEADER_RE) prints the team's
# gap behind the leader right after its own finish time ("3 Naturfreunde
# Wien 1 1:45:25 +19:13" - the leader's own row instead gets a bare "0:00").
# parse_flow_row()'s TIME_RE requires an exact "H:MM:SS" match with no sign,
# so the leading "+" on every non-leader row's Diff value makes the WHOLE
# trailing token fail to parse as a time OR a status - with nothing left
# recognizable as this row's result, the row silently vanishes rather than
# just losing the diff value. Confirmed real: event 3633 ("ÖSTM und ÖM
# Staffel" 2022) dropped every team but the category leader entirely,
# Naturfreunde Wien's real H-14 bronze relay among them.
TEAM_DIFF_SUFFIX_RE = re.compile(
    r"^(?P<body>.+?\s\d{1,3}:\d{2}(?::\d{2})?)\s+\+?\d{1,3}:\d{2}(?::\d{2})?$")


def group_lines(words):
    """Cluster words sharing (approximately) the same vertical position."""
    lines = []
    for word in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if not lines or abs(word["top"] - lines[-1][0]) > LINE_TOLERANCE:
            lines.append([word["top"], [word]])
        else:
            bucket = lines[-1][1]
            bucket.append(word)
            # Track the cluster's mean instead of quantizing into arbitrary
            # 3px bins. Words at 145.33 and 145.57 are visibly on the same
            # row but used to land on opposite sides of a round() boundary,
            # separating a runner's name from rank and times.
            lines[-1][0] = sum(item["top"] for item in bucket) / len(bucket)
    for _top, line in lines:
        yield sorted(line, key=lambda w: w["x0"])


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


def normalize_broken_result_value(value):
    """Repair glyph spacing produced by a few old embedded PDF fonts."""
    value = (value or "").strip()
    value = re.sub(r"^(\d)\s+(\d:\d{2}(?::\d{2})?)$", r"\1\2", value)
    value = re.sub(r"\b(\d{1,2}):\s+(\d{2}):(\d{2})\b", r"\1:\2:\3", value)
    value = re.sub(r"\b(\d{1,2}):(\d)\s+(\d):(\d{2})\b", r"\1:\2\3:\4", value)
    value = re.sub(r"\bF\s+ehlst\b", "Fehlst", value, flags=re.I)
    value = re.sub(r"\bA\s+ufg\b", "Aufg", value, flags=re.I)
    value = re.sub(r"\bD\s+isqu\b", "Disqu", value, flags=re.I)
    value = re.sub(r"^Fn\.e\s+Èhl\.sl\.t$", "Fehlst", value, flags=re.I)
    return value


def repair_interleaved_club_value(club, value, force=False):
    """Recover a long club suffix interleaved with the result value.

    Some embedded PDF fonts place a long Verein value and the adjacent Zeit
    value at the same x positions, so pdfplumber orders their glyphs like
    ``INNSBRUCK 3IM8:S2T8`` (``INNSBRUCK IMST`` + ``38:28``) or
    ``Orienteeri1n2g:13,00`` (``Orienteering`` + ``12:13,00``).  Match the
    observed club prefix against the committed club dictionary, remove the
    missing suffix as a character subsequence, and keep the remaining
    time/status.  Requiring several suffix characters and a parseable result
    prevents this from changing ordinary free text.
    """
    club = re.sub(r"\s+", " ", (club or "")).strip()
    value = (value or "").strip()
    if (not club or not value
            or (not force and (parse_time_loose(value) is not None or parse_status(value)))):
        return club, value

    def letters(text):
        folded = (text or "").casefold().replace("ä", "ae").replace("ö", "oe") \
            .replace("ü", "ue").replace("ß", "ss")
        return "".join(ch for ch in folded if ch.isalpha())

    candidates = []
    club_variants = [club]
    # A country code can itself be interleaved across the club/value
    # boundary (``... SE-MOMH`` + ``TUáNjfutó1 S:4z2a:5ko3``). Trying the
    # club without that one stray uppercase glyph lets the canonical suffix
    # consume Tájfutó Szako and leaves ``HUN 1:42:53`` to normalize below.
    if len(club) > 1 and club[-1].isupper() and club[-2].isalpha():
        club_variants.append(club[:-1].rstrip())
    for club_variant in club_variants:
        club_letters = letters(club_variant)
        for spelling, canonical in CLUBS.items():
            if re.search(r"(?i)\bempty\b", canonical):
                continue
            full_letters = letters(spelling)
            if (not full_letters.startswith(club_letters)
                    or len(full_letters) <= len(club_letters)):
                continue
            if spelling.casefold().startswith(club_variant.casefold()):
                suffix = "".join(
                    ch.casefold() for ch in spelling[len(club_variant):]
                    if ch.isalpha())
            else:
                suffix = full_letters[len(club_letters):]
            kept = []
            at = consumed = 0
            for char in value:
                if (at < len(suffix) and char.isalpha()
                        and char.casefold() == suffix[at]):
                    at += 1
                    consumed += 1
                else:
                    kept.append(char)
            # Truncated PDF columns need not contain the final suffix letters,
            # but short accidental matches are too weak to be trustworthy.
            if consumed < (1 if len(club_letters) >= 12 else 5):
                continue
            cleaned = re.sub(r"\s+", " ", "".join(kept)).strip()
            cleaned = re.sub(r"^[\s\-–—]+|[\s\-–—]+$", "", cleaned)
            cleaned = re.sub(r"^[.,;:]\s*", "", cleaned)
            cleaned = normalize_broken_result_value(cleaned)
            # The nation can be woven between the club suffix and the first hour
            # digit (``H UN1 :42:53``). It is metadata, not part of either field.
            cleaned = re.sub(
                r"^[A-Z\s]{2,6}(\d)\s*:\s*(\d{2}:\d{2})$", r"\1:\2",
                cleaned, flags=re.I)
            status = parse_status(cleaned)
            # In a handful of embedded fonts the final status character and the
            # final club-suffix character occupy the exact same glyph position;
            # pdfplumber returns it only once.  ``Gu`` + suffix ``...t`` and
            # ``Fehls`` + ``...t`` are therefore still unambiguous.
            if not status and suffix and parse_status(cleaned + suffix[-1]):
                cleaned += suffix[-1]
                status = parse_status(cleaned)
            time_match = re.search(r"(?<!\d)(\d{1,3}):(\d{2})(?::(\d{2}))?", cleaned)
            if time_match:
                repaired = time_match.group(0)
            elif ":" in cleaned:
                parts = cleaned.split(":")
                digit_parts = ["".join(ch for ch in part if ch.isdigit())
                               for part in parts]
                if (len(digit_parts) >= 2 and digit_parts[0]
                        and len(digit_parts[1]) >= 2):
                    repaired = f"{digit_parts[0][-3:]}:{digit_parts[1][:2]}"
                    if len(digit_parts) >= 3 and len(digit_parts[2]) >= 2:
                        repaired += f":{digit_parts[2][:2]}"
                else:
                    continue
            elif status:
                repaired = cleaned
            else:
                continue
            candidates.append((consumed, len(suffix), canonical, repaired))

    if not candidates:
        return club, value
    # Prefer the candidate that explains the most overlapping glyphs.  For a
    # tie, retain the longest canonical spelling: the club dictionary also
    # contains historically clipped aliases (``... - Oriente``), but this
    # repair has just proven the corresponding full-name continuation.
    _consumed, _suffix_len, canonical, repaired = max(
        candidates, key=lambda item: (item[0], item[1]))
    return canonical, normalize_broken_result_value(repaired)


def repair_result_club_and_value(club, value):
    """Repair both normal overflow and a value already embedded in Verein."""
    # A qualitative result/status can begin only after the missing tail of a
    # clipped club: ``HSV OL Wiener`` + ``Neustad Gut``.  parse_status(value)
    # quite correctly recognizes the final Gut, but treating the whole cell
    # as a status used to leave ``Neustad`` displayed as part of the result.
    # An uppercase, non-numeric prefix is strong evidence of a club suffix;
    # use the registry when it proves a full spelling and otherwise preserve
    # the visible foreign-club continuation verbatim.
    status_tail = STATUS_TAIL_RE.search((value or "").strip())
    if status_tail and status_tail.end() == len((value or "").strip()):
        raw_value = (value or "").strip()
        displaced = raw_value[:status_tail.start()].strip(" ,;:-")
        if displaced and re.fullmatch(r"[A-ZÀ-Þ][A-Za-zÀ-ž.'’\-]*", displaced):
            joined = re.sub(r"\s+", " ", f"{club} {displaced}").strip()
            canonical = CLUBS.get(joined.casefold())
            if not canonical:
                continuations = [
                    candidate for spelling, candidate in CLUBS.items()
                    if spelling.startswith(joined.casefold())
                    and len(spelling) - len(joined.casefold()) <= 4
                ]
                canonical = max(continuations, key=len) if continuations else joined
            return canonical, raw_value[status_tail.start():].strip()
        if (displaced.casefold() == "(no"
                and (club or "").casefold() in {"vereinslos", "no club"}):
            return "vereinslos (no club)", raw_value[status_tail.start():].strip()
    if (not (value or "").strip()
            and re.fullmatch(r"Leibnitzer AC OrientierungslaFuehlst", club or "", re.I)):
        return "Leibnitzer AC - Orienteering", "Fehlst"
    if not (value or "").strip():
        embedded_status = re.search(
            r"(?i)(?P<status>a.?ufg\.?|f.?ehlst\.?)\s*$", club or "")
        if embedded_status:
            repaired_status = ("Aufg" if "ufg" in embedded_status.group("status").casefold()
                               else "Fehlst")
            return club[:embedded_status.start()].rstrip(" ,;:-"), repaired_status
    # Flow reconstruction can already have moved the detached glyph into the
    # value while preserving its separating whitespace (``1 :01:07`` or
    # ``5 1:16``). Normalize that representation before the club/value
    # boundary repairs below.
    spaced_hour = re.fullmatch(r"(\d)\s+:\s*(\d{2}:\d{2})", (value or "").strip())
    if spaced_hour:
        return club, f"{spaced_hour.group(1)}:{spaced_hour.group(2)}"
    spaced_prefix = re.fullmatch(r"(\d)\s+(\d{1,2}:\d{2})", (value or "").strip())
    club_known = ((club or "").casefold() in CLUBS
                  or (club or "").casefold() in CLUB_CANONICAL_KEYS)
    if spaced_prefix and club_known:
        rest = spaced_prefix.group(2)
        fixed = (f"{spaced_prefix.group(1)}{rest}"
                 if len(rest.split(":", 1)[0]) == 1
                 else f"{spaced_prefix.group(1)}:{rest}")
        return CLUBS.get((club or "").casefold(), club), fixed
    repaired_club, repaired_value = repair_interleaved_club_value(club, value)
    if repaired_club != club or repaired_value != value:
        return repaired_club, repaired_value
    # An exact known club plus an already recognizable status needs no
    # boundary repair.  Otherwise the shorter aliases ``OLC`` and
    # ``OLG Ströck`` can incorrectly win for ``OLC Graz`` and
    # ``OLG Ströck Wien`` merely because the status follows them.
    exact_known = CLUBS.get((club or "").casefold())
    if exact_known and parse_status(value or ""):
        return exact_known, value
    # A status can start immediately after an otherwise complete club in the
    # Verein cell while its tail lands in Zeit (``... KlosterneuburgN`` +
    # ``Ang``).  Prefer the longest exact canonical prefix.
    exact_prefixes = [
        canonical for canonical in set(CLUBS.values())
        if "empty" not in canonical.casefold()
        and (club or "").casefold().startswith(canonical.casefold())
        and len(club or "") > len(canonical)
    ]
    for canonical in sorted(exact_prefixes, key=len, reverse=True):
        combined = f"{club[len(canonical):]} {value}".strip()
        status = parse_status(combined)
        if status:
            display = {"dns": "N Ang", "dnf": "Aufg", "mp": "Fehlst",
                       "dsq": "Disqu", "ok": combined}.get(status, combined)
            return canonical, display
    # Old embedded fonts sometimes put the first elapsed-time digit at the
    # right edge of Verein. An explicit trailing colon is conclusive
    # (``HSV Spittal/Drau 1 :`` + ``01:07`` -> ``1:01:07``). Without the
    # colon, repair only when removing that digit reveals a known club and
    # the remaining value is implausibly below ten minutes; one-digit minute
    # values carry the missing tens digit (``5`` + ``1:16`` -> ``51:16``),
    # while two-digit values carry a missing hour (``3`` + ``07:53`` ->
    # ``3:07:53``).
    split_time = re.fullmatch(
        r"(?P<base>.+?)\s+(?P<prefix>\d)\s*(?P<colon>:)?\s*", club or "")
    value_time = re.fullmatch(r"(?P<minutes>\d{1,2}):(?P<seconds>\d{2})",
                              (value or "").strip())
    if split_time and value_time:
        base = split_time.group("base").strip()
        canonical = CLUBS.get(base.casefold())
        explicit = split_time.group("colon") is not None
        seconds = parse_time(value or "")
        if explicit or (canonical and seconds is not None and seconds < 600):
            if explicit or len(value_time.group("minutes")) == 2:
                fixed_value = f"{split_time.group('prefix')}:{value.strip()}"
            else:
                fixed_value = f"{split_time.group('prefix')}{value.strip()}"
            return canonical or base, fixed_value
    # Sometimes the column boundary falls *inside* the overlapping text, so
    # the first time digits are already attached to Verein and only the tail
    # sits in Zeit (``Kaltenbr1u:n0n`` + ``2:33`` -> ``1:02:33``).
    intrusion = re.search(r"\d", club or "")
    if not intrusion:
        return repaired_club, repaired_value
    prefix = club[:intrusion.start()].rstrip()
    overflow = club[intrusion.start():]
    # Do not move a digit woven into a club code here. Values such as
    # ``Hungary X2S Team`` + ``52:25`` and ``E141 Atominstitut`` + ``5:52``
    # are already valid. The category-level rank-order repair below has the
    # context needed to distinguish those from genuinely interleaved time
    # glyphs such as ``...1F:u2t`` + ``9:22``.
    if ":" in overflow:
        marker = re.sub(r"[^0-9:]", "", overflow)
        woven_value = marker + (value or "").strip()
        if parse_time_loose(woven_value) is not None and re.fullmatch(
                r"\d{1,3}:\d{2}(?::\d{2})?", woven_value):
            clean_club = re.sub(r"[0-9:]", "", club)
            clean_club = re.sub(r"\s+", " ", clean_club).strip()
            if clean_club.startswith("Naturfreunde Wien - Orient"):
                clean_club = "Naturfreunde Wien"
            elif clean_club.casefold() == "ztc zalaegerszegi tájékozódási fut":
                clean_club = "ZTC Zalaegerszegi Fut"
            else:
                clean_club = CLUBS.get(clean_club.casefold(), clean_club)
            return clean_club, woven_value
        exact_prefixes = [
            spelling for spelling, canonical in CLUBS.items()
            if len(spelling) >= 5 and "empty" not in canonical.casefold()
            and club.casefold().startswith(spelling)
        ]
        if exact_prefixes:
            stable_prefix = max(exact_prefixes, key=len)
            boundary = len(stable_prefix)
            fixed_club, fixed_value = repair_interleaved_club_value(
                club[:boundary], f"{club[boundary:]} {value}".strip(), force=True)
        else:
            fixed_club, fixed_value = repair_interleaved_club_value(
                prefix, f"{overflow} {value}".strip(), force=True)
        clean_result = (bool(parse_status(fixed_value))
                        or bool(re.fullmatch(r"\d{1,3}:\d{2}(?::\d{2})?", fixed_value)))
        if clean_result and (fixed_club not in (club, prefix) or fixed_value != value):
            return fixed_club, fixed_value
    if parse_time_loose(value) is not None:
        return club, value
    combined = f"{overflow} {value}".strip()
    fixed_club, fixed_value = repair_interleaved_club_value(prefix, combined, force=True)
    return (fixed_club, fixed_value) if fixed_club != prefix else (club, value)


def repair_shifted_name_club_time(result):
    """Undo a whole-column shift in narrow PDF result rows.

    A missing/overflowing club cell can leave ``Name`` ending in the first
    club words, ``Verein`` ending in the actual finish time, and ``Zeit``
    containing only the gap behind the winner. Rejoin name plus the non-time
    prefix, then accept the repair only when the club dictionary proves a
    trailing club and at least two person-name tokens remain.
    """
    club = (result.get("club") or "").strip()
    name = (result.get("name") or "").strip()
    # A narrow Name column can keep only the surname while the given name
    # spills into the front of Verein (``Kaltenbacher | Pierre HSV OL Wiener
    # Neustadt``).  A known club at the tail plus exactly one leading token is
    # strong enough to restore the boundary without guessing.
    if len(name.split()) == 1 and club:
        repaired_club, leading_tokens = find_trailing_club(club.split(), CLUBS)
        if (repaired_club and len(leading_tokens) == 1
                and re.fullmatch(r"[A-Za-zÀ-ž][A-Za-zÀ-ž'’-]+", leading_tokens[0])):
            result["name"] = f"{name} {leading_tokens[0]}"
            result["club"] = repaired_club
            club = repaired_club
    match = CLUB_TIME_SUFFIX_RE.search(club)
    if not match:
        return result
    combined = f"{result.get('name') or ''} {club[:match.start()].strip()}".strip()
    repaired_club, name_tokens = find_trailing_club(combined.split(), CLUBS)
    if not repaired_club or len(name_tokens) < 2:
        return result
    prior_value = (result.get("timeText") or "").strip()
    result["name"] = " ".join(name_tokens)
    result["club"] = repaired_club
    result["timeText"] = match.group("time")
    if prior_value:
        result["timeBehindText"] = prior_value
    seconds = parse_time_loose(result["timeText"])
    if seconds is not None:
        result["timeS"] = seconds
        result["status"] = "ok"
    return result


def repair_rank_order_embedded_time_markers(results):
    """Recover time digits interleaved inside the final club word.

    Multilingual OE2010 PDFs can weave the first time glyphs into a club's
    last word: ``Naturfreunde OL4G`` + ``3:47`` is ``Naturfreunde OLG`` +
    ``43:47``; ``Zalaegerszegi 1F:u2t`` + ``9:22`` is ``... Fut`` +
    ``1:29:22``.  Unlike dictionary-based repair, this works for foreign club
    names too.  It is accepted only when the current value violates rank/time
    order and the reconstructed value fits between the nearest better and
    worse ranks.  That independent ranking constraint keeps ordinary digits
    in school/team names untouched.
    """
    stable_ranked = [row for row in results
                     if isinstance(row.get("rank"), int)
                     and isinstance(row.get("timeS"), int)
                     and not row.get("outOfCompetition")]

    # When both neighbouring columns overlap completely, the extracted value
    # may no longer look like a time at all: ``...Gymnasium3`` + ``8S:a20``
    # is visually ``...Gymnasium`` + ``38:20``.  Recover such rows only when
    # the digits/colons form one exact time and that time fits the independent
    # ordering constraints supplied by unaffected ranked rows.  Consecutive
    # damaged rows additionally constrain one another, which repairs a run
    # without making guesses from the garbled glyphs alone.
    unparsed_candidates = []
    for row in results:
        if (not isinstance(row.get("rank"), int)
                or isinstance(row.get("timeS"), int)
                or row.get("outOfCompetition")):
            continue
        club = row.get("club") or ""
        value = row.get("timeText") or ""
        if ":" not in value or not re.search(r"[A-Za-z]", value):
            continue
        intrusion = re.search(r"\d", club)
        if intrusion:
            prefix, suffix = club[:intrusion.start()], club[intrusion.start():]
            candidate_text = (re.sub(r"[^0-9:]", "", suffix)
                              + re.sub(r"[^0-9:]", "", value))
            repaired_club = re.sub(
                r"\s+", " ", prefix + re.sub(r"[0-9:]", "", suffix)).strip()
        else:
            candidate_text = re.sub(r"[^0-9:]", "", value)
            displaced_club = re.sub(r"[0-9:]", "", value)
            displaced_club = re.sub(r"\s+", " ", displaced_club).strip()
            if len(re.sub(r"[^A-Za-z]", "", displaced_club)) < 4:
                continue
            repaired_club = f"{club} {displaced_club}".strip()
        candidate_seconds = parse_time(candidate_text)
        if candidate_seconds is not None:
            unparsed_candidates.append(
                (row, repaired_club, candidate_text, candidate_seconds))

    for row, repaired_club, candidate_text, candidate_seconds in unparsed_candidates:
        stable_better = [other["timeS"] for other in stable_ranked
                         if other["rank"] < row["rank"]]
        stable_worse = [other["timeS"] for other in stable_ranked
                        if other["rank"] > row["rank"]]
        peers_before = [seconds for other, _club, _text, seconds in unparsed_candidates
                        if other["rank"] < row["rank"]]
        peers_after = [seconds for other, _club, _text, seconds in unparsed_candidates
                       if other["rank"] > row["rank"]]
        fits = ((not stable_better or candidate_seconds >= max(stable_better))
                and (not stable_worse or candidate_seconds <= min(stable_worse))
                and (not peers_before or candidate_seconds >= max(peers_before))
                and (not peers_after or candidate_seconds <= min(peers_after)))
        if fits and (stable_better or stable_worse):
            row["club"] = repaired_club
            row["timeText"] = candidate_text
            row["timeS"] = candidate_seconds
            row["status"] = "ok"

    ranked = [row for row in results
              if isinstance(row.get("rank"), int)
              and isinstance(row.get("timeS"), int)
              and not row.get("outOfCompetition")]
    for row in ranked:
        club = row.get("club") or ""
        value = row.get("timeText") or ""
        intrusion = re.search(r"\d(?=[^\W\d_]|:)|(?<=[^\W\d_])\d", club)
        if not intrusion or not re.fullmatch(r"\d{1,2}:\d{2}", value.strip()):
            continue
        prefix, suffix = club[:intrusion.start()], club[intrusion.start():]
        marker = re.sub(r"[^0-9:]", "", suffix)
        candidate_text = marker + value.strip()
        candidate_seconds = parse_time_loose(candidate_text)
        if candidate_seconds is None or candidate_seconds == row["timeS"]:
            continue

        rank = row["rank"]
        better_ranks = [other["rank"] for other in ranked if other["rank"] < rank]
        worse_ranks = [other["rank"] for other in ranked if other["rank"] > rank]
        lower = None
        upper = None
        if better_ranks:
            nearest = max(better_ranks)
            lower = max(other["timeS"] for other in ranked
                        if other["rank"] == nearest)
        if worse_ranks:
            nearest = min(worse_ranks)
            upper = min(other["timeS"] for other in ranked
                        if other["rank"] == nearest)
        current_bad = ((lower is not None and row["timeS"] < lower)
                       or (upper is not None and row["timeS"] > upper))
        candidate_fits = ((lower is None or candidate_seconds >= lower)
                          and (upper is None or candidate_seconds <= upper))
        if not current_bad or not candidate_fits or (lower is None and upper is None):
            continue

        clean_suffix = re.sub(r"[0-9:]", "", suffix)
        row["club"] = re.sub(r"\s+", " ", prefix + clean_suffix).strip()
        row["timeText"] = candidate_text
        row["timeS"] = candidate_seconds
        row["status"] = "ok"

    # As with split tens-minute digits, two consecutive woven rows cannot use
    # each other's still-broken values as bounds. Validate the reconstructed
    # run against unaffected placements and against its own candidate order.
    woven_candidates = []
    for row in ranked:
        club = row.get("club") or ""
        value = row.get("timeText") or ""
        intrusion = re.search(r"\d(?=[^\W\d_]|:)|(?<=[^\W\d_])\d", club)
        if not intrusion or not re.fullmatch(r"\d{1,2}:\d{2}", value.strip()):
            continue
        prefix, suffix = club[:intrusion.start()], club[intrusion.start():]
        candidate_text = re.sub(r"[^0-9:]", "", suffix) + value.strip()
        candidate_seconds = parse_time_loose(candidate_text)
        if candidate_seconds is None:
            continue
        clean_suffix = re.sub(r"[0-9:]", "", suffix)
        clean_club = re.sub(r"\s+", " ", prefix + clean_suffix).strip()
        clean_club = CLUBS.get(clean_club.casefold(), clean_club)
        woven_candidates.append((row, clean_club, candidate_text, candidate_seconds))
    woven_ids = {id(item[0]) for item in woven_candidates}
    for row, clean_club, candidate_text, candidate_seconds in woven_candidates:
        stable_better = [other["timeS"] for other in ranked
                         if id(other) not in woven_ids and other["rank"] < row["rank"]]
        stable_worse = [other["timeS"] for other in ranked
                        if id(other) not in woven_ids and other["rank"] > row["rank"]]
        peers_before = [seconds for other, _club, _text, seconds in woven_candidates
                        if other["rank"] < row["rank"]]
        peers_after = [seconds for other, _club, _text, seconds in woven_candidates
                       if other["rank"] > row["rank"]]
        fits = ((not stable_better or candidate_seconds >= max(stable_better))
                and (not stable_worse or candidate_seconds <= min(stable_worse))
                and (not peers_before or candidate_seconds >= max(peers_before))
                and (not peers_after or candidate_seconds <= min(peers_after)))
        current_bad = ((stable_better and row["timeS"] < max(stable_better))
                       or (stable_worse and row["timeS"] > min(stable_worse)))
        if fits and current_bad:
            row["club"] = clean_club
            row["timeText"] = candidate_text
            row["timeS"] = candidate_seconds
            row["status"] = "ok"

    # A second rendering defect puts only the missing tens-minute digit at
    # the right edge of Verein (``... Kaltenbrunn 1`` + ``6:51`` is
    # ``... Kaltenbrunn`` + ``16:51``).  Club-dictionary repair cannot prove
    # this for the many deliberately letter-spaced OE2010 club names.  Rank
    # order can: accept the digit only when the short value is currently out
    # of order and the reconstructed value falls between its nearest better
    # and worse placements.  This also keeps real club suffixes such as
    # ``Team 1`` intact whenever the printed time is already plausible.
    # Two passes let adjacent rows with the same defect establish each
    # other's corrected bounds (Family rank 3/4 in event 1487).
    for _pass in range(2):
        for row in ranked:
            club = row.get("club") or ""
            value = row.get("timeText") or ""
            split = re.fullmatch(r"(?P<club>.+?)\s+(?P<prefix>\d)\s*", club)
            leading_hour = re.fullmatch(r"(?P<club>.+?)\s*:\s*", club)
            value_match = re.fullmatch(r"(?P<minutes>\d{1,2}):(?P<seconds>\d{2})",
                                       value.strip())
            if value_match and split and len(value_match.group("minutes")) == 1:
                candidate_text = split.group("prefix") + value.strip()
                repaired_club = split.group("club").strip()
            elif value_match and leading_hour:
                candidate_text = "1:" + value.strip()
                repaired_club = leading_hour.group("club").strip()
            else:
                continue
            candidate_seconds = parse_time_loose(candidate_text)
            if candidate_seconds is None:
                continue
            rank = row["rank"]
            better_ranks = [other["rank"] for other in ranked if other["rank"] < rank]
            worse_ranks = [other["rank"] for other in ranked if other["rank"] > rank]
            lower = (max(other["timeS"] for other in ranked
                         if other["rank"] == max(better_ranks))
                     if better_ranks else None)
            upper = (min(other["timeS"] for other in ranked
                         if other["rank"] == min(worse_ranks))
                     if worse_ranks else None)
            current_bad = ((lower is not None and row["timeS"] < lower)
                           or (upper is not None and row["timeS"] > upper))
            candidate_fits = ((lower is None or candidate_seconds >= lower)
                              and (upper is None or candidate_seconds <= upper))
            if not current_bad or not candidate_fits or (lower is None and upper is None):
                continue
            row["club"] = repaired_club
            row["timeText"] = candidate_text
            row["timeS"] = candidate_seconds
            row["status"] = "ok"

    # Consecutive damaged values cannot validate one another one-by-one: the
    # following broken row is itself an invalid upper bound. Validate such a
    # run against the nearest unaffected placements on either side instead.
    candidates = []
    for row in ranked:
        club = row.get("club") or ""
        value = row.get("timeText") or ""
        split = re.fullmatch(r"(?P<club>.+?)\s+(?P<prefix>\d)\s*", club)
        leading_hour = re.fullmatch(r"(?P<club>.+?)\s*:\s*", club)
        value_match = re.fullmatch(r"(?P<minutes>\d{1,2}):(?P<seconds>\d{2})",
                                   value.strip())
        if value_match and split and len(value_match.group("minutes")) == 1:
            candidate_text = split.group("prefix") + value.strip()
            repaired_club = split.group("club").strip()
        elif value_match and leading_hour:
            candidate_text = "1:" + value.strip()
            repaired_club = leading_hour.group("club").strip()
        else:
            continue
        candidate_seconds = parse_time_loose(candidate_text)
        if candidate_seconds is not None:
            candidates.append((row, repaired_club, candidate_text, candidate_seconds))
    candidate_ids = {id(item[0]) for item in candidates}
    for row, repaired_club, candidate_text, candidate_seconds in candidates:
        stable_better = [other["timeS"] for other in ranked
                         if id(other) not in candidate_ids and other["rank"] < row["rank"]]
        stable_worse = [other["timeS"] for other in ranked
                        if id(other) not in candidate_ids and other["rank"] > row["rank"]]
        peers_before = [seconds for other, _club, _text, seconds in candidates
                        if other["rank"] < row["rank"]]
        peers_after = [seconds for other, _club, _text, seconds in candidates
                       if other["rank"] > row["rank"]]
        fits = ((not stable_better or candidate_seconds >= max(stable_better))
                and (not stable_worse or candidate_seconds <= min(stable_worse))
                and (not peers_before or candidate_seconds >= max(peers_before))
                and (not peers_after or candidate_seconds <= min(peers_after)))
        current_bad = ((stable_better and row["timeS"] < max(stable_better))
                       or (stable_worse and row["timeS"] > min(stable_worse)))
        if fits and current_bad:
            row["club"] = repaired_club
            row["timeText"] = candidate_text
            row["timeS"] = candidate_seconds
            row["status"] = "ok"

    # Once the interleaved club suffix has been canonicalized, the displaced
    # hour glyph may no longer be visible in the club text. Ranking order can
    # still prove it: ``42:53`` after a rank-four ``1:34:33`` can only be the
    # printed ``1:42:53``. Require the reconstructed value to fit both nearest
    # rank bounds, so an ordinary sub-hour race is never changed.
    for row in ranked:
        value = (row.get("timeText") or "").strip()
        if not re.fullmatch(r"\d{2}:\d{2}", value):
            continue
        rank = row["rank"]
        better = [other["timeS"] for other in ranked if other["rank"] < rank]
        worse = [other["timeS"] for other in ranked if other["rank"] > rank]
        if not better or row["timeS"] >= max(better):
            continue
        candidate_text = f"1:{value}"
        candidate_seconds = parse_time_loose(candidate_text)
        if (candidate_seconds is not None
                and candidate_seconds >= max(better)
                and (not worse or candidate_seconds <= min(worse))):
            row["timeText"] = candidate_text
            row["timeS"] = candidate_seconds
            row["status"] = "ok"

    # A legitimate final club digit can instead have been pulled into an
    # already three-digit minute value (``... Sportclub 190`` + ``147:59``).
    # Put it back only when it completes a known club and the shorter time
    # fits the neighboring ranks.
    for row in ranked:
        value_match = re.fullmatch(r"(?P<prefix>\d)(?P<time>\d{2}:\d{2})",
                                   (row.get("timeText") or "").strip())
        if not value_match:
            continue
        completed = f"{row.get('club') or ''}{value_match.group('prefix')}"
        canonical = CLUBS.get(completed.casefold())
        candidate_seconds = parse_time_loose(value_match.group("time"))
        if not canonical or candidate_seconds is None:
            continue
        rank = row["rank"]
        better = [other["timeS"] for other in ranked if other["rank"] < rank]
        worse = [other["timeS"] for other in ranked if other["rank"] > rank]
        if ((not better or candidate_seconds >= max(better))
                and (not worse or candidate_seconds <= min(worse))):
            row["club"] = canonical
            row["timeText"] = value_match.group("time")
            row["timeS"] = candidate_seconds


def normalize_championship_overall_ranks(categories):
    """Recover overall ranks when national ranks are interleaved in ``Pl``.

    Older championship PDFs print foreign runners with their overall place,
    while eligible Austrians can instead carry a dotted ÖM/ÖSTM place in the
    same column.  Treating both numbers as one ranking creates duplicates and
    time inversions.  If the source row order itself is a complete monotonic
    elapsed-time ranking and at least one champion annotation proves this
    layout, derive the unambiguous overall competition rank from that order.
    """
    for category in categories:
        results = category.get("results") or []
        if not any(row.get("championship") for row in results):
            continue
        ranked = [row for row in results
                  if row.get("status") == "ok"
                  and isinstance(row.get("timeS"), int)
                  and not row.get("outOfCompetition")
                  and row.get("resultKind", "individual") == "individual"]
        if len(ranked) < 2:
            continue
        times = [row["timeS"] for row in ranked]
        if times != sorted(times):
            continue
        existing = [row.get("rank") for row in ranked]
        if existing == sorted(existing) and len(existing) == len(set(existing)):
            continue
        prior_time = None
        prior_rank = None
        for index, row in enumerate(ranked, 1):
            rank = prior_rank if row["timeS"] == prior_time else index
            row["rank"] = rank
            prior_time, prior_rank = row["timeS"], rank


def normalize_rank_time_consensus(categories):
    """Use an exact same-rank time majority to repair one PDF outlier."""
    for category in categories:
        grouped = defaultdict(list)
        for row in category.get("results") or []:
            if (row.get("status") == "ok" and isinstance(row.get("rank"), int)
                    and isinstance(row.get("timeS"), int)
                    and row.get("resultKind", "individual") == "individual"):
                grouped[row["rank"]].append(row)
        for rows in grouped.values():
            counts = defaultdict(list)
            for row in rows:
                counts[row["timeS"]].append(row)
            consensus = max(counts.items(), key=lambda item: len(item[1]))
            if len(consensus[1]) < 2 or len(counts) < 2:
                continue
            consensus_seconds, peers = consensus
            consensus_text = peers[0].get("timeText") or ""
            for row in rows:
                if row["timeS"] != consensus_seconds:
                    row["timeS"] = consensus_seconds
                    row["timeText"] = consensus_text


def parse_mannschaft_prefix(line_words, first_member_x):
    """Read Pl/AK, club and the one shared team time before Text1.

    Long club names legitimately overflow the narrow Verein column into the
    visual Zeit column. Locating the actual time/status token first preserves
    the complete printed club fragment instead of turning "Orientee" or
    "Neustad" into part of the time value.
    """
    tokens = [word["text"] for word in line_words if word["x0"] < first_member_x - 1]
    rank_text = ""
    if tokens and (tokens[0].rstrip(".").isdigit() or is_ooc_status(tokens[0])):
        rank_text = tokens.pop(0).rstrip(".")
    value_at = value_text = None
    for index, token in enumerate(tokens):
        if FLOW_TIME_RE.fullmatch(token.lstrip("+")):
            value_at, value_text = index, token
            break
    if value_at is None:
        # A status is the suffix after the club.  parse_status() deliberately
        # recognizes phrases embedded in longer text, so testing arbitrary
        # slices would misread "ASKÖ Henndorf Orientee N Ang" as one giant
        # status and truncate the club.  Prefer the shortest recognized suffix
        # ("N Ang", "Fehlst", "Disqu", ...).
        for width in (1, 2, 3):
            candidate = " ".join(tokens[-width:])
            # Some clipped result cells contain only ``Ang`` and are valid
            # DNS aliases.  In a normal Mannschaft row, however, ``N Ang``
            # is the complete two-token status; do not leave its ``N`` stuck
            # to the club while trying the shortest suffix first.
            if (width == 1 and candidate.casefold().rstrip(".") == "ang"
                    and len(tokens) >= 2
                    and tokens[-2].casefold().rstrip(".") == "n"):
                continue
            if candidate and parse_status(candidate):
                value_at, value_text = len(tokens) - width, candidate
                break
    if value_at is None:
        return rank_text, " ".join(tokens).strip(), ""
    return rank_text, " ".join(tokens[:value_at]).strip(), value_text


def parse_two_column_pdf(path):
    """Parse newspaper-style PDFs containing two independent tables/page.

    Plain extraction interleaves the rows (HA rank 14, DB rank 1, HA rank
    15, DB rank 2). Splitting at the visual midpoint gives both table streams
    their own category state and prevents one class absorbing the other.
    ``None`` means the document is not this layout.
    """
    import pdfplumber

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pdfplumber.open(path) as pdf:
            is_two_column = False
            for page in pdf.pages[:2]:
                midpoint = page.width / 2
                words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
                side_has_category = []
                for left, right in ((0, midpoint), (midpoint, page.width)):
                    side_has_category.append(any(
                        parse_flow_category_line(" ".join(w["text"] for w in line))
                        for line in group_lines(
                            [w for w in words if left <= w["x0"] < right])
                    ))
                if all(side_has_category):
                    is_two_column = True
                    break
            if not is_two_column:
                return None

            categories = []
            current = [None, None]
            for page in pdf.pages:
                midpoint = page.width / 2
                words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
                for side, (left, right) in enumerate(
                        ((0, midpoint), (midpoint, page.width))):
                    side_words = [w for w in words if left <= w["x0"] < right]
                    for line in group_lines(side_words):
                        text = " ".join(w["text"] for w in line).strip()
                        if (not text or PDF_PAGE_CHROME_RE.search(text)
                                or DATE_HEADER_RE.search(text)
                                or CONTINUATION_RE.match(text)
                                or re.match(r"^(?:Pl|Rang)\b.*(?:Name|Läufer)", text, re.I)):
                            continue
                        category = parse_flow_category_line(text)
                        if category:
                            name, declared = category
                            if current[side] and current[side]["name"] == name:
                                continue
                            current[side] = {
                                "name": name, "declaredStarters": declared, "results": []}
                            categories.append(current[side])
                            continue
                        if current[side] is None:
                            continue
                        row = parse_flow_result_row(text, CLUBS)
                        if row:
                            current[side]["results"].append(row)
                        elif "famil" in current[side]["name"].casefold():
                            family_flow = parse_flow_row(text, CLUBS)
                            if (family_flow and family_flow.get("club")
                                    and (family_flow.get("timeText")
                                         or family_flow.get("statusText"))):
                                family_results = flow_results(family_flow)
                                if family_results:
                                    # A Family row is one anonymous result
                                    # unit even when its label contains ``&``
                                    # or ``+``.  Never turn those names into
                                    # independently indexed people.
                                    family_result = dict(family_results[0])
                                    family_result["name"] = " + ".join(
                                        family_flow.get("names") or
                                        [family_result.get("name") or ""])
                                    family_result["resultKind"] = "family"
                                    family_result.pop("note", None)
                                    current[side]["results"].append(family_result)

    categories = merge_category_continuations(
        [category for category in categories if category["results"]])
    for category in categories:
        if category["declaredStarters"] is None:
            category["declaredStarters"] = len(category["results"])
    return categories


def parse_estimated_time_championship_pdf(pdf):
    """Parse HSV Ried's estimated-vs-actual-time club championship.

    This landscape spreadsheet is not a SportSoftware table despite being
    attached as a result PDF: competitors are ranked by the sum of the
    absolute deviations on Bahn A and B.  Stable x-columns and the distinctive
    headers let us preserve names, course, rank and the actual scoring value
    without pretending that a deviation is an elapsed OL time.
    """
    results = []
    competitive_rank = 0
    for page in pdf.pages:
        for line in group_lines(page.extract_words(
                use_text_flow=False, keep_blank_chars=False)):
            course = " ".join(
                word["text"] for word in line if 120 <= word["x0"] < 165).strip()
            if not re.fullmatch(r"(?:[HD]16-|[HD]-16)", course, re.I):
                continue
            surname = " ".join(
                word["text"] for word in line if word["x0"] < 75).strip()
            given = " ".join(
                word["text"] for word in line if 75 <= word["x0"] < 125).strip()
            surname = re.sub(r"^\d+\.\s*", "", surname).strip()
            name = f"{given} {surname}".strip()
            if not looks_like_person(name) or is_junk_name(name):
                continue
            final_value = " ".join(
                word["text"] for word in line if word["x0"] >= 760).strip()
            score_match = re.match(r"(\d+(?:[.,]\d+)?)\b", final_value)
            result = {
                "name": name,
                "club": "HSV Ried",
                "timeText": "",
                "status": "unknown",
                "note": f"Strecke: {course}",
            }
            if score_match:
                score_text = score_match.group(1)
                competitive_rank += 1
                result.update({
                    "rank": competitive_rank,
                    "status": "ok",
                    "scoreText": f"Abweichung {score_text}",
                })
            else:
                status = parse_status(final_value)
                if status:
                    result["status"] = status
                    result["timeText"] = final_value
                elif final_value:
                    # Preserve a genuinely ambiguous source value such as
                    # ``???``. The quality model can then distinguish it
                    # from a visually blank result cell and show the reviewer
                    # what the source actually said.
                    result["timeText"] = final_value
            results.append(result)
    if not results:
        return None
    return [{
        "name": "Vereinsmeisterschaft",
        "declaredStarters": len(results),
        "results": results,
    }]


def parse_regional_championship_columns_pdf(pdf):
    """Parse the landscape ``KATEGORIE RANG Vorname ... Zeit`` layout."""
    categories = []
    current = None
    for page in pdf.pages:
        for line in group_lines(page.extract_words(
                use_text_flow=False, keep_blank_chars=False)):
            text = " ".join(word["text"] for word in line).strip()
            category_match = re.match(r"^(Damen|Herren)\s*-?\s*(\d+)\b", text, re.I)
            if category_match and line[0]["x0"] < 100:
                current = {
                    "name": f"{category_match.group(1).title()} -{category_match.group(2)}",
                    "declaredStarters": None,
                    "results": [],
                }
                categories.append(current)
                continue
            if current is None:
                continue
            rank_text = " ".join(
                word["text"] for word in line if 115 <= word["x0"] < 160).strip()
            if not (rank_text.rstrip(".").isdigit() or parse_status(rank_text)):
                continue
            given = " ".join(
                word["text"] for word in line if 160 <= word["x0"] < 225).strip()
            surname = " ".join(
                word["text"] for word in line if 225 <= word["x0"] < 310).strip()
            name = f"{given} {surname}".strip()
            if not looks_like_person(name) or is_junk_name(name):
                continue
            yob_text = " ".join(
                word["text"] for word in line if 310 <= word["x0"] < 350).strip()
            club = " ".join(
                word["text"] for word in line if 350 <= word["x0"] < 500).strip()
            value = " ".join(
                word["text"] for word in line if word["x0"] >= 500).strip()
            result = {"name": name, "club": club, "timeText": value}
            if rank_text.rstrip(".").isdigit():
                result["rank"] = int(rank_text.rstrip("."))
            seconds = parse_time_loose(value)
            if seconds is not None:
                result.update({"timeS": seconds, "status": "ok"})
            else:
                source_status = parse_status(value) or parse_status(rank_text)
                result["status"] = source_status or "unknown"
                if source_status and not value:
                    result["timeText"] = rank_text
            if yob_text.isdigit():
                result["yearOfBirth"] = int(yob_text)
            current["results"].append(result)
    parsed = [category for category in categories if category["results"]]
    for category in parsed:
        category["declaredStarters"] = len(category["results"])
    return parsed or None


def prefer_referenced_html_source(categories, head_text):
    """Use the complete HTML original named in a clipped browser PDF.

    Firefox printouts sometimes cut the rightmost Zeit/status columns off the
    page while retaining the original ``event_N_name.html`` URL in the page
    header. If that exact HTML snapshot is already in the ANNE cache and its
    category/name population matches the PDF nearly perfectly, it is the
    higher-fidelity representation of the same source document.
    """
    reference = re.search(r"\bevent_(\d+)_[-\w.]+\.html\b", head_text or "", re.I)
    if not reference or not categories:
        return categories
    try:
        import parse_sportsoftware_html as html_parser
    except ImportError:
        return categories

    pdf_members = {
        (category["name"].casefold(), result["name"].casefold())
        for category in categories for result in category["results"]
    }
    best = None
    for candidate in sorted(FILES.glob(f"{reference.group(1)}-*.html")):
        try:
            parsed = html_parser.parse_document(
                html_parser.decode(candidate.read_bytes()))
        except Exception:
            continue
        html_members = {
            (category["name"].casefold(), result["name"].casefold())
            for category in parsed for result in category["results"]
        }
        if not html_members:
            continue
        overlap = len(pdf_members & html_members)
        coverage = overlap / max(len(pdf_members), len(html_members))
        category_match = {
            category["name"].casefold() for category in categories
        } == {
            category["name"].casefold() for category in parsed
        }
        if category_match and coverage >= 0.95:
            score = (coverage, overlap)
            if best is None or score > best[0]:
                best = (score, parsed)
    return best[1] if best else categories


def parse_round_ranking_pdf(pdf):
    """Parse an official KO-sprint ranking whose last column is a round.

    These sheets intentionally have no elapsed-time column: the placement is
    the championship result and ``Final``/``Semifinal N`` explains how it was
    reached.  The generic PDF path used to retain just one damaged row because
    it requires a time/status anchor.  Champion names are printed on the next
    visual line, so they are joined explicitly here.
    """
    categories = []
    current = None
    pending_winner = None
    category_re = re.compile(
        r"^(?P<name>[DH]\s*21\s*-?E?)\s*\((?P<count>\d+)\s*/\s*\d+\)\s+Runde$",
        re.I)
    round_re = r"(?:Final|Semifinal\s+\d+)"
    champion_re = re.compile(
        rf"^1\.?\s+(?:&|und)\s+(?P<title>.+?meister(?:in)?)\s+"
        rf"(?P<club>.+?)\s+(?P<round>{round_re})$", re.I)
    row_re = re.compile(
        rf"^(?P<rank>\d+)\.?\s+(?P<body>.+?)\s+(?P<round>{round_re})$",
        re.I)

    for page in pdf.pages:
        lines = group_lines(page.extract_words(
            use_text_flow=False, keep_blank_chars=False))
        for line in lines:
            text = " ".join(word["text"] for word in line).strip()
            category_match = category_re.match(text)
            if category_match:
                name = re.sub(r"\s+", "", category_match.group("name")).upper()
                current = {
                    "name": name,
                    "declaredStarters": int(category_match.group("count")),
                    "results": [],
                }
                categories.append(current)
                pending_winner = None
                continue
            if current is None:
                continue
            if pending_winner:
                if (looks_like_person(text) and not re.search(r"\d", text)
                        and not is_junk_name(text)):
                    result = dict(pending_winner)
                    result["name"] = text
                    current["results"].append(result)
                    pending_winner = None
                    continue
                pending_winner = None
            champion_match = champion_re.match(text)
            if champion_match:
                pending_winner = {
                    "club": champion_match.group("club").strip(),
                    "rank": 1,
                    "status": "ok",
                    "timeText": "",
                    "rankingBasis": "other",
                    "note": f"Runde: {champion_match.group('round')}",
                    "championship": classify_championship_text(
                        champion_match.group("title")),
                }
                continue
            row_match = row_re.match(text)
            if not row_match:
                continue
            club, name_tokens = find_trailing_club(
                row_match.group("body").split(), CLUBS)
            name = " ".join(name_tokens).strip()
            if name.casefold().endswith(" lki") and "laufklub" in (club or "").casefold():
                name = name[:-4].rstrip()
            if not club or not looks_like_person(name) or is_junk_name(name):
                continue
            current["results"].append({
                "name": name,
                "club": club,
                "rank": int(row_match.group("rank")),
                "status": "ok",
                "timeText": "",
                "rankingBasis": "other",
                "note": f"Runde: {row_match.group('round')}",
            })
    return [category for category in categories if category["results"]]


def parse_plain_gender_championship_pdf(pdf):
    """Parse a small hand-made Damen/Herren championship result sheet."""
    categories = []
    current = None
    for page in pdf.pages:
        for text in (page.extract_text() or "").splitlines():
            text = text.strip()
            if text in {"Damen", "Herren"}:
                current = {"name": text, "declaredStarters": None, "results": []}
                categories.append(current)
                continue
            if current is None:
                continue
            championship = classify_championship_text(text)
            if championship and current["results"]:
                current["results"][-1]["championship"] = championship
                continue
            result = parse_flow_result_row(text, CLUBS)
            if result:
                current["results"].append(result)
    for category in categories:
        category["declaredStarters"] = len(category["results"])
    return [category for category in categories if category["results"]]


def parse_compact_pursuit_ranking_pdf(pdf):
    """Parse the compact 2026 SkiO pursuit championship ranking.

    Its embedded font removes every space between bib/name and birth-year/
    club.  The two repeated column headers delimit the men's and women's
    rankings; the official ``Wertung`` contains both finishers and status
    rows, all without a separate category heading.
    """
    categories = []
    current = None
    header_count = 0
    value_re = re.compile(
        r"(?P<value>\d{1,3}:\d{2}:\d{2}|Fehlst\.?|Aufg\.?|Disqu?\.?)$", re.I)
    for page in pdf.pages:
        for text in (page.extract_text() or "").splitlines():
            text = text.strip()
            if text.startswith("Pl StnrName"):
                header_count += 1
                current = {
                    "name": "Herren Elite" if header_count == 1 else "Damen Elite",
                    "declaredStarters": None,
                    "results": [],
                }
                categories.append(current)
                continue
            if current is None:
                continue
            value_match = value_re.search(text)
            if not value_match:
                continue
            prefix = text[:value_match.start()].strip()
            lead = re.match(r"^(?:(?P<rank>\d+)\s+)?(?P<bib>\d{2})(?P<body>.+)$", prefix)
            if not lead:
                continue
            body = lead.group("body").strip()
            split = re.match(r"^(?P<name>.+?)\s+(?P<year>\d{2})(?P<club>.+)$", body)
            if not split:
                continue
            name = split.group("name").strip()
            raw_club = split.group("club").strip()
            club = CLUBS.get(raw_club.casefold(), raw_club)
            if not looks_like_person(name) or is_junk_name(name):
                continue
            value = value_match.group("value")
            seconds = parse_time_loose(value)
            result = {
                "name": name,
                "club": club,
                "timeText": value,
                "status": "ok" if seconds is not None else (parse_status(value) or "unknown"),
                "yearOfBirth": int(split.group("year")) + (
                    2000 if int(split.group("year")) <= 26 else 1900),
            }
            if lead.group("rank"):
                result["rank"] = int(lead.group("rank"))
            if seconds is not None:
                result["timeS"] = seconds
            current["results"].append(result)
    for category in categories:
        category["declaredStarters"] = len(category["results"])
    return [category for category in categories if category["results"]]


def repair_wrapped_champion_names(path, categories):
    """Restore winner names printed on a line below their title/club/time."""
    wrapped = []
    import pdfplumber
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            lines = group_lines(page.extract_words(
                use_text_flow=False, keep_blank_chars=False))
            texts = [" ".join(word["text"] for word in line).strip()
                     for line in lines]
            for index, text in enumerate(texts[:-1]):
                if not re.match(r"^1\.?\s+(?:&|und)\s+", text, re.I):
                    continue
                championship = classify_championship_text(text)
                following = texts[index + 1]
                if (championship and looks_like_person(following)
                        and not re.search(r"\d", following)
                        and not is_junk_name(following)):
                    wrapped.append((following, championship))
    candidates = [result for category in categories
                  for result in category.get("results") or []
                  if re.search(r"meister", result.get("name") or "", re.I)]
    for result, (name, championship) in zip(candidates, wrapped):
        result["name"] = name
        result["championship"] = championship
        if result.get("resultKind") == "pair" and result.get("note") == "Partner: ":
            result.pop("resultKind", None)
            result.pop("teamNumber", None)
            result.pop("note", None)
    if wrapped:
        for category in categories:
            for result in category.get("results") or []:
                if result.get("status") == "dns":
                    result["excludedFromDeclaredCount"] = True
    return categories


def repair_known_pdf_extraction_artifacts(path, categories):
    """Repair rows whose embedded font destroys otherwise visible glyphs.

    These are deliberately source-file and person specific.  The values were
    checked against rendered pages; broad OCR guessing would be less safe than
    retaining an unresolved raw value for every other document.
    """
    corrections = {
        ("4089-1.pdf", "Oberneuwirther"): {
            "name": "Oberneuwirther Isabel",
            "club": "Christian-Doppler-Gymnasium",
            "timeText": "38:20", "timeS": 2300, "status": "ok",
            "yearOfBirth": 2011,
        },
        ("4477-3.pdf", "Libor Filip"): {
            "club": "SRK SOOB Spartak Rychnov n.Kn.",
            "timeText": "1:19:24", "timeS": 4764, "status": "ok",
        },
        ("4477-3.pdf", "Ivana Filipová"): {
            "club": "SRK SOOB Spartak Rychnov n.Kn.",
            "timeText": "1:10:07", "timeS": 4207, "status": "ok",
        },
        ("1605-0.pdf", "Bednarik Martin"): {
            "club": "KOB Cingov Spisska Nova Ves",
            "timeText": "33:58", "timeS": 2038, "status": "ok",
        },
        ("1605-0.pdf", "Bednarikova Tatiana"): {
            "club": "KOB Cingov Spisska Nova Ves",
            "timeText": "47:11", "timeS": 2831, "status": "ok",
        },
        ("1605-0.pdf", "Ladics Thomas+Stephan"): {
            "club": "GRG Alterlaa",
            "timeText": "27:01", "timeS": 1621, "status": "ok",
        },
        # Visually verified on page 3: the source keeps rank 14 and elapsed
        # time but inserts its missing-control note between school and time.
        # Preserve all three independent facts instead of making the note a
        # club and the whole prefix a synthetic person.
        ("1200-0.pdf", "Melissa Egg Pos6 (54)"): {
            "name": "Melissa", "club": "Egg", "status": "mp",
            "note": "Posten 6 (54) fehlt",
        },
    }
    file_name = Path(path).name
    for category in categories:
        for result in category.get("results") or []:
            correction = corrections.get((file_name, result.get("name") or ""))
            if correction:
                result.update(correction)
    return categories


def parse_taksoft_school_pdf(pdf):
    """Parse the old ``Platz SI-NR Name Schule Ergebnis`` school layout."""
    lines = []
    for page in pdf.pages:
        lines.extend((page.extract_text(x_tolerance=1, y_tolerance=3) or "").splitlines())
    categories = []
    current = None
    group_number = 0
    last_group = []

    def set_missing_punch(rows):
        for result in rows:
            result.pop("rank", None)
            result["status"] = "mp"
            result["individualStatus"] = "mp"

    for index, raw_line in enumerate(lines):
        line = re.sub(r"\s+", " ", raw_line).strip()
        category_match = re.match(r"^(?P<name>.+?)\s+Bahn\s*:", line, re.I)
        if category_match:
            current = {"name": category_match.group("name").strip(),
                       "declaredStarters": None, "sourceUnitCount": 0, "results": []}
            current.update(parse_course_info(line))
            categories.append(current)
            last_group = []
            continue
        if current is None or not line or line.startswith(("Platz ", "© ")):
            continue
        if re.match(r"^Pos\.?\s*\d", line, re.I) and re.search(
                r"fehl(?:t|en)\b", line, re.I):
            set_missing_punch(last_group)
            continue
        # One long missing-punch description wraps its elapsed time onto the
        # next physical line (event 993). Join only this exact structural case.
        if (re.match(r"^\d{5,}\s+", line) and not re.search(
                r"(?:\d{1,2}:\d{2}:\d{2}|keine Zeit)\s*$", line, re.I)
                and index + 1 < len(lines)
                and re.fullmatch(r"\s*\d{1,2}:\d{2}:\d{2}\s*", lines[index + 1])):
            line += " " + lines[index + 1].strip()
        match = re.match(
            r"^(?:(?P<rank>\d+)\s*\.\s*)?(?P<bib>\d{5,})\s+"
            r"(?P<body>.+?)\s+(?P<value>\d{1,2}:\d{2}:\d{2}|keine Zeit)$",
            line, re.I)
        if not match:
            continue
        body = match.group("body").strip()
        missing = re.search(r"\s+Pos\.?[^ ]*(?:\s+bis\s+[^ ]+)?\s*fehl(?:t|en)\s*$",
                            body, re.I)
        if missing:
            body = body[:missing.start()].strip()
        tokens = body.split()
        if len(tokens) < 2:
            continue
        club = tokens[-1]
        name = " ".join(tokens[:-1]).strip()
        value = match.group("value")
        seconds = parse_time(value)
        base = {"name": name, "club": club, "timeText": value,
                "status": "mp" if missing else ("ok" if seconds is not None else "unknown")}
        if match.group("rank") and not missing:
            base["rank"] = int(match.group("rank"))
        if seconds is not None:
            base["timeS"] = seconds
        if missing:
            base["individualStatus"] = "mp"
        group_number += 1
        current["sourceUnitCount"] += 1
        members = [name]
        if "schnupper" in current["name"].casefold():
            members = [part.strip(" .") for part in re.split(
                r"\s+\bund\b\s+|\s*[&+]\s*|,\s*", name, flags=re.I)
                       if part.strip(" .")]
        if len(members) > 1:
            team_number = f"school-group-{group_number}"
            last_group = []
            for member in members:
                result = dict(base, name=member, resultKind="pair", teamNumber=team_number)
                result["note"] = "Gruppe: " + " + ".join(members)
                current["results"].append(result)
                last_group.append(result)
        else:
            current["results"].append(base)
            last_group = [base]
    for category in categories:
        category["declaredStarters"] = category.get("sourceUnitCount", 0)
    return [category for category in categories if category["results"]]


def parse_supervised_school_pdf(pdf):
    """Parse ``Name Kl. Schule Betreuer Laufzeit`` school-team reports."""
    lines = []
    for page in pdf.pages:
        lines.extend((page.extract_text(x_tolerance=1, y_tolerance=3) or "").splitlines())
    categories = []
    current = None
    group_number = 0
    active_group = []

    def finalize_group():
        if len(active_group) < 2:
            return
        names = [result["name"] for result in active_group]
        for result in active_group:
            result["resultKind"] = "pair"
            result["note"] = "Gruppe: " + " + ".join(names)

    for raw_line in lines:
        line = re.sub(r"\s+", " ", raw_line).strip()
        category_match = re.match(
            r"^(?P<name>(?:Damen|Herren)\s+\d+|Schnupperer)\s*:", line, re.I)
        if category_match:
            finalize_group()
            active_group = []
            current = {"name": category_match.group("name").strip(),
                       "declaredStarters": None, "sourceUnitCount": 0, "results": []}
            current.update(parse_course_info(line))
            categories.append(current)
            continue
        if current is None or not line or line.startswith((
                "Platz ", "3. Lauf ", "Veranstalter", "Auswertung")):
            continue
        match = re.match(
            r"^(?:(?P<rank>\d+)\s+)?(?P<body>.+?)\s+"
            r"(?P<value>\d{1,2}:\d{2}:\d{2}|FST)$", line, re.I)
        if match:
            body = match.group("body").strip()
            body_match = re.match(
                r"^(?P<name>.+?)\s+(?:[DH]\s*\d+|G|S)\s+MS\s+"
                r"(?P<school>.+?)\s+[A-Z]\.?\s+\S+$", body)
            if not body_match:
                continue
            finalize_group()
            active_group = []
            value = match.group("value")
            seconds = parse_time(value)
            status = "mp" if value.casefold() == "fst" else "ok"
            group_number += 1
            result = {
                "name": body_match.group("name").strip(),
                "club": f"MS {body_match.group('school').strip()}",
                "timeText": value, "status": status,
                "teamNumber": f"school-group-{group_number}",
            }
            if match.group("rank") and status == "ok":
                result["rank"] = int(match.group("rank"))
            if seconds is not None:
                result["timeS"] = seconds
            if status == "mp":
                result["individualStatus"] = "mp"
            current["results"].append(result)
            current["sourceUnitCount"] += 1
            active_group.append(result)
            continue
        if ("schnupper" in current["name"].casefold()
                and active_group and re.fullmatch(r"[A-Za-zÀ-ž][A-Za-zÀ-ž .’'\-]+", line)):
            result = dict(active_group[0], name=line.strip())
            current["results"].append(result)
            active_group.append(result)
    finalize_group()
    for category in categories:
        category["declaredStarters"] = category.get("sourceUnitCount", 0)
    return [category for category in categories if category["results"]]


def parse_penalty_results_pdf(pdf):
    """Parse compact Nettozeit/Zuschlag/Bruttozeit result tables."""
    categories = {}
    order = []
    for page in pdf.pages:
        for raw_line in (page.extract_text(x_tolerance=1, y_tolerance=3) or "").splitlines():
            line = re.sub(r"\s+", " ", raw_line).strip()
            category_match = re.search(r"(?P<category>D/H\s+(?:Kurz|Lang))$", line, re.I)
            if not category_match:
                continue
            category_name = category_match.group("category")
            if category_name not in categories:
                categories[category_name] = {
                    "name": category_name, "declaredStarters": 0, "results": []}
                order.append(category_name)
            body = line[:category_match.start()].strip()
            dns = re.match(
                r"^(?P<name>.+?)\s+(?P<year>\d{4})\s+[MF]\s+Nicht angetreten$",
                body, re.I)
            if dns:
                result = {"name": dns.group("name"), "club": "",
                          "timeText": "Nicht angetreten", "status": "dns",
                          "yearOfBirth": int(dns.group("year"))}
            else:
                row = re.match(
                    r"^(?P<rank>\d+)\s+(?P<name>.+?)\s+(?P<year>\d{4})\s+[MF]\s+"
                    r"(?P<values>(?:\d{1,2}:\d{2}:\d{2}\s*){2,3})$", body)
                if not row:
                    continue
                values = re.findall(r"\d{1,2}:\d{2}:\d{2}", row.group("values"))
                value = values[-1]
                result = {"name": row.group("name"), "club": "",
                          "timeText": value, "timeS": parse_time(value), "status": "ok",
                          "rank": int(row.group("rank")),
                          "yearOfBirth": int(row.group("year"))}
            categories[category_name]["results"].append(result)
            categories[category_name]["declaredStarters"] += 1
    return [categories[name] for name in order if categories[name]["results"]]


def parse_pdf(path, allow_inline_splits=False):
    import pdfplumber

    categories = []
    current = None
    headers = None
    team_row_mode = False
    pair_row_mode = False
    school_member_mode = False
    school_row_mode = False
    pair_columns = None
    team_member_labels = []
    head_text = ""
    pending_rank = pending_championship = None  # from a champion-announcement
    team_counts = defaultdict(int)
    school_pair_counter = 0
                     # line ("1. und Staatmeister 2020"), which forms its own
                     # word-cluster/line entirely separate from the winner's
                     # actual row and would garble column assignment if fed
                     # through it, so it's matched against the raw joined
                     # line text and carried forward onto the next data row

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pdfplumber.open(path) as pdf:
            if pdf.pages:
                head_text = pdf.pages[0].extract_text() or ""
            if re.search(r"Platz\s+SI-NR\s+Name\s+Schule\s+Ergebnis", head_text, re.I):
                categories = parse_taksoft_school_pdf(pdf)
                return repair_known_pdf_extraction_artifacts(path, categories), head_text
            if re.search(r"Platz\s+Name\s+Kl\.\s+Schule\s+Betreuer\s+Laufzeit",
                         head_text, re.I):
                return parse_supervised_school_pdf(pdf), head_text
            if re.search(r"Nettozeit\s+Zuschlag\s+\+\s+Bruttozeit", head_text, re.I):
                return parse_penalty_results_pdf(pdf), head_text
            if ("geschätzte Zeit" in head_text and "echte Zeit" in head_text
                    and "Abweichung gesamt" in head_text):
                estimated_time = parse_estimated_time_championship_pdf(pdf)
                if estimated_time is not None:
                    return estimated_time, head_text
            if re.search(
                    r"KATEGORIE\s+RANG\s+Vorname\s+Nachname\s+Jg\s+Verein\s+Zeit",
                    head_text, re.I):
                regional = parse_regional_championship_columns_pdf(pdf)
                if regional is not None:
                    return regional, head_text
            if (re.search(r"\bRunde\b", head_text)
                    and re.search(r"\b(?:Ö|OE)STM\s+KO[- ]Sprint\b", head_text, re.I)):
                round_ranking = parse_round_ranking_pdf(pdf)
                if round_ranking:
                    return round_ranking, head_text
            if (re.search(r"ÖStM\s+Sprint\s+SchiO", head_text, re.I)
                    and re.search(r"^Damen$", head_text, re.I | re.M)
                    and re.search(r"^Herren$", head_text, re.I | re.M)):
                plain_championship = parse_plain_gender_championship_pdf(pdf)
                if plain_championship:
                    return plain_championship, head_text
            if re.search(r"ÖM\s+Verfolgung.*Wertung\s+Herren\s+Elite", head_text, re.I):
                pursuit_ranking = parse_compact_pursuit_ranking_pdf(pdf)
                if pursuit_ranking:
                    return pursuit_ranking, head_text
            # Newspaper-style two-table pages normally expose two ``(N)``
            # category counts on the same extracted line. Use that cheap
            # signal before opening the document a second time for x-splitting;
            # ordinary PDFs now stay single-pass during nightly/full syncs.
            two_column = (parse_two_column_pdf(path)
                          if re.search(r"\(\d+\).{8,}\(\d+\)", head_text) else None)
            if two_column is not None:
                return repair_wrapped_champion_names(path, two_column), head_text
            # Remember the first page's real title/venue/organizer lines above
            # the table header. SportSoftware repeats them on later pages,
            # where the previous category and column coordinates are still
            # active. Exact repetition is the safe signal: unlike a blanket
            # "no time/status" filter, it cannot delete a genuine listed
            # runner whose source result value happens to be blank. Several
            # old exports do not repeat the Pl/Name/... table header at all,
            # so resetting columns at every page would lose whole pages.
            repeated_header_texts = set()
            if pdf.pages:
                pre_table_texts = []
                found_table_header = False
                for first_line in group_lines(pdf.pages[0].extract_words(
                        use_text_flow=False, keep_blank_chars=False)):
                    if first_line and first_line[0]["text"] in ("Pl", "Platz"):
                        found_table_header = True
                        break
                    first_text = " ".join(w["text"] for w in first_line).strip()
                    # Some exports put the first category immediately above
                    # the Pl/Name header. It is data, not repeating page
                    # furniture, and must remain visible on page one.
                    if (first_text and not CAT_LINE_RE.match(first_text)
                            and not COURSE_ONLY_CAT_RE.match(first_text)
                            and not BAHN_CAT_RE.match(first_text)
                            and not BAHN_ONLY_CAT_RE.match(first_text)
                            and not UNCOUNTED_COURSE_CAT_RE.match(first_text)
                            and not PRELIMINARY_CAT_RE.match(first_text)):
                        pre_table_texts.append(first_text)
                # Flowing/nonstandard PDFs may have no Pl header whatsoever;
                # in that case `pre_table_texts` is the whole first page,
                # including real categories and results, and must not be used
                # as a furniture filter.
                if found_table_header:
                    repeated_header_texts.update(pre_table_texts)
            has_inline_splits = bool(SPLITS_RE.search(head_text))
            if has_inline_splits and not allow_inline_splits:
                return [], head_text
            for page in pdf.pages:
                words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
                for line in group_lines(words):
                    if not line:
                        continue
                    text = " ".join(w["text"] for w in line)

                    if text.strip() in repeated_header_texts:
                        continue

                    # Repeated PDF furniture sits inside the same x-columns
                    # as real rows on later pages. It must be rejected before
                    # column assignment can turn "Seite 3" or the generator
                    # copyright into synthetic Mannschaft members.
                    if PDF_PAGE_CHROME_RE.search(text):
                        continue

                    compact_international_header = (
                        line[0]["text"].startswith("Pos.St")
                        and any("No.Name" in w["text"] for w in line))
                    first_header = PDF_HEADER_ALIASES.get(
                        line[0]["text"], line[0]["text"])
                    if ((first_header in ("Pl", "Platz") or compact_international_header)
                            and len(line) >= 3):
                        points_course_header = re.match(
                            r"^Platz\s*\(([A-Z])\)\s+Name\b.*\bZeit\s+Punkte\s*$",
                            text, re.I)
                        if compact_international_header:
                            # OLA/Alpe-Adria's English export glues the header
                            # labels (``Pos.St.``, ``No.Name``, ``CountryTime``)
                            # and often the bib to the surname. Its stable
                            # visual starts are still usable.
                            word_x = {w["text"]: w["x0"] for w in line}
                            country_x = next(
                                (w["x0"] for w in line if w["text"].startswith("Country")),
                                line[-2]["x0"])
                            headers = [
                                ("Pl", line[0]["x0"]),
                                ("Name", next(w["x0"] for w in line if "No.Name" in w["text"])),
                                ("Class", next(w["x0"] for w in line if w["text"] == "Class")),
                                ("Verein", next(w["x0"] for w in line if w["text"] == "Club")),
                                ("Nat", country_x),
                                ("Zeit", country_x + 27),
                            ]
                        else:
                            headers = []
                            skip_header_word = False
                            for index, word in enumerate(line):
                                if skip_header_word:
                                    skip_header_word = False
                                    continue
                                label = word["text"]
                                if (label.casefold() == "z" and index + 1 < len(line)
                                        and line[index + 1]["text"].casefold() == "eit"):
                                    label = "Zeit"
                                    skip_header_word = True
                                headers.append((
                                    PDF_HEADER_ALIASES.get(label, label), word["x0"]))
                        runner_headers = [
                            word for word in line
                            if word["text"].casefold() in {"läufer", "laeufer", "runner"}
                        ]
                        club_header = next(
                            (word for word in line
                             if word["text"].casefold() in {"verein", "club"}), None)
                        time_header = next(
                            (word for word in line
                             if word["text"].casefold() in {"zeit", "time"}), None)
                        pair_row_mode = bool(
                            len(runner_headers) >= 2 and club_header and time_header)
                        name_header = next(
                            (word for word in line
                             if word["text"].casefold() in {"name"}), None)
                        text_member_headers = [
                            word for word in line
                            if re.fullmatch(r"Text\d+", word["text"], re.I)
                        ]
                        school_member_mode = bool(
                            name_header and text_member_headers and club_header and time_header
                            # In championship exports, ``Text1`` is a narrow
                            # announcement column *before* Name (for example
                            # ``Pl Text1 Name Jg Verein Zeit``).  It is not a
                            # second participant.  School/team member columns
                            # are the TextN columns following the primary Name.
                            and all(word["x0"] > name_header["x0"]
                                    for word in text_member_headers)
                            and not any(word["text"].casefold() == "staffel" for word in line))
                        pair_row_mode = pair_row_mode or school_member_mode
                        member_headers = (text_member_headers
                                          if school_member_mode else runner_headers)
                        pair_name_starts = (([name_header["x0"]] if name_header else [])
                                            + [word["x0"] for word in member_headers])
                        pair_columns = (
                            pair_name_starts,
                            club_header["x0"],
                            (club_header["x0"] + time_header["x0"]) / 2,
                        ) if pair_row_mode else None
                        school_row_mode = any(
                            word["text"].casefold() in {"schule", "school"}
                            for word in line)
                        # A team-standings layout ("Pl Verein Zeit Text1 Text2
                        # Text3") has no "Name" column at all - the team's own
                        # name lives in "Verein" and each member gets their own
                        # "TextN" column instead. Recognized by that absence
                        # (plus no "Staffel" either, since "Text1" ALSO gets
                        # reused for the unrelated narrow champion-announcement
                        # column on an otherwise-"Staffel"-headed relay layout -
                        # see LEAKED_TITLE_WORD_RE below) - without this, every
                        # row's `name` comes from the missing "Name" column
                        # (always empty), so is_junk_name() drops the ENTIRE
                        # file silently and it falls through to the flowing-
                        # text fallback, which discards the member names
                        # entirely and misreads the tail of a long club name
                        # as its own separate "club" (confirmed real: event
                        # 3507, "ÖM Mannschaft" 2022 - "HSV OL Wiener
                        # Neustadt" split into name="HSV OL Wiener"/
                        # club="Neustad(t)", with all 3 real team members'
                        # names lost outright).
                        header_labels = {h[0] for h in headers}
                        team_member_labels = sorted(
                            (h[0] for h in headers if re.fullmatch(r"Text\d+", h[0])),
                            key=lambda s: int(s[4:]))
                        team_row_mode = bool(
                            team_member_labels and "Verein" in header_labels
                            and not header_labels & {"Name", "Staffel"})
                        if points_course_header:
                            current = {
                                "name": f"Kategorie {points_course_header.group(1).upper()}",
                                "declaredStarters": None, "results": [],
                            }
                            categories.append(current)
                        elif current is None:
                            # some fun-run/app-based races have no age/gender
                            # classes at all: one flat ranking, no "(N)"
                            # category marker ever appears
                            current = {"name": "Ergebnis", "declaredStarters": None,
                                       "results": []}
                            categories.append(current)
                        continue
                    if CONTINUATION_RE.match(text):
                        continue
                    if MEOS_PAGE_HEADER_RE.match(text):
                        continue
                    if DATE_HEADER_RE.search(text):
                        continue  # repeated page-header/title line

                    numbered_course = NUMBERED_COURSE_CAT_RE.match(text)
                    if numbered_course:
                        # Some official lists are ranked by numbered course
                        # and merely carry the age class in a ``Kat`` column.
                        # Treating ``1 (35)``, ``2 (22)`` ... as noise merges
                        # every independent ranking reset into one generic
                        # list (event 2860).
                        name = f"Bahn {numbered_course.group('number')}"
                        if current and current["name"] == name:
                            continue
                        current = {
                            "name": name,
                            "declaredStarters": int(numbered_course.group("starters")),
                            "results": [],
                        }
                        current.update(parse_course_info(text))
                        categories.append(current)
                        pending_rank = pending_championship = None
                        team_counts = defaultdict(int)
                        continue

                    uncounted_category = (COURSE_ONLY_CAT_RE.match(text)
                                          or BAHN_CAT_RE.match(text)
                                          or BAHN_ONLY_CAT_RE.match(text))
                    if not uncounted_category and not CAT_LINE_RE.match(text):
                        uncounted_category = UNCOUNTED_COURSE_CAT_RE.match(text)
                    if not uncounted_category and not CAT_LINE_RE.match(text):
                        uncounted_category = PRELIMINARY_CAT_RE.match(text)
                    if not uncounted_category and not CAT_LINE_RE.match(text):
                        uncounted_category = UNCOUNTED_STATUS_CAT_RE.match(text)
                    if not uncounted_category and not CAT_LINE_RE.match(text):
                        # Word-processor championship result sheets can put a
                        # plain age-class heading between one shared
                        # ``Platz Name Verein Zeit`` header and its rows. If
                        # it is ignored, every class is merged into a generic
                        # ``Ergebnis`` list and each legitimate rank reset
                        # looks like a time/ranking inversion (event 3852).
                        uncounted_category = PLAIN_AGE_CATEGORY_RE.fullmatch(
                            text.strip())
                    if not uncounted_category and not CAT_LINE_RE.match(text):
                        uncounted_category = PLAIN_LEGACY_AGE_CATEGORY_RE.fullmatch(
                            text.strip())
                    if not uncounted_category and not CAT_LINE_RE.match(text):
                        uncounted_category = PLAIN_SPECIAL_CATEGORY_RE.fullmatch(
                            text.strip())
                    if (not uncounted_category and headers is not None
                            and PLAIN_LETTER_CAT_RE.fullmatch(text.strip())):
                        uncounted_category = re.match(r"^(?P<name>.+)$", text.strip())
                    if uncounted_category:
                        name = uncounted_category.group("name").strip()
                        if current and current["name"] == name:
                            continue
                        current = {"name": name, "declaredStarters": None, "results": []}
                        categories.append(current)
                        pending_rank = pending_championship = None
                        team_counts = defaultdict(int)
                        continue

                    m = CAT_LINE_RE.match(text)
                    if m:
                        name = m.group("name").strip()
                        # Result notes such as ``26 Wendler ... Fehlst.
                        # (46:35)`` and ``... Pos.14(54) fehlt`` are not
                        # category headings. The permissive missing-close
                        # form of CAT_LINE_RE otherwise mistakes their minute
                        # value/control number for a starter count.
                        if (name[:1].isdigit() or re.search(r"\bPos\.?\s*\d", name, re.I)
                                or TIME_TOKEN_RE.search(name)
                                or (STATUS_TAIL_RE.search(name)
                                    # ``Neulinge - OK (3)`` is a real category
                                    # heading (event 1049), not a result row
                                    # whose status happens to be ``OK``.
                                    and not re.search(r"\s[-–]\s*OK\s*$", name, re.I))
                                or (m.group("rest") or "").lstrip().startswith(":")):
                            m = None
                    if m:
                        name = m.group("name").strip()
                        if current and current["name"] == name:
                            continue  # continuation of the same category
                        current = {"name": name,
                                   "declaredStarters": category_starter_count(m),
                                   "results": []}
                        current.update(parse_course_info(m.group("rest")))
                        categories.append(current)
                        pending_rank = pending_championship = None
                        team_counts = defaultdict(int)
                        school_pair_counter = 0
                        continue

                    if current is None or headers is None:
                        continue

                    detached_rank = re.fullmatch(
                        r"(\d{1,3})(?:\s+\d{1,3})*(?:\s+\([^)]*\))?", text.strip())
                    if detached_rank:
                        # In word-processor championship tables a crowded
                        # multi-ranking row can wrap into three visual lines:
                        # title, numeric rank columns, then the runner. Carry
                        # the overall rank to that following runner instead of
                        # silently dropping first/merged-class placements.
                        pending_rank = int(detached_rank.group(1))
                        continue

                    annot_rank, annot_championship = parse_champion_annotation(text)
                    if annot_rank is not None:
                        pending_rank, pending_championship = annot_rank, annot_championship
                        continue

                    if pair_row_mode and pair_columns:
                        name_starts, club_x, time_x = pair_columns
                        first_x = name_starts[0]
                        rank_text = " ".join(
                            word["text"] for word in line if word["x0"] < first_x).strip()
                        lead_numbers = re.findall(r"\b(\d+)\.?\b", rank_text)
                        names = []
                        for index, start_x in enumerate(name_starts):
                            end_x = (name_starts[index + 1]
                                     if index + 1 < len(name_starts) else club_x)
                            names.append(" ".join(
                                word["text"] for word in line
                                if start_x - 1 <= word["x0"] < end_x - 1).strip())
                        names = [name for name in names
                                 if looks_like_person(name) and not is_junk_name(name)]
                        club = " ".join(
                            word["text"] for word in line
                            if club_x - 1 <= word["x0"] < time_x - 1).strip().rstrip(" :")
                        value = " ".join(
                            word["text"] for word in line
                            if word["x0"] >= time_x - 1).strip()
                        if school_member_mode:
                            source_club = club
                            # Some school names end immediately at the time
                            # column, so PDF extraction glues both fields.
                            # A visibly separated trailing value is safest to
                            # peel first; the general glyph repair handles the
                            # no-space/interleaved variants.
                            separated_value = re.search(
                                r"\s+(\d{1,3}:\d{2}(?::\d{2})?)$", source_club)
                            if separated_value:
                                value = separated_value.group(1)
                                club = source_club[:separated_value.start()].rstrip(" :")
                            else:
                                club, value = repair_result_club_and_value(club, value)
                            school_number = re.match(
                                r"(?i)^(?P<prefix>[A-Z]+)\s+(?P<number>\d{1,2})\b",
                                source_club)
                            if (school_number and not re.match(
                                    rf"(?i)^{re.escape(school_number.group('prefix'))}\s+"
                                    rf"{re.escape(school_number.group('number'))}\b", club)):
                                prefix = school_number.group("prefix")
                                number = school_number.group("number")
                                club = re.sub(rf"(?i)^{re.escape(prefix)}\b",
                                              f"{prefix} {number}", club, count=1)
                                if value.startswith(number):
                                    candidate = value[len(number):]
                                    if parse_time_loose(candidate) is not None:
                                        value = candidate
                        seconds = parse_time_loose(value)
                        status = "ok" if seconds is not None else (parse_status(value) or "unknown")
                        if names and (seconds is not None or status != "unknown"):
                            pair_rank = pending_rank
                            pair_number = None
                            if seconds is not None:
                                if len(lead_numbers) >= 2:
                                    pair_rank, pair_number = int(lead_numbers[0]), lead_numbers[1]
                                elif lead_numbers:
                                    pair_rank = int(lead_numbers[0])
                            elif lead_numbers:
                                pair_number = lead_numbers[-1]
                            if school_member_mode and len(names) > 1 and pair_number is None:
                                school_pair_counter += 1
                                pair_number = f"school-team-{school_pair_counter}"
                            for index, name in enumerate(names):
                                result = {
                                    "name": name, "club": club,
                                    "timeText": (TIME_TOKEN_RE.search(value).group(0)
                                                 if seconds is not None else value),
                                    "status": status,
                                }
                                if len(names) > 1:
                                    result["resultKind"] = ("pair" if len(names) == 2 else
                                                            "team")
                                    result["note"] = ("Partner: " if len(names) == 2 else
                                                      "Team: ") + ", ".join(
                                                          other for other in names
                                                          if other != name)
                                if pair_rank is not None:
                                    result["rank"] = pair_rank
                                if pair_number is not None:
                                    result["teamNumber"] = str(pair_number)
                                if re.search(r"\bAK\b", rank_text, re.I):
                                    result["outOfCompetition"] = True
                                if pending_championship:
                                    result["championship"] = pending_championship
                                if seconds is not None:
                                    result["timeS"] = seconds
                                current["results"].append(result)
                            pending_rank = pending_championship = None
                        continue

                    if team_row_mode:
                        # Text1/Text2/Text3 are left-aligned roster columns.
                        # Midpoint assignment is correct for right-aligned
                        # rank/time values but can split a long member name
                        # across two roster columns ("Schgaguler" | "Klaus").
                        # Slice members at the actual next header start.
                        header_x = {label: x0 for label, x0 in headers}
                        rank_text, club, time_text = parse_mannschaft_prefix(
                            line, header_x[team_member_labels[0]])
                        members = []
                        for i, label in enumerate(team_member_labels):
                            start_x = header_x[label]
                            end_x = (header_x[team_member_labels[i + 1]]
                                     if i + 1 < len(team_member_labels) else float("inf"))
                            member = " ".join(
                                word["text"] for word in line
                                if start_x - 1 <= word["x0"] < end_x - 1).strip()
                            members.append(member)
                        members = [m for m in members if m and not is_junk_name(m)]
                        if not club and not members:
                            continue
                        rank = None
                        if rank_text.isdigit():
                            rank = int(rank_text)
                        elif pending_rank is not None:
                            rank = pending_rank
                        seconds = parse_time_loose(time_text)
                        status = "ok" if seconds is not None else (parse_status(time_text) or "unknown")
                        ooc = is_ooc_status(rank_text)
                        team_counts[club] += 1
                        team_name = f"{club} {team_counts[club]}" if club else f"Mannschaft {team_counts[club]}"
                        for i, nm in enumerate(members):
                            mates = [m for j, m in enumerate(members) if j != i]
                            note = "Mannschaft: " + team_name + (" · mit " + ", ".join(mates) if mates else "")
                            res = {"name": nm, "club": club, "timeText": time_text,
                                   "resultKind": "team", "note": note, "status": status,
                                   "teamName": team_name, "teamStatus": status,
                                   "teamTimeText": time_text}
                            if rank is not None:
                                res["rank"] = rank
                            if pending_championship:
                                res["championship"] = pending_championship
                            if seconds is not None:
                                res["timeS"] = seconds
                                res["teamTimeS"] = seconds
                            if ooc:
                                res["outOfCompetition"] = True
                            current["results"].append(res)
                        pending_rank = pending_championship = None
                        continue

                    if "Klasse" in header_labels:
                        course_row = parse_course_class_result(text)
                        if course_row:
                            current["results"].extend(flow_results(course_row))
                            continue

                    if {"Zeit", "Punkte"}.issubset(header_labels):
                        points_row = parse_points_cup_result(text)
                        if points_row:
                            current["results"].append(points_row)
                            continue

                    if {"Bahn", "Punkte", "Ergebnis", "Start", "Ziel"}.issubset(
                            header_labels):
                        score_course_row = parse_score_course_result(text)
                        if score_course_row:
                            current["results"].append(score_course_row)
                            continue

                    if "Runde" in header_labels and "Gesamt" in header_labels:
                        round_row = parse_multi_round_result(text)
                        if round_row:
                            current["results"].append(round_row)
                            continue

                    # Prefer a text parse anchored on the known-club dictionary:
                    # it splits '/' pairs and is robust to flowing layouts where
                    # x-column assignment misplaces name/club. Only used when it
                    # actually recognises a trailing club or finds a pair, so
                    # clean rows whose club isn't in the dictionary fall back to
                    # the column parse below.
                    flow = parse_flow_row(text, CLUBS)
                    if valid_flow(flow):
                        rows = flow_results(flow)
                        if pending_rank is not None:
                            # the champion announcement stole this row's own
                            # Pl, leaving a single leading integer that
                            # parse_flow_row would otherwise misread as the
                            # rank when it's actually just the Stnr
                            for r in rows:
                                r["rank"] = pending_rank
                                if pending_championship:
                                    r["championship"] = pending_championship
                            pending_rank = pending_championship = None
                        current["results"].extend(rows)
                        continue

                    rec = assign_columns(line, headers)
                    name = rec.get("Name", "").strip()
                    school_club = None
                    if school_row_mode:
                        header_x = {label: x0 for label, x0 in headers}
                        if {"Name", "Verein", "Zeit"}.issubset(header_x):
                            name = " ".join(
                                word["text"] for word in line
                                if header_x["Name"] <= word["x0"] < header_x["Verein"]
                            ).strip()
                            school_time_x = next((
                                word["x0"] for word in line
                                if word["x0"] >= header_x["Verein"]
                                and (FLOW_TIME_RE.fullmatch(word["text"].strip("()"))
                                     or parse_status(word["text"]))
                            ), header_x["Zeit"])
                            school_club = " ".join(
                                word["text"] for word in line
                                if header_x["Verein"] <= word["x0"] < school_time_x
                            ).strip()
                    if not name and (rec.get("Nachname") or rec.get("Vorname")):
                        # OE12 can expose surname and given name as two real
                        # columns (``Pl Nachname Vorname Verein Zeit``).
                        # Treating only a literal Name column as a person
                        # silently discarded every ordinary ranked row and
                        # retained just a few status rows caught by the
                        # dictionary-based flow fallback.
                        name = " ".join(filter(None, (
                            (rec.get("Nachname") or "").strip(),
                            (rec.get("Vorname") or "").strip(),
                        )))
                    # Bib and surname are occasionally glued in compact
                    # English exports (``491Foški Oskar``). In ordinary
                    # fixed columns a narrow Stnr can similarly leak one
                    # leading integer into Name while Pl remains intact.
                    name = re.sub(r"^\d+(?=[^\d\s])", "", name)
                    text1 = (rec.get("Text1") or "").strip()
                    leaked_title = None
                    if text1:
                        # Yet another champion-announcement layout: a narrow
                        # "Text1" header column holds "und Österr." (or
                        # "und Staats"), but it's too narrow for the whole
                        # phrase - "Meister"/"Meisterin" spills past its
                        # midpoint into the Name column, leaving a garbled
                        # "Meister <realname>" (confirmed: event 4346,
                        # "1 und Österr. Meister Marina Skern").
                        lm = LEAKED_TITLE_WORD_RE.match(name)
                        if lm:
                            leaked_title = classify_championship_text(f"{text1} {lm.group(0)}")
                            name = name[lm.end():].strip()
                    rank_text = (rec.get("Pl") or rec.get("Platz") or "").strip()
                    # Regional/national champion wording can share the Pl
                    # cell with the actual placement (``1 u. NÖ Meisterin``).
                    # The title is metadata; its leading integer remains the
                    # runner's real rank.  An AK marker can similarly share
                    # the narrow cell with the surname (``A.K. PEKKA``).
                    ooc_prefix = re.match(
                        r"(?i)^A\.?\s*K\.?\s+(.+)$", rank_text)
                    if ooc_prefix and re.search(r"[A-Za-zÀ-ž]", ooc_prefix.group(1)):
                        name = f"{ooc_prefix.group(1).strip()} {name}".strip()
                        rank_text = "AK"
                    rank_match = RANK_TEXT_RE.fullmatch(rank_text)
                    if not rank_match:
                        rank_match = re.match(r"^(\d{1,3})\b", rank_text)
                    if rank_match:
                        rank_text = rank_match.group(1)
                        name = re.sub(r"^\d+\s+(?=\S+\s+\S+)", "", name)
                    else:
                        # a narrow, right-aligned rank column can sit closer
                        # to the next header's x0 than its own, leaking the
                        # digit into the name field instead
                        leaked = RANK_LEAK_RE.match(name)
                        if leaked:
                            rank_text, name = leaked.group(1), leaked.group(2)
                    if is_junk_name(name) or name.lstrip().startswith(("-", "–", "—")):
                        continue
                    if has_inline_splits:
                        # this layout also carries per-control split times
                        # below each name, which repeats the club and its own
                        # split diffs on the following visual line; unlike a
                        # genuine unplaced/DNF row (which still gets a numeric
                        # Stnr), that continuation line has neither Pl nor
                        # Stnr, so it's the one case where requiring one is
                        # safe - a general requirement elsewhere loses real
                        # DNF rows whose Stnr the layout doesn't expose cleanly
                        stnr_text = (rec.get("Stnr") or "").strip()
                        if (any(h[0] == "Stnr" for h in headers)
                                and not rank_text.isdigit() and not stnr_text.isdigit()):
                            continue
                    time_text = normalize_broken_result_value(
                        rec.get("Zeit") or rec.get("Gesamt") or "")
                    stage_labels = [
                        label for label, _x in headers
                        if re.fullmatch(r"(?:E|D|Lauf)\d+", label, re.I)
                    ]
                    # A stage placing printed immediately before the total
                    # can fall just across the Zeit midpoint (``2 35:27``).
                    # It is not an hour prefix; the last token is the total.
                    if stage_labels:
                        total = re.fullmatch(
                            r"\d{1,3}\s+(?P<time>\d{1,3}:\d{2}(?::\d{2})?)",
                            time_text)
                        if total:
                            time_text = total.group("time")
                        component_seconds = []
                        for label in stage_labels:
                            token = TIME_TOKEN_RE.search((rec.get(label) or "").strip())
                            seconds = parse_time_loose(token.group()) if token else None
                            if seconds is not None:
                                component_seconds.append(seconds)
                        total_seconds = parse_time_loose(time_text)
                        expected_seconds = sum(component_seconds)
                        if (len(component_seconds) >= 2 and total_seconds is not None
                                and expected_seconds > total_seconds
                                and (expected_seconds - total_seconds) % 3600 == 0):
                            hours, remainder = divmod(expected_seconds, 3600)
                            minutes, seconds = divmod(remainder, 60)
                            time_text = f"{hours}:{minutes:02d}:{seconds:02d}"
                    stage_text = " ".join(
                        rec.get(label, "").strip() for label in stage_labels
                        if rec.get(label, "").strip())
                    recovered_stage_result = False
                    # A multi-stage total can put the only visible result pair
                    # in the final E/Lauf columns and omit a conventional
                    # Zeit column. Recover the last printed time and the
                    # following placing (for example ``E2=36:30, E3=2`` in
                    # O-Festival 2019) instead of preserving a ranked runner
                    # with an empty result value.
                    if not time_text and stage_labels:
                        stage_cells = [
                            (label, normalize_broken_result_value(
                                (rec.get(label) or "").strip()))
                            for label in stage_labels
                        ]
                        timed_stage_cells = [
                            (index, TIME_TOKEN_RE.search(cell), cell)
                            for index, (_label, cell) in enumerate(stage_cells)
                            if TIME_TOKEN_RE.search(cell)
                        ]
                        if timed_stage_cells:
                            recovered_stage_result = True
                            stage_index, stage_time_match, stage_cell = timed_stage_cells[-1]
                            time_text = stage_time_match.group()
                            if not rank_text.isdigit():
                                trailing = stage_cell[stage_time_match.end():].strip()
                                rank_candidate = re.fullmatch(r"(\d{1,3})", trailing)
                                if not rank_candidate:
                                    rank_candidate = next((
                                        re.fullmatch(r"(\d{1,3})", cell)
                                        for _label, cell in stage_cells[stage_index + 1:]
                                        if re.fullmatch(r"(\d{1,3})", cell)
                                    ), None)
                                if rank_candidate:
                                    rank_text = rank_candidate.group(1)
                    recovered_overflow_club = None
                    if not time_text and (rec.get("Nat") or "").strip():
                        overflow_club, overflow_value = repair_result_club_and_value(
                            f"{(rec.get('Verein') or rec.get('Verein/Schule') or '').strip()} "
                            f"{(rec.get('Nat') or '').strip()}".strip(), "")
                        if (parse_time_loose(overflow_value) is not None
                                or parse_status(overflow_value)):
                            recovered_overflow_club = overflow_club
                            time_text = overflow_value
                    if not rank_text.isdigit() and not time_text and not stage_text:
                        # Horizontally clipped browser-to-PDF exports can cut
                        # the entire time/status column off the page while the
                        # start number, name and club remain visible. Preserve
                        # that real listed competitor with status=unknown;
                        # otherwise DNS/MP rows disappear and the count check
                        # only reports a number, without showing who is gone.
                        stnr_text = (rec.get("Stnr") or "").strip()
                        if not stnr_text.isdigit():
                            continue

                    club_text = (recovered_overflow_club or
                                 (school_club if school_club is not None else
                                  (rec.get("Verein") or rec.get("Verein/Schule") or ""))).strip()
                    nation_text = (rec.get("Nat") or "").strip()
                    overflow_country = None
                    if (nation_text and recovered_overflow_club is None
                            and not re.fullmatch(r"[A-Z]{3}", nation_text)):
                        clean_overflow = re.fullmatch(
                            r"(?P<club>.+?)\s+(?P<country>[A-Z]{3})", nation_text)
                        if clean_overflow:
                            club_text = f"{club_text} {clean_overflow.group('club')}".strip()
                            overflow_country = clean_overflow.group("country")
                        elif not time_text:
                            # When Zeit is empty, the long Verein, nation and
                            # elapsed time are all interleaved in Nat.
                            embedded_club, embedded_value = repair_result_club_and_value(
                                f"{club_text} {nation_text}".strip(), "")
                            if (parse_time_loose(embedded_value) is not None
                                    or parse_status(embedded_value)):
                                club_text, time_text = embedded_club, embedded_value
                            else:
                                time_text = nation_text
                    club_text, time_text = repair_result_club_and_value(club_text, time_text)
                    if overflow_country:
                        club_text = re.sub(
                            rf"\s+{re.escape(overflow_country)}$", "", club_text).strip()
                    # In multi-stage totals the first stage column begins
                    # directly after Verein. A long club can overflow into
                    # it (``HSV OL Wiener Neustadt 35:34``); keep the prefix
                    # as club text and retain the stage value for status
                    # inference below.
                    for label in stage_labels[:1]:
                        cell = (rec.get(label) or "").strip()
                        tm = TIME_TOKEN_RE.search(cell)
                        if tm and cell[:tm.start()].strip():
                            club_text = f"{club_text} {cell[:tm.start()].strip()}".strip()
                            rec[label] = cell[tm.start():]
                    if stage_labels:
                        glued_stage_time = CLUB_TIME_SUFFIX_RE.search(club_text)
                        if glued_stage_time:
                            club_text = club_text[:glued_stage_time.start()].rstrip()

                    result = {
                        "name": name,
                        "club": club_text,
                        "timeText": time_text,
                    }
                    if recovered_stage_result:
                        # The visible value is a stage/result component in a
                        # cumulative table; the printed overall rank is not
                        # ordered by this one elapsed time.
                        result["rankingBasis"] = "other"
                    if not time_text:
                        club_time = CLUB_TIME_SUFFIX_RE.search(result["club"])
                        if club_time:
                            time_text = club_time.group("time")
                            result["club"] = result["club"][:club_time.start()].rstrip()
                            result["timeText"] = time_text
                    if is_ooc_status(rank_text):
                        result["outOfCompetition"] = True
                    # a "Kat" column means this table holds every age bracket
                    # for one gender at once (one row per bracket-member)
                    # rather than the usual one-section-per-bracket layout -
                    # split_by_kat() breaks it back apart after the page loop
                    km = KAT_TOKEN_RE.search((rec.get("Kat") or "").strip())
                    if km:
                        result["kat"] = km.group(1)
                    # a newer OE12 layout gives the champion marker its own
                    # dedicated "ÖStM"/"ÖM" header columns instead of folding
                    # it into the Pl cell - "Österr. Staatsmeister"/"Österr.
                    # Meister" prints starting at that column's x-position but
                    # is wide enough to spill across both, so it lands split
                    # across the two rec keys rather than in just one
                    marker = f"{rec.get('ÖStM', '')} {rec.get('ÖM', '')}".strip()
                    if marker:
                        championship = classify_championship_text(marker)
                        if championship:
                            result["championship"] = championship
                    if leaked_title:
                        result["championship"] = leaked_title
                    if pending_rank is not None:
                        # Immediately after a detached champion announcement,
                        # a number shifted into Pl is the bib. The pending
                        # announcement carries the real placement.
                        result["rank"] = pending_rank
                        if pending_championship:
                            result["championship"] = pending_championship
                    elif rank_text.isdigit():
                        # this row has its own rank after all - it wasn't the
                        # one the pending announcement belonged to (a stray
                        # digit elsewhere in a garbled row, say), so drop the
                        # pending state rather than misattaching the title to
                        # an unrelated rank
                        result["rank"] = int(rank_text)
                    pending_rank = pending_championship = None
                    seconds = parse_time(time_text)
                    if seconds is None:
                        # a long club name can overflow into the time column
                        # ("Naturfreunde Villach - Oriente 21:06"); recover the
                        # time token rather than dropping a real finisher
                        tm = TIME_TOKEN_RE.search(time_text)
                        if tm:
                            seconds = parse_time(tm.group())
                            # The prefix belongs to a long club name that
                            # overflowed into the time column ("HSV OL Wiener
                            # Neustad 21:18"), not to the time itself.
                            overflow = time_text[:tm.start()].strip()
                            if overflow:
                                result["club"] = f"{result['club']} {overflow}".strip()
                            result["timeText"] = tm.group()
                    if seconds is not None:
                        result["timeS"] = seconds
                        line_status = parse_status(text)
                        result["status"] = (line_status if line_status not in (None, "ok")
                                            else "ok")
                        if is_ooc_status(text):
                            result["outOfCompetition"] = True
                    else:
                        # In a crowded score row a long club/school can spill
                        # across the nominal Zeit column while the real status
                        # remains in a later column (event 4106:
                        # ``... Zehnergasse | Wiener Neus | 0 | Aufg``).
                        # The full physical line is authoritative for a
                        # non-OK status; retain the overflow as club text.
                        line_status = parse_status(text)
                        inferred_status = (parse_status(time_text)
                                           or parse_status(stage_text)
                                           or (line_status if line_status != "ok" else None))
                        if (inferred_status in {"dnf", "dns", "mp", "dsq"}
                                and time_text and parse_status(time_text) is None
                                and re.search(r"[A-Za-zÀ-ž]", time_text)):
                            result["club"] = f"{result['club']} {time_text}".strip()
                            result["timeText"] = {
                                "dns": "DNS", "dnf": "DNF", "mp": "MP", "dsq": "DSQ"
                            }[inferred_status]
                        result["status"] = (inferred_status or
                                            ("ok" if result.get("rank") is not None
                                             else "unknown"))
                        if not result["timeText"] and inferred_status:
                            result["timeText"] = {
                                "dns": "DNS", "dnf": "DNF", "mp": "MP", "dsq": "DSQ"
                            }.get(inferred_status, inferred_status.upper())
                    yob = rec.get("Jg", "").strip()
                    if yob.isdigit():
                        y = int(yob)
                        result["yearOfBirth"] = y + (2000 if y <= 26 else 1900) if y < 100 else y
                    if rec.get("Pkt"):
                        result["scoreText"] = rec["Pkt"].strip()
                    if not result.get("scoreText"):
                        score_text = (rec.get("Punkte") or rec.get("Posten") or "").strip()
                        if score_text:
                            score_match = re.search(r"\b(\d+(?:[.,]\d+)?\s*(?:Posten|Punkte?))\b",
                                                    score_text, re.I)
                            if score_match:
                                overflow = score_text[:score_match.start()].strip()
                                if overflow:
                                    result["club"] = f"{result['club']} {overflow}".strip()
                                score_text = score_match.group(1)
                            result["scoreText"] = score_text
                    # Score events print the shared rank only on the first
                    # row of an equal-score group. The following blank-rank
                    # rows keep that rank even when their elapsed times differ.
                    if (result.get("rank") is None and result.get("scoreText")
                            and current["results"]):
                        previous = current["results"][-1]
                        if (previous.get("rank") is not None
                                and previous.get("scoreText") == result["scoreText"]
                                and previous.get("status") == "ok"
                                and result.get("status") == "ok"):
                            result["rank"] = previous["rank"]
                    implicit_pair = (
                        school_row_mode and "schnupper" in current["name"].casefold()
                        and len(name.split()) == 4
                    )
                    if implicit_pair:
                        school_pair_counter += 1
                        pair_names = [" ".join(name.split()[:2]), " ".join(name.split()[2:])]
                        for pair_name in pair_names:
                            pair_result = dict(result)
                            pair_result.update({
                                "name": pair_name, "resultKind": "pair",
                                "teamNumber": f"school-pair-{school_pair_counter}",
                                "note": "Partner: " + next(
                                    other for other in pair_names if other != pair_name),
                            })
                            current["results"].append(pair_result)
                    else:
                        current["results"].append(result)

    categories = repair_wrapped_champion_names(path, categories)
    categories = normalize_exact_time_ties(
        [c for c in categories if c["results"]])
    course_split = []
    for category in categories:
        course_names = {
            result.get("sourceCourse") for result in category["results"]
            if result.get("sourceCourse")
        }
        if course_names and all(result.get("sourceCourse") for result in category["results"]):
            for course_name in sorted(course_names):
                results = [result for result in category["results"]
                           if result.get("sourceCourse") == course_name]
                for result in results:
                    result.pop("sourceCourse", None)
                course_split.append({
                    "name": f"Bahn {course_name}",
                    "declaredStarters": len(results),
                    "results": results,
                })
        else:
            course_split.append(category)
    categories = course_split
    for category in categories:
        prior_scores = []
        for result in category["results"]:
            score_match = re.search(r"\d+(?:[.,]\d+)?", result.get("scoreText") or "")
            if not score_match:
                continue
            score = float(score_match.group().replace(",", "."))
            if result.get("rank") is None and prior_scores:
                # Competition ranking by score: 1 + number of preceding
                # strictly better scores. This also recovers a whole tied
                # group whose first rank cell was blank in the source PDF.
                result["rank"] = 1 + sum(previous > score for previous in prior_scores)
            prior_scores.append(score)
    categories = merge_category_continuations(categories)
    categories = split_by_kat(categories)
    categories = merge_category_continuations(categories)
    categories = normalize_school_schnupper_pairs(categories)
    categories = prefer_referenced_html_source(categories, head_text)
    # prefer_referenced_html_source can replace the initially repaired PDF
    # rows with an independently parsed referenced layout carrying the same
    # wrapped champion artifact; apply the deterministic repair once more to
    # the final selected row set.
    categories = repair_wrapped_champion_names(path, categories)
    # Specialized row paths above (score/course, pair and legacy layouts)
    # do not all pass through the ordinary fixed-column repair call. Apply
    # the same conservative club/time boundary normalization once to every
    # finished result so those paths cannot retain a split hour/minute.
    for category in categories:
        for result in category.get("results") or []:
            repair_shifted_name_club_time(result)
            fixed_club, fixed_time = repair_result_club_and_value(
                result.get("club") or "", result.get("timeText") or "")
            if (fixed_club, fixed_time) != (
                    result.get("club") or "", result.get("timeText") or ""):
                result["club"], result["timeText"] = fixed_club, fixed_time
                seconds = parse_time_loose(fixed_time)
                if seconds is not None:
                    result["timeS"] = seconds
                    result["status"] = "ok"
        repair_rank_order_embedded_time_markers(category.get("results") or [])
    normalize_championship_overall_ranks(categories)
    normalize_rank_time_consensus(categories)
    if re.search(r"\bOEScore(?:\d{4})?\b|\bSCORE[- ]?OL\b", head_text, re.I):
        for category in categories:
            for result in category.get("results") or []:
                result["rankingBasis"] = "score"
    # Some late pages shift the visual result rows left while leaving the
    # repeated Pl/tnr/Name header at its old x positions. The column parser
    # then retains only fragments (event 3642: 1 of 6 and 1 of 9), although
    # the plain text flow still contains every complete row. Replace only an
    # incomplete ordinary category for which the independent flow parser
    # recovers *exactly* the source-declared number of units. This exact-count
    # cross-check is deliberately stronger than merely preferring more rows.
    incomplete_names = {
        category["name"] for category in categories
        if category.get("declaredStarters") is not None
        and category_competitor_unit_count(category) < category["declaredStarters"]
        and all((row.get("resultKind") or "individual") in ("individual", "pair")
                for row in category.get("results") or [])
    }
    if (incomplete_names and not RELAY_HEADER_RE.search(head_text)
            and not RELAY_TITLE_RE.search(head_text)):
        flow_by_name = {
            category["name"]: category for category in parse_flowing_pdf(path)
            if category["name"] in incomplete_names
        }

        def clean_flow_candidate(result):
            result = dict(result)
            embedded_club, name_tokens = find_trailing_club(
                (result.get("name") or "").split(), CLUBS)
            if embedded_club and len(name_tokens) >= 2:
                result["name"] = " ".join(name_tokens)
                result["club"] = embedded_club
            return result

        def result_name_key(result):
            return re.sub(
                r"[^0-9a-zäöüß]+", " ",
                (result.get("name") or "").casefold()).strip()

        for category in categories:
            candidate = flow_by_name.get(category["name"])
            declared = category.get("declaredStarters")
            if (candidate and declared is not None
                    and category_competitor_unit_count(candidate) == declared):
                candidate_results = [
                    clean_flow_candidate(result)
                    for result in candidate["results"]]
                current_count = category_competitor_unit_count(category)
                if current_count * 2 < declared and all(
                        len((row.get("name") or "").split()) >= 2
                        and not is_junk_name(row.get("name") or "")
                        for row in candidate_results):
                    category["results"] = candidate_results
                    continue
                current_names = {
                    result_name_key(result) for result in category["results"]}
                missing = [
                    result for result in candidate_results
                    if result_name_key(result) not in current_names]
                if len(missing) == declared - current_count:
                    category["results"].extend(missing)
    if re.search(r"^NÖ\s*$", head_text, re.M) and re.search(r"\bNÖ\s+MS\b", head_text):
        # This export is an explicit Lower-Austria-only subranking. Its class
        # heading repeats the size of the parent race, while only NÖ-eligible
        # rows (with deliberately gapped overall ranks) belong in this file.
        # The reliable declared size for this source is therefore the number
        # of visible subranking units, not the parent heading's total.
        for category in categories:
            category["declaredStarters"] = None
    for c in categories:
        if c["declaredStarters"] is None:
            unit_keys = []
            for index, result in enumerate(c["results"]):
                if result.get("resultKind") == "pair":
                    unit_keys.append((
                        "pair", result.get("rank"), result.get("status"),
                        result.get("timeS"), result.get("club")))
                else:
                    unit_keys.append(("row", index))
            c["declaredStarters"] = len(set(unit_keys))
    categories = repair_known_pdf_extraction_artifacts(path, categories)
    return categories, head_text


def parse_flow_category_line(text):
    """Recognize a category header in the numbered-list layout. Returns
    (name, declaredStarters_or_None) or None. Guards against a numbered data
    row ('1. Erik Simkovics ... 1 (Posten 60)') being mistaken for one: a
    genuine category never starts with a rank prefix."""
    if RANK_PREFIX_RE.match(text.split(" ", 1)[0]):
        return None
    if MEOS_PAGE_HEADER_RE.match(text):
        return None
    m = (COURSE_ONLY_CAT_RE.match(text) or BAHN_CAT_RE.match(text)
         or BAHN_ONLY_CAT_RE.match(text))
    if m:
        return m.group("name").strip(), None
    m = FLOW_CAT_RE.match(text)
    if m and re.search(r"\(\d", text):
        if ((m.group("rest") or "").lstrip().startswith(":")
                or re.search(r"\bPos\.?\s*\d", m.group("name"), re.I)):
            return None
        # ``OPEN (2500m, 40m)`` is a course length, not 2500 starters.
        if re.match(r"\s*(?:m|km)\b", m.group("rest") or "", re.I):
            return m.group("name").strip(), None
        if ("preliminary" in (m.group("rest") or "").casefold()
                and ")" not in text and "/" not in text):
            return m.group("name").strip(), None
        name = re.sub(r"\s*\(\d+\s*Min\.?\)\s*$", "", m.group("name"),
                      flags=re.I).strip()
        return name, category_starter_count(m)
    m = UNCOUNTED_COURSE_CAT_RE.match(text)
    if m:
        return m.group("name").strip(), None
    m = PRELIMINARY_CAT_RE.match(text)
    if m:
        return m.group("name").strip(), None
    m = UNCOUNTED_STATUS_CAT_RE.match(text)
    if m:
        return m.group("name").strip(), None
    m = FLOW_CAT_PLAIN_RE.match(text)
    if m:
        return m.group("name").strip(), None
    return None


def parse_flow_result_row(text, clubs):
    """Parse one result row from the numbered-list layout: '25. Josef Hilbert
    Naturfreunde Wien 34:08 1 (Posten 53)' or, for a non-finisher, 'Doris
    Gittmaier HSV Ried Fehlst'. Unlike parse_flow_row() (built for the
    Pl/Stnr-column PDFs' flowing fallback), this format's trailing fields
    after the finish time vary (Rückstand, Zeit verloren, penalty notes), so
    it takes the *first* time-like token as the finish time and discards
    everything after it, rather than requiring the time to be the last token."""
    toks = text.split()
    if not toks:
        return None
    forced_ooc = False
    if re.fullmatch(r"A\.?\s*K\.?", toks[0], re.I):
        # "außer Konkurrenz" - non-competitive entry
        forced_ooc = True
        toks = toks[1:]
    if not toks:
        return None
    rank = None
    if RANK_PREFIX_RE.match(toks[0]):
        rank = int(toks[0].rstrip("."))
        toks = toks[1:]
        # Headerless OE result lists carry both placement and start number.
        # The first-place champion annotation is printed on a separate line,
        # so its winner row only has the start number; every following row
        # has ``rank bib name ...``.
        if toks and toks[0].isdigit():
            toks = toks[1:]
    if not toks:
        return None

    time_idx = next(
        (i for i, t in enumerate(toks) if FLOW_TIME_RE.match(t.strip("()"))), None)
    if time_idx is not None:
        raw_time = toks[time_idx]
        body, time_text, status_text = toks[:time_idx], raw_time.strip("()").lstrip("+"), None
        trailing_text = " ".join(toks[time_idx + 1:])
        explicit_status = parse_status(trailing_text)
        score_text = None
        if body and re.fullmatch(r"-?\d+(?:[.,]\d+)?", body[-1]):
            # OEScore rows place points immediately before elapsed time and
            # optionally repeat the adjusted score afterwards. Without
            # peeling this value, the club is no longer the trailing token
            # sequence and otherwise valid competitors disappear.
            score_text = body.pop()
    else:
        joined = " ".join(toks)
        m = STATUS_TAIL_RE.search(joined)
        if not m:
            return None
        status_text, time_text = m.group(0).strip(), None
        body = joined[: m.start()].split()
        score_text = None

    if not body:
        return None
    club, name_toks = find_trailing_club(body, clubs)
    yob = None
    if club is None:
        year_at = next((i for i, token in enumerate(body)
                        if i >= 2 and i < len(body) - 1
                        and re.fullmatch(r"\d{2}|\d{4}", token)), None)
        if year_at is not None:
            # International/guest clubs are not necessarily present in the
            # Austrian club dictionary.  The explicit birth-year column is a
            # reliable boundary: surname+forename | Jg | complete club.
            name_toks, yob = body[:year_at], body[year_at]
            club = " ".join(body[year_at + 1:])
        else:
            # Compact hand-made lists can omit the club column altogether
            # (``2. Martina Zeiner 1:48:23``). With exactly two person-like
            # tokens, treating the surname as a club corrupts both fields.
            club, name_toks = (("", body) if len(body) == 2 else
                               ((body[-1], body[:-1]) if len(body) > 1 else ("", body)))
    if yob is None and name_toks and re.fullmatch(r"\d{2}|\d{4}", name_toks[-1]):
        yob = name_toks.pop()
    name = " ".join(name_toks).strip()
    name = re.sub(r"^([^,]+),\s+(.+)$", r"\1 \2", name)
    pair_names = split_pair_names(name) if "/" in name or "+" in name else [name]
    valid_pair = (len(pair_names) > 1 and all(
        len(pair_name.split()) == 2 and looks_like_person(pair_name)
        for pair_name in pair_names))
    if is_junk_name(name) or (not looks_like_person(name) and not valid_pair):
        return None

    result = {"name": name, "club": club or "", "timeText": time_text or status_text or ""}
    if rank is not None:
        result["rank"] = rank
    if score_text is not None:
        result["scoreText"] = score_text
    seconds = parse_time(time_text) if time_text else None
    if seconds is not None:
        result["timeS"] = seconds
    if yob:
        year = int(yob)
        result["yearOfBirth"] = year + (2000 if year <= 26 else 1900) if year < 100 else year
    result["status"] = (explicit_status if time_idx is not None and explicit_status
                        not in (None, "ok") else "ok" if seconds is not None
                        else (parse_status(status_text or "") or "unknown"))
    if time_idx is None:
        # In unranked DNS/DNF/MP/qualitative rows the only leading number is
        # the start number, not a placement (e.g. ``894 Name ... Gut``).
        result.pop("rank", None)
    if (forced_ooc or is_ooc_status(status_text)
            or (time_idx is not None and is_ooc_status(trailing_text))):
        result["outOfCompetition"] = True
    if time_idx is not None and raw_time.startswith("("):
        result["outOfCompetition"] = True
    return result


def parse_flowing_pdf(path):
    """Fallback for the numbered-list layout (no Pl/Stnr/Verein columns) that
    parse_pdf() can't see at all, since it never finds a "Pl"/"Platz" header
    to anchor on. Works on plain extracted text, not word x-positions -
    there are no columns to align."""
    import pdfplumber

    categories, current = [], None
    pending_rank = pending_championship = None
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                for line in (page.extract_text() or "").split("\n"):
                    line = line.strip()
                    if (not line or DATE_HEADER_RE.search(line)
                            or MEOS_PAGE_HEADER_RE.match(line)):
                        continue
                    # Championship result lists can put the overall place,
                    # then the national place (or ``*)`` ineligible marker)
                    # before the runner: ``2. 1. Name ...`` and
                    # ``3. *) Name ...``. The first number is the result-list
                    # rank; the second token is an annotation, not a bib or
                    # part of the name.
                    line = re.sub(
                        r"^(\d{1,3})\.\s+(?:\d{1,3}\.\s+)?(?:\*\)\s*)?",
                        r"\1 ", line)
                    if line.casefold() == "mannschaftswertung":
                        current = None
                        continue
                    detached_rank = re.fullmatch(
                        r"(\d{1,3})(?:\s+\d{1,3})*(?:\s+\([^)]*\))?", line)
                    if current is not None and detached_rank:
                        pending_rank = int(detached_rank.group(1))
                        continue
                    annot_rank, annot_championship = parse_champion_annotation(line)
                    if annot_rank is not None:
                        pending_rank, pending_championship = annot_rank, annot_championship
                        continue
                    cat = parse_flow_category_line(line)
                    if cat:
                        name, starters = cat
                        if current and current["name"] == name:
                            continue  # repeated header across pages
                        current = {"name": name, "declaredStarters": starters, "results": []}
                        categories.append(current)
                        pending_rank = pending_championship = None
                        continue
                    if current is None:
                        continue
                    bracket_rank = re.match(r"^\((\d+)\)\s*(.+)$", line)
                    if bracket_rank:
                        # Foreign/non-championship finishers are printed with
                        # their overall place in parentheses, occasionally
                        # glued directly to the surname.  Preserve the row as
                        # OOC instead of feeding ``(2)Surname`` to the name
                        # validator and losing the person entirely.
                        line = bracket_rank.group(2)
                    flow = parse_flow_row(line, CLUBS)
                    if valid_flow(flow):
                        if bracket_rank:
                            flow["rank"] = int(bracket_rank.group(1))
                            flow["outOfCompetition"] = True
                        # Older night championships list the -12/-14 pairs
                        # as either ``A/B`` or four consecutive name tokens.
                        # The latter has no textual separator, but in these
                        # explicitly paired child classes its 2+2 structure
                        # is unambiguous and both people must stay clickable.
                        if (len(flow["names"]) == 1
                                and re.search(r"(?:^|\s)[DH]-?(?:12|14)(?:\s|$)",
                                              current["name"], re.I)
                                and len(flow["names"][0].split()) == 4):
                            tokens = flow["names"][0].split()
                            flow["names"] = [" ".join(tokens[:2]), " ".join(tokens[2:])]
                        rows = flow_results(flow)
                        if pending_rank is not None:
                            for result in rows:
                                result["rank"] = pending_rank
                                if pending_championship:
                                    result["championship"] = pending_championship
                        pending_rank = pending_championship = None
                        current["results"].extend(rows)
                        continue
                    row = parse_flow_result_row(line, CLUBS)
                    if row:
                        if pending_rank is not None:
                            # A detached champion line consumes rank 1. The
                            # following row consequently starts with its bib,
                            # which the flow fallback otherwise mistakes for
                            # a placement (e.g. 226 instead of rank 1).
                            row["rank"] = pending_rank
                            if pending_championship:
                                row["championship"] = pending_championship
                        pending_rank = pending_championship = None
                        current["results"].extend(
                            expand_pair_result(row, current.get("name")))

    categories = [c for c in categories if c["results"]]
    categories = merge_category_continuations(categories)
    categories = normalize_exact_time_ties(categories)
    categories = normalize_school_schnupper_pairs(categories)
    for c in categories:
        for result in c["results"]:
            repair_shifted_name_club_time(result)
            fixed_club, fixed_time = repair_result_club_and_value(
                result.get("club") or "", result.get("timeText") or "")
            if (fixed_club, fixed_time) != (
                    result.get("club") or "", result.get("timeText") or ""):
                result["club"], result["timeText"] = fixed_club, fixed_time
                seconds = parse_time_loose(fixed_time)
                if seconds is not None:
                    result["timeS"] = seconds
                    result["status"] = "ok"
        repair_rank_order_embedded_time_markers(c.get("results") or [])
        if "famil" in (c.get("name") or "").casefold():
            # Family lists can print the same named combination more than
            # once with different outcomes (event 2675 lists Grozak
            # Ivan+Anna once MP and once DNS). They are two physical source
            # starts even though identity handling deliberately collapses
            # each row to a non-person family result during the DB build.
            c["sourceUnitCount"] = category_competitor_unit_count(c)
        if c["declaredStarters"] is None:
            unit_keys = []
            for index, result in enumerate(c["results"]):
                if result.get("resultKind") == "pair":
                    unit_keys.append((
                        "pair", result.get("rank"), result.get("status"),
                        result.get("timeS"), result.get("club")))
                else:
                    unit_keys.append(("row", index))
            c["declaredStarters"] = len(set(unit_keys))
    normalize_championship_overall_ranks(categories)
    return categories


def parse_glued_header_pdf(path):
    """Parse compact ``PlName JgVerein Zeit`` exports.

    The PDF font removes every space at the column boundaries: placement is
    glued to the surname (``1Pötsch``) and year to the club (``14OLC Graz``).
    The ordinary x-column parser never sees a ``Pl`` header and the flowing
    fallback consequently drops almost the complete field.
    """
    import pdfplumber

    categories, current = [], None
    pending_rank = pending_championship = None
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                for raw in (page.extract_text(layout=True) or "").splitlines():
                    line = re.sub(r"\s+", " ", raw).strip()
                    if not line or PDF_PAGE_CHROME_RE.search(line):
                        continue
                    category = CAT_LINE_RE.match(line)
                    if category:
                        current = {
                            "name": category.group("name").strip(),
                            "declaredStarters": category_starter_count(category),
                            "results": [],
                        }
                        categories.append(current)
                        pending_rank = pending_championship = None
                        continue
                    if current is None or re.match(r"^PlName\s+JgVerein\s+Zeit", line):
                        continue
                    annotation_line = re.sub(
                        r"^(\d{1,3})und\b", r"\1 und", line, flags=re.I)
                    annotation_rank, annotation_championship = parse_champion_annotation(
                        annotation_line)
                    if annotation_rank is not None:
                        pending_rank = annotation_rank
                        pending_championship = annotation_championship
                        continue
                    value_match = re.search(
                        r"(?P<value>\d{1,3}:\d{2}(?::\d{2})?|"
                        r"Aufg\.?|Fehlst\.?|Disqu?\.?|N\.?\s*Ang\.?)\s*$",
                        line, re.I)
                    if not value_match:
                        continue
                    body = line[:value_match.start()].strip()
                    rank_match = re.match(r"^(?P<rank>\d{1,3})(?=[A-Za-zÀ-ž])", body)
                    rank = int(rank_match.group("rank")) if rank_match else None
                    if rank_match:
                        body = body[rank_match.end():]
                    body = re.sub(r"\s+(\d{2})(?=[A-ZÄÖÜ])", r" \1 ", body)
                    club, name_tokens = find_trailing_club(body.split(), CLUBS)
                    if not club:
                        continue
                    yob = None
                    if name_tokens and re.fullmatch(r"\d{2}|\d{4}", name_tokens[-1]):
                        yob = name_tokens.pop()
                    name = " ".join(name_tokens).strip()
                    if not looks_like_person(name) or is_junk_name(name):
                        continue
                    value = value_match.group("value").strip()
                    seconds = parse_time_loose(value)
                    result = {"name": name, "club": club, "timeText": value,
                              "status": "ok" if seconds is not None else
                                        (parse_status(value) or "unknown")}
                    if rank is not None:
                        result["rank"] = rank
                    elif pending_rank is not None:
                        result["rank"] = pending_rank
                        if pending_championship:
                            result["championship"] = pending_championship
                    pending_rank = pending_championship = None
                    if seconds is not None:
                        result["timeS"] = seconds
                    if yob:
                        year = int(yob)
                        result["yearOfBirth"] = (year + (2000 if year <= 26 else 1900)
                                                 if year < 100 else year)
                    current["results"].append(result)
    return [category for category in categories if category["results"]]


def parse_school_score_pdf(path):
    """Parse the ASVÖ school score layout with score columns after Schule."""
    import pdfplumber

    categories, current = [], None
    category_re = re.compile(
        r"^(Unterstufe|Oberstufe)\s*[‐–-]\s*(männlich|weiblich)\s*$", re.I)
    row_re = re.compile(
        r"^(?P<rank>\d{1,3})\s+(?P<chip>\d+)\s+(?P<name>.+?)\s+"
        r"(?P<time>\d{2}:\d{2}:\d{2})\s+(?P<school>.+?)\s+"
        r"Fun\s+[mw]\s+(?:\d{2}:\d{2}:\d{2}\s+)?\d+\s+[‐–-]?\d+\s+"
        r"(?P<score>\d+)\s*$", re.I)
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            for raw in (page.extract_text(layout=True) or "").splitlines():
                line = re.sub(r"\s+", " ", raw).strip()
                category = category_re.match(line)
                if category:
                    current = {"name": f"{category.group(1).title()} – "
                                      f"{category.group(2).lower()}",
                               "declaredStarters": None, "results": []}
                    categories.append(current)
                    continue
                if current is None:
                    continue
                row = row_re.match(line)
                if not row:
                    continue
                time_text = row.group("time")
                current["results"].append({
                    "name": row.group("name").strip(),
                    "club": row.group("school").strip(),
                    "timeText": time_text, "timeS": parse_time(time_text),
                    "status": "ok", "rank": int(row.group("rank")),
                    "scoreText": row.group("score"),
                })
    for category in categories:
        category["declaredStarters"] = len(category["results"])
    return [category for category in categories if category["results"]]


def parse_school_final_pdf(path):
    """Parse the 2014 school championship's glued bib/year columns."""
    import pdfplumber

    categories, current = [], None
    category_re = re.compile(
        r"^(Unterstufe\s+(?:männlich|weiblich)|Oberstufe\s+(?:männlich|weiblich)|"
        r"R1\s+Unterstufe|R1\s+Unter-u\.?Obe)\s+Endergebnis$", re.I)
    school_codes = r"FF|KIGA|GIBS|Pest|See|URS|Leib|LI|HTBLVA|MONS|KEP|HIB|WIKU|KLUSE"
    row_re = re.compile(
        r"^(?:(?P<rank>\d{1,3})\s+)?(?P<bib>\d{1,3})\s*(?P<body>.+?)\s+"
        r"(?P<value>\d{1,3}:\d{2}(?::\d{2})?|Fehlst|Aufg|Disqu?)\s*$", re.I)
    split_re = re.compile(
        rf"^(?P<name>.+?)\s+(?:(?P<yob>\d{{1,2}}))?"
        rf"(?P<code>{school_codes})\s+(?P<place>.+)$", re.I)
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            for raw in (page.extract_text(layout=True) or "").splitlines():
                line = re.sub(r"\s+", " ", raw).strip()
                category = category_re.match(line)
                if category:
                    current = {"name": category.group(1), "declaredStarters": None,
                               "results": []}
                    categories.append(current)
                    continue
                if current is None:
                    continue
                row = row_re.match(line)
                if not row:
                    continue
                split = split_re.match(row.group("body"))
                if not split:
                    continue
                value = row.group("value")
                seconds = parse_time_loose(value)
                result = {
                    "name": split.group("name").strip(),
                    "club": f"{split.group('code')} {split.group('place')}".strip(),
                    "timeText": value,
                    "status": "ok" if seconds is not None else
                              (parse_status(value) or "unknown"),
                }
                if row.group("rank"):
                    result["rank"] = int(row.group("rank"))
                if seconds is not None:
                    result["timeS"] = seconds
                if split.group("yob"):
                    year = int(split.group("yob"))
                    result["yearOfBirth"] = year + (2000 if year <= 26 else 1900)
                current["results"].append(result)
    for category in categories:
        category["declaredStarters"] = len(category["results"])
    return [category for category in categories if category["results"]]


def parse_excel_web_pdf(path):
    """Parse PDFs printed from SportSoftware's historic Excel web export.

    These have no usable PDF columns and glue bib/year fields to the next
    token (``55Breitschädl ... 78ASKÖ Henndorf``). The normal flowing parser
    consequently retained mostly foreign rows without those numeric fields
    and lost the Austrian championship field. Normalize only those structural
    numeric joins, then reuse the ordinary club/name/status parser.
    """
    import pdfplumber

    categories, current = [], None
    pending_rank = pending_championship = None
    annotation_re = re.compile(
        r"^(?P<rank>\d+)\s+\d*und\s+österreich\w*\s+.*meister", re.I)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                for raw_line in (page.extract_text() or "").split("\n"):
                    line = raw_line.strip()
                    if (not line or "ExcelWebPagePrevi" in line
                            or PDF_PAGE_CHROME_RE.search(line)
                            or re.match(r"^\d+\s+von\s+\d+\b", line, re.I)):
                        continue
                    category_match = CAT_LINE_RE.match(line)
                    if category_match:
                        current = {
                            "name": category_match.group("name").strip(),
                            "declaredStarters": category_starter_count(category_match),
                            "results": [],
                        }
                        current.update(parse_course_info(category_match.group("rest")))
                        categories.append(current)
                        pending_rank = pending_championship = None
                        continue
                    annotation = annotation_re.match(line)
                    if annotation:
                        pending_rank = int(annotation.group("rank"))
                        pending_championship = classify_championship_text(line)
                        continue
                    if current is None:
                        continue

                    line = re.sub(r"^\(keine\s+ÖMS\)\s*", "", line, flags=re.I)
                    rank = None
                    ranked = re.match(r"^(\d+)\s+(.+)$", line)
                    if ranked:
                        rank, line = int(ranked.group(1)), ranked.group(2)
                        # Optional national/Tageswertung rank before the bib:
                        # ``2 2 53Schachinger`` -> retain overall rank 2,
                        # discard TW rank 2, then strip bib 53 below.
                        tw = re.match(r"^\d+\s+(?=\d+[A-Za-zÀ-žÄÖÜäöü])", line)
                        if tw:
                            line = line[tw.end():]
                    line = re.sub(r"^\d+(?=[A-Za-zÀ-žÄÖÜäöü])", "", line)
                    # Birth year glued to the first club token.
                    line = re.sub(r"\b\d{1,2}(?=[A-ZÄÖÜ])", "", line)
                    flow = parse_flow_row(line, CLUBS)
                    if not valid_flow(flow):
                        continue
                    if rank is not None:
                        flow["rank"] = rank
                    rows = flow_results(flow)
                    if pending_rank is not None:
                        for result in rows:
                            result["rank"] = pending_rank
                            if pending_championship:
                                result["championship"] = pending_championship
                    pending_rank = pending_championship = None
                    current["results"].extend(rows)

    return merge_category_continuations(
        [category for category in categories if category["results"]])


def parse_meos_individual_pdf(path):
    """Parse MeOS' column-positioned individual result report.

    The plain-text fallback needs a known club dictionary to separate person
    and club. That drops foreign clubs and schools. MeOS gives stable visual
    starts for the club and result columns in every category heading, so the
    PDF coordinates provide a complete, language-independent split.
    """
    import pdfplumber

    categories, current = [], None
    pair_counter = 0
    club_x = time_x = None
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                for words in group_lines(page.extract_words(
                        use_text_flow=False, keep_blank_chars=False)):
                    if not words:
                        continue
                    text = " ".join(word["text"] for word in words).strip()
                    if (not text or MEOS_PAGE_HEADER_RE.match(text)
                            or DATE_HEADER_RE.search(text)
                            or PDF_PAGE_CHROME_RE.search(text)):
                        continue
                    category_match = FLOW_CAT_RE.match(text)
                    time_header = next(
                        (word for word in words
                         if word["text"].casefold() in {"time", "zeit"}), None)
                    count_header = next(
                        (word for word in words if word["text"].startswith("(")), None)
                    if category_match and time_header and count_header:
                        name = re.sub(
                            r"\s*\(\d+\s*Min\.?\)\s*$", "",
                            category_match.group("name"), flags=re.I).strip()
                        if current and current["name"] == name:
                            club_x, time_x = count_header["x0"], time_header["x0"]
                            continue
                        current = {
                            "name": name,
                            "declaredStarters": category_starter_count(category_match),
                            "results": [],
                        }
                        categories.append(current)
                        pair_counter = 0
                        club_x, time_x = count_header["x0"], time_header["x0"]
                        continue
                    if current is None or club_x is None or time_x is None:
                        continue

                    row_words = list(words)
                    rank = None
                    out_of_competition = False
                    if row_words and re.fullmatch(r"\d+\.", row_words[0]["text"]):
                        rank = int(row_words.pop(0)["text"].rstrip("."))
                    elif row_words and row_words[0]["text"].strip("*").casefold() == "ak":
                        out_of_competition = True
                        row_words.pop(0)
                    # Embedded-font rounding moves identically aligned glyphs
                    # by tiny fractions of a point from one row to the next.
                    # Use a one-point boundary tolerance so a club/time that
                    # starts 0.0001pt left of its header does not slide into
                    # the preceding field (confirmed in MeOS event 3739).
                    boundary_tolerance = 1.0
                    name = " ".join(
                        word["text"] for word in row_words
                        if word["x0"] < club_x - boundary_tolerance).strip()
                    club = " ".join(
                        word["text"] for word in row_words
                        if (club_x - boundary_tolerance <= word["x0"]
                            < time_x - boundary_tolerance)).strip()
                    value = " ".join(
                        word["text"] for word in row_words
                        if word["x0"] >= time_x - boundary_tolerance).strip()
                    # MeOS writes child/pair entries with '/', '&' or '+'. A
                    # trailing '*' is only a footnote. Keep the real people
                    # even when the other member is a non-person placeholder
                    # such as ``Begleitung``.
                    is_pair = any(separator in name for separator in ("/", "&", "+"))
                    raw_pair_names = split_pair_names(name) if is_pair else [name]
                    raw_pair_names = [re.sub(r"\*+$", "", part).strip()
                                      for part in raw_pair_names]
                    pair_names = [part for part in raw_pair_names
                                  if looks_like_person(part)
                                  and not re.fullmatch(
                                      r"(?i)begleitung|begl\.?|und andere|and others", part)]
                    time_match = re.search(r"\(?\d{1,3}:\d{2}(?::\d{2})?\)?", value)
                    if time_match:
                        raw_time = time_match.group(0)
                        out_of_competition = out_of_competition or raw_time.startswith("(")
                        time_text = raw_time.strip("()")
                        seconds = parse_time(time_text)
                        status = "ok"
                    else:
                        status = parse_status(value) or "unknown"
                        time_text = value
                        seconds = None
                    if not time_text:
                        continue
                    if not name and club and (rank is not None or time_text):
                        # The source itself can contain a ranked, timed row
                        # with only a club and no participant names. Preserve
                        # the result unit without inventing a person.
                        pair_counter += 1
                        result = {
                            "name": "", "club": club, "timeText": time_text,
                            "status": status, "resultKind": "pair",
                            "memberlessTeam": True,
                            "teamNumber": f"pair-{pair_counter}",
                            "teamName": club,
                            "note": "Paar ohne Teilnehmernamen in der Quelle",
                            "teamStatus": status, "teamTimeText": time_text,
                        }
                        if rank is not None:
                            result["rank"] = rank
                        if seconds is not None:
                            result["timeS"] = seconds
                            result["teamTimeS"] = seconds
                        if out_of_competition or is_ooc_status(value):
                            result["outOfCompetition"] = True
                        current["results"].append(result)
                        continue
                    if not pair_names or (not is_pair and is_junk_name(name)):
                        continue
                    if is_pair:
                        pair_counter += 1
                    for pair_name in pair_names:
                        result = {"name": pair_name, "club": club,
                                  "timeText": time_text, "status": status}
                        if is_pair:
                            result.update({
                                "resultKind": "pair",
                                "teamNumber": f"pair-{pair_counter}",
                                "note": "Partner: " + ", ".join(
                                    other for other in raw_pair_names if other != pair_name),
                            })
                        if rank is not None:
                            result["rank"] = rank
                        if seconds is not None:
                            result["timeS"] = seconds
                        if out_of_competition or is_ooc_status(value):
                            result["outOfCompetition"] = True
                        current["results"].append(result)

    return [category for category in categories if category["results"]]


def parse_meos_relay_pdf(path):
    """Parse MeOS' indented team/member relay report.

    MeOS has no explicit table header. Team rows start at the category's left
    edge, member rows are indented, and ``(classified / entered)`` belongs to
    the category. Keeping those structural signals avoids counting three or
    four legs as separate starts and prevents page headers such as ``(2/4)``
    from becoming synthetic categories.
    """
    import pdfplumber

    categories, current, pending_team = [], None, None
    category_x = None
    team_name_counts = defaultdict(int)

    def flush():
        nonlocal pending_team
        if pending_team and current is not None:
            current["sourceUnitCount"] = current.get("sourceUnitCount", 0) + 1
        if not pending_team or current is None:
            pending_team = None
            return
        if not pending_team["members"]:
            team_status = pending_team.get("status") or "unknown"
            if team_status != "unknown" or pending_team.get("rank") is not None:
                result = {
                    "name": "", "club": pending_team["club"],
                    "timeText": pending_team.get("timeText") or "",
                    "resultKind": "relay", "memberlessTeam": True,
                    "note": f"Staffel: {pending_team['displayName']} · keine Teilnehmernamen in der Quelle",
                    "status": team_status, "individualStatus": None,
                    "teamStatus": team_status, "teamNumber": None,
                    "teamName": pending_team["displayName"],
                    "teamTimeText": pending_team.get("timeText") or "",
                }
                if pending_team.get("rank") is not None:
                    result["rank"] = pending_team["rank"]
                if pending_team.get("timeS") is not None:
                    result["teamTimeS"] = pending_team["timeS"]
                if pending_team.get("outOfCompetition"):
                    result["outOfCompetition"] = True
                current["results"].append(result)
            pending_team = None
            return
        names = [member["name"] for member in pending_team["members"]]
        member_statuses = []
        for member in pending_team["members"]:
            seconds = parse_time_loose(member["timeText"]) if member["timeText"] else None
            member_statuses.append(
                "ok" if seconds is not None else
                (parse_status(member["timeText"] or "") or "unknown"))
        team_status = aggregate_team_status(pending_team.get("status"), member_statuses)
        for index, member in enumerate(pending_team["members"]):
            seconds = parse_time_loose(member["timeText"]) if member["timeText"] else None
            mates = list(dict.fromkeys(name for name in names if name != member["name"]))
            notes = [f"Staffel: {pending_team['displayName']}",
                     f"Leg {index + 1}/{len(names)}"]
            if mates:
                notes.append("Team: " + ", ".join(mates))
            result = {
                "name": member["name"], "club": pending_team["club"],
                "timeText": member["timeText"], "resultKind": "relay",
                "note": " · ".join(notes), "status": team_status,
                "individualStatus": member_statuses[index],
                "teamStatus": team_status, "teamNumber": None,
                "teamName": pending_team["displayName"],
                "leg": index + 1, "legCount": len(names),
                "teamTimeText": pending_team["timeText"],
            }
            if pending_team.get("rank") is not None:
                result["rank"] = pending_team["rank"]
            if pending_team.get("timeS") is not None:
                result["teamTimeS"] = pending_team["timeS"]
            if seconds is not None:
                result["timeS"] = seconds
            if pending_team.get("outOfCompetition"):
                result["outOfCompetition"] = True
            current["results"].append(result)
        pending_team = None

    def start_team(text):
        nonlocal pending_team
        raw = text.strip()
        out_of_competition = False
        rank = None
        rank_match = re.match(r"^(\d+)\.\s+(.+)$", raw)
        if rank_match:
            rank, raw = int(rank_match.group(1)), rank_match.group(2)
        else:
            ak_match = re.match(r"^(?:\*AK|AK\*)\s+(.+)$", raw, re.I)
            if ak_match:
                out_of_competition, raw = True, ak_match.group(1)
        if re.search(r"\*AK\b|\bAK\*", raw, re.I):
            out_of_competition = True
            raw = re.sub(r"\s*(?:\*AK\b|\bAK\*)\s*", " ", raw,
                         flags=re.I).strip()

        # Regional champion announcements are prose between rank and team.
        raw = re.sub(r"^und\s+[^:]{1,60}Meister(?:in)?:\s*", "", raw, flags=re.I)
        time_match = re.search(r"\(?\d{1,3}:\d{2}(?::\d{2})?\)?", raw)
        if time_match:
            time_text = time_match.group(0)
            if time_text.startswith("("):
                out_of_competition = True
            time_text = time_text.strip("()")
            club = raw[:time_match.start()].strip()
        else:
            status_match = STATUS_TAIL_RE.search(raw)
            if not status_match:
                return False
            time_text = status_match.group(0).strip()
            club = raw[:status_match.start()].strip()
        if not club:
            return False
        team_name_counts[club] += 1
        display_name = (club if team_name_counts[club] == 1
                        else f"{club} {team_name_counts[club]}")
        seconds = parse_time_loose(time_text)
        pending_team = {
            "club": club, "displayName": display_name, "rank": rank,
            "timeText": time_text, "timeS": seconds,
            "status": "ok" if seconds is not None else
                      (parse_status(time_text) or "unknown"),
            "outOfCompetition": out_of_competition, "members": [],
        }
        return True

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                for words in group_lines(page.extract_words(
                        use_text_flow=False, keep_blank_chars=False)):
                    if not words:
                        continue
                    text = " ".join(word["text"] for word in words).strip()
                    if (not text or MEOS_PAGE_HEADER_RE.match(text)
                            or DATE_HEADER_RE.search(text)
                            or PDF_PAGE_CHROME_RE.search(text)):
                        continue
                    category_match = FLOW_CAT_RE.match(text)
                    if (category_match and re.search(
                            r"\b(?:Time|Zeit|Rückstand|Behind)\b",
                            category_match.group("rest") or "", re.I)):
                        flush()
                        name = category_match.group("name").strip()
                        if current and current["name"] == name:
                            continue
                        current = {
                            "name": name,
                            "declaredStarters": category_starter_count(category_match),
                            "results": [],
                        }
                        categories.append(current)
                        category_x = words[0]["x0"]
                        team_name_counts = defaultdict(int)
                        continue
                    if current is None or category_x is None:
                        continue
                    is_member = words[0]["x0"] > category_x + 8
                    numbered_member = bool(re.match(r"^\d+\.\s+", text))
                    if is_member and pending_team is not None and numbered_member:
                        member = parse_relay_member_line(text, numbered_leg=True)
                        if member:
                            pending_team["members"].append(member)
                        continue
                    # Rankless MP/DNS/DSQ/AK teams are indented to the same
                    # x-position as their leg rows. They are distinguished by
                    # the missing leading leg number, not indentation alone.
                    if not is_member or not numbered_member:
                        flush()
                        start_team(text)
            flush()

    return [category for category in categories if category["results"]]


def parse_oribos_relay_pdf(path):
    """Parse Oribos' ``RELAY RESULTS`` export.

    Oribos prints a team header (``N°: ...``), followed by one visual row per
    leg.  The first integer at the far left of the first leg is the *team*
    rank, while the next integer is the leg number.  Treating the repeated
    ``Bib. Name ...`` header as a runner was the old failure mode for event
    4645.  Reading the stable x columns also keeps cumulative ``Prog.`` time
    separate from the individual leg time.

    The youngest classes in the same report are individual rather than relay
    classes.  They use the explicit ``Pos. Name Year Team ...`` header and are
    retained as ordinary results.
    """
    import pdfplumber

    categories, current, pending_team = [], None, None
    individual_layout = False

    def oribos_time(value):
        value = (value or "").strip()
        # A parenthesized leg placing follows the actual time (``00.19.35
        # (1)``).  It is metadata, not part of the elapsed value.
        match = re.match(
            r"(?P<h>\d{1,2})[.:](?P<m>\d{2})[.:](?P<s>\d{2})(?:,\d+)?", value)
        if match:
            return (f"{int(match.group('h'))}:"
                    f"{match.group('m')}:{match.group('s')}")
        return value

    def oribos_status(value):
        folded = re.sub(r"\s+", "", (value or "")).casefold()
        if folded in {"incompleta", "incomplete", "didnotfinish"}:
            return "dnf"
        if folded in {"didnotstart", "notstarted"}:
            return "dns"
        if folded in {"missingpunch", "mispunch"}:
            return "mp"
        if folded in {"disqualified", "disqualificato"}:
            return "dsq"
        normalized = oribos_time(value)
        return "ok" if parse_time_loose(normalized) is not None else (
            parse_status(value or "") or "unknown")

    def flush_team():
        nonlocal pending_team
        if not pending_team or current is None:
            pending_team = None
            return
        members = pending_team["members"]
        if not members:
            pending_team = None
            return
        team_status = pending_team["status"]
        # A valid team time is authoritative.  For an unclassified team, its
        # printed team status applies to every leg; the member who caused it
        # still keeps the more specific individualStatus/time cell.
        if team_status == "unknown":
            team_status = aggregate_team_status(
                None, [member["status"] for member in members])
        names = [member["name"] for member in members]
        for index, member in enumerate(members):
            mates = [name for name in names if name != member["name"]]
            notes = [f"Staffel: {pending_team['name']}",
                     f"Leg {member['leg']}/{len(members)}"]
            if mates:
                notes.append("Team: " + ", ".join(mates))
            result = {
                "name": member["name"], "club": pending_team["club"],
                "timeText": member["timeText"], "status": team_status,
                "individualStatus": member["status"],
                "resultKind": "relay", "note": " · ".join(notes),
                "teamNumber": pending_team["number"],
                "teamName": pending_team["name"],
                "teamStatus": team_status,
                "teamTimeText": pending_team["timeText"],
                "leg": member["leg"], "legCount": len(members),
                # Oribos permits the same person on multiple legs. Keep each
                # explicitly numbered source row; legacy sprint-relay formats
                # remain on their established one-result-per-person model
                # until award generation is migrated with them.
                "preserveRepeatedRelayLeg": True,
            }
            if pending_team.get("rank") is not None:
                result["rank"] = pending_team["rank"]
            if pending_team.get("timeS") is not None:
                result["teamTimeS"] = pending_team["timeS"]
            if member.get("timeS") is not None:
                result["timeS"] = member["timeS"]
            current["results"].append(result)
        pending_team = None

    def text_between(words, left, right):
        return " ".join(
            word["text"] for word in words
            if left <= float(word["x0"]) < right).strip()

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                for words in group_lines(page.extract_words(
                        use_text_flow=False, keep_blank_chars=False)):
                    if not words:
                        continue
                    text = " ".join(word["text"] for word in words).strip()
                    category_match = re.match(r"^(?:…)?Class:\s*(.+)$", text, re.I)
                    if category_match:
                        flush_team()
                        name = category_match.group(1).strip()
                        if current and current["name"] == name:
                            continue
                        current = {"name": name, "declaredStarters": None,
                                   "results": []}
                        categories.append(current)
                        individual_layout = False
                        continue
                    if current is None:
                        continue
                    if re.match(r"^Pos\.\s+Name\s+Year\s+Team\b", text, re.I):
                        individual_layout = True
                        continue
                    if re.match(r"^Bib\.\s+Name\s+Nat\s+Team\s+Prog\.\s+Time$",
                                text, re.I):
                        individual_layout = False
                        continue
                    if (not text or text.startswith("(Length:")
                            or text.startswith("RELAY RESULTS")
                            or text.startswith("Arge Alp Relay")
                            or text.startswith("Creation date:")
                            or re.match(r"^pag\.\s+\d+\s+of\s+\d+$", text, re.I)
                            or text.startswith("Oribos ")):
                        continue

                    if individual_layout:
                        rank_text = text_between(words, 45, 72)
                        name = text_between(words, 72, 215)
                        year = text_between(words, 215, 255)
                        club = text_between(words, 255, 440)
                        value = text_between(words, 440, 550)
                        if not (rank_text.isdigit() and looks_like_person(name)):
                            continue
                        time_text = oribos_time(value)
                        result = {
                            "rank": int(rank_text), "name": name, "club": club,
                            "timeText": time_text, "status": oribos_status(value),
                        }
                        seconds = parse_time_loose(time_text)
                        if seconds is not None:
                            result["timeS"] = seconds
                        if year.isdigit():
                            result["yearOfBirth"] = int(year)
                        current["results"].append(result)
                        continue

                    if text.startswith("N°:"):
                        flush_team()
                        number = text_between(words, 90, 138)
                        team_name = text_between(words, 138, 300)
                        club = text_between(words, 300, 450)
                        value = text_between(words, 495, 580)
                        if not number or not team_name:
                            continue
                        time_text = oribos_time(value)
                        seconds = parse_time_loose(time_text)
                        pending_team = {
                            "number": number, "name": team_name,
                            "club": club or team_name, "rank": None,
                            "timeText": time_text, "timeS": seconds,
                            "status": oribos_status(value), "members": [],
                        }
                        continue

                    if pending_team is None:
                        continue
                    leg_text = text_between(words, 72, 92)
                    name = text_between(words, 138, 335)
                    if not leg_text.isdigit() or not looks_like_person(name):
                        continue
                    rank_text = text_between(words, 45, 72)
                    if pending_team["rank"] is None and rank_text.isdigit():
                        pending_team["rank"] = int(rank_text)
                    value = text_between(words, 500, 580)
                    time_text = oribos_time(value)
                    seconds = parse_time_loose(time_text)
                    pending_team["members"].append({
                        "leg": int(leg_text), "name": name,
                        "timeText": time_text, "timeS": seconds,
                        "status": oribos_status(value),
                    })
            flush_team()

    for category in categories:
        relay_rows = [
            row for row in category["results"]
            if row.get("resultKind") == "relay"]
        if relay_rows:
            # An incomplete/DNS team may omit an unnamed final-leg row.  The
            # class-wide maximum is still the correct denominator shown in
            # the review UI (e.g. Leg 1/3, not the misleading Leg 1/1).
            class_leg_count = max(row.get("leg") or 0 for row in relay_rows)
            for row in relay_rows:
                row["legCount"] = class_leg_count
                row["note"] = re.sub(
                    r"\bLeg\s+(\d+)/\d+\b",
                    rf"Leg \1/{class_leg_count}", row.get("note") or "")
            category["declaredStarters"] = len({
                row.get("teamNumber") for row in relay_rows
                if row.get("teamNumber")})
        else:
            category["declaredStarters"] = len(category["results"])
    return [category for category in categories if category["results"]]


def parse_relay_member_line(line, numbered_leg=False):
    """Read the member's name and first (own-leg) result value.

    Detailed relay exports append cumulative time, combination, control and
    placing columns after the own-leg time. The generic flow parser peels the
    *last* time and consequently only accepted the final leg in many older
    PDFs. Here the first time/status after Name/[Jg] is authoritative.
    """
    tokens = line.split()
    if numbered_leg and tokens and tokens[0].rstrip(".").isdigit():
        tokens = tokens[1:]
    if not tokens:
        return None

    value_at = next(
        (i for i, token in enumerate(tokens)
         if FLOW_TIME_RE.fullmatch(token.strip("()"))), None)
    if value_at is not None:
        name_tokens = tokens[:value_at]
        time_text = tokens[value_at].strip("()")
    else:
        joined = " ".join(tokens)
        status_match = STATUS_TAIL_RE.search(joined)
        if status_match:
            name_tokens = joined[:status_match.start()].split()
            time_text = status_match.group(0).strip()
        else:
            # A few SkiO relay PDFs contain a visibly broken ``Fehler NN``
            # value in the individual-time column.  The embedded font leaves
            # only fragments such as ``er 11`` or ``ht 95`` behind.  Those
            # fragments belong to the result cell, not to the athlete's name
            # (event 5204: Pia Aspalter / Lisa Habenicht).  Preserve the real
            # person and the raw, unresolved value without inventing a time.
            broken_value = re.search(r"\s+(?:er|ht)\s+\d+\s*$", joined, re.I)
            if broken_value:
                name_tokens = joined[:broken_value.start()].split()
                time_text = joined[broken_value.start():].strip()
            else:
            # A team already declared MP/DSQ can leave a later member's own
            # result cell blank. Preserve the named leg instead of silently
            # reducing the roster.
                name_tokens, time_text = tokens, ""
    if name_tokens and re.fullmatch(r"\d{2}|\d{4}", name_tokens[-1]):
        name_tokens.pop()
    name = " ".join(name_tokens).strip()
    name = re.sub(r"^([^,]+),\s+(.+)$", r"\1 \2", name)
    if not looks_like_person(name) or is_junk_name(name):
        return None
    return {"name": name, "timeText": time_text}


def parse_relay_pdf(path, team_mode=False):
    """PDF twin of parse_relay_document() in the HTML parser: 'Pl Stnr
    Staffel Zeit' header, then per-category team rows ('1 24 FUN-OL NÖ 1
    39:06') each immediately followed by that team's member rows
    ('Hartberger Peter 13 13:28', no leading digit). parse_flow_row() (with
    an empty club dict, since a team name isn't a real club) already knows
    how to peel a leading rank/Stnr and a trailing time/status from either
    row shape - the only new logic here is grouping consecutive rows into
    (team, members) blocks, mirroring parse_relay_document()'s flush()."""
    import pdfplumber

    categories, current, pending_team = [], None, None
    pending_team_rank = pending_team_championship = None
    relay_team_name_counts = defaultdict(int)

    def full_team_label(raw_line):
        """Return the complete printed relay label, including ``/`` clubs.

        ``parse_flow_row`` deliberately treats slash-separated text as
        several participant names.  That is correct for pairs, but not for a
        relay header such as ``NF Kitzb./HSV Wr. Neust. 29:00``.  Recover the
        label from the original row before the result value instead.
        """
        body = raw_line.strip()
        diff_match = TEAM_DIFF_SUFFIX_RE.match(body)
        if diff_match:
            body = diff_match.group("body").strip()
        value_match = TIME_TOKEN_RE.search(body) or STATUS_TAIL_RE.search(body)
        if value_match:
            body = body[:value_match.start()].strip()
        body = re.sub(r"^(?:AK|--|—)\s+", "", body, flags=re.I)
        body = re.sub(r"^(?:\d+\.?\s+){1,2}", "", body).strip()
        return body

    def flush():
        nonlocal pending_team
        if pending_team and current is not None:
            current["sourceUnitCount"] = current.get("sourceUnitCount", 0) + 1
        if not pending_team:
            pending_team = None
            return
        raw_team_name = pending_team["name"]
        relay_team_name_counts[raw_team_name] += 1
        display_team_name = (
            raw_team_name if relay_team_name_counts[raw_team_name] == 1
            else f"{raw_team_name} {relay_team_name_counts[raw_team_name]}")
        if not pending_team["members"]:
            # Some registered teams never start and consequently have no leg
            # rows at all. Preserve their one printed team/status line as a
            # personless result unit; do not fabricate three DNS runners.
            team_status = pending_team.get("status") or "unknown"
            if team_status != "unknown" or pending_team.get("rank") is not None:
                label = "Mannschaft" if team_mode else "Staffel"
                result = {
                    "name": "", "club": raw_team_name,
                    "timeText": pending_team.get("timeText") or "",
                    "resultKind": "team" if team_mode else "relay",
                    "memberlessTeam": True,
                    "note": f"{label}: {display_team_name} · keine Teilnehmernamen in der Quelle",
                    "status": team_status, "individualStatus": None,
                    "teamStatus": team_status,
                    "teamNumber": pending_team.get("number"),
                    "teamName": display_team_name,
                    "teamTimeText": pending_team.get("timeText") or "",
                }
                if pending_team.get("timeS") is not None:
                    result["teamTimeS"] = pending_team["timeS"]
                if pending_team.get("outOfCompetition"):
                    result["outOfCompetition"] = True
                if pending_team.get("rank") is not None:
                    result["rank"] = pending_team["rank"]
                if pending_team.get("championship"):
                    result["championship"] = pending_team["championship"]
                current["results"].append(result)
            pending_team = None
            return
        names = [m["name"] for m in pending_team["members"]]
        member_statuses = []
        member_seconds = []
        for m in pending_team["members"]:
            seconds = parse_time_loose(m["timeText"]) if m["timeText"] else None
            member_seconds.append(seconds)
            member_statuses.append(
                "ok" if seconds is not None else
                (parse_status(m["timeText"] or "") or "unknown"))
        # This SkiO export's team-time field is only mm:ss wide.  Once the
        # sum crosses an hour it visibly wraps (61:01 is printed ``01:01``).
        # Recover the missing whole hour only when every leg is timed and the
        # printed value is exactly the modulo-hour remainder; this cannot
        # affect ordinary relay PDFs with a genuine short team time.
        if (member_seconds and all(seconds is not None for seconds in member_seconds)
                and pending_team.get("timeS") is not None):
            summed_time = sum(member_seconds)
            if (summed_time >= 3600 and pending_team["timeS"] < 3600
                    and summed_time % 3600 == pending_team["timeS"]):
                pending_team["timeS"] = summed_time
        team_status = aggregate_team_status(pending_team.get("status"), member_statuses)
        for i, m in enumerate(pending_team["members"]):
            seconds = parse_time_loose(m["timeText"]) if m["timeText"] else None
            individual_status = member_statuses[i]
            mates = list(dict.fromkeys(n for n in names if n != m["name"]))
            note_bits = ([f"Mannschaft: {display_team_name}"] if team_mode else
                         [f"Staffel: {display_team_name}", f"Leg {i + 1}/{len(names)}"])
            if mates:
                note_bits.append("Team: " + ", ".join(mates))
            result = {"name": m["name"], "club": raw_team_name,
                      "timeText": m["timeText"] or "",
                      "resultKind": "team" if team_mode else "relay",
                      "note": " · ".join(note_bits), "status": team_status,
                      "individualStatus": individual_status,
                      "teamStatus": team_status,
                      "teamNumber": pending_team.get("number"),
                      "teamName": display_team_name,
                      "teamTimeText": pending_team.get("timeText") or ""}
            if not team_mode:
                result.update({"leg": i + 1, "legCount": len(names),
                               # SkiO sprint relays intentionally let the
                               # same athlete cover several legs.  The build
                               # dedup key must therefore retain leg identity.
                               "preserveRepeatedRelayLeg": True})
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

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pdfplumber.open(path) as pdf:
            first_page_text = pdf.pages[0].extract_text() or "" if pdf.pages else ""
            broken_time_spacing = bool(re.search(r"\bZ\s+eit\b", first_page_text))
            for page in pdf.pages:
                for line in (page.extract_text() or "").split("\n"):
                    line = line.strip()
                    # A stray PDF glyph can be extracted as a leading dot on
                    # an otherwise ordinary member name (``.Cart Andreas``).
                    # Leaving it in makes the person look like a new team and
                    # drops the real start-number block around it.
                    line = re.sub(r"^\.(?=[A-ZÄÖÜ])", "", line)
                    if broken_time_spacing:
                        # A damaged embedded font in a small family of relay
                        # PDFs extracts 1:31:54 as ``1 :31:54`` and 27:32 as
                        # ``2 7:32``. Repair only documents whose own header
                        # exhibits the same ``Z eit`` split, avoiding any
                        # ambiguity with normal ``YB Time`` member columns.
                        line = re.sub(r"(?<=\d)\s+(?=:\d{2}(?::\d{2})?\b)", "", line)
                        line = re.sub(r"(?<!\d)(\d)\s+(\d:\d{2})\s*$", r"\1\2", line)
                        line = re.sub(r"\bF\s+ehlst\b", "Fehlst", line, flags=re.I)
                        line = re.sub(r"\bA\s+ufg\b", "Aufg", line, flags=re.I)
                        line = re.sub(r"\bD\s+isqu\b", "Disqu", line, flags=re.I)
                        # The same damaged font splits the repeated member
                        # header into ``Name Jg Z eit``. Normalize the header
                        # label before the generic header filter below; it is
                        # never a relay leg (confirmed visually in event 1123).
                        line = re.sub(r"\bZ\s+eit\b", "Zeit", line, flags=re.I)
                    # A corrupted embedded font can interleave the team time
                    # with the final team-name word even when the document's
                    # own ``Zeit`` header is intact (for example
                    # ``Beweg2u:0n4g:58``). Reuse the conservative result-row
                    # repair only for a numeric team row that otherwise has
                    # neither a readable time nor a status.
                    numeric_team = re.match(r"^((?:\d+\s+){1,2})(.+)$", line)
                    if (numeric_team and not TIME_TOKEN_RE.search(line)
                            and not STATUS_TAIL_RE.search(line)):
                        repaired_label, repaired_value = repair_result_club_and_value(
                            numeric_team.group(2), "")
                        if parse_time_loose(repaired_value) is not None:
                            line = (numeric_team.group(1) + repaired_label + " "
                                    + repaired_value)
                    if not line or CONTINUATION_RE.match(line) or DATE_HEADER_RE.search(line):
                        continue
                    if (re.match(r"^Pl(?:atz)?\b.*(?:Staffel|Team|Verein).*(?:Z\s*eit|Zeit|Time)\b", line)
                            or re.match(
                                r"^(?:\S+\s+)?Name\s+(?:(?:\S+\s+)?(?:Zeit|Time)|Einzelzeit)\b",
                                line)
                            or (team_mode and re.match(r"^Name\s+Jg\b", line))):
                        continue
                    m = CAT_LINE_RE.match(line)
                    if m and re.search(r"\(\d", line):
                        name = m.group("name").strip()
                        if current and current["name"] == name:
                            # the category header repeats after a page break
                            # ("(Forts.)") - a categoryless team/member row can
                            # legitimately start with the same leading digit as
                            # a fresh rank too, so this dedup must key on the
                            # exact category name matching, not just skip any
                            # repeat - continuing into the SAME dict rather than
                            # starting a fresh one (confirmed real: event 3825,
                            # "Mixed Staffel ab 50" splitting into two separate
                            # category entries lost every mid-category team's
                            # rank<=3 from national_rank's per-category count)
                            flush()
                            continue
                        flush()
                        current = {"name": name, "declaredStarters": category_starter_count(m),
                                   "results": []}
                        categories.append(current)
                        relay_team_name_counts = defaultdict(int)
                        pending_team_rank = pending_team_championship = None
                        continue
                    if not m:
                        flow_category = parse_flow_category_line(line)
                        if flow_category:
                            name, declared = flow_category
                            if current and current["name"] == name:
                                flush()
                                continue
                            flush()
                            current = {"name": name, "declaredStarters": declared,
                                       "results": []}
                            categories.append(current)
                            relay_team_name_counts = defaultdict(int)
                            pending_team_rank = pending_team_championship = None
                            continue
                    if current is None:
                        continue

                    annotation_rank, annotation_championship = parse_champion_annotation(line)
                    if (annotation_rank is not None and not TIME_TOKEN_RE.search(line)
                            and not STATUS_TAIL_RE.search(line)):
                        flush()
                        pending_team_rank = annotation_rank
                        pending_team_championship = annotation_championship
                        continue

                    # SportSoftware commonly renders a relay placement with
                    # a trailing period (``2. 39 OLT ...``). parse_flow_row's
                    # numeric-column logic intentionally accepts bare digits;
                    # normalize only this leading Pl token so rank, start
                    # number and team name remain three separate fields.
                    line = re.sub(r"^(\d{1,3})\.\s+", r"\1 ", line)

                    championship = None
                    is_leg_member = False
                    if line[0].isdigit():
                        # A non-finishing/unclassified team has no placement;
                        # its first number is the team/start number, followed
                        # by the team name and a status ("14 OLC Wienerwald
                        # Fehlst", "7 HSV Spittal / Drau Aufg"). Previously
                        # the first shape was mistaken for a numbered leg of
                        # the preceding team and both teams were merged. Keep
                        # this ahead of the numbered-member heuristic. A true
                        # numbered member still parses to one person name.
                        rankless_team = parse_flow_row(line, {})
                        if rankless_team and rankless_team.get("statusText"):
                            status_match = STATUS_TAIL_RE.search(line)
                            number_match = re.match(r"^(\d+)\s+(.+)$", line)
                            team_label = (line[number_match.end(1):status_match.start()].strip()
                                          if number_match and status_match else "")
                            parsed_names = rankless_team.get("names") or []
                            is_numbered_member = (
                                int(number_match.group(1)) <= 4
                                and len(parsed_names) == 1
                                and looks_like_person(parsed_names[0])
                                and "/" not in team_label
                            )
                            if team_label and not is_numbered_member:
                                flush()
                                team_time_text = status_match.group(0).strip()
                                pending_team = {
                                    "name": team_label, "rank": None,
                                    "number": number_match.group(1),
                                    "timeText": team_time_text, "timeS": None,
                                    "status": parse_status(team_time_text) or "unknown",
                                    "championship": None, "members": [],
                                }
                                continue
                        # Ambiguous with a brand-new team's own rank digit -
                        # for the "Lnr Name Zeit" member sub-header shape
                        # (see RELAY_HEADER_RE), each member row ALSO leads
                        # with its own small digit (leg number), not a blank
                        # cell like the layout this function was originally
                        # built for. Told apart by shape, not by whether it's
                        # a team row we're expecting more members from: after
                        # the leading digit, a genuine member row is always
                        # "Firstname Lastname time/status" (exactly 2 name
                        # tokens, and they read as a real person) - a team
                        # row's own name usually has 3+ tokens (SportSoftware
                        # always appends its own trailing squad-instance
                        # number, "Naturfreunde Wien 1"), but a few clubs
                        # (confirmed real: "WAT-OL") collapse to a single
                        # token, landing at the same 2-token count as a
                        # member row purely by coincidence - looks_like_
                        # person() is the tiebreaker there ("WAT-OL 1" isn't
                        # a person; "Linus Dobler" is). Capped at 4 already-
                        # collected members as a last-resort backstop (no
                        # relay leg in this dataset runs longer). Confirmed
                        # real: event 3633 ("ÖSTM und ÖM Staffel" 2022) -
                        # every member row's own leg-number was being read as
                        # a brand-new team, flushing the real team empty and
                        # fabricating a bogus one out of a single runner's
                        # name, losing entire teams (Naturfreunde Wien's
                        # H-14 bronze relay among them) outright.
                        toks = line.split()
                        if (pending_team is not None and len(pending_team["members"]) < 4
                                and len(toks) >= 2):
                            tail = toks[1:]
                            time_idx = next((i for i, t in enumerate(tail)
                                              if FLOW_TIME_RE.match(t.lstrip("+"))
                                              or parse_status(t)), None)
                            member_tokens = tail[:time_idx] if time_idx is not None else []
                            split_at = len(member_tokens) // 2
                            duplicated_team_label = bool(
                                len(member_tokens) >= 2
                                and len(member_tokens) % 2 == 0
                                and member_tokens[:split_at] == member_tokens[split_at:])
                            if (time_idx is not None and 1 <= time_idx <= 4
                                    and not duplicated_team_label
                                    and not looks_like_status_team_label(
                                        " ".join(member_tokens))
                                    and looks_like_person(" ".join(member_tokens))):
                                is_leg_member = True
                        if not is_leg_member:
                            am = RELAY_TEAM_ANNOT_RE.match(line)
                            if am:
                                championship = classify_championship_text(am.group("title"))
                                line = line[: am.start("rank")] + am.group("rank") + " " + line[am.end():]
                            dm = TEAM_DIFF_SUFFIX_RE.match(line)
                            if dm:
                                line = dm.group("body")
                    else:
                        if pending_team_rank is not None:
                            announced = parse_flow_row(line, {})
                            if (announced and announced["names"]
                                    and (announced["timeText"] or announced["statusText"])):
                                flush()
                                team_time_text = (announced["timeText"]
                                                  or announced["statusText"] or "")
                                team_time_s = parse_time_loose(team_time_text)
                                pending_team = {
                                    "name": full_team_label(line) or announced["names"][0],
                                    "rank": pending_team_rank, "number": None,
                                    "timeText": team_time_text, "timeS": team_time_s,
                                    "status": "ok" if team_time_s is not None else
                                              (parse_status(team_time_text) or "unknown"),
                                    "championship": pending_team_championship,
                                    "members": [],
                                }
                                pending_team_rank = pending_team_championship = None
                                continue
                        # An "AK" (außer Konkurrenz / out-of-competition) team
                        # is printed with the literal text "AK" standing in
                        # for its missing rank number - otherwise a normal
                        # team row (name + time + diff), e.g. "AK OLC
                        # Wienerwald 1 2:08:46 +23:47". Without this check it
                        # fell through to the member-row path below and was
                        # swallowed as a phantom extra member of whatever
                        # team preceded it (pushing that team over the 4-
                        # member cap); its OWN following member rows then had
                        # no team to attach to and got misread as fabricated
                        # single-person "teams" instead (confirmed real:
                        # event 3633's phantom "Klaus Kramer"/"Guni Palme" and
                        # "Tim Lechner"/"Martin Bogensperger" entries),
                        # knocking the real next team's national_rank off by
                        # one.
                        ak_m = re.match(r"^AK\s+(.+)$", line)
                        if ak_m:
                            rest = ak_m.group(1)
                            dm = TEAM_DIFF_SUFFIX_RE.match(rest)
                            if dm:
                                rest = dm.group("body")
                            ak_flow = parse_flow_row(rest, {})
                            if ak_flow and ak_flow["names"]:
                                flush()
                                team_time_text = ak_flow["timeText"] or ak_flow["statusText"] or ""
                                team_time_s = parse_time_loose(team_time_text)
                                pending_team = {"name": full_team_label(rest) or ak_flow["names"][0],
                                                 "rank": None,
                                                 "number": None, "timeText": team_time_text,
                                                 "timeS": team_time_s,
                                                 "status": "ok" if team_time_s is not None else
                                                           (parse_status(team_time_text) or "unknown"),
                                                 "outOfCompetition": True,
                                                 "championship": None, "members": []}
                                continue
                        # A DNF/non-finishing team is printed with NO leading
                        # rank at all - just its name directly followed by a
                        # status word ("SU Schöckl Orienteering SUSO-14-1
                        # Fehlst", "OC Fürstenfeld OCFF2 Aufg") - so it looks
                        # exactly like a rankless "blank first cell" MEMBER
                        # row at a glance, but a real member row's own status
                        # only ever follows a clean two-token person name,
                        # never a team-shaped one. Checked ahead of that
                        # member-row path so it isn't swallowed as a phantom
                        # extra member of whatever team precedes it - which
                        # then knocks its OWN first real member (misread as a
                        # brand-new team next) out of alignment too. Confirmed
                        # real: event 3633, where this exact cascade invented
                        # a phantom "Moritz Mosing" team ahead of Naturfreunde
                        # Wien's real bronze-medal relay, corrupting its
                        # national_rank by one full place.
                        sm = STATUS_TAIL_RE.search(line)
                        member_flow = parse_flow_row(line, {}) if sm else None
                        member_candidate = (
                            re.sub(r"^([^,]+),\s+(.+)$", r"\1 \2",
                                   member_flow["names"][0])
                            if member_flow and len(member_flow.get("names") or []) == 1
                            else "")
                        team_label = re.sub(r"^(?:--|—)\s*", "", line[: sm.start()].strip()) if sm else ""
                        artifact_team_label = bool(
                            team_label and not re.search(r"[A-Za-zÄÖÜäöüß]{2}", team_label))
                        is_status_member = bool(
                            member_candidate and looks_like_person(member_candidate)
                            and not looks_like_status_team_label(team_label)
                        )
                        if sm and not is_status_member and not artifact_team_label:
                            flush()
                            team_time_text = sm.group(0).strip()
                            pending_team = {"name": team_label, "rank": None,
                                             "number": None, "timeText": team_time_text,
                                             "timeS": None,
                                             "status": parse_status(team_time_text) or "unknown",
                                             "championship": None, "members": []}
                            continue
                        tm = MEMBER_TWO_TIME_RE.match(line)
                        if tm:
                            line = tm.group("body")

                    direct_member = None
                    if pending_team is not None and (not line[0].isdigit() or is_leg_member):
                        direct_member = parse_relay_member_line(line, numbered_leg=is_leg_member)
                    if direct_member is not None:
                        pending_team["members"].append(direct_member)
                        continue

                    flow = parse_flow_row(line, {})
                    if not flow or not flow["names"] or not (flow["timeText"] or flow["statusText"]):
                        continue
                    time_text = flow["timeText"] or flow["statusText"] or ""
                    if line[0].isdigit() and not is_leg_member:
                        flush()
                        leading = []
                        for token in line.split():
                            if token.isdigit() and len(leading) < 2:
                                leading.append(token)
                            else:
                                break
                        # Finishers carry Pl + Stnr; an unranked team carrying
                        # a status has only Stnr. A one-number finisher has no
                        # start number in that particular export layout.
                        team_number = (leading[1] if len(leading) >= 2 else
                                       leading[0] if (flow["rank"] is None
                                                      or pending_team_rank is not None)
                                       and leading else None)
                        team_time_s = parse_time_loose(time_text)
                        effective_rank = (pending_team_rank if pending_team_rank is not None
                                          else flow["rank"])
                        effective_championship = (pending_team_championship
                                                  if pending_team_rank is not None
                                                  else championship)
                        pending_team = {"name": full_team_label(line) or flow["names"][0],
                                         "rank": effective_rank,
                                         "number": team_number, "timeText": time_text,
                                         "timeS": team_time_s,
                                         "status": "ok" if team_time_s is not None else
                                                   (parse_status(time_text) or "unknown"),
                                         "championship": effective_championship, "members": []}
                        pending_team_rank = pending_team_championship = None
                    elif pending_team is not None:
                        pending_team["members"].append({"name": flow["names"][0], "timeText": time_text})
            flush()

    categories = merge_category_continuations(
        [c for c in categories if c["results"]])
    for c in categories:
        # A damaged historic export can repeat the exact same start number a
        # second time below the ranked teams as MP (event 1455, team 93).
        # The ranked block is the authoritative result; retaining both would
        # both inflate the source check and propagate MP onto the valid team.
        ranked_numbers = {
            r.get("teamNumber") for r in c["results"]
            if r.get("teamNumber") and r.get("rank") is not None
        }
        if ranked_numbers:
            c["results"] = [
                r for r in c["results"]
                if not (r.get("teamNumber") in ranked_numbers
                        and r.get("rank") is None)
            ]
        if c["declaredStarters"] is None:
            if team_mode:
                c["declaredStarters"] = len({
                    r.get("teamNumber") or r.get("teamName") for r in c["results"]})
            else:
                c["declaredStarters"] = len(c["results"])
    return categories


WINTERTOUR_HEADER_RE = re.compile(r"^Nachname\s+Vorname\b")
WINTERTOUR_SKIP_LINES = {"Strafminuten", "Spzialwertung"}
STATUS_WORD_RE = re.compile(r"^(fehlst(?:empel)?|aufg(?:egeben)?|disq(?:ualifiziert)?|dns|dnf|dsq|mp|n\.?\s*ang\.?)\.?$", re.I)
WINTERTOUR_CATEGORY_RE = re.compile(
    r"^(?:Kurz|Lang)(?:\s+[A-Za-zÄÖÜäöüß-]+){0,3}\s+(?:Damen|Herren)$", re.I)


def parse_wintertour_row(text):
    """One data row of the 'Nachname Vorname Verein Zeit [Platz Strafminuten
    Gesamtzeit] Rang/Gesamtrang' layout (e.g. the Wintertour series): unlike
    every other PDF format here, the surname/forename are their own leading
    columns and the rank comes *last*, with a variable number of extra
    scoring columns in between depending on whether a penalty applies to
    that row. Club is whatever sits between the name and the first numeric
    (time or status) token, however many words that takes."""
    toks = text.split()
    if len(toks) < 4:
        return None
    lastname, firstname = toks[0], toks[1]
    rest = toks[2:]
    idx = next((i for i, t in enumerate(rest) if t[0].isdigit() or STATUS_WORD_RE.match(t)), None)
    if not idx:
        return None
    club, values = " ".join(rest[:idx]), rest[idx:]
    name = f"{lastname} {firstname}"
    if is_junk_name(name):
        return None
    # Rankings use Gesamtzeit, i.e. the last time value before Gesamtrang.
    # The first time is the raw Laufzeit and can be faster when penalty
    # minutes apply (``00:26:18 ... 2 00:28:18 6``).
    parsed_times = [(value, parse_time_loose(value)) for value in values]
    parsed_times = [(value, seconds) for value, seconds in parsed_times
                    if seconds is not None]
    time_text, seconds = parsed_times[-1] if parsed_times else (values[0], None)
    result = {"name": name, "club": club, "timeText": time_text}
    if seconds is not None:
        result["timeS"], result["status"] = seconds, "ok"
        if values[-1].isdigit():
            result["rank"] = int(values[-1])
    else:
        result["status"] = parse_status(time_text) or "unknown"
    return result


def parse_wintertour_pdf(path):
    import pdfplumber

    categories, current, candidate_name = [], None, None
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                for line in (page.extract_text() or "").split("\n"):
                    line = line.strip()
                    if not line or line in WINTERTOUR_SKIP_LINES or DATE_HEADER_RE.search(line):
                        continue
                    if WINTERTOUR_HEADER_RE.match(line):
                        if candidate_name and (not current or current["name"] != candidate_name):
                            current = {"name": candidate_name, "declaredStarters": None, "results": []}
                            categories.append(current)
                        candidate_name = None
                        continue
                    row = parse_wintertour_row(line) if current else None
                    if row:
                        current["results"].append(row)
                    elif current is not None and WINTERTOUR_CATEGORY_RE.match(line):
                        if current["name"] != line:
                            current = {"name": line, "declaredStarters": None,
                                       "results": []}
                            categories.append(current)
                        candidate_name = None
                    else:
                        candidate_name = line

    categories = [c for c in categories if c["results"]]
    for c in categories:
        c["declaredStarters"] = len(c["results"])
    return categories


LABYRINTH_ROW_RE = re.compile(
    r"^(?:(?P<rank>\d{1,3})\s+)?(?P<name>.+?)\s+"
    r"(?P<course_a>\d{1,3}:\d{2}|Fehlst\.?|-)\s+"
    r"(?P<course_b>\d{1,3}:\d{2}|Fehlst\.?|-)\s+"
    r"(?P<total>\d{1,3}:\d{2}|-)\s*$", re.I)


def parse_labyrinth_challenge_pdf(path):
    """Parse the Labyrinth Challenge's two-course result matrix.

    Its rank is based on the ``Summe`` column, while the ordinary PDF fallback
    interpreted ``Bahn A`` as the finish time and merged the separate Damen
    and Herren tables into one category.  Preserve partial-course starters as
    DNF/MP entries, but only use the total for ranked finishers.
    """
    import pdfplumber

    categories, current = [], None
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                for raw in (page.extract_text(layout=True) or "").splitlines():
                    line = re.sub(r"\s+", " ", raw).strip()
                    if line in ("Damen", "Herren"):
                        current = {"name": line, "declaredStarters": None,
                                   "rankingBasis": "time", "results": []}
                        categories.append(current)
                        continue
                    if current is None or "Bahn A Bahn B Summe" in line:
                        continue
                    match = LABYRINTH_ROW_RE.match(line)
                    if not match:
                        continue
                    name = match.group("name").strip()
                    if is_junk_name(name) or not looks_like_person(name):
                        continue
                    total = match.group("total")
                    course_values = (match.group("course_a"), match.group("course_b"))
                    explicit_status = next(
                        (parse_status(value) for value in course_values
                         if parse_status(value) not in (None, "ok")), None)
                    seconds = parse_time_loose(total) if total != "-" else None
                    result = {
                        "name": name,
                        "club": "",
                        "timeText": total if seconds is not None else
                                    (next((value for value in course_values
                                           if parse_status(value)), "-")),
                        "status": "ok" if seconds is not None else
                                  (explicit_status or "dnf"),
                        "note": f"Bahn A: {course_values[0]} · Bahn B: {course_values[1]}",
                    }
                    if seconds is not None:
                        result["timeS"] = seconds
                    if match.group("rank") and seconds is not None:
                        result["rank"] = int(match.group("rank"))
                    current["results"].append(result)

    categories = [category for category in categories if category["results"]]
    for category in categories:
        category["declaredStarters"] = len(category["results"])
    return categories


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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="only process N files (0 = all)")
    ap.add_argument("--event-id", type=int, help="only process one ANNE event")
    ap.add_argument("--cached", action="store_true",
                    help="reparse already downloaded PDFs without fetching them again")
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
        # a Zwischenzeiten-titled PDF that's the event's *only* attachment
        # can't be a duplicate of some other results file - safe to parse
        # its inline per-control splits rather than skip it outright
        sole_attachment = len(files or []) == 1
        for n, f in enumerate(files or []):
            if f["mimeType"] == "application/pdf":
                jobs.append((int(eid), n, f, sole_attachment))
    jobs = select_jobs(jobs, args.event_id, args.attachment_manifest)
    if args.limit:
        jobs = jobs[: args.limit]
    print(f"pdf files to parse: {len(jobs)}")

    ok = empty = failed = 0
    for eid, n, f, sole_attachment in jobs:
        if (eid, f["fileName"]) in MANUAL_ATTACHMENT_SKIP:
            empty += 1
            continue
        out_path = OUT / f"{eid}-{n}.json"
        pdf_path = FILES / f"{eid}-{n}.pdf"
        try:
            if args.cached and not pdf_path.exists():
                empty += 1
                continue
            if not args.cached:
                fetch(f["url"], pdf_path, args.force_download)
            cats, head_text = parse_pdf(pdf_path, allow_inline_splits=sole_attachment)
            list_type = detect_list_type(f["fileName"], head_text, sole_attachment)
            if list_type == "overall":
                # Cumulative standings, split-time sheets and per-course
                # relay rankings are not a physical race result. In
                # particular, a sole Zwischenzeiten PDF still cannot be fed
                # through the ordinary result parser: control numbers such as
                # ``15(47)`` become phantom categories and split values become
                # finish times. A dedicated split parser may recover those in
                # future; until then, omission is safer than invented data.
                cats = []
            elif SPLITS_RE.search(head_text) and not sole_attachment:
                # A genuine Zwischenzeiten/split-times report only duplicates
                # the real results file that exists elsewhere for this same
                # event - parse_pdf() already refuses it (returning empty
                # cats) via its own has_inline_splits check, but that check
                # only guards ITS OWN table-column parsing; every fallback
                # path below (relay, wintertour, flowing) had no such guard
                # at all and would happily misparse the same per-control
                # cumulative-time rows as if they were real placements.
                # Confirmed real: event 3824's "...-rel-result-splits.pdf" -
                # parse_flowing_pdf() read a split line ("1 HSV OL Wiener
                # Neustadt 1 2:59 4:56 ...", where "1" is the control number
                # and "2:59" a cumulative split, not a placement or a finish
                # time) as a real team result, inventing a phantom rank-1
                # "Herren 150" team years before anyone noticed - nothing
                # else ever surfaced that category's actual winner for
                # comparison until the Wettkämpfe view's "Meister" toggle did.
                cats = []
            elif ("Labyrinth-Challenge" in head_text
                  and "Bahn A" in head_text and "Bahn B" in head_text
                  and "Summe" in head_text):
                cats = parse_labyrinth_challenge_pdf(pdf_path)
            elif "ExcelWebPagePrevi" in head_text:
                cats = parse_excel_web_pdf(pdf_path)
            elif re.search(
                    r"\bPl\s+Familienname\s+Vorname\s+Verein\s+Zeit\b",
                    head_text, re.I):
                # Word-processor championship lists have one clean flowing
                # row per runner. The generic x-column parser can produce a
                # few plausible-looking fragments and thereby prevent its
                # own fallback, so this structural header must override it.
                cats = parse_flowing_pdf(pdf_path)
            elif re.search(r"\bPlName\s+JgVerein\s+Zeit\b", head_text, re.I):
                cats = parse_glued_header_pdf(pdf_path)
            elif re.search(r"Platz\s+Chipnr\s+Nachname\s+Vorname\s+Zeit\s+Schule",
                           head_text, re.I):
                cats = parse_school_score_pdf(pdf_path)
            elif ("Steirische Schulmeisterschaft" in head_text
                  and re.search(r"Pl\s+Stnr\s+Name\s+Jg\s+Verein\s+Zeit", head_text)):
                cats = parse_school_final_pdf(pdf_path)
            elif (re.search(r"Staffel", head_text, re.I)
                  and re.search(r"\(\d+\s*/\s*\d+\).*\b(?:Time|Zeit)\b",
                                head_text, re.I)):
                cats = parse_meos_relay_pdf(pdf_path)
            elif re.search(r"^RELAY RESULTS\b", head_text, re.I):
                cats = parse_oribos_relay_pdf(pdf_path)
            elif (MEOS_PAGE_HEADER_RE.match(head_text)
                  or MEOS_CLASS_HEADER_RE.search(head_text)):
                cats = parse_meos_individual_pdf(pdf_path)
            elif (re.search(r"Mannschaft", head_text, re.I)
                  and MANNSCHAFT_HEADER_RE.search(head_text)):
                cats = parse_relay_pdf(pdf_path, team_mode=True)
            elif RELAY_HEADER_RE.search(head_text) or RELAY_TITLE_RE.search(head_text):
                # parse_pdf()'s flat Pl/Stnr/Name/Verein/Zeit column model
                # doesn't understand the two-tier team+member relay layout -
                # confirmed by hand (event 4829) that it doesn't just come up
                # empty on one, it actively misreads team/member rows as
                # individual data ("WAT-OL" and "AK" as runner names), so a
                # relay header always overrides its output rather than only
                # being consulted when parse_pdf came up empty
                cats = parse_relay_pdf(pdf_path)
            elif not cats:
                # parse_pdf() only understands the fixed Pl/Stnr/Verein/Zeit
                # column layout; other export styles need their own logic
                # entirely (see their docstrings)
                if "Nachname Vorname" in head_text:
                    cats = parse_wintertour_pdf(pdf_path)
                else:
                    cats = parse_flowing_pdf(pdf_path)
            cats = repair_wrapped_champion_names(pdf_path, cats)
            cats = normalize_qualitative_result_ranks(cats)
            if not cats:
                empty += 1
                # a file that used to parse (under an earlier, buggier
                # version of this script) but correctly comes up empty now
                # must not leave its stale, wrong JSON sitting on disk
                # forever - load_legacy_results() would otherwise go on
                # using it indefinitely, since nothing else ever prunes
                # data/normalized/ on its own.
                out_path.unlink(missing_ok=True)
                continue
            out_path.write_text(json.dumps({
                "eventId": eid,
                "source": "sportsoftware-pdf",
                "sourceUrl": f["url"],
                "fileName": f["fileName"],
                "listType": list_type,
                "docDate": MANUAL_DOC_DATE_OVERRIDES.get((eid, f["fileName"]))
                           or guess_doc_date(f["fileName"], head_text),
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
