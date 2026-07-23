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
import hashlib
import itertools
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
    CAT_LINE_RE, KAT_TOKEN_RE, MANUAL_ATTACHMENT_INDEX_SKIP,
    MANUAL_ATTACHMENT_SKIP, MANUAL_DOC_DATE_OVERRIDES, STATUS_TAIL_RE,
    aggregate_team_status, category_starter_count, classify_championship_text,
    detect_list_type, find_trailing_club, guess_doc_date,
    expand_pair_result, is_junk_name, is_ooc_status, load_clubs, looks_like_person,
    is_auxiliary_attachment_name,
    split_by_kat, split_pair_names, kat_to_category_name,
    parse_champion_annotation, parse_course_info, parse_flow_row, parse_status, parse_time,
    parse_time_loose, repair_official_club_status_overflow,
    strip_champion_name_prefix,
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
VERIFIED_SCAN_TRANSCRIPTS = ROOT / "data" / "verified_scan_transcripts"

HEADERS = {"User-Agent": "olresults-sync/0.1 (+https://github.com/josefhilbert/olresults)"}


def load_verified_scan_transcript(pdf_path, event_id, attachment_index):
    """Load a visually reviewed transcript for an image-only result PDF.

    OCR is intentionally not run during normal builds: it is nondeterministic
    across engines and platforms. A reviewed transcript is accepted only for
    the exact source bytes it was made from. If ANNE replaces the attachment,
    the hash mismatch stops the parse instead of silently applying stale rows
    to a different document.
    """
    transcript_path = (
        VERIFIED_SCAN_TRANSCRIPTS / f"{event_id}-{attachment_index}.json"
    )
    if not transcript_path.exists():
        return None
    document = json.loads(transcript_path.read_text())
    expected_hash = document.get("sourceSha256")
    actual_hash = hashlib.sha256(Path(pdf_path).read_bytes()).hexdigest()
    if not expected_hash or expected_hash != actual_hash:
        raise ValueError(
            f"verified scan transcript hash mismatch for "
            f"{event_id}-{attachment_index}: expected {expected_hash}, "
            f"got {actual_hash}"
        )
    if (
        document.get("eventId") != event_id
        or document.get("attachmentIndex") != attachment_index
    ):
        raise ValueError(
            f"verified scan transcript identity mismatch for "
            f"{event_id}-{attachment_index}"
        )
    categories = document.get("categories")
    if not isinstance(categories, list) or not categories:
        raise ValueError(
            f"verified scan transcript has no categories for "
            f"{event_id}-{attachment_index}"
        )
    for category in categories:
        results = category.get("results")
        if not isinstance(results, list):
            raise ValueError(
                f"verified scan transcript category has no rows: "
                f"{category.get('name')!r}"
            )
        declared = category.get("declaredStarters")
        if declared is not None and declared != category_competitor_unit_count(
                category):
            raise ValueError(
                f"verified scan transcript count mismatch in "
                f"{event_id}-{attachment_index} {category.get('name')!r}: "
                f"{declared} declared, "
                f"{category_competitor_unit_count(category)} transcribed"
            )
    return document

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
FLOW_TIME_RE = re.compile(r"^\+?\d{1,3}[:,.]\d{2}(?::\d{2})?$")
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
SIMPLE_FLOW_CATEGORY_RE = re.compile(
    r"^(?P<name>"
    r"[A-E]\s*[-–]\s*(?:Damen|Herren)|"
    r"(?:Damen|Herren)(?:\s+(?:lang|kurz|[A-E]))|"
    r"Strecke\s+[„\"']?[A-E][“\"']?(?:\s*,?\s*(?:weiblich|männlich))?|"
    r"[A-E]\s+Postennetz|\d+\s+Posten(?:\s+Team)?|"
    r"[DH](?:\s*-?\s*\d{1,2}|\d{1,2}-)|"
    r"[A-E]\s+.+\s+(?:Damen|Herren)"
    r")\s*:?$", re.I)
INLINE_SIMPLE_CATEGORY_HEADER_RE = re.compile(
    r"^(?:Rg|Rang|Platz)\b.*?\b(?P<name>Damen|Herren)\s+(?P<class>[A-E])\b"
    r".*\b(?:Startzeit|Laufzeit|Zeit)\b", re.I)
NUMBERED_COURSE_CAT_RE = re.compile(
    r"^(?P<number>\d+)\s+\((?P<starters>\d+)\)\s+"
    r"\d+(?:[.,]\d+)?\s*km\b", re.I)
UNCOUNTED_COURSE_CAT_RE = re.compile(
    r"^(?P<name>(?!\d).+?)\s*:?\s+\d+(?:[.,]\d+)?\s*km\b.*$", re.I)
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
        if "schnupper" in category["name"].casefold():
            expanded = []
            pair_counter = 0
            for result in results:
                repair_shifted_name_club_time(result)
                raw_name = result.get("name") or ""
                pair_names = split_pair_names(raw_name) if "/" in raw_name else []
                if ((result.get("resultKind") or "individual") == "individual"
                        and len(pair_names) == 2
                        and all(len(person.split()) == 2 and looks_like_person(person)
                                for person in pair_names)):
                    pair_counter += 1
                    team_number = (result.get("sourceBib")
                                   or f"school-pair-{pair_counter}")
                    for person in pair_names:
                        pair = dict(result)
                        pair.update({
                            "name": person,
                            "resultKind": "pair",
                            "teamNumber": str(team_number),
                            "note": "Partner: " + next(
                                other for other in pair_names if other != person),
                        })
                        expanded.append(pair)
                    continue
                expanded.append(result)
            results = category["results"] = expanded
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


def repair_interleaved_regional_nat(club, value):
    """Separate a state code interleaved into a long club suffix.

    PDF text layers can order overlapping glyphs as
    ``KlosterneubuNrgÖ`` (``Klosterneuburg`` + ``NÖ``) or
    ``KaltenbBrunn`` (``Kaltenbrunn`` + ``B``). We accept a deinterleaving
    only when removing the ordered code characters makes the complete club an
    exact known parsing-boundary name. That keeps the repair deterministic and
    prevents a random B/W in an international club from becoming a state.
    """
    value = (value or "").strip()
    if not club or not value:
        return None
    matches = set()
    for code, label in (("nö", "NÖ"), ("st", "St"), ("w", "W"), ("b", "B")):
        for positions in itertools.combinations(range(len(value)), len(code)):
            if "".join(value[index] for index in positions).casefold() != code:
                continue
            remainder = "".join(
                char for index, char in enumerate(value) if index not in positions)
            candidate = re.sub(r"\s+", " ", f"{club} {remainder}").strip()
            canonical = CLUBS.get(candidate.casefold())
            if canonical:
                matches.add((canonical, label))
    return next(iter(matches)) if len(matches) == 1 else None


FIXED_CHILD_PAIR_CATEGORY_RE = re.compile(
    r"^[DH]\s*-?\s*(?:12|14)(?:\b|\()", re.I)


def _clean_interleaved_pair_given_names(value, club):
    """Recover ``Given1/Given2`` plus an interleaved year/HSV fragment."""
    value = re.sub(r"\d", "", value or "").strip()
    if "/" not in value:
        return value
    first, second = value.split("/", 1)
    if not (club or "").startswith("HSV "):
        return f"{first}/{second}"
    candidates = set()
    for positions in itertools.combinations(range(len(second)), 3):
        if "".join(second[index] for index in positions).casefold() != "hsv":
            continue
        remainder = "".join(
            char for index, char in enumerate(second) if index not in positions)
        if remainder.isalpha():
            candidates.add(remainder.casefold())
    if len(candidates) == 1:
        second = next(iter(candidates)).capitalize()
    return f"{first}/{second}"


