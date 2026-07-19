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
import sys
import time
import urllib.parse
import urllib.request
import warnings
from pathlib import Path

from sportsoftware_common import (
    CAT_LINE_RE, KAT_TOKEN_RE, MANUAL_ATTACHMENT_SKIP, MANUAL_DOC_DATE_OVERRIDES, STATUS_TAIL_RE,
    aggregate_team_status, category_starter_count, classify_championship_text,
    detect_list_type, find_trailing_club, guess_doc_date,
    is_junk_name, is_ooc_status, load_clubs, looks_like_person, split_by_kat,
    parse_champion_annotation, parse_course_info, parse_flow_row, parse_status, parse_time,
    parse_time_loose, strip_champion_name_prefix,
)

CLUBS = load_clubs()


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
BAHN_ONLY_CAT_RE = re.compile(r"^(?P<name>Bahn\s+\d+)\b.*$", re.I)
PLAIN_LETTER_CAT_RE = re.compile(r"^[A-ZÄÖÜ](?:\d+)?$")
UNCOUNTED_COURSE_CAT_RE = re.compile(
    r"^(?P<name>(?!\d).+?)\s*:?\s+\d+(?:[.,]\d+)?\s+km\b.*$", re.I)
PRELIMINARY_CAT_RE = re.compile(
    r"^(?P<name>(?!\d).+?)(?:\s+\([^)]*)?\s+"
    r"(?:Preliminary\s+results|Vorl[aä]ufiges\s+Ergebnis)\b.*$", re.I)
UNCOUNTED_STATUS_CAT_RE = re.compile(
    r"^(?P<name>(?!\d).+?)\s+\(\s*(?:Stand\s+von|Status)\s*:", re.I)
MEOS_PAGE_HEADER_RE = re.compile(r"^MeOS\s+\d{4}-\d{2}-\d{2}\b", re.I)