def expand_fixed_child_pair_rows(categories):
    """Split fixed-column D/H-12 and D/H-14 night teams into people.

    In this layout surname pairs live in Name while given names and the year
    can overflow into Jg. Each emitted person retains one shared team number,
    rank, time, status and source Nat value, so downstream result and regional
    views count one performance but keep both people individually clickable.
    """
    for category in categories:
        if not FIXED_CHILD_PAIR_CATEGORY_RE.match(category.get("name") or ""):
            continue
        expanded = []
        for row in category.get("results") or []:
            raw_name = (row.get("name") or "").strip()
            leading_bib = re.match(r"^(\d+)\s+(.+)$", raw_name)
            bib = row.get("sourceBib") or (leading_bib.group(1) if leading_bib else None)
            name = leading_bib.group(2) if leading_bib else raw_name
            source_jg = row.get("sourceJg") or ""
            if (name.count("/") == 1 and len(name.split()) == 1
                    and "/" in source_jg):
                given = _clean_interleaved_pair_given_names(
                    source_jg, row.get("club"))
                name = f"{name} {given}"
            names = split_pair_names(name) if "/" in name else [name]
            if not (len(names) == 2 and all(
                    len(person.split()) == 2 and looks_like_person(person)
                    for person in names)):
                expanded.append(row)
                continue
            digits = "".join(re.findall(r"\d", source_jg))
            year = None
            if len(digits) in (2, 4):
                year = int(digits)
                year = year + (2000 if year <= 26 else 1900) if year < 100 else year
            for person in names:
                result = dict(row)
                result.update({
                    "name": person,
                    "resultKind": "pair",
                    "note": "Partner: " + next(other for other in names if other != person),
                })
                if bib:
                    result["teamNumber"] = str(bib)
                if year is not None:
                    result["yearOfBirth"] = year
                result.pop("sourceBib", None)
                result.pop("sourceJg", None)
                expanded.append(result)
        category["results"] = expanded
    return categories


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
    exact_club = CLUBS.get(club.lower())
    if exact_club:
        result["club"] = club = exact_club
    # A long Verein value can start in the tail of Name and finish in Verein:
    # ``Frédéric Genevois Naturfreunde Villach | Orienteering``.  Move that
    # boundary only when the combined suffix is an exact registered club
    # spelling after punctuation normalization and at least two person-name
    # tokens remain.  This is intentionally stricter than fuzzy prefix repair.
    name_tokens = name.split()
    if len(name_tokens) >= 3 and club:
        normalized_clubs = {}
        for alias, canonical in CLUBS.items():
            for spelling in (alias, canonical):
                key = re.sub(r"[\W_]+", "", spelling.casefold())
                if key:
                    normalized_clubs[key] = canonical
        shifted_candidates = []
        for suffix_count in range(1, min(5, len(name_tokens) - 1)):
            candidate = " ".join([*name_tokens[-suffix_count:], club])
            key = re.sub(r"[\W_]+", "", candidate.casefold())
            canonical = normalized_clubs.get(key)
            if canonical:
                shifted_candidates.append((suffix_count, canonical))
        if shifted_candidates:
            suffix_count, canonical = max(shifted_candidates)
            result["name"] = name = " ".join(name_tokens[:-suffix_count])
            result["club"] = club = canonical
    # OE2003/MeOS can move a short school/club acronym to the end of Name while
    # the remainder starts cleanly in Verein (``Jasmin-LivBG | Zehnergasse``
    # or ``Kathrin Kollndorfer HSV | Grossmittel``). Restore it only when the
    # resulting complete club is an exact dictionary alias. An optional final
    # Austrian state code belongs to Nat, not to the club.
    if name and club:
        name_tokens = name.split()
        last_name_token = name_tokens[-1]
        state_match = re.search(r"\s+(NÖ|NOE|OÖ|St|W|B|S|K|T|V)$", club, re.I)
        club_without_state = (club[:state_match.start()].strip()
                              if state_match else club)
        glued_prefix_candidates = []
        for alias, canonical in CLUBS.items():
            alias_words = alias.split()
            canonical_words = canonical.split()
            if len(alias_words) < 2 or len(canonical_words) < 2:
                continue
            alias_prefix = alias_words[0]
            canonical_prefix = canonical_words[0]
            remainder = " ".join(alias_words[1:])
            if (not (2 <= len(alias_prefix) <= 7 and canonical_prefix.isupper())
                    or remainder.casefold() != club_without_state.casefold()):
                continue
            if (last_name_token == canonical_prefix
                    and len(name_tokens) >= 3):
                clean_name_tokens = name_tokens[:-1]
            elif (last_name_token.endswith(canonical_prefix)
                    and len(last_name_token) > len(canonical_prefix) + 1):
                if last_name_token[-len(canonical_prefix) - 1] in "/+&":
                    continue
                clean_name_tokens = [
                    *name_tokens[:-1], last_name_token[:-len(canonical_prefix)]]
            else:
                continue
            glued_prefix_candidates.append((
                len(alias), canonical, clean_name_tokens,
                state_match.group(1) if state_match else None))
        if not glued_prefix_candidates:
            # School names are not part of the official ÖFOL club registry,
            # so many perfectly valid ``BRG SolarCity``/``BG Ursulinen``
            # labels have no dictionary alias. The physical error is still
            # unambiguous when one of these exact organisation acronyms is a
            # separate final Name token (or glued to a pair's given names)
            # and at least two person-name tokens remain.
            for prefix in ("BG/BRG/BORG", "BG/BRG", "BRG", "BORG", "NMS",
                           "MMS", "GRG", "HBLA", "HTL", "HSV", "OLC", "NF"):
                if (last_name_token == prefix
                        and len(name_tokens) >= 3):
                    clean_name_tokens = name_tokens[:-1]
                elif (last_name_token.endswith(prefix)
                      and len(last_name_token) > len(prefix) + 1
                      and len(name_tokens) >= 2):
                    clean_name_tokens = [
                        *name_tokens[:-1], last_name_token[:-len(prefix)]]
                else:
                    continue
                combined_club = (
                    club_without_state
                    if (prefix == "NF"
                        and club_without_state.casefold().startswith("naturfreunde"))
                    else f"{prefix} {club_without_state}".strip()
                )
                canonical = CLUBS.get(combined_club.casefold(), combined_club)
                glued_prefix_candidates.append((
                    len(prefix), canonical, clean_name_tokens,
                    state_match.group(1) if state_match else None))
                break
        if glued_prefix_candidates:
            _length, canonical, clean_name_tokens, state = max(
                glued_prefix_candidates)
            result["name"] = name = " ".join(clean_name_tokens)
            result["club"] = club = canonical
            if state and not result.get("sourceNat"):
                result["sourceNat"] = state
    # A narrow Name column can keep only the surname while the given name
    # spills into the front of Verein (``Kaltenbacher | Pierre HSV OL Wiener
    # Neustadt``).  A known club at the tail plus exactly one leading token is
    # strong enough to restore the boundary without guessing.
    if (len(name.split()) == 1 and club
            and "/" not in name and "+" not in name):
        repaired_club, leading_tokens = find_trailing_club(club.split(), CLUBS)
        # Excel-to-PDF occasionally removes even the space between the
        # overflowing given name and the club (``JohannHSV Feldbach``). Find
        # a known club as a literal suffix in that case. One or two clean
        # leading name tokens are accepted; anything wider remains untouched
        # rather than turning a team/organisation label into a person.
        if not repaired_club:
            suffixes = []
            for alias, canonical in CLUBS.items():
                if len(alias) < 5 or "empty" in canonical.casefold():
                    continue
                match = re.search(re.escape(alias) + r"$", club, re.I)
                if match and match.start() > 0:
                    prefix = club[:match.start()].strip()
                    suffixes.append((len(alias), canonical, prefix.split()))
            if suffixes:
                _length, repaired_club, leading_tokens = max(suffixes)
        if not repaired_club:
            # The same overflow can truncate the *club itself* before the
            # time column (``Ziegerhofer | M. Christ HSV OL Wiener Neusta``).
            # Accept a partial tail only when the remaining text covers most
            # of one known club and differs by at most twelve final letters.
            # One/two plausible leading name tokens are then restored to Name.
            partials = []
            club_tokens = club.split()
            for lead_count in (1, 2):
                if len(club_tokens) <= lead_count:
                    continue
                candidate_leading = club_tokens[:lead_count]
                remainder = " ".join(club_tokens[lead_count:])
                # A single generic organisation word (for example the final
                # ``Orienteering`` in the complete foreign club
                # ``Naturfreunde Villach Orienteering``) is not a sufficient
                # truncated-club signature.  The confirmed overflow cases
                # retain at least two club tokens (``HSV OL ...`` or
                # ``Orienteering Kloster``).
                if len(remainder.split()) < 2:
                    continue
                for canonical in set(CLUBS.values()):
                    if "empty" in canonical.casefold():
                        continue
                    missing = len(canonical) - len(remainder)
                    if (canonical.casefold().startswith(remainder.casefold())
                            and 0 <= missing <= 12
                            and len(remainder) / len(canonical) >= 0.55):
                        partials.append((len(remainder), -missing, canonical,
                                         candidate_leading))
            if partials:
                _length, _missing, repaired_club, leading_tokens = max(partials)
        if not repaired_club:
            # On a few narrow PDF rows the given name and the beginning of
            # the club are not merely concatenated but glyph-interleaved:
            # ``Marie LHuSisVe Feldbach`` visually represents
            # ``Marie Luise | HSV Feldbach``.  Accept a de-interleaving only
            # when a complete known club alias is an ordered subsequence,
            # its distinctive final word remains a literal suffix, and all
            # leftover glyphs form one or two clean name tokens.  Those
            # constraints prevent a fuzzy club match from rewriting normal
            # organisation names.
            compact_club = [(index, char.casefold()) for index, char in enumerate(club)
                            if char.isalnum()]
            interleaved = []
            for alias, canonical in CLUBS.items():
                if "empty" in canonical.casefold():
                    continue
                alias_words = alias.split()
                if (not alias_words or len(alias_words[0]) < 3
                        or len(alias_words[-1]) < 4):
                    continue
                # A literal acronym/name is an ordinary shorter alias inside
                # a longer valid organisation (``NF`` in Naturfreunde,
                # ``Czech MTBO Team`` inside ``Czech Junior MTBO Team``), not
                # evidence of glyph overlap.  Real overlap breaks the leading
                # club token itself (``H...S...V``).
                if alias_words[0].casefold() in club.casefold():
                    continue
                if not club.casefold().endswith(alias_words[-1].casefold()):
                    continue
                alias_compact = "".join(char.casefold() for char in alias
                                        if char.isalnum())
                if len(alias_compact) < 5:
                    continue
                matched_indexes = []
                cursor = 0
                for wanted in alias_compact:
                    while (cursor < len(compact_club)
                           and compact_club[cursor][1] != wanted):
                        cursor += 1
                    if cursor == len(compact_club):
                        matched_indexes = []
                        break
                    matched_indexes.append(compact_club[cursor][0])
                    cursor += 1
                if not matched_indexes:
                    continue
                matched = set(matched_indexes)
                leftover = re.sub(
                    r"\s+", " ",
                    "".join(char for index, char in enumerate(club)
                            if index not in matched),
                ).strip()
                tokens = leftover.split()
                if (1 <= len(tokens) <= 2 and all(
                        re.fullmatch(r"[A-Za-zÀ-ž][A-Za-zÀ-ž'’-]+", token)
                        for token in tokens)):
                    interleaved.append((len(alias_compact), canonical, tokens))
            if interleaved:
                _length, repaired_club, leading_tokens = max(interleaved)
        clean_leading = (1 <= len(leading_tokens) <= 2 and all(
            (re.fullmatch(r"[A-Za-zÀ-ž]\.", token)
             or (re.fullmatch(r"[A-Za-zÀ-ž][A-Za-zÀ-ž'’-]+", token)
                 and not (len(token) > 1 and token.isupper())))
            for token in leading_tokens))
        if repaired_club and clean_leading:
            result["name"] = " ".join([name, *leading_tokens])
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
                            if "famil" in current[side]["name"].casefold():
                                row["resultKind"] = "family"
                                current[side]["results"].append(row)
                            else:
                                current[side]["results"].extend(
                                    expand_pair_result(row, current[side]["name"]))
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
        for result in category["results"]:
            repair_shifted_name_club_time(result)
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
    additions = {
        # The embedded text layer interleaves the long Hungarian club with
        # both the Nat and status columns. The rendered page clearly shows
        # this listed NYK competitor and the Fehlst status.
        ("2765-2.pdf", "NYK"): {
            "name": "Hites Gergõ",
            "club": "VBT Veszprémi Bridzs és Táj. SE",
            "timeText": "Fehlst",
            "status": "mp",
            "yearOfBirth": 2012,
        },
    }
    file_name = Path(path).name
    for category in categories:
        addition = additions.get((file_name, category.get("name") or ""))
        if addition and not any(
                (result.get("name") or "").casefold()
                == addition["name"].casefold()
                for result in category.get("results") or []):
            category["results"].append(dict(addition))
        for result in category.get("results") or []:
            correction = corrections.get((file_name, result.get("name") or ""))
            if correction:
                result.update(correction)
            if file_name == "4089-3.pdf":
                # The Sprint-Bundesmeisterschaft's Nat column is printed on
                # top of long school names.  The PDF text layer consequently
                # weaves the state code into the school (``OberschüBtzen`` =
                # ``Oberschützen`` + ``B``) even though both columns remain
                # visually distinct.  These substitutions were checked on
                # the rendered source and recover both independent fields.
                club = result.get("club") or ""
                school_nat_repairs = (
                    ("BG/BRG Villach St. MartinK",
                     "BG/BRG Villach St. Martin", "K"),
                    ("BG/BRG/BORG OberschüBtzen",
                     "BG/BRG/BORG Oberschützen", "B"),
                    ("BG/BRG Kirchengasse GrSazt",
                     "BG/BRG Kirchengasse Graz", "St"),
                    ("Christian-Doppler-GymnasSium",
                     "Christian-Doppler-Gymnasium", "S"),
                    ("BG/BRG Zehnergasse WieNnÖer Neus",
                     "BG/BRG Zehnergasse Wiener Neus", "NÖ"),
                    ("BG/BRG Zehnergasse WieNnÖer Neu",
                     "BG/BRG Zehnergasse Wiener Neu", "NÖ"),
                )
                for damaged, repaired, nat in school_nat_repairs:
                    if club == damaged:
                        result["club"] = repaired
                        result["sourceNat"] = nat
                        break
                # A separate narrow-column overflow can repeat the given
                # name and leave it in Jg as well.  Consecutive equality plus
                # the independently present two-digit year makes this repair
                # deterministic rather than a fuzzy person-name rewrite.
                name_tokens = (result.get("name") or "").split()
                if (len(name_tokens) >= 3
                        and name_tokens[-1].casefold() == name_tokens[-2].casefold()):
                    result["name"] = " ".join(name_tokens[:-1])
                source_jg = (result.get("sourceJg") or "").strip()
                year_match = re.search(r"(?:^|\s)(\d{2}|\d{4})$", source_jg)
                if year_match and source_jg != year_match.group(1):
                    result["sourceJg"] = year_match.group(1)
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
            # Most two-table exports place both ``(N)`` category markers on
            # one extracted line.  In some Linz-Cup PDFs the two headings are
            # vertically staggered by a few pixels, so text extraction emits
            # separate lines even though the page is still a two-column
            # matrix.  Let the x-coordinate based detector decide for this
            # stable report family as well; otherwise the generic parser
            # either returns no rows or assigns the entire left table to the
            # first right-hand category (event 3711).
            two_column = (
                parse_two_column_pdf(path)
                if (
                    re.search(r"\(\d+\).{8,}\(\d+\)", head_text)
                    or re.search(r"Ergebnisliste\s+\d+\.\s*Linz-Cup\b",
                                 head_text, re.I)
                )
                else None
            )
            # A visually split page can still use a row shape outside the
            # generic two-column parser's contract.  An empty parse must
            # therefore fall back to the ordinary format-specific pipeline
            # instead of suppressing a previously valid result document.
            if two_column:
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
                            # Real additional participant columns sit between
                            # the primary Name and Verein columns (for example
                            # ``Name Text1 Text2 Verein Zeit``).  Classic OE2003
                            # individual exports instead put one narrow Text1
                            # annotation column *after* Zeit.  Treating that as
                            # a participant made the name ranges run backwards
                            # and silently discarded entire school result lists.
                            and all(word["x0"] < club_header["x0"]
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
                        if (uncounted_category and re.search(
                                r"\d{1,3}:\d{2}", uncounted_category.group("name"))):
                            uncounted_category = None
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
                    fixed_school_nat_layout = bool(
                        {"Stnr", "Name", "Verein", "Nat", "Zeit"}
                        .issubset(header_labels)
                        and re.search(r"Schul|School|Schüler", head_text, re.I)
                    )
                    flow = (None if fixed_school_nat_layout
                            else parse_flow_row(text, CLUBS))
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
                    # Name, Verein and Nat are left-aligned columns.  The
                    # midpoint strategy in assign_columns() is appropriate
                    # for right-aligned numeric cells, but moves the second
                    # half of a long name into Verein and long club suffixes
                    # into Nat.  Re-slice these textual cells at their actual
                    # printed header starts.  This also makes sourceNat safe:
                    # only glyphs that really begin in the Nat column are
                    # retained, rather than an overflowing school/club name.
                    header_x = {label: x0 for label, x0 in headers}
                    classic_oe2003_text_columns = bool(
                        {"Pl", "Stnr", "Name", "Verein", "Nat", "Zeit", "Text1"}
                        .issubset(header_x)
                        and header_x["Text1"] > header_x["Zeit"]
                    )
                    fixed_school_text_columns = (
                        classic_oe2003_text_columns or fixed_school_nat_layout)
                    if fixed_school_text_columns and "Name" in header_x:
                        name_end = min(
                            (x0 for label, x0 in headers
                             if x0 > header_x["Name"]
                             and label in {"Jg", "Verein", "Verein/Schule", "Nat",
                                           "Zeit", "Gesamt"}),
                            default=float("inf"),
                        )
                        rec["Name"] = " ".join(
                            word["text"] for word in line
                            if header_x["Name"] - 1 <= word["x0"] < name_end - 1
                        ).strip()
                    club_label = next(
                        (label for label in ("Verein", "Verein/Schule")
                         if label in header_x), None)
                    if fixed_school_text_columns and club_label and "Nat" in header_x:
                        rec[club_label] = " ".join(
                            word["text"] for word in line
                            if header_x[club_label] - 1 <= word["x0"] < header_x["Nat"] - 1
                        ).strip()
                        next_numeric_x = min(
                            (x0 for label, x0 in headers
                             if x0 > header_x["Nat"]
                             and label in {"Zeit", "Gesamt", "Punkte", "Pkt"}),
                            default=float("inf"),
                        )
                        # Result values are usually right-aligned and begin a
                        # few pixels before their header.  Their midpoint is
                        # therefore the correct right boundary for Nat.
                        nation_end = ((header_x["Nat"] + next_numeric_x) / 2
                                      if next_numeric_x != float("inf")
                                      else float("inf"))
                        rec["Nat"] = " ".join(
                            word["text"] for word in line
                            if header_x["Nat"] - 1 <= word["x0"] < nation_end - 1
                        ).strip()
                    name = rec.get("Name", "").strip()
                    source_jg = (rec.get("Jg") or "").strip()
                    school_club = None
                    if school_row_mode:
                        header_x = {label: x0 for label, x0 in headers}
                        if {"Name", "Verein", "Zeit"}.issubset(header_x):
                            name = " ".join(
                                word["text"] for word in line
                                if (header_x["Name"] - 1 <= word["x0"]
                                    < header_x["Verein"] - 1)
                            ).strip()
                            school_time_x = next((
                                word["x0"] for word in line
                                if word["x0"] >= header_x["Verein"]
                                and (FLOW_TIME_RE.fullmatch(word["text"].strip("()"))
                                     or parse_status(word["text"]))
                            ), header_x["Zeit"])
                            if fixed_school_text_columns and "Nat" in header_x:
                                school_club = (rec.get("Verein") or "").strip()
                            else:
                                school_club = " ".join(
                                    word["text"] for word in line
                                    if (header_x["Verein"] - 1 <= word["x0"]
                                        < school_time_x - 1)
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
                    # A long surname can push the given name into Jg while
                    # leaving the year at the end (``Aus der Schmitten | Paul
                    # 03`` or ``Lipphart-Kirchmeir | Harald 67``). Rejoin only
                    # this explicit alpha+year form; slash-pair Jg cells are
                    # handled by expand_fixed_child_pair_rows below.
                    overflow_given = re.fullmatch(
                        r"(?P<given>[A-Za-zÀ-ž.'’\-]+)\s+(?P<year>\d{2}|\d{4})",
                        source_jg)
                    if overflow_given and "/" not in name:
                        name = f"{name} {overflow_given.group('given')}".strip()
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
                    joined_pair = expand_pair_result(
                        {"name": name}, current["name"])
                    valid_joined_pair = len(joined_pair) > 1
                    if ((is_junk_name(name) and not valid_joined_pair)
                            or name.lstrip().startswith(("-", "–", "—"))):
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
                        status_tail = STATUS_TAIL_RE.search(text)
                        physical_status = parse_status(
                            status_tail.group(0) if status_tail else "")
                        if not stnr_text.isdigit() and physical_status in (None, "ok"):
                            continue
                        if physical_status not in (None, "ok"):
                            # Score and club-championship tables can print
                            # DNS/DNF/MP in a final ``Erg`` column beyond the
                            # nominal ``Zeit`` boundary.  Keep that visible
                            # row even when it has neither placement nor bib.
                            time_text = status_tail.group(0).strip()
                            name = re.sub(r"^([^,]+),\s+(.+)$", r"\1 \2", name)

                    club_text = (recovered_overflow_club or
                                 (school_club if school_club is not None else
                                  (rec.get("Verein") or rec.get("Verein/Schule") or ""))).strip()
                    nation_text = (rec.get("Nat") or "").strip()
                    # In a few crowded OE2003 school tables the one/two-letter
                    # state code is printed directly over the final club word.
                    # pdfplumber then interleaves both glyph streams into one
                    # token which starts left of the Nat boundary.  Repair the
                    # two observed deterministic forms; leaving them intact
                    # would invent clubs such as ``OberschützeBn`` and lose the
                    # authoritative state discriminator at the same time.
                    if not nation_text and club_text.endswith("OberschützeBn"):
                        club_text = club_text[:-len("OberschützeBn")] + "Oberschützen"
                        nation_text = "B"
                    elif not nation_text and club_text.endswith("NeuNsÖ"):
                        club_text = club_text[:-len("NeuNsÖ")] + "Neus"
                        nation_text = "NÖ"
                    # In narrow Landes-MS tables a long club can spill into
                    # Nat and touch its one/two-letter state code without a
                    # space (``HSV OL Wiener | NeustadtNÖ``). Repair this only
                    # when the document header independently establishes a
                    # regional championship; in an international Country
                    # column a short trailing letter must remain untouched.
                    if re.search(r"\b(?:LM|Landesmeister)", head_text, re.I):
                        regional_overflow = re.fullmatch(
                            r"(?P<club>.*?)(?P<state>NÖ|NOE|St\.?|W|B)",
                            nation_text, re.I)
                        if regional_overflow:
                            overflow_club = regional_overflow.group("club").strip()
                            if overflow_club:
                                club_text = f"{club_text} {overflow_club}".strip()
                            nation_text = regional_overflow.group("state")
                        # In this paired night layout the final ``HSV`` of
                        # the club overlaps the preceding Jg/given-name cell.
                        # Recover it only when both independent fragments are
                        # present: the surviving unique club suffix and an
                        # H...S...V glyph sequence in that overflow cell.
                        if (club_text == "OL Wiener Neustadt"
                                and re.search(r"h.*s.*v", rec.get("Jg", ""), re.I)):
                            club_text = "HSV OL Wiener Neustadt"
                        interleaved = repair_interleaved_regional_nat(
                            club_text, nation_text)
                        if interleaved:
                            club_text, nation_text = interleaved
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
                    if source_jg:
                        result["sourceJg"] = source_jg
                    source_bib = (rec.get("Stnr") or "").strip()
                    if source_bib.isdigit():
                        result["sourceBib"] = source_bib
                    # Preserve the source's Nat column verbatim. In ordinary
                    # international result lists this is a country code, but
                    # joint Austrian Landesmeisterschaften also use it as the
                    # authoritative state-ranking discriminator (W, NÖ, B,
                    # St). The database builder interprets those short codes
                    # only inside a confirmed regional-championship context.
                    if nation_text:
                        result["sourceNat"] = nation_text
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
                    yob_match = re.search(r"(?:^|\s)(\d{2}|\d{4})$", source_jg)
                    if source_jg.isdigit() or yob_match:
                        y = int(source_jg if source_jg.isdigit() else yob_match.group(1))
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
                    elif "famil" in current["name"].casefold():
                        result["resultKind"] = "family"
                        current["results"].append(result)
                    else:
                        # ``/`` child pairs need the coordinate-aware repair
                        # below, which can recover a club prefix glued to the
                        # second name. A literal ``&`` is unambiguous in the
                        # Linz-Cup sources; ``+`` is expanded here only for
                        # explicit school/child classes. Other plus-names
                        # remain one source identity (for example the historic
                        # Ladics Thomas+Stephan row whose embedded glyph repair
                        # is keyed to the literal source spelling).
                        should_expand_pair = (
                            "&" in name
                            or ("+" in name and re.search(
                                r"sch(?:u|ü)ler|(?:^|\\s)[DH]-?(?:12|14)(?:\\s|$)",
                                current["name"], re.I))
                        )
                        if should_expand_pair:
                            current["results"].extend(
                                expand_pair_result(result, current["name"]))
                        else:
                            current["results"].append(result)

    categories = repair_wrapped_champion_names(path, categories)
    categories = expand_fixed_child_pair_rows(categories)
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
            if candidate and declared is not None:
                candidate_results = [
                    clean_flow_candidate(result)
                    for result in candidate["results"]]
                current_count = category_competitor_unit_count(category)
                candidate_complete = (
                    category_competitor_unit_count(candidate) == declared)
                if candidate_complete and current_count * 2 < declared and all(
                        len((row.get("name") or "").split()) >= 2
                        and not is_junk_name(row.get("name") or "")
                        for row in candidate_results):
                    category["results"] = candidate_results
                    continue
                current_names = {
                    result_name_key(result) for result in category["results"]}
                candidate_names = {
                    result_name_key(result) for result in candidate_results}
                if (candidate_complete and current_names <= candidate_names
                        and all(
                            len((row.get("name") or "").split()) >= 2
                            and not is_junk_name(row.get("name") or "")
                            for row in candidate_results)):
                    category["results"] = candidate_results
                    continue
                missing = [
                    result for result in candidate_results
                    if (result_name_key(result) not in current_names
                        and not any(
                            result_name_key(result).startswith(current_key + " ")
                            for current_key in current_names))]
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
    # Exact-count flow recovery above runs after the ordinary boundary repair
    # and can reintroduce the same shifted acronym on the replacement rows.
    # One final idempotent pass keeps every exit path consistent.
    for category in categories:
        for result in category.get("results") or []:
            repair_shifted_name_club_time(result)
    return categories, head_text


def parse_flow_category_line(text):
    """Recognize a category header in the numbered-list layout. Returns
    (name, declaredStarters_or_None) or None. Guards against a numbered data
    row ('1. Erik Simkovics ... 1 (Posten 60)') being mistaken for one: a
    genuine category never starts with a rank prefix."""
    numeric_course = re.fullmatch(r"(?P<name>\d+\s+Posten(?:\s+Team)?)\s*:?", text, re.I)
    if numeric_course:
        return numeric_course.group("name"), None
    if RANK_PREFIX_RE.match(text.split(" ", 1)[0]):
        return None
    if MEOS_PAGE_HEADER_RE.match(text):
        return None
    text = re.sub(r"(\d)[,.]O\b", r"\1,0", text)
    m = INLINE_SIMPLE_CATEGORY_HEADER_RE.match(text)
    if m:
        return f"{m.group('name').title()} {m.group('class').upper()}", None
    m = re.match(r"^(?P<name>[A-E])\s*-\s*\d+\s*P\b", text, re.I)
    if m:
        return m.group("name").upper(), None
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
    if m and not re.search(r"\d{1,3}:\d{2}", m.group("name")):
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
    m = (PLAIN_LETTER_CAT_RE.fullmatch(text)
         or PLAIN_AGE_CATEGORY_RE.fullmatch(text)
         or PLAIN_SPECIAL_CATEGORY_RE.fullmatch(text)
         or SIMPLE_FLOW_CATEGORY_RE.fullmatch(text))
    if m:
        return (m.groupdict().get("name") or m.group(0)).strip(" :"), None
    return None


def parse_flow_result_row(text, clubs, prefer_last_time=False):
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

    # A crowded last column can glue a long club's final word directly to
    # the elapsed time (``Orienteering1:13:30``). Split only a terminal,
    # well-formed time suffix; the prefix remains available to the ordinary
    # club resolver.
    glued_time = CLUB_TIME_SUFFIX_RE.search(toks[-1])
    if glued_time and glued_time.start() > 0:
        prefix = toks[-1][:glued_time.start()]
        toks[-1:] = [prefix, glued_time.group("time")]
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

    time_indices = [
        i for i, token in enumerate(toks)
        if FLOW_TIME_RE.match(token.strip("()"))]
    if prefer_last_time:
        clock_indices = [i for i in time_indices if ":" in toks[i]]
        time_idx = clock_indices[-1] if clock_indices else (
            time_indices[-1] if time_indices else None)
    else:
        time_idx = time_indices[0] if time_indices else None
    if time_idx is not None:
        raw_time = toks[time_idx]
        incomplete_elapsed = (
            prefer_last_time
            and 0 < len(clock_indices) < 3
            and time_idx + 1 < len(toks)
        )
        # In Startzeit/Zielzeit/Laufzeit tables the earlier clock values are
        # columns, not part of the runner name. Stop the identity body before
        # the first of them while selecting the final clock as elapsed time.
        body_end = time_indices[0] if prefer_last_time and time_indices else time_idx
        body = toks[:body_end]
        trailing_text = " ".join(toks[time_idx + 1:])
        if incomplete_elapsed:
            # Only start and finish clocks are present; the final Laufzeit
            # cell contains a note. Never publish the finish clock as an
            # elapsed time (e.g. ``17:56:36 Nr.10 fehlt``).
            time_text = None
            status_text = trailing_text
            explicit_status = parse_status(trailing_text) or "unknown"
            raw_time = ""
        else:
            time_text = re.sub(
                r"[,.]", ":", raw_time.strip("()").lstrip("+"), count=1)
            status_text = None
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
    qualifiers = [token for token in body if re.fullmatch(r"\(\d+\)", token)]
    if qualifiers:
        body = [token for token in body if not re.fullmatch(r"\(\d+\)", token)]
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
    # Several Linz-Cup result sheets put an isolated numeric club code
    # between the name and the textual club. It carries no identity value.
    if name_toks and name_toks[-1] == "0":
        name_toks.pop()
    name = " ".join(name_toks).strip()
    name = re.sub(r"^([^,]+),\s+(.+)$", r"\1 \2", name)
    pair_names = (
        split_pair_names(name)
        if "/" in name or "+" in name or "&" in name
        else [name]
    )
    valid_pair = (len(pair_names) > 1 and all(
        len(pair_name.split()) == 2 and looks_like_person(pair_name)
        for pair_name in pair_names))
    if is_junk_name(name) or (not looks_like_person(name) and not valid_pair):
        return None

    result = {"name": name, "club": club or "", "timeText": time_text or status_text or ""}
    if name.casefold().startswith("team "):
        result.update({
            "name": "", "resultKind": "team", "memberlessTeam": True,
            "teamName": name, "note": f"Mannschaft: {name} · keine vollständigen Teilnehmernamen in der Quelle",
        })
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
    if qualifiers:
        result["note"] = "Quellzusatz: " + " ".join(qualifiers)
    return result


def parse_flowing_pdf(path):
    """Fallback for the numbered-list layout (no Pl/Stnr/Verein columns) that
    parse_pdf() can't see at all, since it never finds a "Pl"/"Platz" header
    to anchor on. Works on plain extracted text, not word x-positions -
    there are no columns to align."""
    import pdfplumber

    categories, current = [], None
    pending_rank = pending_championship = None
    prefer_last_time = False
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                for line in (page.extract_text() or "").split("\n"):
                    line = line.strip()
                    if (not line or DATE_HEADER_RE.search(line)
                            or MEOS_PAGE_HEADER_RE.match(line)):
                        continue
                    if re.search(r"\bStartzeit\b.*\bZielzeit\b.*\bLaufzeit\b", line, re.I):
                        prefer_last_time = True
                    explicit_place = re.match(r"^(\d{1,3})\.\s+", line)
                    leading_status = re.match(
                        r"^(Fehlst|Aufg|Disqu?|N\s*Ang)\.?\s+(.+)$", line, re.I)
                    if leading_status:
                        # Some hand-made ranking lists put the classification
                        # before, rather than after, the runner. Reorder only
                        # this explicit status token for the shared row parser.
                        line = f"{leading_status.group(2)} {leading_status.group(1)}"
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
                        r"(\d{1,3})\.?(?:\s+\d{1,3})*(?:\s+\([^)]*\))?", line)
                    if current is not None and detached_rank:
                        pending_rank = int(detached_rank.group(1))
                        continue
                    annot_rank, annot_championship = parse_champion_annotation(line)
                    if annot_rank is not None:
                        pending_rank, pending_championship = annot_rank, annot_championship
                        continue
                    # ``AK Name (2) ... Fehlst`` is an OOC result row whose
                    # parenthesized source qualifier resembles a category's
                    # starter count. Do not let that shape start a synthetic
                    # category and swallow the competitor.
                    probable_ooc_result = bool(
                        re.match(r"^A\.?\s*K\.?\s+", line, re.I)
                        and STATUS_TAIL_RE.search(line))
                    cat = None if probable_ooc_result else parse_flow_category_line(line)
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
                        if explicit_place and flow.get("rank") is None:
                            # A dot after the leading number is an explicit
                            # placement, even in ranking-only KO lists which
                            # intentionally publish no elapsed times.
                            flow["rank"] = int(explicit_place.group(1))
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
                    row = parse_flow_result_row(line, CLUBS, prefer_last_time)
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


def parse_nolv_school_result_pdf(path):
    """Parse FUN.O NÖ's ``SI Rang NACHNAME ... Schule Kategorie Zeit`` PDF.

    The report centers its headers but left-aligns the cells beneath them.
    Generic midpoint assignment therefore puts the tail of ``Klasse`` into
    Schule and the tail of Schule into Zeit, retaining only a few DSQ rows.
    The physical column starts are stable across all pages and the explicit
    ``Kategorie X`` continuation headings provide the result-list boundary.
    """
    import pdfplumber

    categories = []
    current = None
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            for line_words in group_lines(page.extract_words()):
                text = " ".join(word["text"] for word in line_words).strip()
                category_match = re.fullmatch(r"Kategorie\s+(.+)", text, re.I)
                if category_match:
                    current = {
                        "name": category_match.group(1).strip(),
                        "declaredStarters": None,
                        "results": [],
                    }
                    categories.append(current)
                    continue
                if current is None or not line_words:
                    continue
                first = line_words[0]["text"].strip()
                if not first.isdigit() or line_words[0]["x0"] > 75:
                    continue

                def cell(start, end=None):
                    return " ".join(
                        word["text"] for word in line_words
                        if word["x0"] >= start and (end is None or word["x0"] < end)
                    ).strip()

                rank_text = cell(75, 110).rstrip(".")
                surname = cell(110, 210)
                given = cell(210, 280)
                class_text = cell(280, 325)
                school = cell(325, 449)
                source_category = cell(449, 510)
                raw_value = cell(510)
                if not surname or not given or not school or not raw_value:
                    continue
                time_text = raw_value
                if re.fullmatch(r"\d{1,3}\.\d{2}", time_text):
                    time_text = time_text.replace(".", ":")
                result = {
                    "name": f"{surname} {given}".strip(),
                    "club": school,
                    "timeText": time_text,
                    "sourceBib": first,
                    "status": parse_status(time_text) or "unknown",
                }
                if source_category:
                    result["sourceCategory"] = source_category
                if rank_text.isdigit():
                    result["rank"] = int(rank_text)
                seconds = parse_time(time_text)
                if seconds is not None:
                    result["timeS"] = seconds
                    result["status"] = "ok"
                year_match = re.search(r"\b(?:19|20)\d{2}\b", class_text)
                if year_match:
                    result["yearOfBirth"] = int(year_match.group())
                current["results"].append(result)
    for category in categories:
        category["declaredStarters"] = len(category["results"])
    return [category for category in categories if category["results"]]


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
                repair_shifted_name_club_time(result)
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
                        (word for word in words
                         if re.fullmatch(r"\(\d+", word["text"])), None)
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
                    if (not is_pair and not pair_names
                            and looks_like_person(re.sub(r",\s*", " ", name).strip())):
                        # A hand-entered MeOS identity may contain a comma
                        # inside an otherwise person-like multiword name.
                        # Preserve that source value as one result unit.
                        pair_names = [name]
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
                    cleaned_person_name = re.sub(r",\s*", " ", name).strip()
                    if (not pair_names or
                            (not is_pair and is_junk_name(name)
                             and not looks_like_person(cleaned_person_name))):
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

    for category in categories:
        for result in category["results"]:
            repair_shifted_name_club_time(result)
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


def parse_inline_mannschaft_pdf(path):
    """Parse landscape Mannschaft tables with three members on each row.

    The single result value classifies the whole team. Members therefore get
    no invented individual time or leg number; they share ``teamTimeText``
    and ``teamStatus``. Plain squad numbers are only unique within a club, so
    they remain part of ``teamName`` instead of becoming a colliding global
    ``teamNumber``. Distinct codes such as ``OCFF1`` stay as team numbers.
    """
    import pdfplumber

    categories = []
    current = None
    category_re = re.compile(
        r"^(?P<name>.+?)\s+\((?P<count>\d+)\)(?:\s+\(Forts\.\))?$", re.I)

    def column_text(words, left, right):
        return re.sub(
            r"\s+", " ",
            " ".join(word["text"] for word in words
                     if left <= word["x0"] < right),
        ).strip()

    def clean_member(value):
        value = re.sub(r"\s+", " ", (value or "").strip(" ,"))
        if not value or re.fullmatch(r"N\.?\s*N\.?", value, re.I):
            return None
        value = re.sub(r"^([^,]+),\s*(.+)$", r"\1 \2", value)
        return value if looks_like_person(value) and not is_junk_name(value) else None

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                for words in group_lines(page.extract_words(
                        x_tolerance=1, y_tolerance=2)):
                    line = " ".join(word["text"] for word in words).strip()
                    category_match = category_re.match(line)
                    if category_match:
                        current = {
                            "name": category_match.group("name").strip(),
                            "declaredStarters": int(category_match.group("count")),
                            "sourceUnitCount": 0,
                            "results": [],
                        }
                        categories.append(current)
                        continue
                    if current is None or not words:
                        continue
                    rank_text = column_text(words, 25, 56)
                    club = column_text(words, 56, 215)
                    squad = column_text(words, 215, 281)
                    members = [
                        clean_member(column_text(words, 281, 400)),
                        clean_member(column_text(words, 400, 517)),
                        clean_member(column_text(words, 517, 640)),
                    ]
                    members = [member for member in members if member]
                    team_value = column_text(words, 640, 703)
                    seconds = parse_time_loose(team_value)
                    team_status = ("ok" if seconds is not None
                                   else (parse_status(team_value) or "unknown"))
                    is_ooc = bool(re.search(r"\bAK\b", rank_text, re.I))
                    rank_match = re.search(r"\d+", rank_text)
                    rank = (int(rank_match.group())
                            if rank_match and not is_ooc else None)
                    # Repeated page headers fall inside the preceding class's
                    # vertical flow, but ``Zeit`` is not a result value. A
                    # real row always has a normalized time/status; the squad
                    # label itself may legitimately be blank (trainer team in
                    # event 3334).
                    if (not club or team_status == "unknown"
                            or (not team_value and rank is None and not is_ooc)):
                        continue

                    team_name = re.sub(r"\s+", " ", f"{club} {squad}").strip()
                    team_number = (squad if squad and not squad.isdigit() else None)
                    current["sourceUnitCount"] += 1
                    common = {
                        "club": club,
                        "resultKind": "team",
                        "status": team_status,
                        "individualStatus": None,
                        "teamStatus": team_status,
                        "teamNumber": team_number,
                        "teamName": team_name,
                        "teamTimeText": team_value,
                    }
                    if seconds is not None:
                        common["teamTimeS"] = seconds
                    if rank is not None:
                        common["rank"] = rank
                    if is_ooc:
                        common["outOfCompetition"] = True

                    if not members:
                        result = dict(common)
                        result.update({
                            "name": "",
                            "timeText": "",
                            "memberlessTeam": True,
                            "note": ("Mannschaft: " + team_name
                                     + " · keine Teilnehmernamen in der Quelle"),
                        })
                        current["results"].append(result)
                        continue

                    for member in members:
                        mates = [name for name in members if name != member]
                        result = dict(common)
                        result.update({
                            "name": member,
                            "timeText": "",
                            "note": ("Mannschaft: " + team_name
                                     + (" · Team: " + ", ".join(mates)
                                        if mates else "")),
                        })
                        current["results"].append(result)

    return merge_category_continuations(
        [category for category in categories if category["results"]])


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


def parse_sime_pdf(path):
    """Parse SIME's ``Bahn (N) / # NR Name Club Resultat`` PDF printout."""
    import pdfplumber

    category_re = re.compile(r"^(?P<name>.+?)\s+Bahn\s+\(\d+\):", re.I)
    row_re = re.compile(
        r"^(?:(?P<rank>\d+)\.\s+)?(?P<bib>\d+)\s+(?P<body>.+?)\s+"
        r"(?P<value>\d{2}:\d{2}:\d{2}|\d{1,3}:\d{2}|DQ|DSQ|DNS|DNF|MP)\b",
        re.I)
    categories, current = [], None
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                for raw in (page.extract_text() or "").splitlines():
                    line = re.sub(r"\s+", " ", raw).strip()
                    category = category_re.match(line)
                    if category:
                        current = {
                            "name": category.group("name").strip(),
                            "declaredStarters": None, "results": [],
                        }
                        categories.append(current)
                        continue
                    if current is None:
                        continue
                    match = row_re.match(line)
                    if not match:
                        continue
                    body = match.group("body").split()
                    club, name_tokens = find_trailing_club(body, CLUBS)
                    if club is None:
                        if len(body) < 2:
                            continue
                        name_tokens, club = body, ""
                    name = " ".join(name_tokens)
                    if not looks_like_person(name) or is_junk_name(name):
                        continue
                    value = match.group("value")
                    seconds = parse_time_loose(value)
                    result = {
                        "name": name, "club": club, "timeText": value,
                        "status": "ok" if seconds is not None else
                                  (parse_status(value) or "unknown"),
                    }
                    if match.group("rank"):
                        result["rank"] = int(match.group("rank"))
                    if seconds is not None:
                        result["timeS"] = seconds
                    current["results"].append(result)
    parsed = [category for category in categories if category["results"]]
    for category in parsed:
        category["declaredStarters"] = len(category["results"])
    return parsed


def parse_origare_pdf(path):
    """Parse the Italian Origare category/ranking result format."""
    import pdfplumber

    category_re = re.compile(r"^Categoria:\s*(?P<name>.+?)\s*\(", re.I)
    row_re = re.compile(
        r"^(?P<rank>\d+)\s+(?P<bib>\d+)\s+(?P<time>\d{2}:\d{2}:\d{2})\s+"
        r"(?P<body>.+?)\s+(?P<nation>[A-Z]{3})\s+(?P<points>\d+[.,]\d+)\s*$")
    categories, current = [], None
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                for raw in (page.extract_text() or "").splitlines():
                    line = re.sub(r"\s+", " ", raw).strip()
                    category = category_re.match(line)
                    if category:
                        current = {
                            "name": category.group("name").strip(),
                            "declaredStarters": None, "results": [],
                        }
                        categories.append(current)
                        continue
                    if current is None:
                        continue
                    match = row_re.match(line)
                    if not match:
                        continue
                    tokens = match.group("body").split()
                    boundary = next((index for index, token in enumerate(tokens)
                                     if index >= 2 and any(ch.isdigit() for ch in token)), None)
                    if boundary is None:
                        continue
                    name = " ".join(tokens[:boundary])
                    club = " ".join(tokens[boundary:])
                    if not looks_like_person(name) or is_junk_name(name):
                        continue
                    seconds = parse_time_loose(match.group("time"))
                    current["results"].append({
                        "name": name, "club": club,
                        "timeText": match.group("time"), "timeS": seconds,
                        "status": "ok", "rank": int(match.group("rank")),
                        "scoreText": match.group("points"),
                    })
    parsed = [category for category in categories if category["results"]]
    for category in parsed:
        category["declaredStarters"] = len(category["results"])
    return parsed


def parse_course_kat_pdf(path):
    """Parse course-grouped OE results whose real class is in each row."""
    import pdfplumber

    by_category = {}

    def category_name(code):
        regional = re.fullmatch(r"([DH])(\d+)(Bgld|Stmk|W|NÖ)", code, re.I)
        if regional:
            gender = "Damen" if regional.group(1).upper() == "D" else "Herren"
            return f"{gender} ab {regional.group(2)} {regional.group(3)}"
        general = re.fullmatch(r"([DH])(\d+)A", code, re.I)
        if general:
            gender = "Damen" if general.group(1).upper() == "D" else "Herren"
            return f"{gender} ab {general.group(2)}"
        return kat_to_category_name(code)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                for raw in (page.extract_text() or "").splitlines():
                    line = re.sub(r"\s+", " ", raw).strip()
                    value_match = (re.search(r"(\d{1,3}:\d{2}(?::\d{2})?)\s*$", line)
                                   or STATUS_TAIL_RE.search(line))
                    if not value_match or value_match.end() != len(line):
                        continue
                    value = value_match.group(0).strip()
                    prefix = line[:value_match.start()].strip()
                    rank_match = re.match(r"^(\d+)\s+(.+)$", prefix)
                    source_rank = int(rank_match.group(1)) if rank_match else None
                    body_text = rank_match.group(2) if rank_match else prefix
                    kat_match = re.search(r"\s+([DH][A-Za-zÄÖÜäöüß\d-]+)\s*$", body_text)
                    if not kat_match:
                        continue
                    kat = kat_match.group(1)
                    identity = body_text[:kat_match.start()].split()
                    if len(identity) < 2:
                        continue
                    year_at = next((i for i, token in enumerate(identity)
                                    if i >= 2 and re.fullmatch(r"\d{2}|\d{4}", token)), None)
                    yob = None
                    if year_at is not None:
                        name_tokens, yob = identity[:year_at], identity[year_at]
                        club = " ".join(identity[year_at + 1:])
                    else:
                        club, name_tokens = find_trailing_club(identity, CLUBS)
                        if club is None:
                            continue
                    pair_names = None
                    if kat.upper() in {"H14", "D14"}:
                        if len(name_tokens) == 4:
                            pair_names = [" ".join(name_tokens[:2]), " ".join(name_tokens[2:])]
                        elif (len(name_tokens) >= 5
                              and [token.casefold() for token in name_tokens[:3]]
                              == ["aus", "der", "schmitten"]):
                            pair_names = [" ".join(name_tokens[:4]), " ".join(name_tokens[4:])]
                    name = " ".join(name_tokens)
                    if ((not pair_names and not looks_like_person(name))
                            or is_junk_name(name)):
                        continue
                    seconds = parse_time_loose(value)
                    row = {
                        "name": name, "club": club or "", "timeText": value,
                        "status": "ok" if seconds is not None else
                                  (parse_status(value) or "unknown"),
                        "_sourceRank": source_rank,
                    }
                    if seconds is not None:
                        row["timeS"] = seconds
                    if yob:
                        year = int(yob)
                        row["yearOfBirth"] = year + (
                            2000 if year <= 26 else 1900) if year < 100 else year
                    category = by_category.setdefault(kat, {
                        "name": category_name(kat), "sourceCategory": kat,
                        "declaredStarters": None, "sourceUnitCount": 0, "results": [],
                    })
                    category["sourceUnitCount"] += 1
                    if pair_names and all(looks_like_person(member) for member in pair_names):
                        for member in pair_names:
                            paired = dict(row)
                            paired.update({
                                "name": member, "resultKind": "pair",
                                "note": "Partner: " + next(
                                    other for other in pair_names if other != member),
                            })
                            category["results"].append(paired)
                    else:
                        category["results"].append(row)

    for category in by_category.values():
        next_rank = 1
        rank_map = {}
        for result in category["results"]:
            source_rank = result.pop("_sourceRank", None)
            if source_rank is not None and result.get("status") == "ok":
                if source_rank not in rank_map:
                    rank_map[source_rank] = next_rank
                    next_rank += 1
                result["rank"] = rank_map[source_rank]
        category["declaredStarters"] = category["sourceUnitCount"]
    return list(by_category.values())


def parse_vorarlberg_school_pdf(path):
    """Parse the stable Vorarlberg school-cup Name/Team/Time layout."""
    import pdfplumber

    category_re = re.compile(
        r"^(?P<name>(?:Damen|Herren)\s+\d+)\s+Distanz:\s*", re.I)
    categories, current = [], None
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                for raw in (page.extract_text() or "").splitlines():
                    line = re.sub(r"\s+", " ", raw).strip()
                    category = category_re.match(line)
                    if category:
                        current = {
                            "name": category.group("name").strip(),
                            "declaredStarters": None, "results": [],
                        }
                        current.update(parse_course_info(line))
                        categories.append(current)
                        continue
                    if current is None or line.startswith("Rang"):
                        continue
                    prefix = re.match(
                        r"^(?P<mark>\d+|Gast|DNS|FSt|[–—-])\s*(?P<body>[A-Za-zÄÖÜäöüÀ-ž].+)$",
                        line, re.I)
                    if not prefix:
                        continue
                    body = prefix.group("body")
                    time_match = re.search(r"\b(\d{1,2}:\d{2}:\d{2})\b", body)
                    status = None
                    if re.search(r"nicht angetreten|\bDNS\b", line, re.I):
                        status = "dns"
                    elif re.search(r"Posten fehlt|\bFSt\b", line, re.I):
                        status = "mp"
                    if not time_match and status is None:
                        continue
                    identity = (body[:time_match.start()] if time_match else
                                re.split(r"\s+[–—-]\s+|\s+nicht angetreten", body,
                                         maxsplit=1, flags=re.I)[0]).strip().split()
                    if not time_match and identity and re.fullmatch(r"\d+(?:[.,]\d+)?", identity[-1]):
                        identity.pop()  # trailing Cup score on DNS rows
                    if len(identity) < 2:
                        continue
                    name = " ".join(identity[:2])
                    club = " ".join(identity[2:])
                    if not looks_like_person(name) or is_junk_name(name):
                        continue
                    value = time_match.group(1) if time_match else (
                        "N Ang" if status == "dns" else "Fehlst")
                    seconds = parse_time_loose(value)
                    result = {
                        "name": name, "club": club, "timeText": value,
                        "status": status or "ok",
                    }
                    mark = prefix.group("mark")
                    if mark.isdigit():
                        result["rank"] = int(mark)
                    elif mark.casefold() == "gast":
                        result["outOfCompetition"] = True
                    if seconds is not None:
                        result["timeS"] = seconds
                    current["results"].append(result)
    parsed = [category for category in categories if category["results"]]
    for category in parsed:
        category["declaredStarters"] = len(category["results"])
    return parsed