PDF_HEADER_ALIASES = {
    "Místo": "Pl", "Jméno": "Name", "Oddíl": "Verein",
    "Čas": "Zeit", "Ztráta": "Diff",
    "Pos.": "Pl", "Pos": "Pl", "Club": "Verein",
    "Time": "Zeit", "YB": "Jg", "Stno": "Stnr",
    "Schule": "Verein", "l": "Pl", "Familienname": "Nachname",
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
    value = re.sub(r"\bF\s+ehlst\b", "Fehlst", value, flags=re.I)
    value = re.sub(r"\bA\s+ufg\b", "Aufg", value, flags=re.I)
    value = re.sub(r"\bD\s+isqu\b", "Disqu", value, flags=re.I)
    return value


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
                                for family_result in flow_results(family_flow):
                                    family_result["resultKind"] = "family"
                                    current[side]["results"].append(family_result)

    categories = merge_category_continuations(
        [category for category in categories if category["results"]])
    for category in categories:
        if category["declaredStarters"] is None:
            category["declaredStarters"] = len(category["results"])
    return categories


def parse_pdf(path, allow_inline_splits=False):
    import pdfplumber

    categories = []
    current = None
    headers = None
    team_row_mode = False
    pair_row_mode = False
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
            # Newspaper-style two-table pages normally expose two ``(N)``
            # category counts on the same extracted line. Use that cheap
            # signal before opening the document a second time for x-splitting;
            # ordinary PDFs now stay single-pass during nightly/full syncs.
            two_column = (parse_two_column_pdf(path)
                          if re.search(r"\(\d+\).{8,}\(\d+\)", head_text) else None)
            if two_column is not None:
                return two_column, head_text
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
                        pair_name_starts = (([name_header["x0"]] if name_header else [])
                                            + [word["x0"] for word in runner_headers])
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

                    uncounted_category = (COURSE_ONLY_CAT_RE.match(text)
                                          or BAHN_CAT_RE.match(text)
                                          or BAHN_ONLY_CAT_RE.match(text))
                    if not uncounted_category and not CAT_LINE_RE.match(text):
                        uncounted_category = UNCOUNTED_COURSE_CAT_RE.match(text)
                    if not uncounted_category and not CAT_LINE_RE.match(text):
                        uncounted_category = PRELIMINARY_CAT_RE.match(text)
                    if not uncounted_category and not CAT_LINE_RE.match(text):
                        uncounted_category = UNCOUNTED_STATUS_CAT_RE.match(text)
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
                                or STATUS_TAIL_RE.search(name)
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
                                if start_x <= word["x0"] < end_x).strip())
                        names = [name for name in names
                                 if looks_like_person(name) and not is_junk_name(name)]
                        club = " ".join(
                            word["text"] for word in line
                            if club_x <= word["x0"] < time_x).strip()
                        value = " ".join(
                            word["text"] for word in line if word["x0"] >= time_x).strip()
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
                            for index, name in enumerate(names):
                                result = {
                                    "name": name, "club": club,
                                    "timeText": (TIME_TOKEN_RE.search(value).group(0)
                                                 if seconds is not None else value),
                                    "status": status,
                                }
                                if len(names) > 1:
                                    result["resultKind"] = "pair"
                                    result["note"] = "Partner: " + ", ".join(
                                        other for other in names if other != name)
                                if pair_rank is not None:
                                    result["rank"] = pair_rank
                                if pair_number is not None:
                                    result["teamNumber"] = pair_number
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
                    stage_text = " ".join(
                        rec.get(label, "").strip() for label in stage_labels
                        if rec.get(label, "").strip())
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

                    club_text = (school_club if school_club is not None else
                                 (rec.get("Verein") or rec.get("Verein/Schule") or "")).strip()
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
                    if rank_text.isdigit():
                        # this row has its own rank after all - it wasn't the
                        # one the pending announcement belonged to (a stray
                        # digit elsewhere in a garbled row, say), so drop the
                        # pending state rather than misattaching the title to
                        # an unrelated rank
                        result["rank"] = int(rank_text)
                    elif pending_rank is not None:
                        result["rank"] = pending_rank
                        if pending_championship:
                            result["championship"] = pending_championship
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
                        inferred_status = parse_status(time_text) or parse_status(stage_text)
                        result["status"] = inferred_status or "unknown"
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
    if is_junk_name(name) or not looks_like_person(name):
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
                        if pending_rank is not None and row.get("rank") is None:
                            row["rank"] = pending_rank
                            if pending_championship:
                                row["championship"] = pending_championship
                        pending_rank = pending_championship = None
                        current["results"].append(row)

    categories = [c for c in categories if c["results"]]
    categories = merge_category_continuations(categories)
    categories = normalize_exact_time_ties(categories)
    categories = normalize_school_schnupper_pairs(categories)
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
                    name = " ".join(
                        word["text"] for word in row_words if word["x0"] < club_x).strip()
                    club = " ".join(
                        word["text"] for word in row_words
                        if club_x <= word["x0"] < time_x).strip()
                    value = " ".join(
                        word["text"] for word in row_words if word["x0"] >= time_x).strip()
                    # MeOS also writes compact child/pair entries as
                    # ``Diana&Ronja`` (not only ``Anna / Berta``). Preserve
                    # both clickable identities while counting the shared
                    # source row as one competitor unit.
                    pair_names = [part.strip() for part in
                                  re.split(r"\s*(?:/|&)\s*", name)]
                    if (not name or is_junk_name(name)
                            or not all(looks_like_person(part) for part in pair_names)):
                        continue
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
                    if len(pair_names) > 1:
                        pair_counter += 1
                    for pair_name in pair_names:
                        result = {"name": pair_name, "club": club,
                                  "timeText": time_text, "status": status}
                        if len(pair_names) > 1:
                            result.update({
                                "resultKind": "pair",
                                "teamNumber": f"pair-{pair_counter}",
                                "note": "Partner: " + ", ".join(
                                    other for other in pair_names if other != pair_name),
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
        if not pending_team or not pending_team["members"] or current is None:
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

    def flush():
        nonlocal pending_team
        if not pending_team or not pending_team["members"]:
            pending_team = None
            return
        raw_team_name = pending_team["name"]
        relay_team_name_counts[raw_team_name] += 1
        display_team_name = (
            raw_team_name if relay_team_name_counts[raw_team_name] == 1
            else f"{raw_team_name} {relay_team_name_counts[raw_team_name]}")
        names = [m["name"] for m in pending_team["members"]]
        member_statuses = []
        for m in pending_team["members"]:
            seconds = parse_time_loose(m["timeText"]) if m["timeText"] else None
            member_statuses.append(
                "ok" if seconds is not None else
                (parse_status(m["timeText"] or "") or "unknown"))
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
                result.update({"leg": i + 1, "legCount": len(names)})
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
                            if (time_idx is not None and 1 <= time_idx <= 4
                                    and looks_like_person(" ".join(tail[:time_idx]))):
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
                                    "name": announced["names"][0],
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
                                pending_team = {"name": ak_flow["names"][0], "rank": None,
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
                        is_status_member = bool(
                            member_candidate and looks_like_person(member_candidate)
                        )
                        if sm and not is_status_member:
                            flush()
                            team_time_text = sm.group(0).strip()
                            team_label = re.sub(r"^(?:--|—)\s*", "", line[: sm.start()].strip())
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
                        pending_team = {"name": flow["names"][0], "rank": effective_rank,
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
    time_text = values[0]
    seconds = parse_time_loose(time_text)
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
                        continue
                    row = parse_wintertour_row(line) if current else None
                    if row:
                        current["results"].append(row)
                    else:
                        candidate_name = line

    categories = [c for c in categories if c["results"]]
    for c in categories:
        c["declaredStarters"] = len(c["results"])
    return categories


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
    ap.add_argument("--event-id", type=int, help="only process one ANNE event")
    ap.add_argument("--cached", action="store_true",
                    help="reparse already downloaded PDFs without fetching them again")
    args = ap.parse_args()

    FILES.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)
    attachments = json.loads((RAW / "attachments.json").read_text())

    jobs = []
    for eid, files in attachments.items():
        if args.event_id is not None and int(eid) != args.event_id:
            continue
        # a Zwischenzeiten-titled PDF that's the event's *only* attachment
        # can't be a duplicate of some other results file - safe to parse
        # its inline per-control splits rather than skip it outright
        sole_attachment = len(files or []) == 1
        for n, f in enumerate(files or []):
            if f["mimeType"] == "application/pdf":
                jobs.append((int(eid), n, f, sole_attachment))
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
            if not (args.cached and pdf_path.exists()):
                fetch(f["url"], pdf_path)
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
            elif MEOS_PAGE_HEADER_RE.match(head_text):
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