def parse_southeast_cup_matrix_pdf(path):
    """Parse the Süd-Ost-Cup A/B/C/D matrix (one ranked result per cell)."""
    import pdfplumber

    categories = {
        code: {"name": code, "declaredStarters": None, "results": []}
        for code in "ABCD"
    }
    value_re = re.compile(r"^(?:\d{1,3}:\d{2}|dnf|dns|mp|dsq)$", re.I)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                for raw in (page.extract_text() or "").splitlines():
                    tokens = raw.split()
                    if len(tokens) < 4 or not tokens[0].isdigit():
                        continue
                    rank, points = int(tokens[0]), tokens[1]
                    if not re.fullmatch(r"-?\d+", points):
                        continue
                    segments, start = [], 2
                    for index in range(2, len(tokens)):
                        if value_re.fullmatch(tokens[index]):
                            segments.append((tokens[start:index], tokens[index]))
                            start = index + 1
                    for code, (name_tokens, value) in zip("ABCD", segments):
                        name = " ".join(name_tokens).strip()
                        if not name or is_junk_name(name) or not looks_like_person(name):
                            continue
                        seconds = parse_time_loose(value)
                        result = {
                            "name": name, "club": "", "timeText": value,
                            "status": "ok" if seconds is not None else
                                      (parse_status(value) or "unknown"),
                            "rank": rank, "scoreText": points,
                        }
                        if seconds is not None:
                            result["timeS"] = seconds
                        categories[code]["results"].append(result)
    parsed = [category for category in categories.values() if category["results"]]
    for category in parsed:
        category["declaredStarters"] = len(category["results"])
    return parsed


def parse_southeast_plain_pdf(path):
    """Parse the older Südostcup list with one A/B/C/D block after another.

    The first winner's time can wrap onto a separate line, tied rows omit
    their repeated placement, and there is no club column. Feeding this shape
    to the generic flowing parser merged B-D into A and interpreted the next
    category's rank reset as a time-ranking inversion.
    """
    import pdfplumber

    categories = []
    current = None
    pending = None
    category_re = re.compile(
        r"^(?P<name>[A-D])\s*-\s*.*\bRunden\b.*$", re.I
    )
    value_re = re.compile(
        r"(?P<value>\d{1,3}[,:]\d{2}|disq?\.?|dnf|dns|mp)\s*$",
        re.I,
    )

    def append_result(rank, name, raw_value, out_of_competition=False):
        if current is None:
            return
        name = re.sub(r"\bM\s+ax\b", "Max", name)
        name = re.sub(r"\s+\.$", "", name).strip()
        if not looks_like_person(name) or is_junk_name(name):
            return
        value = raw_value.replace(",", ":")
        seconds = parse_time_loose(value)
        result = {
            "name": name,
            "club": "",
            "timeText": value,
            "status": (
                "ok" if seconds is not None
                else parse_status(raw_value) or "unknown"
            ),
        }
        if rank is not None:
            result["rank"] = rank
        elif seconds is not None and current["results"]:
            previous = current["results"][-1]
            if (
                previous.get("rank") is not None
                and previous.get("timeS") == seconds
            ):
                result["rank"] = previous["rank"]
        if seconds is not None:
            result["timeS"] = seconds
        if out_of_competition:
            result["outOfCompetition"] = True
        current["results"].append(result)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                for raw_line in (page.extract_text() or "").splitlines():
                    line = re.sub(r"\s+", " ", raw_line).strip()
                    category_match = category_re.fullmatch(line)
                    if category_match:
                        current = {
                            "name": category_match.group("name").upper(),
                            "declaredStarters": None,
                            "results": [],
                        }
                        categories.append(current)
                        pending = None
                        continue
                    if current is None or not line:
                        continue
                    if pending and re.fullmatch(r"\d{1,3}[,:]\d{2}", line):
                        append_result(*pending, line)
                        pending = None
                        continue
                    match = value_re.search(line)
                    prefix = line[:match.start()].strip() if match else line
                    rank_match = re.match(r"^(?P<rank>\d+)\.\s*(?P<name>.+)$",
                                          prefix)
                    ooc_match = re.match(r"^ak\s+(?P<name>.+)$", prefix, re.I)
                    if match and (rank_match or ooc_match):
                        append_result(
                            int(rank_match.group("rank")) if rank_match else None,
                            (rank_match or ooc_match).group("name"),
                            match.group("value"),
                            out_of_competition=bool(ooc_match),
                        )
                    elif match:
                        append_result(None, prefix, match.group("value"))
                    elif rank_match:
                        pending = (
                            int(rank_match.group("rank")),
                            rank_match.group("name"),
                        )

    parsed = [category for category in categories if category["results"]]
    for category in parsed:
        category["declaredStarters"] = len(category["results"])
    return parsed


def parse_start_finish_class_pdf(path):
    """Parse Startzeit/Zielzeit/Laufzeit lists with the class in every row."""
    import pdfplumber

    categories = {}
    row_re = re.compile(
        r"^(?:(?P<rank>\d+)\.\s+)?(?:(?P<bib>\d+(?:/\d+)?)\s+)?"
        r"(?P<name>.+?)\s+(?P<class>[A-E])\s+(?P<values>.+)$")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                for raw in (page.extract_text() or "").splitlines():
                    line = re.sub(r"\s+", " ", raw).strip()
                    match = row_re.match(line)
                    if not match:
                        continue
                    values = match.group("values").split()
                    clocks = [value for value in values
                              if re.fullmatch(r"\d{2}:\d{2}:\d{2}", value)]
                    explicit_status = next((parse_status(value) for value in values
                                            if parse_status(value) not in (None, "ok")), None)
                    if not clocks and explicit_status is None:
                        continue
                    time_text = values[-1] if explicit_status else (
                        clocks[-1] if clocks else values[-1])
                    seconds = None if explicit_status else parse_time_loose(time_text)
                    raw_name = match.group("name").strip()
                    pair_names = None
                    if re.search(r"\s+u\s+", raw_name, re.I):
                        parts = re.split(r"\s+u\s+", raw_name, maxsplit=1, flags=re.I)
                        if all(len(part.split()) >= 2 for part in parts):
                            pair_names = parts
                        elif len(parts) == 2 and all(len(part.split()) == 1 for part in parts):
                            pair_names = parts
                        elif len(parts) == 2 and len(parts[0].split()) == 1:
                            surname = parts[1].split()[-1]
                            pair_names = [f"{parts[0]} {surname}", parts[1]]
                    names = pair_names or [raw_name]
                    if not all(looks_like_person(name) and not is_junk_name(name)
                               for name in names):
                        continue
                    category = categories.setdefault(match.group("class"), {
                        "name": match.group("class"), "declaredStarters": None,
                        "sourceUnitCount": 0, "results": [],
                    })
                    category["sourceUnitCount"] += 1
                    for name in names:
                        result = {
                            "name": name, "club": "", "timeText": time_text,
                            "status": explicit_status or ("ok" if seconds is not None else "unknown"),
                        }
                        if match.group("rank"):
                            result["rank"] = int(match.group("rank"))
                        if seconds is not None:
                            result["timeS"] = seconds
                        if pair_names:
                            result.update({
                                "resultKind": "pair",
                                "note": "Partner: " + next(
                                    other for other in names if other != name),
                            })
                        category["results"].append(result)
    parsed = [category for category in categories.values() if category["results"]]
    ranked_names = {
        result["name"].casefold()
        for category in parsed
        for result in category["results"]
        if result.get("rank") is not None
    }
    for category in parsed:
        for result in category["results"]:
            if (
                result.get("rank") is None
                and result.get("timeS") is not None
                and result["name"].casefold() in ranked_names
            ):
                # A runner who already has a classified result in another
                # class can take a second course, printed with time but no
                # placement. The blank rank is the source's OOC marker.
                result["outOfCompetition"] = True
    for category in parsed:
        category["declaredStarters"] = category["sourceUnitCount"]
    return parsed


def parse_apprentice_sport_pdf(path):
    """Parse the Lehrlingssporttag's one-list time/participation result."""
    import pdfplumber

    category = {"name": "Laufzeit", "declaredStarters": None, "results": []}
    row_re = re.compile(
        r"^(?P<rank>\d+)\s+(?P<name>.+?)\s+(?P<gender>[mw])\s+"
        r"(?P<club>.+?)\s+(?P<value>\d{1,3}[,.]\d{1,2}|teilgenommen)\s*$",
        re.I)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                for raw in (page.extract_text() or "").splitlines():
                    line = re.sub(r"\s+", " ", raw).strip()
                    match = row_re.match(line)
                    if not match and line.casefold().endswith(" teilgenommen"):
                        match = re.match(
                            r"^(?P<rank>\d+)\s+(?P<name>\S+\s+\S+)\s+"
                            r"(?P<club>.+?)\s+(?P<value>teilgenommen)$", line, re.I)
                    if not match:
                        continue
                    name = match.group("name")
                    if not looks_like_person(name) or is_junk_name(name):
                        continue
                    raw_value = match.group("value")
                    if raw_value.casefold() == "teilgenommen":
                        value, seconds, status = raw_value, None, "ok"
                    else:
                        value = re.sub(r"[,.]", ":", raw_value, count=1)
                        seconds, status = parse_time_loose(value), "ok"
                    result = {
                        "name": name, "club": match.group("club"),
                        "timeText": value, "status": status,
                        "rank": int(match.group("rank")),
                    }
                    if seconds is not None:
                        result["timeS"] = seconds
                    category["results"].append(result)
    category["declaredStarters"] = len(category["results"])
    return [category] if category["results"] else []


def parse_wings_for_life_pdf(path):
    """Parse the simple three-line ``Name / rank controls time / club`` list."""
    import pdfplumber

    lines = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            lines.extend(re.sub(r"\s+", " ", line).strip()
                         for line in (page.extract_text() or "").splitlines())
    categories = []
    current = None
    pending_name = None
    value_re = re.compile(r"^(?P<rank>\d+)\s+(?P<score>\d+)\s+"
                          r"(?P<time>\d{1,2}:\d{2}:\d{2})$")
    for index, line in enumerate(lines):
        if not line:
            continue
        if line.startswith("Platzierung Name / Verein"):
            title = next((lines[i] for i in range(index - 1, -1, -1)
                          if lines[i]), "Ergebnis")
            current = {"name": title, "declaredStarters": None,
                       "rankingBasis": "score", "results": []}
            categories.append(current)
            pending_name = None
            continue
        match = value_re.fullmatch(line)
        if current is None:
            continue
        if match and pending_name:
            # The following non-empty line is the club; keep the source name
            # until that line arrives so identity and club never get swapped.
            pending_name = (pending_name, match)
            continue
        if isinstance(pending_name, tuple):
            name, match = pending_name
            seconds = parse_time_loose(match.group("time"))
            current["results"].append({
                "name": name, "club": line,
                "rank": int(match.group("rank")),
                "scoreText": match.group("score"),
                "timeText": match.group("time"), "timeS": seconds,
                "status": "ok", "rankingBasis": "score",
            })
            pending_name = None
            continue
        pending_name = line
    for category in categories:
        category["declaredStarters"] = len(category["results"])
    return [category for category in categories if category["results"]]


def parse_funol_two_column_pdf(path):
    """Parse FUN-OL result sheets with independent left/right categories."""
    import pdfplumber

    categories = {}
    current = {"left": None, "right": None}
    row_re = re.compile(
        r"^(?:(?P<rank>\d+)\.\s+)?(?P<name>.+?)\s+"
        r"(?P<value>\d{1,2}:\d{2}:\d{2}|fehlst\.?|aufg\.?|disqu?\.?)$", re.I)

    def add(side, text):
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            return
        heading = re.fullmatch(r"(.+?):", text)
        if heading:
            name = heading.group(1).strip()
            current[side] = categories.setdefault(
                name, {"name": name, "declaredStarters": None, "results": []})
            return
        match = row_re.fullmatch(text)
        category = current[side]
        if not match or category is None:
            return
        value = match.group("value").rstrip(".")
        seconds = parse_time_loose(value)
        status = "ok" if seconds is not None else (parse_status(value) or "unknown")
        name = match.group("name").strip()
        result = {"name": name, "club": "", "timeText": value,
                  "status": status}
        if match.group("rank"):
            result["rank"] = int(match.group("rank"))
        if seconds is not None:
            result["timeS"] = seconds
        if name.casefold().startswith("team "):
            result.update({"resultKind": "team", "teamName": name,
                           "teamNumber": match.group("rank") or name})
        category["results"].append(result)

    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            for words in group_lines(page.extract_words()):
                left = " ".join(w["text"] for w in words if w["x0"] < 315)
                right = " ".join(w["text"] for w in words if w["x0"] >= 315)
                add("left", left)
                add("right", right)
    parsed = list(categories.values())
    for category in parsed:
        category["declaredStarters"] = len(category["results"])
    return [category for category in parsed if category["results"]]


def parse_tirol_school_individual_pdf(path):
    """Parse the minimal ``rank surname given time/status`` Tirol school list."""
    import pdfplumber

    categories = []
    current = None
    category_re = re.compile(
        r"^(?:\d+\.?/\d+\.|Unterstufe|Oberstufe)\s+"
        r"(?:männlich|weiblich)$", re.I)
    row_re = re.compile(
        r"^(?:(?P<rank>\d+)\s+)?(?P<name>.+?)\s+"
        r"(?P<value>\d{1,2}:\d{2}:\d{2}|fehlst|n\.a\.)$", re.I)
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            for raw in (page.extract_text() or "").splitlines():
                line = re.sub(r"\s+", " ", raw).strip()
                if category_re.fullmatch(line):
                    current = {"name": line, "declaredStarters": None, "results": []}
                    categories.append(current)
                    continue
                match = row_re.fullmatch(line)
                if current is None or not match:
                    continue
                value = match.group("value")
                seconds = parse_time_loose(value)
                result = {"name": match.group("name"), "club": "",
                          "timeText": value,
                          "status": "ok" if seconds is not None
                                    else (parse_status(value) or "unknown")}
                if match.group("rank"):
                    result["rank"] = int(match.group("rank"))
                if seconds is not None:
                    result["timeS"] = seconds
                current["results"].append(result)
    for category in categories:
        category["declaredStarters"] = len(category["results"])
    return [category for category in categories if category["results"]]


def _excel_elapsed_time(value):
    """Normalize Excel's visible ``MM:SS:00`` duration bug, preserving raw text."""
    value = (value or "").strip()
    parts = value.split(":")
    if len(parts) == 3 and parts[2] == "00" and int(parts[0]) >= 2:
        return int(parts[0]) * 60 + int(parts[1])
    return parse_time_loose(value)


def parse_nolv_excel_result_pdf(path):
    """Parse the wide NOLV ``Start Ziel Zeit Wertung Ort Kurz Platz`` export."""
    import pdfplumber

    categories = {}
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            for words in group_lines(page.extract_words()):
                if not words or words[0]["x0"] > 80:
                    continue

                def cell(start, end=None):
                    return " ".join(w["text"] for w in words
                                    if w["x0"] >= start
                                    and (end is None or w["x0"] < end)).strip()

                surname, given = cell(45, 160), cell(160, 260)
                elapsed, club = cell(365, 420), cell(465, 615)
                code, place = cell(615, 650), cell(650)
                if (not surname or surname.casefold() == "nachname" or not given
                        or not code or not re.fullmatch(r"[A-Z]{2,3}", code)):
                    continue
                status = parse_status(place)
                seconds = _excel_elapsed_time(elapsed)
                if seconds is None and status is None:
                    continue
                result = {"name": f"{surname} {given}", "club": club,
                          "timeText": elapsed,
                          "status": status or "ok"}
                if place.isdigit():
                    result["rank"] = int(place)
                elif is_ooc_status(place):
                    result["outOfCompetition"] = True
                if seconds is not None:
                    result["timeS"] = seconds
                category = categories.setdefault(
                    code, {"name": code, "sourceCategory": code,
                           "declaredStarters": None, "results": []})
                surname_parts = [part.strip() for part in surname.split("&")]
                given_parts = [part.strip() for part in given.split("&")]
                if (len(surname_parts) == len(given_parts) == 2
                        and all(surname_parts) and all(given_parts)
                        and all(part != "?" for part in given_parts)):
                    names = [f"{surname_parts[i]} {given_parts[i]}" for i in range(2)]
                    expanded = []
                    for name in names:
                        member = dict(result)
                        member.update({
                            "name": name, "resultKind": "pair",
                            "teamNumber": f"{code}-{place or elapsed}",
                            "note": "Partner: " + next(n for n in names if n != name),
                        })
                        expanded.append(member)
                else:
                    expanded = expand_pair_result(result, category=code)
                category["results"].extend(expanded)
    parsed = list(categories.values())
    for category in parsed:
        category["declaredStarters"] = category_competitor_unit_count(category)
        category["sourceUnitCount"] = category["declaredStarters"]
    return [category for category in parsed if category["results"]]


def parse_family_adventure_pdf(path):
    """Parse the child sprint table whose course is encoded by a marker column."""
    import pdfplumber

    categories = {}
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            for words in group_lines(page.extract_words()):
                if not words or not words[0]["text"].isdigit():
                    continue
                source_bib = words[0]["text"]

                def cell(start, end=None):
                    return " ".join(w["text"] for w in words if w["x0"] >= start
                                    and (end is None or w["x0"] < end)).strip()

                surname, given = cell(30, 135), cell(135, 210)
                gender, age, raw_time = cell(210, 245), cell(245, 285), cell(455)
                marker = next((w for w in words
                               if 285 <= w["x0"] < 455 and w["text"] == "1"), None)
                if not surname or not given or gender.casefold() not in {"m", "w"} or not marker:
                    continue
                course = ("Zwerge" if marker["x0"] < 330 else
                          "Elche" if marker["x0"] < 380 else "Bushmen/-girls")
                category_name = f"{course} {'männlich' if gender.casefold() == 'm' else 'weiblich'}"
                time_parts = raw_time.split(".")
                if (
                    not 1 <= len(time_parts) <= 3
                    or not all(part.isdigit() for part in time_parts)
                ):
                    continue
                if len(time_parts) == 3:
                    minutes, seconds_part = map(int, time_parts[:2])
                elif len(time_parts) == 2 and len(time_parts[1]) == 1:
                    # ``59.8`` is 59.8 seconds, not 59 minutes 8 seconds.
                    minutes, seconds_part = 0, int(time_parts[0])
                elif len(time_parts) == 2:
                    minutes, seconds_part = map(int, time_parts)
                else:
                    minutes, seconds_part = 0, int(time_parts[0])
                seconds = minutes * 60 + seconds_part
                result = {"name": f"{surname} {given}", "club": "",
                          "sourceBib": source_bib, "timeText": raw_time,
                          "timeS": seconds, "status": "ok",
                          "rankingBasis": "other"}
                if age.isdigit():
                    result["note"] = f"Alter: {age}"
                category = categories.setdefault(
                    category_name, {"name": category_name,
                                    "declaredStarters": None,
                                    "rankingBasis": "other", "results": []})
                category["results"].append(result)
    parsed = list(categories.values())
    for category in parsed:
        category["declaredStarters"] = len(category["results"])
    return [category for category in parsed if category["results"]]


def parse_knockout_final_pdf(path):
    """Parse only the source's authoritative overall Final placement grid."""
    import pdfplumber

    results = []
    cell_re = re.compile(r"^(?P<rank>\d+)\.\s+(?P<name>.+)$")
    ranges = ((220, 420), (420, 595), (595, 775), (775, 980))
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            in_final = False
            for words in group_lines(page.extract_words()):
                text = " ".join(w["text"] for w in words).strip()
                if text == "Final":
                    in_final = True
                    continue
                if text == "Semifinal":
                    in_final = False
                    break
                if not in_final:
                    continue
                for start, end in ranges:
                    cell = " ".join(w["text"] for w in words
                                    if start <= w["x0"] < end).strip()
                    match = cell_re.fullmatch(cell)
                    if match:
                        results.append({"name": match.group("name"), "club": "",
                                        "rank": int(match.group("rank")),
                                        "timeText": "", "status": "ok",
                                        "rankingBasis": "other"})
    return [{"name": "Final", "declaredStarters": len(results),
             "rankingBasis": "other", "results": results}] if results else []


TIROL_SCHOOL_TEAM_CATEGORY_RE = re.compile(
    r"^(?:5\./6\.|5\.-8\.|9\./12\.|9\.-12\.)\s+(?:weiblich|männlich)$",
    re.I,
)
TIROL_SCHOOL_TEAM_VALUE_RE = (
    r"(?:\d{2}:\d{2}:\d{2}|Fehlstempel|Disqu|Aufg\.?|N\.\s*An\.?)"
)


def parse_tirol_school_2016_individual_pdf(path):
    """Parse the OE2010 individual list whose school-grade classes are not
    recognised by the generic D/H category grammar."""
    import pdfplumber

    categories = []
    current = None
    category_re = re.compile(
        r"^(?P<name>(?:5\./6\.|5\.-8\.|9\./12\.|9\.-12\.)\s+"
        r"(?:weiblich|männlich))\s+\((?P<count>\d+)\)"
        r"(?P<course>.*?)(?:\s+\(Forts\.\))?$",
        re.I,
    )

    def cell(words, start, end=None):
        return " ".join(
            word["text"] for word in words
            if word["x0"] >= start
            and (end is None or word["x0"] < end)
        ).strip()

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                for words in group_lines(page.extract_words()):
                    text = " ".join(word["text"] for word in words).strip()
                    category_match = category_re.fullmatch(text)
                    if category_match:
                        name = category_match.group("name")
                        if current is not None and current["name"] == name:
                            continue
                        current = {
                            "name": name,
                            "declaredStarters": int(category_match.group("count")),
                            "rankingBasis": "time", "results": [],
                        }
                        current.update(parse_course_info(category_match.group("course")))
                        categories.append(current)
                        continue
                    if current is None:
                        continue
                    bib_text = cell(words, 55, 78)
                    name = cell(words, 78, 250)
                    club = cell(words, 250, 410)
                    raw_value = cell(words, 410)
                    seconds = parse_time_loose(raw_value)
                    status = "ok" if seconds is not None else parse_status(raw_value)
                    if (not bib_text.isdigit() or not name or not club
                            or status is None):
                        continue
                    result = {
                        "name": name, "club": club, "timeText": raw_value,
                        "status": status,
                    }
                    rank_text = cell(words, 20, 55).rstrip(".")
                    if rank_text.isdigit():
                        result["rank"] = int(rank_text)
                    if seconds is not None:
                        result["timeS"] = seconds
                    current["results"].append(result)
    for category in categories:
        category["sourceUnitCount"] = len(category["results"])
    return [category for category in categories if category["results"]]


def parse_tirol_school_2020_individual_pdf(path):
    """Parse the 2019/20 Tirol school individual ranking.

    The four source classes use ``5./6.`` and ``5./8.`` labels and therefore
    fall outside the ordinary D/H category grammar.  Column boundaries are
    stable across the three-page OE2010 report; using them also prevents the
    school-team suffix (for example ``Langkampfen 1``) from leaking into a
    long time such as ``1:08:10``.
    """
    import pdfplumber

    categories = []
    current = None
    category_re = re.compile(
        r"^(?P<name>5\./(?:6|8)\.\s+(?:weiblich|männlich))\s+"
        r"\((?P<count>\d+)\)(?P<course>.*?)(?:\s+\(Forts\.\))?$",
        re.I,
    )

    def cell(words, start, end=None):
        return " ".join(
            word["text"] for word in words
            if word["x0"] >= start
            and (end is None or word["x0"] < end)
        ).strip()

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                for words in group_lines(page.extract_words()):
                    text = " ".join(word["text"] for word in words).strip()
                    category_match = category_re.fullmatch(text)
                    if category_match:
                        name = category_match.group("name")
                        if current is not None and current["name"] == name:
                            continue
                        current = {
                            "name": name,
                            "declaredStarters": int(category_match.group("count")),
                            "rankingBasis": "time", "results": [],
                        }
                        current.update(parse_course_info(category_match.group("course")))
                        categories.append(current)
                        continue
                    if current is None:
                        continue
                    bib_text = cell(words, 60, 93)
                    name = cell(words, 93, 242)
                    club = cell(words, 261, 409)
                    raw_value = cell(words, 409, 455)
                    seconds = parse_time_loose(raw_value)
                    status = "ok" if seconds is not None else parse_status(raw_value)
                    if (not bib_text.isdigit() or not name or not club
                            or status is None):
                        continue
                    result = {
                        "name": name, "club": club, "timeText": raw_value,
                        "status": status, "sourceBib": bib_text,
                    }
                    rank_text = cell(words, 20, 60).rstrip(".")
                    if rank_text.isdigit():
                        result["rank"] = int(rank_text)
                    if seconds is not None:
                        result["timeS"] = seconds
                    current["results"].append(result)
    for category in categories:
        category["sourceUnitCount"] = len(category["results"])
    return [category for category in categories if category["results"]]


def parse_tirol_school_team_lines(lines):
    """Parse the 2016 Tirol school team ranking from extracted PDF lines."""
    categories = []
    current = None
    pending = None

    team_re = re.compile(
        r"^(?P<rank>\d+|x)\s+(?P<club>.+?)\s+"
        r"(?P<value>\d{2}:\d{2}:\d{2}|ohne\s+Wertung)$", re.I)
    member_re = re.compile(
        rf"^(?:\d+\s+)?(?P<name>.*?)\s*(?P<value>{TIROL_SCHOOL_TEAM_VALUE_RE})$",
        re.I)

    def flush():
        nonlocal pending
        if current is None or pending is None:
            pending = None
            return
        current["sourceUnitCount"] += 1
        team_number = f"{current['name']}-{current['sourceUnitCount']}"
        members = pending["members"]
        for index, member in enumerate(members, 1):
            result = {
                "name": member["name"], "club": pending["club"],
                "status": "ok", "individualStatus": member["status"],
                "timeText": member["timeText"],
                "resultKind": "team", "teamNumber": team_number,
                "teamName": pending["club"],
                "teamTimeText": pending["teamTimeText"],
                "note": (
                    f"Mannschaft {pending['club']} · "
                    f"Mitglied {index}/{len(members)}"
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
        pending = None

    def start_team(match):
        nonlocal pending
        raw_value = match.group("value")
        team_seconds = parse_time_loose(raw_value)
        rank_text = match.group("rank")
        pending = {
            "rank": int(rank_text) if rank_text.isdigit() else None,
            "club": match.group("club").strip(),
            "teamTimeText": raw_value,
            "teamTimeS": team_seconds,
            "outOfCompetition": rank_text.casefold() == "x",
            "members": [], "slots": 0,
        }

    for raw_line in lines:
        line = re.sub(r"\s+", " ", raw_line or "").strip()
        if not line:
            continue
        if TIROL_SCHOOL_TEAM_CATEGORY_RE.fullmatch(line):
            flush()
            current = {
                "name": line, "declaredStarters": None,
                "sourceUnitCount": 0, "rankingBasis": "time", "results": [],
            }
            categories.append(current)
            continue
        if current is None:
            continue

        if pending is None:
            team_match = team_re.fullmatch(line)
            if team_match:
                start_team(team_match)
            continue

        member_match = member_re.fullmatch(line)
        if member_match and pending["slots"] < 4:
            pending["slots"] += 1
            name = member_match.group("name").strip()
            raw_value = member_match.group("value")
            seconds = parse_time_loose(raw_value)
            status = "ok" if seconds is not None else (parse_status(raw_value) or "unknown")
            if name and name.casefold() != "vakant":
                pending["members"].append({
                    "name": name, "timeText": raw_value,
                    "timeS": seconds, "status": status,
                })
            if pending["slots"] == 4:
                flush()
            continue

        # A new team after a shorter exceptional team (the source contains
        # one one-person OOC team) must not be consumed as a member row.
        team_match = team_re.fullmatch(line)
        if team_match:
            flush()
            start_team(team_match)
    flush()

    for category in categories:
        category["declaredStarters"] = category["sourceUnitCount"]
    return [category for category in categories if category["results"]]


def parse_tirol_school_team_pdf(path):
    import pdfplumber

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pdfplumber.open(path) as pdf:
            lines = [
                line
                for page in pdf.pages
                for line in (page.extract_text() or "").splitlines()
            ]
    return parse_tirol_school_team_lines(lines)


def _school_team_result_rows(current, pending):
    """Materialize one best-three school team without inventing relay legs."""
    if current is None or pending is None or not pending["members"]:
        return
    current["sourceUnitCount"] += 1
    team_number = f"{current['name']}-{current['sourceUnitCount']}"
    team_status = pending["teamStatus"]
    for index, member in enumerate(pending["members"], 1):
        result = {
            "name": member["name"], "club": pending["club"],
            "status": team_status, "teamStatus": team_status,
            "individualStatus": member["status"],
            "timeText": member["timeText"],
            "resultKind": "team", "teamNumber": team_number,
            "teamName": pending["teamName"],
            "teamTimeText": pending.get("teamTimeText") or "",
            "note": (
                f"Mannschaft {pending['teamName']} · "
                f"Mitglied {index}/{len(pending['members'])}"
            ),
        }
        if pending.get("rank") is not None:
            result["rank"] = pending["rank"]
        if member.get("timeS") is not None:
            result["timeS"] = member["timeS"]
        if pending.get("teamTimeS") is not None:
            result["teamTimeS"] = pending["teamTimeS"]
        current["results"].append(result)


def _school_club_from_team_name(team_name):
    return re.sub(r"\s+\d+$", "", (team_name or "").strip())


def parse_tirol_school_team_2020_pdf(path):
    """Parse the 2019/20 Tirol school best-three Mannschaftswertung."""
    import pdfplumber

    categories = []
    current = None
    pending = None
    category_re = re.compile(r"^5\./(?:6|8)\.\s+[wm]$", re.I)

    def flush():
        nonlocal pending
        _school_team_result_rows(current, pending)
        pending = None

    def cell(words, start, end=None):
        return " ".join(
            word["text"] for word in words
            if word["x0"] >= start
            and (end is None or word["x0"] < end)
        ).strip()

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                for words in group_lines(page.extract_words()):
                    text = " ".join(word["text"] for word in words).strip()
                    if category_re.fullmatch(text):
                        flush()
                        current = {
                            "name": text, "declaredStarters": None,
                            "sourceUnitCount": 0,
                            "rankingBasis": "time", "results": [],
                        }
                        categories.append(current)
                        continue
                    if current is None:
                        continue
                    rank_text = cell(words, 45, 110).rstrip(".")
                    team_cell = cell(words, 110, 235)
                    name = cell(words, 235, 410)
                    raw_time = cell(words, 410, 475)
                    raw_total = cell(words, 475)
                    seconds = parse_time_loose(raw_time)
                    status = "ok" if seconds is not None else parse_status(raw_time)
                    if not name or status is None:
                        continue

                    if rank_text:
                        flush()
                        rank_match = re.fullmatch(r"(\d+)\.", rank_text + ".")
                        declared_status = parse_status(rank_text)
                        pending = {
                            "rank": int(rank_text) if rank_text.isdigit() else None,
                            "teamName": team_cell,
                            "club": _school_club_from_team_name(team_cell),
                            "teamStatus": (
                                declared_status if declared_status not in (None, "ok")
                                else "ok"
                            ),
                            "members": [],
                        }
                    if pending is None:
                        continue
                    if team_cell and team_cell != pending["teamName"]:
                        # Fancy team names are followed by the actual school
                        # in the same column on member two.
                        pending["club"] = _school_club_from_team_name(team_cell)
                    pending["members"].append({
                        "name": name, "timeText": raw_time,
                        "timeS": seconds, "status": status,
                    })
                    total_seconds = parse_time_loose(raw_total)
                    total_status = parse_status(raw_total)
                    if total_seconds is not None:
                        pending.update({
                            "teamTimeText": raw_total,
                            "teamTimeS": total_seconds,
                            "teamStatus": "ok",
                        })
                    elif total_status not in (None, "ok"):
                        pending["teamStatus"] = total_status
                    if len(pending["members"]) == 4:
                        flush()
    flush()
    for category in categories:
        category["declaredStarters"] = category["sourceUnitCount"]
    return [category for category in categories if category["results"]]


def parse_tirol_school_team_2023_pdf(path):
    """Parse the 2023 Tirol school best-three Mannschaftswertung."""
    import pdfplumber

    categories = []
    current = None
    pending = None
    category_re = re.compile(
        r"^(?:5\./6\.\s+(?:männlich|weiblich)|"
        r"Untestufe männlich|Unterstufe weiblich|"
        r"Oberstufe männlich/weiblich gemischt)$",
        re.I,
    )

    def flush():
        nonlocal pending
        _school_team_result_rows(current, pending)
        pending = None

    def cell(words, start, end=None):
        return " ".join(
            word["text"] for word in words
            if word["x0"] >= start
            and (end is None or word["x0"] < end)
        ).strip()

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                for words in group_lines(page.extract_words()):
                    text = " ".join(word["text"] for word in words).strip()
                    if category_re.fullmatch(text):
                        flush()
                        current = {
                            "name": text.replace("Untestufe", "Unterstufe"),
                            "declaredStarters": None,
                            "sourceUnitCount": 0,
                            "rankingBasis": "time", "results": [],
                        }
                        categories.append(current)
                        continue
                    if current is None:
                        continue
                    team_cell = cell(words, 45, 160)
                    name = cell(words, 160, 335)
                    # Individual values start near x=339.  The team total or
                    # a collective ``disqu.`` starts near x=404; using 420 as
                    # the boundary attached the member time to the club and
                    # misread the collective status as that member's time.
                    raw_time = cell(words, 335, 400)
                    raw_total = cell(words, 400, 480)
                    rank_or_status = cell(words, 480).rstrip(".")
                    seconds = parse_time_loose(raw_time)
                    status = "ok" if seconds is not None else parse_status(raw_time)
                    if not name or status is None:
                        continue
                    if team_cell:
                        flush()
                        pending = {
                            "rank": None, "teamName": team_cell,
                            "club": _school_club_from_team_name(team_cell),
                            "teamStatus": "unknown",
                            "members": [],
                        }
                    if pending is None:
                        continue
                    pending["members"].append({
                        "name": name, "timeText": raw_time,
                        "timeS": seconds, "status": status,
                    })
                    total_seconds = parse_time_loose(raw_total)
                    total_status = parse_status(raw_total)
                    if total_seconds is not None:
                        pending.update({
                            "teamTimeText": raw_total,
                            "teamTimeS": total_seconds,
                            "teamStatus": "ok",
                        })
                    elif total_status not in (None, "ok"):
                        pending.update({
                            "teamTimeText": raw_total,
                            "teamStatus": total_status,
                        })
                    if rank_or_status.isdigit():
                        pending["rank"] = int(rank_or_status)
                    else:
                        rank_status = parse_status(rank_or_status)
                        if rank_status not in (None, "ok"):
                            pending["teamStatus"] = rank_status
                    if (pending.get("rank") is not None
                            and pending.get("teamTimeS") is not None):
                        # Some pages print rank on member three and the team
                        # sum on member four; do not flush until the total.
                        if len(pending["members"]) >= 3 and raw_total:
                            flush()
                    elif len(pending["members"]) == 4 and pending["teamStatus"] != "unknown":
                        flush()
    flush()
    for category in categories:
        category["declaredStarters"] = category["sourceUnitCount"]
    return [category for category in categories if category["results"]]


def parse_surprise_three_person_team_pdf(path):
    """Parse the 2013 Surprise three-person aggregate team ranking."""
    import pdfplumber

    category = {
        "name": "Teamwertung", "declaredStarters": None,
        "sourceUnitCount": 0, "rankingBasis": "time", "results": [],
    }
    members = []
    rank = None

    def cell(words, start, end=None):
        return " ".join(
            word["text"] for word in words
            if word["x0"] >= start
            and (end is None or word["x0"] < end)
        ).strip()

    def flush(team_time_text):
        nonlocal members, rank
        team_seconds = parse_time_loose(team_time_text)
        if rank is None or team_seconds is None or len(members) != 3:
            members = []
            rank = None
            return
        category["sourceUnitCount"] += 1
        team_number = str(rank)
        team_name = f"Team {rank}"
        for index, member in enumerate(members, 1):
            result = {
                "name": member["name"], "club": member["club"],
                "rank": rank, "status": "ok",
                "individualStatus": "ok",
                "timeText": member["timeText"], "timeS": member["timeS"],
                "resultKind": "team", "teamNumber": team_number,
                "teamName": team_name,
                "teamTimeText": team_time_text, "teamTimeS": team_seconds,
                "note": f"Mannschaft {team_name} · Mitglied {index}/3",
            }
            category["results"].append(result)
        members = []
        rank = None

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pdfplumber.open(path) as pdf:
            for words in group_lines(pdf.pages[0].extract_words()):
                name = cell(words, 60, 165)
                club = cell(words, 165, 320)
                individual_total = cell(words, 465)
                individual_seconds = parse_time_loose(individual_total)
                if name and club and individual_seconds is not None:
                    rank_text = cell(words, 40, 60).rstrip(".")
                    if rank_text.isdigit():
                        rank = int(rank_text)
                    members.append({
                        "name": name, "club": club,
                        "timeText": individual_total,
                        "timeS": individual_seconds,
                    })
                    continue
                team_total = cell(words, 450)
                if (len(members) == 3
                        and parse_time_loose(team_total) is not None):
                    flush(team_total)
    category["declaredStarters"] = category["sourceUnitCount"]
    return [category] if category["results"] else []


def parse_uwg_multistage_pdf(path):
    """Extract the missing E2/E3 individual races from UWG 2018's total PDF."""
    import pdfplumber

    specs = [
        {
            "stageNumber": 3, "stageDate": "2018-06-23",
            "stageTitle": "Mixed Staffel / E2-Einzelwertung",
            # Two-digit E2 ranks begin at x≈436.  Keeping the time cell open
            # until x=440 therefore joined values such as ``1:18:29 10`` and
            # made every hour-long E2 result unreadable.  The blank between
            # the time (ending at x≈425) and rank is the stable boundary.
            "sourceColumn": "E2", "valueRange": (390, 430),
            "rankRange": (430, 450),
        },
        {
            "stageNumber": 4, "stageDate": "2018-06-24",
            "stageTitle": "Einzelwettkampf und Siegerehrung / E3",
            # Hour-long values are wider and begin near x=457, while shorter
            # E3 values begin near x=465.
            "sourceColumn": "E3", "valueRange": (450, 500),
            "rankRange": (500, 525),
        },
    ]
    stage_categories = [[] for _ in specs]
    current = [None for _ in specs]
    category_re = re.compile(
        r"^(?P<name>.+?)\s+\((?P<count>\d+)\)"
        r"(?:\s+Annulliert\s+E2)?(?:\s+\(Forts\.\))?$", re.I)

    def cell(words, start, end=None):
        return " ".join(
            word["text"] for word in words
            if word["x0"] >= start
            and (end is None or word["x0"] < end)
        ).strip()

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                for words in group_lines(page.extract_words()):
                    text = " ".join(word["text"] for word in words).strip()
                    category_match = category_re.fullmatch(text)
                    if category_match and words[0]["x0"] < 5:
                        name = category_match.group("name").strip()
                        annulled_e2 = bool(re.search(
                            r"Annulliert\s+E2", text, re.I))
                        for index in range(len(specs)):
                            if (annulled_e2
                                    and specs[index].get("sourceColumn") == "E2"):
                                current[index] = None
                                continue
                            if (current[index] is not None
                                    and current[index]["name"] == name):
                                continue
                            category = {
                                "name": name, "sourceCategory": text,
                                "declaredStarters": int(category_match.group("count")),
                                "sourceUnitCount": 0,
                                "rankingBasis": "time",
                                "_hasObservedStageValue": False,
                                "results": [],
                            }
                            stage_categories[index].append(category)
                            current[index] = category
                        continue
                    name = cell(words, 56, 193)
                    club = cell(words, 209, 330)
                    club = re.sub(
                        r"\s+\d{1,3}:\d{2}(?::\d{2})?$", "", club).strip()
                    if not name or not club or is_junk_name(name):
                        continue
                    for index, spec in enumerate(specs):
                        if current[index] is None:
                            continue
                        raw_value = cell(words, *spec["valueRange"])
                        seconds = parse_time_loose(raw_value)
                        status = (
                            "ok" if seconds is not None
                            else parse_status(raw_value)
                        )
                        inferred_dns = status is None and not raw_value
                        if inferred_dns:
                            status = "dns"
                            raw_value = "DNS"
                        elif status is None:
                            continue
                        if not inferred_dns:
                            current[index]["_hasObservedStageValue"] = True
                        rank_text = cell(words, *spec["rankRange"]).rstrip(".")
                        result = {
                            "name": name, "club": club,
                            "timeText": raw_value, "status": status,
                        }
                        if inferred_dns:
                            result["note"] = (
                                f"{spec['sourceColumn']}-Wert leer (als DNS)"
                            )
                        if seconds is not None:
                            result["timeS"] = seconds
                        if rank_text.isdigit() and status == "ok":
                            result["rank"] = int(rank_text)
                        elif is_ooc_status(rank_text):
                            result["outOfCompetition"] = True
                        current[index]["results"].append(result)

    stage_documents = []
    for spec, categories in zip(specs, stage_categories):
        categories = [category for category in categories
                      if (category["results"]
                          and category.pop("_hasObservedStageValue", False))]
        for category in categories:
            category["sourceUnitCount"] = len(category["results"])
        if categories:
            stage_documents.append({
                "stageNumber": spec["stageNumber"],
                "stageDate": spec["stageDate"],
                "stageTitle": spec["stageTitle"],
                "listType": "race", "categories": categories,
            })
    return stage_documents


def parse_mtbo_two_stage_overall_pdf(path):
    """Extract both physical stages from the Waldviertel MTBO total table."""
    import pdfplumber

    specs = [
        {
            "stageNumber": 1, "stageDate": "2025-06-14",
            "stageTitle": "Austria Masters MTBO – Stage 1",
            "timeRange": (420, 520), "rankRange": (520, 540),
        },
        {
            "stageNumber": 2, "stageDate": "2025-06-15",
            "stageTitle": "4.AC MTBO ÖSTM/ÖM Langdistanz",
            "timeRange": (540, 640), "rankRange": (640, 665),
        },
    ]
    stage_categories = [[] for _ in specs]
    current = [None for _ in specs]

    def cell(words, start, end=None):
        return " ".join(
            word["text"] for word in words
            if word["x0"] >= start
            and (end is None or word["x0"] < end)
        ).strip()

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                for words in group_lines(page.extract_words()):
                    if any(word["text"] == "Name" and 155 <= word["x0"] < 180
                           for word in words):
                        category_name = cell(words, 45, 165)
                        if not category_name:
                            continue
                        for index in range(len(specs)):
                            category = {
                                "name": category_name,
                                "sourceCategory": category_name,
                                "declaredStarters": None,
                                "sourceUnitCount": 0,
                                "rankingBasis": "time", "results": [],
                            }
                            stage_categories[index].append(category)
                            current[index] = category
                        continue
                    name = cell(words, 165, 284)
                    club = cell(words, 284, 420)
                    if not name or not club or is_junk_name(name):
                        continue
                    for index, spec in enumerate(specs):
                        if current[index] is None:
                            continue
                        raw_value = cell(words, *spec["timeRange"])
                        seconds = _excel_elapsed_time(raw_value)
                        status = (
                            "ok" if seconds is not None
                            else parse_status(raw_value)
                        )
                        if status is None:
                            continue
                        rank_text = cell(words, *spec["rankRange"]).rstrip(".")
                        result = {
                            "name": name, "club": club,
                            "timeText": raw_value, "status": status,
                        }
                        if seconds is not None:
                            result["timeS"] = seconds
                        if rank_text.isdigit() and status == "ok":
                            result["rank"] = int(rank_text)
                        elif is_ooc_status(rank_text):
                            result["outOfCompetition"] = True
                        current[index]["results"].append(result)

    stage_documents = []
    for spec, categories in zip(specs, stage_categories):
        categories = [category for category in categories
                      if category["results"]]
        for category in categories:
            category["sourceUnitCount"] = len(category["results"])
            category["declaredStarters"] = len(category["results"])
        if categories:
            stage_documents.append({
                "stageNumber": spec["stageNumber"],
                "stageDate": spec["stageDate"],
                "stageTitle": spec["stageTitle"],
                "listType": "race", "categories": categories,
            })
    return stage_documents


def _repair_glued_school_name(value):
    value = re.sub(r"(?<=[a-zäöüß])(?=[A-ZÄÖÜ])", " ", value or "")
    return re.sub(r"\s+", " ", value).strip()


def parse_noe_school_team_pdf(path):
    """Parse ranked three-person teams from the 2022 NÖ school result."""
    import pdfplumber

    categories = []
    current = None
    loose_previous = None
    pending = None
    category_re = re.compile(r"^(?:Damen|Herren)\s+(?:Oberstufe|Unterstufe)$")

    def flush_pending():
        nonlocal pending
        if current is None or pending is None:
            pending = None
            return
        members = pending["members"]
        if len(members) != 3:
            pending = None
            return
        current["sourceUnitCount"] += 1
        team_number = f"{current['name']}-{current['sourceUnitCount']}"
        for index, member in enumerate(members, 1):
            result = {
                "name": member["name"], "club": member["club"],
                "rank": pending["rank"], "status": "ok",
                "individualStatus": member["status"],
                "timeText": member["timeText"],
                "resultKind": "team", "teamNumber": team_number,
                "teamName": pending["club"],
                "teamTimeText": pending["teamTimeText"],
                "teamTimeS": pending["teamTimeS"],
                "note": (
                    f"Mannschaft {pending['club']} · "
                    f"Mitglied {index}/3"
                ),
            }
            if member.get("timeS") is not None:
                result["timeS"] = member["timeS"]
            current["results"].append(result)
        pending = None

    def cell(words, start, end=None):
        return " ".join(
            word["text"] for word in words
            if word["x0"] >= start
            and (end is None or word["x0"] < end)
        ).strip()

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                for words in group_lines(page.extract_words()):
                    text = " ".join(word["text"] for word in words).strip()
                    if category_re.fullmatch(text):
                        flush_pending()
                        loose_previous = None
                        current = {
                            "name": text, "declaredStarters": None,
                            "sourceUnitCount": 0,
                            "rankingBasis": "time", "results": [],
                        }
                        categories.append(current)
                        continue
                    if current is None:
                        continue

                    raw_time = cell(words, 425, 485)
                    seconds = parse_time_loose(raw_time)
                    status = "ok" if seconds is not None else parse_status(raw_time)
                    name = _repair_glued_school_name(cell(words, 145, 295))
                    club = cell(words, 295, 425)
                    if not name or not club or status is None:
                        continue
                    row = {
                        "name": name, "club": club, "timeText": raw_time,
                        "timeS": seconds, "status": status,
                    }

                    raw_team_time = cell(words, 485)
                    team_seconds = parse_time_loose(raw_team_time)
                    rank_text = cell(words, 0, 110).rstrip(".")
                    if team_seconds is not None and rank_text.isdigit():
                        flush_pending()
                        if loose_previous is None:
                            continue
                        pending = {
                            "rank": int(rank_text), "club": club,
                            "teamTimeText": raw_team_time,
                            "teamTimeS": team_seconds,
                            "members": [loose_previous, row],
                        }
                        loose_previous = None
                        continue
                    if pending is not None:
                        pending["members"].append(row)
                        flush_pending()
                        loose_previous = None
                    else:
                        loose_previous = row

    flush_pending()
    for category in categories:
        category["declaredStarters"] = category["sourceUnitCount"]
    return [category for category in categories if category["results"]]


def parse_school_score_team_pdf(path):
    """Parse the wide Wiener Neustadt school score sheet with up to 3 names."""
    import pdfplumber

    categories = []
    current = None
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            for words in group_lines(page.extract_words()):
                if not words:
                    continue
                first = words[0]["text"]
                if words[0]["x0"] < 45 and re.fullmatch(r"[DH]\d+", first):
                    current = {"name": first, "sourceCategory": first,
                               "declaredStarters": None,
                               "sourceUnitCount": 0,
                               "rankingBasis": "score", "results": []}
                    categories.append(current)
                    continue
                if current is None or words[0]["x0"] > 50 or not first.isdigit():
                    continue

                def cell(start, end=None):
                    return " ".join(w["text"] for w in words
                                    if w["x0"] >= start
                                    and (end is None or w["x0"] < end)).strip()

                rank = int(first)
                fields = ((55, 220), (220, 345), (345, 445))
                members = [cell(start, end) for start, end in fields]
                members = [member for member in members
                           if member and looks_like_person(member)]
                club, raw_time, score = cell(445, 555), cell(555, 625), cell(715)
                if not members or not club or not score.lstrip("-").isdigit():
                    continue
                seconds = _excel_elapsed_time(raw_time)
                current["sourceUnitCount"] += 1
                team_number = f"{current['name']}-{rank}"
                for member in members:
                    result = {"name": member, "club": club, "rank": rank,
                              "timeText": raw_time, "scoreText": score,
                              "status": "ok", "rankingBasis": "score"}
                    if seconds is not None:
                        result["timeS"] = seconds
                    if len(members) > 1:
                        result.update({
                            "resultKind": "team", "teamNumber": team_number,
                            "teamName": club,
                            "note": "Team: " + ", ".join(
                                other for other in members if other != member),
                        })
                    current["results"].append(result)
    for category in categories:
        category["declaredStarters"] = category["sourceUnitCount"]
    return [category for category in categories if category["results"]]


def parse_vorarlberg_school_team_pdf(path):
    """Parse four-runner Vorarlberg school teams (best three score)."""
    import pdfplumber

    categories = []
    current = None
    groups = {}
    last_key = None

    def flush_category():
        nonlocal groups, last_key
        if current is None:
            groups = {}
            return
        for (club, team_no), team in groups.items():
            members = team["members"]
            team_status = "ok" if team.get("teamTimeS") is not None else "unknown"
            for index, member in enumerate(members, 1):
                result = {
                    "name": member["name"], "club": club,
                    "status": team_status, "teamStatus": team_status,
                    "individualStatus": member["status"],
                    "timeText": member["timeText"],
                    "resultKind": "team", "teamNumber": team_no,
                    "teamName": f"{club} {team_no}".strip(),
                    "teamTimeText": team.get("teamTimeText") or "",
                    "leg": index, "legCount": len(members),
                    "note": f"Mannschaft {club} {team_no} · Mitglied {index}/{len(members)}",
                }
                if member.get("timeS") is not None:
                    result["timeS"] = member["timeS"]
                if team.get("rank") is not None:
                    result["rank"] = team["rank"]
                if team.get("teamTimeS") is not None:
                    result["teamTimeS"] = team["teamTimeS"]
                current["results"].append(result)
        current["sourceUnitCount"] = len(groups)
        current["declaredStarters"] = len(groups)
        groups = {}
        last_key = None

    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            for words in group_lines(page.extract_words()):
                if not words:
                    continue
                text = " ".join(w["text"] for w in words).strip()
                if text.startswith("Platz SI "):
                    flush_category()
                    header = re.sub(r"^Platz\s+SI\s+", "", text)
                    name = re.split(r"\s+Laufzeit\b", header, maxsplit=1)[0].strip()
                    current = {"name": name, "declaredStarters": None,
                               "sourceUnitCount": 0, "results": []}
                    categories.append(current)
                    continue
                if current is None:
                    continue

                def cell(start, end=None):
                    return " ".join(w["text"] for w in words
                                    if w["x0"] >= start
                                    and (end is None or w["x0"] < end)).strip()

                chip = cell(75, 125)
                surname, given = cell(125, 190), cell(190, 250)
                raw_team = cell(250, 325)
                raw_individual = cell(325, 400)
                raw_total = cell(450)
                total_seconds = parse_time_loose(raw_total)
                if (not chip and total_seconds is not None and last_key in groups):
                    groups[last_key]["teamTimeText"] = raw_total
                    groups[last_key]["teamTimeS"] = total_seconds
                    continue
                if not chip.isdigit() or not surname or not given or not raw_team:
                    continue
                team_match = re.match(r"^(.*?)(\d+)\s*$", raw_team)
                if not team_match:
                    continue
                club, team_no = team_match.group(1).strip(), team_match.group(2)
                key = (club, team_no)
                team = groups.setdefault(key, {"members": []})
                last_key = key
                seconds = parse_time_loose(raw_individual)
                individual_status = ("ok" if seconds is not None else
                                     parse_status(raw_individual) or "unknown")
                team["members"].append({
                    "name": f"{surname} {given}", "timeText": raw_individual,
                    "timeS": seconds, "status": individual_status,
                })
                rank_text = cell(45, 75)
                if rank_text.isdigit():
                    team["rank"] = int(rank_text)
                if total_seconds is not None:
                    team["teamTimeText"] = raw_total
                    team["teamTimeS"] = total_seconds
    flush_category()
    return [category for category in categories if category["results"]]


def parse_arge_alp_relay_pdf(path):
    """Parse the Italian Oribos ``CLASSIFICA STAFFETTE`` layout.

    Its first member row contains the *team* rank before the leg number.
    Subsequent rows start directly with the leg number.  The status on the
    ``N°:`` team header is authoritative for every member, while the last
    value on a member row records which leg caused an MP/DNF/DNS.
    """
    import pdfplumber

    categories = []
    current = None
    pending_team = None
    team_value_re = re.compile(
        r"(?P<value>\d{2}\.\d{2}\.\d{2}|Punz\.\s+(?:Errata|Mancante)|"
        r"Ritirato|Non Partito|Fuori Tempo Max|Incompleta)(?:\s+L)?$",
        re.I,
    )
    member_first_re = re.compile(
        r"^(?P<rank>\d+|--)\s+(?P<leg>1)\s+(?P<bib>\d+)\s+(?P<body>.+)$")
    member_re = re.compile(
        r"^(?P<leg>[123])\s+(?P<bib>\d+)\s+(?P<body>.+)$")
    nation_re = re.compile(r"^(?P<name>.+?)\s+(?P<nation>\d{4})\s+(?P<tail>.+)$")
    dotted_time_re = re.compile(r"\d{2}\.\d{2}\.\d{2}")

    def clean_team_label(label):
        tokens = re.sub(r"\s+", " ", label or "").strip().split()
        middle = len(tokens) // 2
        if (tokens and len(tokens) % 2 == 0
                and tokens[:middle] == tokens[middle:]):
            tokens = tokens[:middle]
        return " ".join(tokens)

    def normalize_time(value):
        value = (value or "").strip()
        match = dotted_time_re.search(value)
        return match.group().replace(".", ":") if match else value

    def result_status(value):
        normalized = normalize_time(value)
        if parse_time_loose(normalized) is not None:
            return "ok"
        return parse_status(value or "") or "unknown"

    def flush_team():
        nonlocal pending_team
        if current is None or not pending_team:
            pending_team = None
            return
        members = pending_team["members"]
        if not members:
            pending_team = None
            return
        names = [member["name"] for member in members]
        team_status = pending_team["status"]
        if team_status == "unknown":
            team_status = aggregate_team_status(
                None, [member["status"] for member in members])
        for member in members:
            notes = [
                f"Staffel: {pending_team['name']}",
                f"Leg {member['leg']}/{len(members)}",
            ]
            mates = [name for name in names if name != member["name"]]
            if mates:
                notes.append("Team: " + ", ".join(mates))
            result = {
                "name": member["name"],
                "club": pending_team["name"],
                "status": team_status,
                "individualStatus": member["status"],
                "timeText": member["timeText"],
                "resultKind": "relay",
                "teamNumber": pending_team["number"],
                "teamName": pending_team["name"],
                "teamStatus": team_status,
                "teamTimeText": pending_team["timeText"],
                "leg": member["leg"],
                "legCount": len(members),
                "note": " · ".join(notes),
            }
            if pending_team.get("rank") is not None:
                result["rank"] = pending_team["rank"]
            if pending_team.get("timeS") is not None:
                result["teamTimeS"] = pending_team["timeS"]
            if member.get("timeS") is not None:
                result["timeS"] = member["timeS"]
            current["results"].append(result)
        pending_team = None

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                for raw_line in (page.extract_text() or "").splitlines():
                    text = re.sub(r"\s+", " ", raw_line).strip()
                    category_match = re.match(
                        r"^(?:\.\.\.)?Categoria:\s*(.+)$", text, re.I)
                    if category_match:
                        flush_team()
                        category_name = category_match.group(1).strip()
                        if current and current["name"] == category_name:
                            continue
                        current = {
                            "name": category_name,
                            "declaredStarters": None,
                            "sourceUnitCount": 0,
                            "results": [],
                        }
                        categories.append(current)
                        continue
                    if current is None:
                        continue
                    if text.startswith("N°:"):
                        flush_team()
                        value_match = team_value_re.search(text)
                        if not value_match:
                            continue
                        prefix = text[3:value_match.start()].strip()
                        number_match = re.match(r"(?P<number>\d+)\s+(?P<label>.+)", prefix)
                        if not number_match:
                            continue
                        raw_value = value_match.group("value")
                        time_text = normalize_time(raw_value)
                        pending_team = {
                            "number": number_match.group("number"),
                            "name": clean_team_label(number_match.group("label")),
                            "rank": None,
                            "timeText": time_text,
                            "timeS": parse_time_loose(time_text),
                            "status": result_status(raw_value),
                            "members": [],
                        }
                        current["sourceUnitCount"] += 1
                        continue
                    if pending_team is None:
                        continue
                    member_match = member_first_re.match(text)
                    if member_match:
                        if member_match.group("rank").isdigit():
                            pending_team["rank"] = int(member_match.group("rank"))
                    else:
                        member_match = member_re.match(text)
                    if not member_match:
                        continue
                    body_match = nation_re.match(member_match.group("body"))
                    if not body_match:
                        continue
                    name = body_match.group("name").strip()
                    if not looks_like_person(name):
                        continue
                    tail = body_match.group("tail")
                    times = dotted_time_re.findall(tail)
                    raw_value = times[-1] if times else tail
                    time_text = normalize_time(raw_value)
                    pending_team["members"].append({
                        "leg": int(member_match.group("leg")),
                        "name": name,
                        "timeText": time_text,
                        "timeS": parse_time_loose(time_text),
                        "status": result_status(raw_value),
                    })
            flush_team()

    for category in categories:
        category["declaredStarters"] = category["sourceUnitCount"]
        leg_count = max(
            (row.get("leg") or 0 for row in category["results"]), default=0)
        for row in category["results"]:
            row["legCount"] = leg_count
            row["note"] = re.sub(
                r"\bLeg\s+(\d+)/\d+\b", rf"Leg \1/{leg_count}",
                row.get("note") or "")
    return [category for category in categories if category["results"]]


def parse_orienteering_online_pdf(path):
    """Parse a browser-printed OrienteeringOnline result table.

    The PDF repeats the category in every row, which gives us a stronger
    boundary than page x-coordinates.  The optional ``+gap`` is discarded;
    the preceding value is the actual finish time or status.
    """
    import pdfplumber

    categories = []
    current = None
    category_re = re.compile(
        r"^(?:KIDS\s*/\s*Family|OPEN\s*/\s*E|[MW]\s+\d+(?:\s+Short)?)$",
        re.I,
    )
    value_re = re.compile(
        r"(?P<value>\d{1,3}:\d{2}|Fehlst\.?|N\.?\s*Ang\.?|Aufg\.?)"
        r"(?:\s+\+\d{1,3}:\d{2})?$",
        re.I,
    )

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                for raw_line in (page.extract_text() or "").splitlines():
                    text = re.sub(r"\s+", " ", raw_line).strip()
                    if category_re.fullmatch(text):
                        current = {
                            "name": text,
                            "declaredStarters": None,
                            "results": [],
                        }
                        categories.append(current)
                        continue
                    if current is None:
                        continue
                    prefix = re.sub(r"^(?P<rank>\d+)\.\s+", "", text)
                    rank_match = re.match(r"^(?P<rank>\d+)\.\s+", text)
                    delimiter = f" {current['name']} "
                    if delimiter not in f" {prefix} ":
                        continue
                    name, tail = prefix.split(delimiter, 1)
                    name = name.strip()
                    tail = tail.strip()
                    value_match = value_re.search(tail)
                    if not value_match or not looks_like_person(name):
                        continue
                    raw_value = value_match.group("value")
                    club_country = tail[:value_match.start()].strip()
                    country_match = re.search(
                        r"(?:^|\s)(?P<country>[A-Z]{3})$", club_country)
                    if country_match:
                        club = club_country[:country_match.start()].strip()
                        country = country_match.group("country")
                    else:
                        club, country = club_country, ""
                    time_text = raw_value.rstrip(".")
                    seconds = parse_time_loose(time_text)
                    result = {
                        "name": name,
                        "club": club,
                        "timeText": time_text,
                        "status": ("ok" if seconds is not None else
                                   parse_status(raw_value) or "unknown"),
                    }
                    if country:
                        result["country"] = country
                    if rank_match:
                        result["rank"] = int(rank_match.group("rank"))
                    if seconds is not None:
                        result["timeS"] = seconds
                    current["results"].append(result)

    categories = [category for category in categories if category["results"]]
    for category in categories:
        category["declaredStarters"] = len(category["results"])
    return categories


def parse_plain_regional_championship_pdf(path):
    """Parse a simple regional championship list produced in a text editor.

    The document prints ``1. und ... Meister/in`` on its own line, then often
    omits the leading ``1`` on the champion's result row.  Some subsequent
    places are omitted as well.  Within each category the displayed row order
    therefore supplies the missing consecutive ranks, while MP rows remain
    unranked.
    """
    import pdfplumber

    categories = []
    current = None
    category_re = re.compile(r"^(?:Damen|Herren)\s+[-\d]+(?:-\d+)?$", re.I)
    row_re = re.compile(
        r"^(?:(?P<rank>\d+)\s+)?(?P<name>.+?)\s+"
        r"(?P<value>\d{2}:\d{2}:\d{2}|Fehlst\.?)\s+"
        r"(?P<club>.+)$",
        re.I,
    )

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                for raw_line in (page.extract_text() or "").splitlines():
                    text = re.sub(r"\s+", " ", raw_line).strip()
                    if category_re.fullmatch(text):
                        current = {
                            "name": text,
                            "declaredStarters": None,
                            "results": [],
                        }
                        categories.append(current)
                        continue
                    if current is None:
                        continue
                    match = row_re.match(text)
                    if not match or not looks_like_person(match.group("name")):
                        continue
                    raw_value = match.group("value").rstrip(".")
                    seconds = parse_time_loose(raw_value)
                    status = ("ok" if seconds is not None else
                              parse_status(raw_value) or "unknown")
                    result = {
                        "name": re.sub(r"-\s+", "-", match.group("name").strip()),
                        "club": match.group("club").strip(),
                        "timeText": raw_value,
                        "status": status,
                    }
                    if seconds is not None:
                        result["timeS"] = seconds
                        result["rank"] = (
                            int(match.group("rank"))
                            if match.group("rank")
                            else 1 + max(
                                (row.get("rank", 0)
                                 for row in current["results"]),
                                default=0,
                            )
                        )
                    current["results"].append(result)

    categories = [category for category in categories if category["results"]]
    for category in categories:
        category["declaredStarters"] = len(category["results"])
    return categories


WIEN_SPRINT_CATEGORY_RE = re.compile(
    r"^(?:[DH]\s+(?:15-|-14|45-|55-)|Damen offen|Herren offen|Neulinge)$",
    re.I,
)


def parse_wien_sprint_result_line(text, inferred_rank=None):
    """Parse one row of the 2019 Wiener Sprintmeisterschaft Word export."""
    match = re.match(
        r"^(?:(?P<rank>\d+)\.\s+)?(?P<body>.+?)\s+"
        r"(?P<value>\d{1,2}:\d{2}(?::\d{2})?|MP|DNF|DNS|DSQ)$",
        text, re.I)
    if not match:
        return None
    club, name_tokens = find_trailing_club(match.group("body").split(), CLUBS)
    name = " ".join(name_tokens).strip()
    if not club or not looks_like_person(name) or is_junk_name(name):
        return None
    raw_value = match.group("value")
    seconds = parse_time_loose(raw_value)
    result = {
        "name": name,
        "club": club,
        "timeText": raw_value,
        "status": ("ok" if seconds is not None else
                   parse_status(raw_value) or "unknown"),
    }
    if seconds is not None:
        result["timeS"] = seconds
        if match.group("rank"):
            result["rank"] = int(match.group("rank"))
        elif inferred_rank is not None:
            result["rank"] = inferred_rank
    return result


def parse_wien_sprint_championship_pdf(path):
    """Parse the official 2019 Wiener individual Sprint championship list.

    Its champion announcement is printed on a separate line, so the winner
    row has no leading rank while later finishers do.
    """
    import pdfplumber

    categories = []
    current = None
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                for raw_line in (page.extract_text() or "").splitlines():
                    text = re.sub(r"\s+", " ", raw_line).strip()
                    text = re.sub(r"\s+Laufzeit$", "", text)
                    if WIEN_SPRINT_CATEGORY_RE.fullmatch(text):
                        current = {
                            "name": text,
                            "declaredStarters": None,
                            "results": [],
                        }
                        categories.append(current)
                        continue
                    if current is None or text.startswith("1. und Wiener "):
                        continue
                    row = parse_wien_sprint_result_line(
                        text, inferred_rank=1 if not current["results"] else None)
                    if row:
                        current["results"].append(row)

    categories = [category for category in categories if category["results"]]
    for category in categories:
        category["declaredStarters"] = len(category["results"])
    return categories


def academic_championship_table_result(row):
    """Normalize one five-column row from the Kärntner academic PDF."""
    if len(row or []) != 5:
        return None
    rank_text, surname, firstname, raw_value, club = [
        re.sub(r"\s+", " ", value or "").strip() for value in row]
    rank_match = re.fullmatch(r"(\d+)\.", rank_text)
    seconds = parse_time_loose(raw_value)
    if not rank_match or seconds is None:
        return None
    # The PDF's character positioning inserts spaces inside some surnames
    # (``Ka ltenbacher``); the visual table has one surname cell, so those
    # spaces are extraction artifacts rather than word boundaries.
    surname = re.sub(r"\s+", "", surname)
    name = f"{surname} {firstname}".strip()
    if not looks_like_person(name) or is_junk_name(name):
        return None
    return {
        "name": name,
        "club": club,
        "timeText": raw_value,
        "timeS": seconds,
        "status": "ok",
        "rank": int(rank_match.group(1)),
    }


def parse_academic_championship_pdf(path):
    """Parse the four Word tables of the 2018 Kärntner academic ranking."""
    import pdfplumber

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pdfplumber.open(path) as pdf:
            headings = []
            tables = []
            for page in pdf.pages:
                headings.extend(
                    re.sub(r"\s+", " ", line).strip()
                    for line in (page.extract_text() or "").splitlines()
                    if re.fullmatch(
                        r"(?:Herren|Damen).+\(Bahn\s+\d+\)",
                        re.sub(r"\s+", " ", line).strip(),
                        re.I,
                    )
                )
                tables.extend(page.extract_tables() or [])
    categories = []
    for heading, table in zip(headings, tables):
        results = [
            parsed for row in table
            if (parsed := academic_championship_table_result(row)) is not None
        ]
        if results:
            categories.append({
                "name": heading,
                "declaredStarters": len(results),
                "results": results,
            })
    return categories


def parse_wien_mixed_sprint_relay_pdf(path):
    """Parse the 2019 Wiener Mixed-Sprintstaffel spreadsheet export."""
    import pdfplumber

    categories = []
    current = None
    pending_team = None
    value = r"(?:\d{1,2}:\d{2}(?::\d{2})?|MP|DNF|DNS|DSQ)"
    member_re = re.compile(
        rf"^(?P<leg>[123])\.\s+(?P<name>.+?)\s+"
        rf"(?P<own>{value})\s+(?P<total>{value})$", re.I)
    team_re = re.compile(
        rf"^(?:(?P<rank>\d+)\.\s+)?(?P<label>.+?)\s+"
        rf"(?P<result>{value})$", re.I)
    category_re = re.compile(
        r"^(?:Allgemeine Kategorie(?:\s+Teilzeit\s+Gesamtzeit)?|"
        r"Nachwuchs\s+-16|Senioren\s+35-|"
        r"Senioren\s+50-(?:\s+\([^)]*\)\s+Time)?|Offen)$",
        re.I,
    )

    def normalized_club(team_label):
        label = re.sub(r"^Natufreunde\b", "Naturfreunde", team_label,
                       flags=re.I)
        return re.sub(r"\s+\d+$", "", label).strip()

    def value_details(raw_value):
        seconds = parse_time_loose(raw_value)
        return (
            raw_value,
            seconds,
            "ok" if seconds is not None else
            (parse_status(raw_value) or "unknown"),
        )

    def flush_team():
        nonlocal pending_team
        if current is None or not pending_team or not pending_team["members"]:
            pending_team = None
            return
        names = [member["name"] for member in pending_team["members"]]
        for member in pending_team["members"]:
            notes = [
                f"Staffel: {pending_team['name']}",
                f"Leg {member['leg']}/3",
            ]
            mates = [name for name in names if name != member["name"]]
            if mates:
                notes.append("Team: " + ", ".join(mates))
            result = {
                "name": member["name"],
                "club": pending_team["club"],
                "status": pending_team["status"],
                "individualStatus": member["status"],
                "timeText": member["timeText"],
                "resultKind": "relay",
                "teamNumber": pending_team["number"],
                "teamName": pending_team["name"],
                "teamStatus": pending_team["status"],
                "teamTimeText": pending_team["timeText"],
                "leg": member["leg"],
                "legCount": 3,
                "note": " · ".join(notes),
            }
            if pending_team.get("rank") is not None:
                result["rank"] = pending_team["rank"]
            if pending_team.get("timeS") is not None:
                result["teamTimeS"] = pending_team["timeS"]
            if member.get("timeS") is not None:
                result["timeS"] = member["timeS"]
            current["results"].append(result)
        pending_team = None

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                for raw_line in (page.extract_text() or "").splitlines():
                    text = re.sub(r"\s+", " ", raw_line).strip()
                    if category_re.fullmatch(text):
                        flush_team()
                        name = re.sub(r"\s+\([^)]*\)\s+Time$", "", text)
                        name = re.sub(
                            r"\s+Teilzeit\s+Gesamtzeit$", "", name)
                        current = {
                            "name": name,
                            "declaredStarters": None,
                            "sourceUnitCount": 0,
                            "results": [],
                        }
                        categories.append(current)
                        continue
                    if current is None:
                        continue
                    member_match = member_re.match(text)
                    if member_match and pending_team is not None:
                        raw_value, seconds, status = value_details(
                            member_match.group("own"))
                        pending_team["members"].append({
                            "leg": int(member_match.group("leg")),
                            "name": member_match.group("name").strip(),
                            "timeText": raw_value,
                            "timeS": seconds,
                            "status": status,
                        })
                        continue
                    team_match = team_re.match(text)
                    if not team_match or text.startswith("1. und "):
                        continue
                    flush_team()
                    team_label = re.sub(
                        r"^Natufreunde\b", "Naturfreunde",
                        team_match.group("label").strip(), flags=re.I)
                    raw_value, seconds, status = value_details(
                        team_match.group("result"))
                    trailing_number = re.search(r"\s+(\d+)$", team_label)
                    pending_team = {
                        "name": team_label,
                        "club": normalized_club(team_label),
                        "number": (
                            trailing_number.group(1)
                            if trailing_number else team_label
                        ),
                        "rank": (
                            int(team_match.group("rank"))
                            if team_match.group("rank") else None
                        ),
                        "timeText": raw_value,
                        "timeS": seconds,
                        "status": status,
                        "members": [],
                    }
                    current["sourceUnitCount"] += 1
            flush_team()

    categories = [category for category in categories if category["results"]]
    for category in categories:
        category["declaredStarters"] = category["sourceUnitCount"]
    return categories


def parse_trailo_tempo_pdf(path):
    """Parse the compact Trakošćan TempO station matrix.

    The hundreds of answer cells are deliberately ignored here; the source's
    class rank and total penalty seconds at the row end are authoritative.
    """
    import pdfplumber

    categories_by_code = {
        code: {
            "name": name,
            "declaredStarters": None,
            "rankingBasis": "time",
            "results": [],
        }
        for code, name in (("E", "Elite"), ("A", "A"), ("N", "N"))
    }
    class_re = re.compile(
        r"\s(?P<country>[A-Z]{3})\s+(?P<class>[EAN])"
        r"(?:\s+(?P<para>P))?\s+(?P<age>[SJ])\s")
    total_re = re.compile(
        r"\s(?P<seconds>\d+)\s+(?P<pct>\d+,\d{2})"
        r"(?:\s+\d+,\d{2})*\s*$")
    club_markers = (
        " OK ", " Tipo Orienteering Club", " REM MAPS",
        " Zalaegerszegi Tájékozódási Futó Club", " Bimahev",
    )

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pdfplumber.open(path) as pdf:
            text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    for line in text.splitlines():
        rank_match = re.match(r"^(?P<rank>\d+)\s+(?P<rest>.+)$", line)
        class_match = class_re.search(line)
        total_match = total_re.search(line)
        if not rank_match or not class_match or not total_match:
            continue
        prefix = line[rank_match.end("rank"):class_match.start()].strip()
        # Some rows carry a second, Croatian-only placing after the overall
        # class rank. It is not a second competitor identifier.
        prefix = re.sub(r"^\d+\s+", "", prefix).strip()
        marker_positions = [
            (prefix.find(marker), marker) for marker in club_markers
            if prefix.find(marker) >= 0
        ]
        if not marker_positions:
            continue
        position, marker = min(marker_positions)
        name = prefix[:position].strip()
        club = prefix[position + 1:].strip()
        name = name.replace("ÁgnesErdősné", "Ágnes Erdősné")
        if not looks_like_person(name):
            continue
        seconds = int(total_match.group("seconds"))
        result = {
            "rank": int(rank_match.group("rank")),
            "name": name,
            "club": club,
            "country": class_match.group("country"),
            "timeText": f"{seconds // 60}:{seconds % 60:02d}",
            "timeS": seconds,
            "status": "ok",
            "scoreText": total_match.group("pct").replace(",", "."),
            "rankingBasis": "time",
            "note": f"TempO Strafzeit: {seconds} s",
        }
        if class_match.group("para"):
            result["note"] += " · Para"
        categories_by_code[class_match.group("class")]["results"].append(result)

    categories = [
        category for category in categories_by_code.values()
        if category["results"]]
    for category in categories:
        category["declaredStarters"] = len(category["results"])
    return categories


def parse_nolv_name_first_pdf(path):
    """Parse ``Name Vorname Verein Zeit Platz`` NOLV result reports.

    Unlike SportSoftware, these word-processor PDFs print placement after the
    time. The generic parser recovered every name and time but silently lost
    that final column, making all classified finishers appear unranked.
    """
    import pdfplumber

    categories = []
    current = None
    category_re = re.compile(r"^(Herren|Damen)\s+([A-Z])$", re.I)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                for words in group_lines(page.extract_words()):
                    text = " ".join(word["text"] for word in words).strip()
                    category_match = category_re.fullmatch(text)
                    if category_match:
                        current = {
                            "name": f"{category_match.group(1).title()} "
                                    f"{category_match.group(2).upper()}",
                            "declaredStarters": None,
                            "results": [],
                        }
                        categories.append(current)
                        continue
                    if current is None or not words:
                        continue

                    def cell(start, end=None):
                        return " ".join(
                            word["text"] for word in words
                            if word["x0"] >= start
                            and (end is None or word["x0"] < end)
                        ).strip()

                    surname = cell(45, 125)
                    given = cell(125, 195)
                    club = cell(195, 325)
                    value = cell(325, 420).rstrip(".")
                    rank_text = cell(420).rstrip(".")
                    if (not surname or not given or not value
                            or not looks_like_person(f"{surname} {given}")):
                        continue
                    seconds = parse_time_loose(value)
                    status = "ok" if seconds is not None else parse_status(value)
                    if status is None:
                        continue
                    result = {
                        "name": f"{surname} {given}", "club": club,
                        "timeText": value, "status": status,
                    }
                    if seconds is not None:
                        result["timeS"] = seconds
                    if rank_text.isdigit():
                        result["rank"] = int(rank_text)
                    current["results"].append(result)
    for category in categories:
        category["declaredStarters"] = len(category["results"])
    return [category for category in categories if category["results"]]


def parse_nolv_freeform_school_pdf(path):
    """Parse NOLV school lists whose category code is carried per row."""
    import pdfplumber

    categories = {}
    row_re = re.compile(
        r"^(?:(?P<rank>\d+)\.?\s+)?(?:(?P<chip>\d{5,})\s+)?"
        r"(?P<body>.+?)\s+(?P<cat>[DH][A-Z]{1,3})\s+"
        r"(?P<value>\d{1,3}[,.]\d{1,2}|techn\.\s*Fehler)"
        r"(?:\s+(?P<errors>\d+))?\s*$", re.I)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                for raw in (page.extract_text() or "").splitlines():
                    line = re.sub(r"\s+", " ", raw).strip()
                    collapsed_line = re.sub(r"(.)\1", r"\1", line)
                    if (
                        len(line.split()) >= 4
                        and len(collapsed_line) <= 0.65 * len(line)
                    ):
                        # A handful of scanned school sheets contain one
                        # overprinted row whose extracted glyphs are all
                        # doubled (``SSttooiieerr ... 3311,3399``). It is a
                        # real competitor, not a header or duplicate result.
                        line = collapsed_line
                    match = row_re.match(line)
                    if not match:
                        continue
                    identity = match.group("body").split()
                    if len(identity) < 2:
                        continue
                    name = " ".join(identity[:2])
                    club = " ".join(identity[2:])
                    if not looks_like_person(name) or is_junk_name(name):
                        continue
                    raw_value = match.group("value")
                    if raw_value.casefold().startswith("techn"):
                        value, seconds = raw_value, None
                        status = parse_status(raw_value) or "unknown"
                    else:
                        # These school lists use decimal punctuation as the
                        # minute/second separator.  A single digit after the
                        # comma is a clipped trailing zero (``28,3`` means
                        # 28:30), not a leading zero (28:03).  Right-padding
                        # preserves the printed precision and the ranking
                        # order.
                        value = re.sub(
                            r"^(\d+)[,.](\d)$", r"\1:\g<2>0", raw_value)
                        value = re.sub(r"[,.]", ":", value, count=1)
                        seconds = parse_time_loose(value)
                        status = ("mp" if match.group("errors")
                                  and int(match.group("errors")) > 0 else "ok")
                    result = {
                        "name": name, "club": club, "timeText": value,
                        "status": status,
                    }
                    if match.group("rank"):
                        result["rank"] = int(match.group("rank"))
                    if seconds is not None:
                        result["timeS"] = seconds
                    code = match.group("cat").upper()
                    category = categories.setdefault(code, {
                        "name": code, "sourceCategory": code,
                        "declaredStarters": None, "results": [],
                    })
                    category["results"].append(result)
    parsed = [category for category in categories.values() if category["results"]]
    for category in parsed:
        previous_rank = None
        previous_time = None
        for result in category["results"]:
            if result.get("rank") is not None:
                previous_rank = result["rank"]
                previous_time = result.get("timeS")
                continue
            if result.get("status") != "ok" or previous_rank is None:
                continue
            if (
                previous_time is not None
                and result.get("timeS") == previous_time
            ):
                result["rank"] = previous_rank
            else:
                result["rank"] = previous_rank + 1
                previous_rank = result["rank"]
                previous_time = result.get("timeS")
        category["declaredStarters"] = len(category["results"])
    return parsed


def parse_primary_school_team_pdf(path):
    """Parse primary-school pair/triple teams with one shared finish time."""
    import pdfplumber

    categories = {}
    course = None
    course_re = re.compile(r"^(\d+(?:[.,]\d+)?)\s*km\s*/?\s*(\d+)\s*Posten", re.I)
    row_re = re.compile(
        r"^(?P<rank>\d+)\.\s+(?P<body>.+?)\s+"
        r"(?P<time>\d+:\d{2}:\d{2}(?:[,.]\d+)?)\s+(?P<errors>\d+)\s*$")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                for raw in (page.extract_text() or "").splitlines():
                    line = re.sub(r"\s+", " ", raw).strip()
                    course_match = course_re.match(line)
                    if course_match:
                        course = (course_match.group(1).replace(",", ".")
                                  + " km / " + course_match.group(2) + " Posten")
                        continue
                    if course is None:
                        continue
                    match = row_re.match(line)
                    if not match:
                        continue
                    tokens = match.group("body").split()
                    gender_at = next((i for i, token in enumerate(tokens)
                                      if token.casefold() in {"m", "w"}), None)
                    if gender_at is not None:
                        name_tokens = tokens[:gender_at]
                        rest = tokens[gender_at + 1:]
                        gender = tokens[gender_at].upper()
                    else:
                        class_at = next((i for i, token in enumerate(tokens)
                                         if i >= 4 and re.fullmatch(r"\d+[a-z]?", token, re.I)), None)
                        if class_at is None:
                            continue
                        name_tokens, rest, gender = tokens[:class_at], tokens[class_at:], None
                    raw_roster = " ".join(name_tokens)
                    members = ([" ".join(name_tokens[i:i + 2])
                                for i in range(0, len(name_tokens), 2)]
                               if len(name_tokens) >= 4 and len(name_tokens) % 2 == 0
                               else [])
                    if members and not all(
                            looks_like_person(member) and not is_junk_name(member)
                            for member in members):
                        members = []
                    class_token = (
                        rest[0] if rest
                        and re.fullmatch(r"\d+[a-z]?", rest[0], re.I)
                        else ""
                    )
                    school = " ".join(rest[1:] if class_token else rest)
                    grade_match = re.match(r"\d+", class_token)
                    grade = grade_match.group(0) if grade_match else ""
                    category_name = (
                        course
                        + (f" {gender}" if gender else "")
                        + (f" · {grade}. Klasse" if grade else "")
                        + (f" · {school}" if school else "")
                    )
                    category = categories.setdefault(category_name, {
                        "name": category_name, "declaredStarters": None,
                        "sourceCategory": course,
                        "sourceUnitCount": 0, "rankingBasis": "score",
                        "results": [],
                    })
                    category["sourceUnitCount"] += 1
                    value = re.sub(r"([,.]\d+)$", "", match.group("time"))
                    seconds = parse_time_loose(value)
                    kind = "pair" if len(members) == 2 else "team"
                    team_name = f"{school} {match.group('rank')}".strip()
                    if not members:
                        result = {
                            "name": "", "club": school, "timeText": value,
                            "status": "ok", "rank": int(match.group("rank")),
                            "rankingBasis": "score",
                            "resultKind": "team", "memberlessTeam": True,
                            "individualStatus": None, "teamStatus": "ok",
                            "teamName": team_name, "teamTimeText": value,
                            "teamTimeS": seconds,
                            "note": ("Mannschaft: " + team_name
                                     + " · Teilnehmertext: " + raw_roster),
                        }
                        if int(match.group("errors")):
                            result["scoreText"] = match.group("errors") + " Fehler"
                        category["results"].append(result)
                        continue
                    for member in members:
                        mates = [other for other in members if other != member]
                        result = {
                            "name": member, "club": school, "timeText": value,
                            "timeS": seconds, "status": "ok",
                            "rankingBasis": "score",
                            "rank": int(match.group("rank")), "resultKind": kind,
                            "note": ("Partner: " + mates[0] if kind == "pair" else
                                     "Mannschaft: " + team_name + " · Team: " + ", ".join(mates)),
                        }
                        if kind == "team":
                            result.update({
                                "individualStatus": None, "teamStatus": "ok",
                                "teamName": team_name, "teamTimeText": value,
                                "teamTimeS": seconds,
                            })
                        if int(match.group("errors")):
                            result["scoreText"] = match.group("errors") + " Fehler"
                        category["results"].append(result)
    parsed = [category for category in categories.values() if category["results"]]
    for category in parsed:
        category["declaredStarters"] = category["sourceUnitCount"]
    return parsed


def parse_corrected_time_pdf(path):
    """Parse Bahn rows whose authoritative result is ``Zeit korrigiert``."""
    import pdfplumber

    categories = {}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                for raw in (page.extract_text() or "").splitlines():
                    tokens = raw.split()
                    if len(tokens) < 7 or tokens[2] not in "ABCD":
                        continue
                    si_at = next((index for index in range(3, len(tokens))
                                  if re.fullmatch(r"\d{4,}", tokens[index])), None)
                    if si_at is None:
                        continue
                    name = " ".join(tokens[:2])
                    if not looks_like_person(name) or is_junk_name(name):
                        continue
                    course, club = tokens[2], " ".join(tokens[3:si_at])
                    values = tokens[si_at + 1:]
                    explicit_status = next((parse_status(value) for value in values
                                            if parse_status(value) not in (None, "ok")), None)
                    clocks = [value for value in values
                              if re.fullmatch(r"\d{2}:\d{2}:\d{2}", value)]
                    if explicit_status:
                        value, seconds, status = values[-1], None, explicit_status
                    elif clocks:
                        value, seconds, status = clocks[-1], parse_time_loose(clocks[-1]), "ok"
                    else:
                        continue
                    category = categories.setdefault(course, {
                        "name": course, "declaredStarters": None, "results": [],
                    })
                    result = {
                        "name": name, "club": club, "timeText": value,
                        "status": status,
                    }
                    if seconds is not None:
                        result["timeS"] = seconds
                        result["rank"] = 1 + sum(
                            row.get("rank") is not None for row in category["results"])
                    category["results"].append(result)
    parsed = [category for category in categories.values() if category["results"]]
    for category in parsed:
        category["declaredStarters"] = len(category["results"])
    return parsed


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


def parse_mtbo_dns_supplement_pdf(path):
    """Recover named DNS rows from event 1909's official split-time PDF.

    The compact result attachment omits seven ``N Ang`` rows even though its
    class counts include them.  ANNE also publishes SportSoftware's official
    ``Zwischenzeiten Ergebnis`` attachment, where those names and clubs are
    explicit.  Only the missing DNS observations are returned: ordinary
    finishers and the one runner whose compact result says ``Aufg`` remain
    authoritative in the primary result attachment.
    """
    import pdfplumber

    category_map = {
        "Herren -14": "Herren/Damen -14",
        "Herren Elite": "Herren Elite",
        "Herren 60": "Herren 60",
        "Herren 70": "Herren 70",
    }
    categories = {
        target: {"name": target, "results": []}
        for target in category_map.values()
    }
    lines = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            lines.extend(
                line.strip()
                for line in (page.extract_text(x_tolerance=2, y_tolerance=3) or "").splitlines()
                if line.strip()
            )

    current = None
    for index, line in enumerate(lines):
        heading = next(
            (source for source in category_map
             if re.match(rf"^{re.escape(source)}\s+\(\d+\)(?:\s|$)", line)),
            None,
        )
        if heading:
            current = category_map[heading]
            continue
        match = re.match(r"^\d+\s+(.+?)\s+N\s+Ang$", line)
        if current is None or not match:
            continue
        name = re.sub(r"\s+", " ", match.group(1)).strip()
        # The compact, higher-priority result attachment records Jan Rochford
        # as ``Aufg``. Do not let the older split snapshot downgrade that
        # explicit final status to DNS.
        if name == "Rochford Jan":
            continue
        club = lines[index + 1] if index + 1 < len(lines) else ""
        if (not club or re.search(r"\b(?:Ziel|\d+:\d{2})\b", club)
                or re.match(r"^(?:Herren|Damen)\b", club)):
            club = ""
        categories[current]["results"].append({
            "name": name,
            "club": club,
            "timeText": "N Ang",
            "status": "dns",
            "note": "DNS aus offiziellem ANNE-Zwischenzeiten-Anhang",
        })

    return [
        category for category in categories.values()
        if category["results"]
    ]


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
        primary_count = sum(
            not is_auxiliary_attachment_name(f.get("fileName") or "")
            for f in (files or []))
        sole_attachment = primary_count == 1
        for n, f in enumerate(files or []):
            # ANNE's older calendar records often label a direct ``.pdf``
            # URL as the generic ``text/link`` MIME.  The text parser cannot
            # decode those bytes and used to leave the event completely
            # empty.  A PDF URL is unambiguous and belongs here.
            linked_pdf = (
                f["mimeType"] == "text/link"
                and urllib.parse.urlparse(f["url"]).path.casefold().endswith(".pdf")
            )
            if f["mimeType"] == "application/pdf" or linked_pdf:
                jobs.append((int(eid), n, f, sole_attachment))
    jobs = select_jobs(jobs, args.event_id, args.attachment_manifest)
    if args.limit:
        jobs = jobs[: args.limit]
    print(f"pdf files to parse: {len(jobs)}")

    ok = empty = failed = 0
    for eid, n, f, sole_attachment in jobs:
        if ((eid, n) in MANUAL_ATTACHMENT_INDEX_SKIP
                or (eid, f["fileName"]) in MANUAL_ATTACHMENT_SKIP):
            empty += 1
            continue
        out_path = OUT / f"{eid}-{n}.json"
        pdf_path = FILES / f"{eid}-{n}.pdf"
        try:
            if args.cached and not pdf_path.exists():
                # Before direct PDF links were routed to this parser they
                # were downloaded by the text parser with a misleading
                # ``.html`` suffix. Reuse that cache when its magic bytes
                # prove it is a PDF.
                legacy_link_path = FILES / f"{eid}-{n}.html"
                if (legacy_link_path.exists()
                        and legacy_link_path.read_bytes()[:4] == b"%PDF"):
                    pdf_path = legacy_link_path
                else:
                    empty += 1
                    continue
            if not args.cached:
                fetch(f["url"], pdf_path, args.force_download)
            verified_scan = load_verified_scan_transcript(pdf_path, eid, n)
            if verified_scan:
                cats = verified_scan["categories"]
                head_text = (
                    verified_scan.get("headText")
                    or f["fileName"]
                    or "Ergebnis"
                )
            else:
                cats, head_text = parse_pdf(
                    pdf_path, allow_inline_splits=sole_attachment)
            list_type = detect_list_type(f["fileName"], head_text, sole_attachment)
            stage_documents = None
            if (eid, n) == (1909, 0):
                cats = parse_mtbo_dns_supplement_pdf(pdf_path)
                list_type = "race"
            elif (eid, n) == (2091, 1):
                stage_documents = parse_uwg_multistage_pdf(pdf_path)
                cats = []
            elif (eid, n) == (4835, 1):
                stage_documents = parse_mtbo_two_stage_overall_pdf(pdf_path)
                cats = []
            elif list_type == "overall":
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
            elif ("Origare By Giuseppe Simoni" in head_text
                  and "Categoria:" in head_text):
                cats = parse_origare_pdf(pdf_path)
            elif ("SIME::" in head_text
                  or re.search(r"\bBahn\s+\(\d+\):.*#\s+NR\s+Name\s+Club\s+Resultat",
                               head_text, re.I | re.S)):
                cats = parse_sime_pdf(pdf_path)
            elif re.search(
                    r"\bPl\s+\w*\s*Name\s+.*\bVerein\s+Kat\s+Zeit\b",
                    head_text, re.I):
                cats = parse_course_kat_pdf(pdf_path)
            elif (re.search(r"(?:Vorarlberger Schulcup|OL-Schulcup Vorarlberg)",
                            head_text, re.I)
                  and re.search(r"Rang\s*Nachname\s+Vorname\s+Team\s+Zeit",
                                head_text, re.I)):
                cats = parse_vorarlberg_school_pdf(pdf_path)
            elif (re.search(r"Landesmeisterschaft\s+Vorarlberg", head_text, re.I)
                  and re.search(r"Platz\s+SI\s+Kat\.", head_text, re.I)):
                cats = parse_vorarlberg_school_team_pdf(pdf_path)
            elif re.search(r"Ergebnisse\s+2\.\s*Süd\s*Ost\s*Cup", head_text, re.I):
                cats = parse_southeast_cup_matrix_pdf(pdf_path)
            elif re.search(r"Volksbanken\s+Südostcuplauf", head_text, re.I):
                cats = parse_southeast_plain_pdf(pdf_path)
            elif "WINGS FOR LIFE OL" in head_text:
                cats = parse_wings_for_life_pdf(pdf_path)
            elif re.search(r"Ergebnisliste\s+vom\s+4\.\s+Lauf\s+zum\s+Fun\s+OL\s+Cup",
                           head_text, re.I):
                cats = parse_funol_two_column_pdf(pdf_path)
            elif re.search(r"Einzelergebnisse,?\s+Schulcup\s+2023", head_text, re.I):
                cats = parse_tirol_school_individual_pdf(pdf_path)
            elif ("TM Schulen 2016" in head_text
                  and re.search(r"5\./6\.\s+männlich", head_text, re.I)):
                cats = parse_tirol_school_2016_individual_pdf(pdf_path)
            elif ((eid, n) == (3004, 1)
                  or ("1. Schulcup 2019/20" in head_text
                      and re.search(r"5\./8\.\s+männlich", head_text, re.I))):
                cats = parse_tirol_school_2020_individual_pdf(pdf_path)
            elif ("Tiroler Schulmeisterschaft 2016" in head_text
                  and re.search(r"5\./6\.\s+weiblich", head_text, re.I)):
                cats = parse_tirol_school_team_pdf(pdf_path)
            elif (re.search(r"^5\./6\.\s+w$", head_text, re.I | re.M)
                  and re.search(r"^5\./6\.\s+m$", head_text, re.I | re.M)):
                cats = parse_tirol_school_team_2020_pdf(pdf_path)
            elif ("Ergebnisse Schulcuplauf Mannschaftswertung" in head_text
                  and re.search(r"5\./6\.\s+männlich", head_text, re.I)):
                cats = parse_tirol_school_team_2023_pdf(pdf_path)
            elif ("ERGEBNISSE SURPISE MANNSCHAFT 31.08.2013" in head_text
                  and "TEAMWERTUNG" in head_text):
                cats = parse_surprise_three_person_team_pdf(pdf_path)
            elif ("NÖ Schulmeisterschaft der Schulen im Orientierungslauf"
                  in head_text
                  and re.search(r"Damen\s+Oberstufe", head_text, re.I)):
                cats = parse_noe_school_team_pdf(pdf_path)
            elif re.search(
                    r"Nachname\s+Vorname\s+Start\s+Ziel\s+Zeit\s+Wertung\s+Ort\s+Kurz\s+Platz",
                    head_text, re.I):
                cats = parse_nolv_excel_result_pdf(pdf_path)
            elif re.search(r"Familien-\s*Abenteuer\s+im\s+Wald", head_text, re.I):
                cats = parse_family_adventure_pdf(pdf_path)
            elif re.search(r"Ergebnisse\s+Knock-Out-Sprint", head_text, re.I):
                cats = parse_knockout_final_pdf(pdf_path)
            elif re.search(
                    r"Rang\s+Nachname\s+Vorname\s+Name\s+2\s+Name\s+3\s+"
                    r"Schule\s+Zeit\s+Punkte\s+Strafe\s+Ergebnis",
                    head_text, re.I):
                cats = parse_school_score_team_pdf(pdf_path)
            elif re.search(r"Startnr\.\s+Namen\s+Klasse\s+Startzeit\s+Zielzeit\s+Laufzeit",
                           head_text, re.I):
                cats = parse_start_finish_class_pdf(pdf_path)
            elif re.search(r"Name\s+Vorname\s+Verein\s+Zeit\s+Platz",
                           head_text, re.I):
                cats = parse_nolv_name_first_pdf(pdf_path)
            elif re.search(r"Lehrlingssporttag\s+Zeltweg", head_text, re.I):
                cats = parse_apprentice_sport_pdf(pdf_path)
            elif re.search(r"NOLV.{0,4}Schulcup", head_text, re.I | re.S):
                cats = parse_nolv_freeform_school_pdf(pdf_path)
            elif re.search(r"ASVÖ\s+OL\s+für\s+Volksschulen", head_text, re.I):
                cats = parse_primary_school_team_pdf(pdf_path)
            elif re.search(r"Vname\s+Nname\s+Bahn\s+Verein\s+SI_Karte.*Zeit\s+korrigiert",
                           head_text, re.I | re.S):
                cats = parse_corrected_time_pdf(pdf_path)
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
            elif re.search(
                    r"SI\s+Rang\s+NACHNAME\s+Vorname\s+Klasse\s+Schule\s+Kategorie\s+Zeit",
                    head_text, re.I):
                cats = parse_nolv_school_result_pdf(pdf_path)
            elif ("Steirische Schulmeisterschaft" in head_text
                  and re.search(r"Pl\s+Stnr\s+Name\s+Jg\s+Verein\s+Zeit", head_text)):
                cats = parse_school_final_pdf(pdf_path)
            elif (re.search(r"Staffel", head_text, re.I)
                  and re.search(r"\(\d+\s*/\s*\d+\).*\b(?:Time|Zeit)\b",
                                head_text, re.I)):
                cats = parse_meos_relay_pdf(pdf_path)
            elif re.search(r"^RELAY RESULTS\b", head_text, re.I):
                cats = parse_oribos_relay_pdf(pdf_path)
            elif ("CLASSIFICA STAFFETTE" in head_text
                  and "Arge Alp - Relay" in head_text):
                cats = parse_arge_alp_relay_pdf(pdf_path)
            elif ("OrienteeringOnline.net" in head_text
                  and re.search(r"Rg\.\s+Name\s+Kategorie\s+Verein\s+Land\s+Zeit",
                                head_text, re.I)):
                cats = parse_orienteering_online_pdf(pdf_path)
            elif re.search(
                    r"Niederösterreichische Meisterschaft im "
                    r"Sprint-Orientierungslauf", head_text, re.I):
                cats = parse_plain_regional_championship_pdf(pdf_path)
            elif ("Wiener Sprintmeisterschaft" in head_text
                  and "Offizielle Ergebnisliste" in head_text):
                cats = parse_wien_sprint_championship_pdf(pdf_path)
            elif "Ktn. Akademische Meisterschaft Orientierungslauf" in head_text:
                cats = parse_academic_championship_pdf(pdf_path)
            elif ("Wiener Meisterschaft Mixed-Sprintstaffel" in head_text
                  and "Offizielle Ergebnisliste" in head_text):
                cats = parse_wien_mixed_sprint_relay_pdf(pdf_path)
            elif ("Trakoscan TempO" in head_text
                  and "CRO-ITA-SLO TrailO Cup" in head_text):
                cats = parse_trailo_tempo_pdf(pdf_path)
            elif (MEOS_PAGE_HEADER_RE.match(head_text)
                  or MEOS_CLASS_HEADER_RE.search(head_text)):
                cats = parse_meos_individual_pdf(pdf_path)
            elif re.search(
                    r"Pl\s+Verein\s+Mannschaft\s+Läufer\*in\s+1\s+"
                    r"Läufer\*in\s+2\s+Läufer\*in\s+3\s+Zeit",
                    head_text, re.I):
                cats = parse_inline_mannschaft_pdf(pdf_path)
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
            normalized_category_sets = [cats]
            if stage_documents:
                for stage in stage_documents:
                    stage["categories"] = normalize_qualitative_result_ranks(
                        stage.get("categories") or [])
                    normalized_category_sets.append(stage["categories"])
            for category in itertools.chain.from_iterable(normalized_category_sets):
                for result in category.get("results") or []:
                    repair_official_club_status_overflow(result)
            if not cats and not stage_documents:
                empty += 1
                # a file that used to parse (under an earlier, buggier
                # version of this script) but correctly comes up empty now
                # must not leave its stale, wrong JSON sitting on disk
                # forever - load_legacy_results() would otherwise go on
                # using it indefinitely, since nothing else ever prunes
                # data/normalized/ on its own.
                out_path.unlink(missing_ok=True)
                continue
            normalized_document = {
                "eventId": eid,
                "source": "sportsoftware-pdf",
                "sourceUrl": f["url"],
                "fileName": f["fileName"],
                "listType": "multi-stage" if stage_documents else list_type,
                "docDate": MANUAL_DOC_DATE_OVERRIDES.get((eid, f["fileName"]))
                           or guess_doc_date(f["fileName"], head_text),
                "categories": cats,
            }
            if stage_documents:
                normalized_document["stageDocuments"] = stage_documents
            if verified_scan:
                normalized_document["verifiedScanTranscript"] = {
                    "schemaVersion": verified_scan.get("schemaVersion"),
                    "sourceSha256": verified_scan["sourceSha256"],
                    "verification": verified_scan.get("verification"),
                }
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
