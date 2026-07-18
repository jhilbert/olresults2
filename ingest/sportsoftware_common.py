"""Shared parsing helpers for Stephan Krämer SportSoftware (OE/OE2003/OE12/
OEScore) result exports, used by both the HTML and PDF adapters.
"""
import html
import json
import re
from functools import lru_cache
from pathlib import Path

# Historical attachments that were already unavailable when the repository's
# cache was established. They stay visible in parser summaries, but unlike a
# new/unrecognized failure they do not make every nightly sync permanently
# impossible. The ledger is committed and reviewed, never inferred at runtime.
SOURCE_FAILURE_ALLOWLIST_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "source_failure_allowlist.json")


@lru_cache(maxsize=1)
def load_source_failure_allowlist():
    if not SOURCE_FAILURE_ALLOWLIST_PATH.exists():
        return {}
    return {source: set(entries) for source, entries in
            json.loads(SOURCE_FAILURE_ALLOWLIST_PATH.read_text()).items()}


def is_expected_source_failure(source, event_id, attachment_index):
    return f"{event_id}-{attachment_index}" in load_source_failure_allowlist().get(source, set())

# Club domains verified to embed usable result pages (SportSoftware <pre>
# exports, or a dedicated custom parser). ANNE sometimes mislabels the actual
# results link on these domains (e.g. type "splittimes"), so anne_sync fetches
# any attachment on these domains regardless of its ANNE-assigned type; a
# dedicated adapter or the fixed-width text parser decides what to do with it.
# Extend as more are confirmed — we do not crawl all external result links.
CLUB_LINK_ALLOWLIST = {"olc-wienerwald.at", "hsvwrn-ol.at"}

# ANNE's own stored attachment URL is dead/wrong for these events (the
# organizer's site was restructured; the historical page still exists, just
# at a different address found via the site's own archive). Verified by hand:
# exact event date matches the page's own printed date, and it contains real
# results. {event_id: [(url, fileName), ...]}
MANUAL_ATTACHMENT_OVERRIDES = {
    1303: [("http://www.hsvwrn-ol.at/german/events/ergebnisse/2015/wintertour6.htm", "")],
}

# Attachments that must not be ingested at all - not "redundant" in
# detect_list_type()'s sense (a split-times/cumulative-standings sheet), but
# pure garbage: parse_pdf() has no column model for a split-times report, so
# it comes out with nonsense category names like "1:11 +0:34", a bare "17".
# detect_list_type() doesn't catch these two - confirmed by hand, not worth
# teaching it a whole new report shape for what's otherwise redundant with
# the two real result PDFs for the same event anyway (see
# MANUAL_CATEGORY_SKIP). {(event_id, fileName)}
MANUAL_ATTACHMENT_SKIP = {
    (4894, "event_4894_ergebnis-split-ostm-sprint-ski-o-2025.pdf"),
    (4894, "event_4894_split-ostm-mittel-ski-o-2025.pdf"),
}

# Categories to drop from a specific attachment because a *different*
# attachment of the same physical race already provides a clean version of
# that exact category, and this one doesn't - Ski-O Weekend Hochfilzen 2025
# (event 4894) publishes each race twice: an "Austria Cup"-scored export
# (event_4894_ergebnis-3-ac.../4-ac...html) with the full field including
# foreign guest starters under generic category names, and a narrower
# official "ÖSTM/ÖM"-scored PDF (event_4894_ostm-mittel/sprint...pdf, see
# MANUAL_DOC_DATE_OVERRIDES) covering only the three championship-eligible
# brackets per gender (Elite/21, 35+, U20) with foreign guests already
# excluded by the organizer. Loading the AC file's version of those same
# three brackets too would reintroduce a foreign competitor the clean PDF
# doesn't have at all (so simple (stage, category, name) dedup can't catch
# it - the interloper isn't a duplicate of anyone in the clean list). Every
# *other* bracket in the AC files (35-44, 45+, youth, "Kurz", ...) has no
# clean-PDF counterpart at all and is confirmed foreign-guest-free by hand,
# so it loads normally. {(event_id, fileName): {category name, ...}}
MANUAL_CATEGORY_SKIP = {
    (4894, "event_4894_ergebnis-3-ac-2025-ski-o.html"): {
        "Herren ab 21 Elite", "Herren ab 35", "Herren bis 20",
        "Damen ab 21 Elite", "Damen ab 35", "Damen bis 20",
    },
    (4894, "event_4894_ergebnis-4-ac-2025-ski-o.html"): {
        "Herren ab 21 Elite", "Herren ab 35", "Herren bis 20",
        "Damen ab 21 Elite", "Damen ab 35", "Damen bis 20",
    },
}

# SportSoftware's compact age/gender-bracket code -> the "{Damen/Herren}
# {ab|bis N}[ Elite]" label style used throughout this dataset. Needed for a
# results table that gives one age bracket per ROW (a "Kat" column) rather
# than the far more common one-table-per-bracket layout with the bracket
# named only once in a section header - see split_by_kat().
KAT_RE = re.compile(r"^([DH])(-)?(\d+)(-)?(E)?$")
# Same shape, unanchored: a long club name can overflow a narrow "Kat" column
# and leak its last word(s) in ahead of the real value ("Orienteering
# Innsbruck Imst H-20" instead of just "H-20") - take the trailing match
# rather than trusting the whole cell.
KAT_TOKEN_RE = re.compile(r"([DH]-?\d{1,3}-?E?)\s*$")


def kat_to_category_name(kat):
    m = KAT_RE.match(kat.strip())
    if not m:
        return kat
    gender = "Damen" if m.group(1) == "D" else "Herren"
    leading_dash, age, trailing_dash, elite = m.group(2), m.group(3), m.group(4), m.group(5)
    if leading_dash:
        label = f"bis {age}"
    elif trailing_dash or elite:
        label = f"ab {age}"
    else:
        label = age
    if elite:
        label += " Elite"
    return f"{gender} {label}"


def split_by_kat(categories):
    """A category whose results carry a per-row 'kat' field (this dataset's
    combined-gender-table-with-a-Kat-column PDF layout, as opposed to the
    normal one-section-per-bracket layout) needs splitting into one category
    per distinct kat value - otherwise every age bracket gets lumped into a
    single category and national/overall rank is computed across the whole
    gender field instead of within each bracket."""
    out = []
    for cat in categories:
        kats = {r.get("kat") for r in cat["results"] if r.get("kat")}
        if not kats:
            out.append(cat)
            continue
        by_kat = {}
        for r in cat["results"]:
            k = r.pop("kat", None)
            name = kat_to_category_name(k) if k else cat["name"]
            sub = by_kat.setdefault(
                name, {**cat, "name": name, "declaredStarters": None, "results": []})
            sub["results"].append(r)
        for sub in by_kat.values():
            # the source "Pl" column numbers everyone in the combined table
            # (e.g. Marina is 6th among ALL Damen), not within her own
            # bracket (3rd among D35-) - renumber sequentially per bracket,
            # in the order results already arrived in (itself rank-ordered,
            # since a subsequence of a sorted sequence stays sorted), so the
            # displayed rank matches every other category's own-bracket
            # numbering. Rows with no rank at all (DNS/DNF) are left alone.
            next_rank = 1
            for r in sub["results"]:
                if "rank" in r:
                    r["rank"] = next_rank
                    next_rank += 1
        out.extend(by_kat.values())
    return out

# Same idea, but for organizers who publish a genuine SportSoftware PDF on
# their own site with no ANNE attachment at all (ANNE only links to their
# homepage). {event_id: [(url, fileName), ...]}
MANUAL_PDF_OVERRIDES = {
    4552: [("https://carinthian-lakecup.at/wp-content/uploads/2024/06/20240622Ergday2.pdf",
            "20240622Ergday2.pdf")],  # 7. KOLV Cup, Schiefling, 2024-06-22 (Etappe 2)
    4553: [("https://carinthian-lakecup.at/wp-content/uploads/2024/06/20240622Ergday3.pdf",
            "20240622Ergday3.pdf")],  # 8. KOLV Cup, Rosegg-Bergl, 2024-06-23 (Etappe 3)
    # 1. AC MTBO Mittel ÖStM/ÖM, Hirzenriegel/Fehring, 2025-04-26: ANNE's own
    # structured results for this event are 100% unusable placeholder rows
    # (firstName/lastName like "8112114"/"empty"), and hasOfficialResults=
    # False means anne_sync never discovers the real attachment on its own -
    # but the file exists on ANNE's own CDN under the standard legacy-PDF
    # naming convention, confirmed by hand.
    4884: [("https://anne-cdn.oefol.at/public/legacy/event_4884_ergebniss-nach-kategorien-inkl-meister-in.pdf",
            "event_4884_ergebniss-nach-kategorien-inkl-meister-in.pdf")],
}

# Events whose results live only on liveresultat.orientering.se (a live-
# timing service some organizers use instead of a SportSoftware export) with
# no ANNE attachment pointing there either - found by hand on the
# organizer's own results archive. {event_id: [comp_id, ...]}
# guess_doc_date()'s DOC_DATE_RE fallback can be fooled by a SportSoftware
# export's own "Fr DD.MM.YYYY HH:MM" report-generation timestamp (printed
# whenever the file was last re-exported, which can be months after the
# actual competition) when the filename itself carries no date. Confirmed by
# hand against the event's own dateFrom and a third-party results archive.
# {(event_id, fileName): "YYYY-MM-DD"}
MANUAL_DOC_DATE_OVERRIDES = {
    # Ski-O Weekend Hochfilzen 2025: both files were re-exported together on
    # 2025-05-23 (both print that exact "Fr 23.05.2025 11:02" timestamp), but
    # the competition itself was 2025-02-22 (Mittel) / 2025-02-23 (Sprint) -
    # confirmed against event 4894's own dateFrom and the ÖM medal record.
    (4894, "event_4894_ostm-mittel-ski-o-2025.pdf"): "2025-02-22",
    (4894, "event_4894_ostm-sprint-ski-o-2025.pdf"): "2025-02-23",
    # Its own header prints "So 23.02.2025" (Sunday, likely a post-weekend
    # batch re-export), but the "3.AC Mittel" content itself - confirmed by
    # matching finish times against the Mittel PDF above - is 2025-02-22.
    (4894, "event_4894_ergebnis-3-ac-2025-ski-o.html"): "2025-02-22",
}

MANUAL_LIVERESULTAT_COMPS = {
    4233: [30654],         # Vienna O Challenge 2024, Etappe 1 (2024-08-30)
    4440: [30655],         # Vienna O Challenge 2024, Etappe 2 (2024-08-31)
    4292: [30957, 30657],  # Vienna O Challenge & Sprint Relay 2024, Etappe 3 + relay (2024-09-01)
}

# Same idea as MANUAL_PDF_OVERRIDES, but the organizer's own results page is
# itself an HTML export in the same liveresultat-style table layout (see
# parse_bracket_html() in parse_sportsoftware_html.py) - no API needed, just
# the page. Earlier VOC editions than 2024's didn't route through
# liveresultat's API at all, so MANUAL_LIVERESULTAT_COMPS doesn't apply here.
# Deliberately excludes the "-total" 3-day combined-standings page, which
# would just duplicate each day's own results under one cumulative ranking.
MANUAL_HTML_OVERRIDES = {
    3340: [("https://viennaochallenge.com/voc22-1-results", "voc22-1-results.html")],
    3816: [("https://viennaochallenge.com/voc22-2-results", "voc22-2-results.html")],
    3474: [("https://viennaochallenge.com/voc22-3-results", "voc22-3-results.html"),
           ("https://viennaochallenge.com/relay22-results", "relay22-results.html")],
}

CAT_RE = re.compile(r"^(?P<name>.+?)\s+\((?P<starters>\d+)\)\s*$")
# same, but for formats (PDF, fixed-width text) where course info trails the
# category on the same line: "H21-Wien (21) 7.8 km 280 Hm 27 P". Also tolerates
# a "(finished/entered" count and a missing close paren, as in "Ultimate (35/
# Preliminary results 21:45".
CAT_LINE_RE = re.compile(r"^(?P<name>.+?)\s+\((?P<starters>\d+)(?:/\d*)?\)?\s*(?P<rest>.*)$")

# English-locale SportSoftware exports use different column headers
COLUMN_ALIASES = {"Time": "Zeit", "Club": "Verein", "YB": "Jg",
                  "Stno": "Stnr", "Runner": "Name", "Pos": "Pl", "Place": "Pl"}
COURSE_RE = re.compile(r"(?:(?P<km>[\d.,]+)\s*km)?\s*(?:(?P<climb>\d+)\s*Hm)?")
CONTROLS_RE = re.compile(r"(\d+)\s*P\b")
TIME_RE = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{2})$")
JUNK_NAME_RE = re.compile(r"^[\d\s:.,()/-]*$")
JUNK_NAMES = {"empty", "vacant", "leer", "frei"}

# German status strings SportSoftware prints in the time column
STATUS_MAP = {
    "aufg": "dnf", "aufgegeben": "dnf",
    "fehlst": "mp", "fehlstempel": "mp",
    "disq": "dsq", "disqualifiziert": "dsq",
    "n. angetr.": "dns", "n.angetr.": "dns", "nicht angetreten": "dns",
    "n ang": "dns", "nicht ang": "dns",
    "ohne wertung": "ok", "außer konkurrenz": "ok", "ausser konkurrenz": "ok",
    "wertungsfrei": "ok", "ak": "ok",
    "dnf": "dnf", "dns": "dns", "dsq": "dsq", "mp": "mp",
}


def parse_time(text):
    """'26:21' -> seconds; '1:02:33' -> seconds; else None."""
    m = TIME_RE.match(text.strip())
    if not m:
        return None
    h, mi, s = m.groups()
    return (int(h or 0)) * 3600 + int(mi) * 60 + int(s)


TIME_TOKEN_RE = re.compile(r"\d{1,3}:\d{2}(?::\d{2})?")


def parse_time_loose(text):
    """Like parse_time but tolerates a trailing marker SportSoftware appends to
    some times, e.g. '19:24 (*)' or '22:46 (+)' (note / twilight flags)."""
    s = parse_time(text)
    if s is not None:
        return s
    m = TIME_TOKEN_RE.search(text or "")
    return parse_time(m.group()) if m else None


def parse_status(text):
    t = text.strip().lower().rstrip(".")
    for key, val in STATUS_MAP.items():
        if key in t:
            return val
    return None


def is_ooc_status(text):
    return bool(re.search(
        r"(?:^|\b)(?:AK|au(?:ß|ss)er konkurrenz|ohne wertung|wertungsfrei)(?:\b|$)",
        text or "", re.I))


# SportSoftware announces the Austrian champion on the winner's own row in
# place of a plain rank number, e.g. "1. und Österr.Meister 2022" or "1. und
# Staatsmeister 2016" - the only place this survives in a legacy export at
# all. It isn't always rank 1: if the fastest finisher is a foreign guest or
# otherwise ineligible, the title goes to the highest-placed eligible runner
# instead ("2. und Österr.Meisterin 2022").
#
# 'Staatsmeister' (ÖSTM, the "real" national championship) only exists for
# the near-elite/elite categories; 'Meister' qualified by "Österr(eichisch)"
# (ÖM) spans many age categories. Both are easily confused with a same-
# shaped regional title - Landesmeister, Bezirksmeister, Stadtmeister,
# Bundesmeisterschaft (a schools championship, distinct from the ÖFOL one),
# or a state adjective like "Steirische"/"NÖ"/"Wiener" - which must NOT
# count. \b anchors "österr" to a genuine word start so a compound regional
# name like "Niederösterreichischer Meister" (Lower Austria, a real title
# but not the national one) doesn't false-positive just because it contains
# "österreich" as a substring: there is no word boundary between "Nieder"
# and "österreichischer" since they're fused into one word.
CHAMPION_ANNOT_LEAD_RE = re.compile(r"(?i)^(\d+)\.?\s*und\s+(.+)$")
# A layout where the announcement occupies its own table row entirely -
# real name/club/time sit on the FOLLOWING row instead, which has no rank
# of its own - so there's no "und" connecting the rank to anything, just
# "1 Österreichischer Staatsmeister" standing alone (word-processor line-
# wrap even split it across two table cells, "Österreichischer" landing in
# the Name column and "Staatsmeister" in Verein, joined back into one
# string by the caller before this ever sees it). Anchored at both ends
# ($ included) so it only matches when the title is truly the row's ONLY
# content - never a prefix that could eat into a real name that happens to
# start the same way. Confirmed real: event 4316 ("11. Austria Cup
# (Mitteldistanz, WRE)"), "Herren ab 21 Elite" - Jannis Bonek's real ÖSTM
# gold row lost its rank entirely to a phantom "Österreichischer"/
# "Staatsmeister" competitor.
CHAMPION_ANNOT_ALONE_RE = re.compile(
    r"(?i)^(\d+)\.?\s+(öster(?:r|reich\w*)?\.?\s+(?:staats?)?meister(?:in(?:nen)?)?)\s*$")
STAATSMEISTER_RE = re.compile(r"(?i)\bstaats?meister")
# "Österr." (double r) and "Öster." (single r) both appear as abbreviations
# for "österreichisch" in the wild, alongside the unabbreviated word.
OM_RE = re.compile(r"(?i)\böster(?:r|reich\w*)?\.?\s*meister")
# The already-abbreviated "ÖM"/"ÖSTM" forms - confirmed real: event 3825's
# relay PDF prints its team-row champion announcement as the bare
# abbreviation ("1 und ÖM Naturfreunde Wien 1 35:06"), never the spelled-out
# "Meister" word STAATSMEISTER_RE/OM_RE were built for.
OSTM_ABBR_RE = re.compile(r"(?i)\bö\(?st\)?m\b")
OM_ABBR_RE = re.compile(r"(?i)(?<![a-zäöüß])öm(?![a-zäöüß])")
# A genuine announcement carries no time value of its own - it just replaces
# the winner's rank number, on its own line/cell. One PDF export (a 2013
# relay event) instead embeds it mid-row alongside the team's real Stnr/name/
# time ("1 und österr. Meister 31 Naturfreunde Wien 53:04"); matching that
# greedily would swallow and discard the team's own data. A time token is the
# tell: bail out and let normal row parsing handle a line that has one.
TIME_TOKEN_IN_ANNOT_RE = re.compile(r"\d{1,3}:\d{2}")


def classify_championship_text(text):
    """'Österr.Meister'/'Österreichischer Meister' -> 'ÖM', 'Staatsmeister'
    (with or without an 'österreichischer' qualifier) -> 'ÖSTM', anything
    else (regional/other title, or no title at all) -> None."""
    if not text:
        return None
    if STAATSMEISTER_RE.search(text) or OSTM_ABBR_RE.search(text):
        return "ÖSTM"
    if OM_RE.search(text) or OM_ABBR_RE.search(text):
        return "ÖM"
    return None


def parse_champion_annotation(text):
    """Split a champion-annotation cell/line ('1. und Österr.Meister 2022',
    or the standalone-row form 'CHAMPION_ANNOT_ALONE_RE' matches) into
    (rank, championship). Returns (None, None) if `text` isn't one - the
    rank is recovered regardless of whether the title itself turns out to
    be a recognized national one, since this text otherwise replaces the
    plain rank number entirely and the row would lose its placement. Also
    (None, None) if a time token follows the 'und' form - see
    TIME_TOKEN_IN_ANNOT_RE (the standalone form never has one to begin
    with, by construction of its own regex)."""
    t = text.strip()
    m = CHAMPION_ANNOT_LEAD_RE.match(t)
    if m and not TIME_TOKEN_IN_ANNOT_RE.search(m.group(2)):
        return int(m.group(1)), classify_championship_text(m.group(2))
    m = CHAMPION_ANNOT_ALONE_RE.match(t)
    if m:
        return int(m.group(1)), classify_championship_text(m.group(2))
    return None, None


# Yet another PDF layout (newer OE12 exports) gives the marker its own
# "ÖStM"/"ÖM" header columns rather than replacing the rank - a table-aware
# parser reads that cleanly (see parse_sportsoftware_pdf's column path), but
# a text/flow parser with no notion of columns just sees it prefixed onto
# the name field itself: "Österr. Staatsmeister Andreas Waldmann".
CHAMPION_NAME_PREFIX_RE = re.compile(
    r"(?i)^(öster(?:r|reich\w*)?\.?\s+(?:staats?)?meister(?:in)?)\s+(?=\S)")


def strip_champion_name_prefix(name):
    """(clean_name, championship) - championship is None and name unchanged
    if there's no marker prefix to strip."""
    m = CHAMPION_NAME_PREFIX_RE.match(name)
    if not m:
        return name, None
    return name[m.end():], classify_championship_text(m.group(1))


def detect_list_type(file_name, doc_text, is_sole_attachment=False):
    """Relay lists parse poorly as tables; cumulative multi-day standings and
    split-time reports shouldn't count as a single race. Classify by the file
    name, not the document head — a relay/team event often also ships an
    individual result file whose head still mentions 'Staffel'.

    'gesamt' ("overall/combined") is ambiguous: it usually means a cumulative
    multi-race series standings sheet (redundant with each race's own
    results), but SportSoftware also uses it for a single race's own combined
    results across categories (e.g. 'ergebnis-gesamt.pdf' for one sprint-
    series round) - when that file is the event's only attachment, there is
    no other source to fall back on, so treat it as this race's own results
    rather than silently discarding the entire event."""
    head = doc_text[:4000]
    # "split" (English) is as common a filename marker for a split-times
    # report as the German "zwischenzeit" - confirmed real: event 4515's
    # "split-teame-slit.html" (a typo'd "team-split") was never recognized,
    # so it got parsed as a second, real 'race' file instead of skipped as
    # redundant - a split-times report's own per-control layout garbles
    # names badly enough to invent phantom extra "teams" out of name
    # fragments ("Berger H", "Berger Adenstedt" alongside the real "Ingrid
    # Adenstedt"/"Gislind Berger"/"Hedi Berger"), corrupting the medal
    # count for that category. Checked ahead of the "staffel|relay" filename
    # check below, since a split-times file can ALSO have "staffel" in its
    # name (event 3824's "...-relind-result-splits.pdf") and must still be
    # recognized as redundant, not misrouted to the relay parser instead.
    if re.search(r"zwischenzeit|split", file_name, re.I) or re.match(r"\s*\S*\s*Zwischenzeiten", head):
        return "overall"                       # split-times report, redundant
    if re.search(r"einzel", file_name, re.I):
        return "race"                          # individual results within a Staffel event
    if re.search(r"staffel|relay", file_name, re.I):
        return "relay"
    if (re.search(r"gesamt", file_name, re.I) or "Gesamtwertung" in head) and not is_sole_attachment:
        return "overall"
    return "race"


FILENAME_DATE_RE = re.compile(r"erg(\d{2})(\d{2})(\d{2})(?!\d)", re.I)
DOC_DATE_RE = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(20\d{2})\b")
HTML_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)


def extract_html_title(html_text):
    """SportSoftware's own <title> often names the exact championship a
    legacy multi-race weekend's ONE stage covers ('... + ÖSTM/ÖM + 3.AC
    Mittel + TM') even when the ANNE event's own title/slug is completely
    generic ('AC Weekend Seefeld') - the event bundles an Austria-Cup
    weekend with an embedded championship day, and only the per-file
    SportSoftware title records which day that was. Confirmed real: event
    3938 ("AC Weekend Seefeld"), Sunday stage title "... + ÖSM/ÖM SkiO
    Middle + TM" - Wolfgang Waldhäusl's bronze there had no other way to
    be recognized as a championship placing at all."""
    m = HTML_TITLE_RE.search(html_text)
    if not m:
        return None
    title = re.sub(r"\s+", " ", html.unescape(m.group(1))).strip()
    return title or None


def guess_doc_date(file_name, doc_text):
    """A multi-day event (e.g. an Austria-Cup weekend: Lang one day, Mittel
    the next) is often ingested as legacy files under a single ANNE event id
    with no per-day structure of its own - build_db.py needs to know which
    calendar day each file belongs to so same-named categories on different
    days ('Herren ab 55' both days) don't collide into one stage and silently
    drop one day's results. SportSoftware's own 'ergDDMMYY...' filename
    convention is the most reliable signal (many exports carry no date in
    their own text at all); a 'DD.MM.YYYY' date printed in the document head
    is the fallback for filenames that don't follow it."""
    m = FILENAME_DATE_RE.search(file_name or "")
    if m:
        d, mo, y = m.groups()
        return f"20{y}-{mo}-{d}"
    m = DOC_DATE_RE.search((doc_text or "")[:500])
    if m:
        d, mo, y = m.groups()
        return f"{y}-{int(mo):02d}-{int(d):02d}"
    return None


def parse_course_info(text):
    """'2,3 km  130 Hm  8 P' -> {courseLengthM, courseClimbM, courseControls}."""
    out = {}
    m = COURSE_RE.search(text)
    if m and m.group("km"):
        out["courseLengthM"] = int(float(m.group("km").replace(",", ".")) * 1000)
    if m and m.group("climb"):
        out["courseClimbM"] = int(m.group("climb"))
    cm = CONTROLS_RE.search(text)
    if cm:
        out["courseControls"] = int(cm.group(1))
    return out


def is_junk_name(name):
    return not name or bool(JUNK_NAME_RE.match(name)) or name.lower() in JUNK_NAMES


PERSON_TOKEN_RE = re.compile(r"^[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ.'’-]*$")


def looks_like_person(name):
    """A plausible person name: 1-4 alphabetic tokens, no digits or markup.
    Guards the '/'-pair split against title/header fragments that also contain
    a slash (e.g. 'Ergebnis - ÖSTM / ÖM 1. Austria Cup 2012')."""
    toks = name.split()
    if not (1 <= len(toks) <= 4):
        return False
    return all(PERSON_TOKEN_RE.match(t) for t in toks)


# ---- multi-runner (pairs) handling for flowing-layout PDFs ----

@lru_cache(maxsize=1)
def load_clubs():
    """Normalized club name -> canonical, from data/clubs.json (build_club_dict.py)."""
    path = Path(__file__).resolve().parent.parent / "data" / "clubs.json"
    if not path.exists():
        return {}
    out = {}
    for c in json.loads(path.read_text()):
        out[re.sub(r"\s+", " ", c).strip().lower()] = c
    return out


def find_trailing_club(tokens, clubs):
    """Longest trailing run of tokens (up to 5) matching a known club name.
    Returns (club_or_None, remaining_leading_tokens)."""
    for k in range(min(5, len(tokens)), 0, -1):
        cand = " ".join(tokens[-k:]).lower()
        if cand in clubs:
            return clubs[cand], tokens[:-k]
    return None, tokens


MEMBER_COL_RE = re.compile(r"^(?:name|l[äa]ufer|runner)\s*\d*$", re.I)


def team_results_from_pairs(pairs, club, rank_text, time_text):
    """Build one team result per member from ordered (header, value) column
    pairs. Handles every SportSoftware team layout seen: 'Name 1/2/3',
    'Name Läufer2 Läufer3', and three identical 'Name' headers (which a dict
    would collapse). Returns None when it isn't a team row (fewer than two
    member columns), so the caller falls back to the individual path."""
    members = []
    for header, val in pairs:
        if MEMBER_COL_RE.match((header or "").strip()):
            v = re.sub(r"\s+", " ", (val or "").replace(",", " ")).strip()
            if v and not is_junk_name(v):
                members.append(v)
    if len(members) < 2:
        return None
    rank = int(rank_text) if rank_text.strip().isdigit() else None
    secs = parse_time_loose(time_text)
    out = []
    for nm in members:
        others = ", ".join(o for o in members if o != nm)
        res = {"name": nm, "club": club, "timeText": time_text, "resultKind": "team",
               "note": "Mannschaft: " + club + (" · mit " + others if others else "")}
        if rank is not None:
            res["rank"] = rank
        if secs is not None:
            res["timeS"], res["status"] = secs, "ok"
        else:
            res["status"] = parse_status(time_text) or "unknown"
        out.append(res)
    return out


# D/H-12 and D/H-14 night-run categories run in pairs; some exports name
# the pair "Firstname1-Firstname2 Lastname1-Lastname2" instead of the more
# common '/'-joined form (confirmed real: event 4315, "Albert-Adam
# Imriska-Imriska" - Albert Imriska + Adam Imriska - and "Matilda-Annina
# Schreiber-Urbanek" - Matilda Schreiber + Annina Urbanek). Unlike '/',
# which never appears in a real single person's name, a hyphen legitimately
# does (Biel-Pretting, Kastner-Jirka, Eibel-Lenane, ... all real surnames
# seen elsewhere in this dataset) - splitting on it unconditionally would
# risk fragmenting a genuine hyphenated name into a fake pair. Gated to
# only these specific pair-run categories keeps that risk at zero: a
# hyphenated INDIVIDUAL name can appear in any category, but two people's
# names glued together with a hyphen in each half only happens here.
PAIR_CATEGORY_RE = re.compile(r"(?i)\bbis\s*1[24]\b")


def expand_pair_result(result, category=None):
    """If a parsed result's name holds a joined pair of clean two-token
    names (run-in-pairs events - '/'-joined generally, or '-'-joined but
    only within a confirmed pair-run category, see PAIR_CATEGORY_RE),
    return one result per runner sharing the club/time/rank, each with a
    'Partner: …' note. Otherwise return [result] unchanged. For HTML/text
    sources where name and club are already column-separated — the
    flowing-PDF path has its own club-anchored handling."""
    name = result.get("name", "")
    if "/" in name:
        names = split_pair_names(name)
    elif category and PAIR_CATEGORY_RE.search(category):
        names = split_hyphenated_pair_names(name)
        if len(names) < 2:
            # No delimiter at all between the two runners, not even a
            # hyphen - just 4 bare space-separated tokens, "Lastname1
            # Firstname1 Lastname2 Firstname2" (confirmed real: event 3851,
            # "ÖM Nacht" 2022 - "Skern Anna Urbanek Annina" for the actual
            # ÖM-champion pair, Anna Skern/Annina Urbanek). Splitting evenly
            # down the middle is safe even though it's a guess: the
            # all(...) validation right below rejects the split back to a
            # single unchanged result unless BOTH halves independently look
            # like a real two-token person name, so a genuine single (non-
            # pair) 4-token name in this category is never wrongly split.
            toks = name.split()
            if len(toks) == 4:
                names = [f"{toks[0]} {toks[1]}", f"{toks[2]} {toks[3]}"]
    else:
        return [result]
    if len(names) < 2 or not all(
            len(n.split()) == 2 and looks_like_person(n) for n in names):
        return [result]
    out = []
    for nm in names:
        r = dict(result)
        r["name"] = nm
        r["resultKind"] = "pair"
        r["note"] = "Partner: " + ", ".join(o for o in names if o != nm)
        out.append(r)
    return out


STATUS_TAIL_RE = re.compile(
    r"(?i)(n\.?\s*ang\.?|nicht angetreten|aufg\.?|fehlst\.?|disq\.?|"
    r"ohne wertung|außer konkurrenz|ausser konkurrenz|wertungsfrei|AK|"
    r"dnf|dns|dsq|mp)\s*$")


def parse_flow_row(text, clubs):
    """Parse one result row from its reconstructed text when fixed columns are
    unavailable (flowing-layout PDFs). Peels rank, start number, trailing
    time/status, then the trailing club (via the club dictionary) and a Jg,
    leaving the name(s) - which may be a pair joined by '/'. Returns a dict or
    None. Only trustworthy when a club or a '/' pair was actually found."""
    toks = text.split()
    if not toks:
        return None
    # peel up to two leading integers (Pl and/or Stnr)
    lead = []
    while toks and toks[0].isdigit() and len(lead) < 2:
        lead.append(toks.pop(0))
    body = toks
    if not body:
        return None

    time_text = status_text = None
    if TIME_RE.match(body[-1]):
        time_text = body[-1]
        body = body[:-1]
    else:
        m = STATUS_TAIL_RE.search(" ".join(body[-3:]))
        if m:
            status_text = m.group(0).strip()
            body = body[: len(body) - len(status_text.split())]

    # a finisher's first leading integer is the rank; a non-finisher (status,
    # no time) has no Pl, so its single leading integer is just the start number
    rank = int(lead[0]) if (time_text is not None and lead) else None

    club, body = find_trailing_club(body, clubs)
    jg = None
    if body and re.fullmatch(r"\d{2}|\d{4}", body[-1]):
        jg, body = body[-1], body[:-1]
    names = split_pair_names(" ".join(body))
    return {"rank": rank, "names": names, "club": club, "jg": jg,
            "timeText": time_text, "statusText": status_text,
            "outOfCompetition": is_ooc_status(status_text)}


def split_pair_names(name_text):
    """'Kasper Matilda / Hoffmann Marlene' -> two names. 'Hnilica Hannes/Sonja'
    (shared surname, second part is a lone forename) -> ['Hnilica Hannes',
    'Hnilica Sonja']. SportSoftware uses Lastname-Firstname order here, so the
    shared surname is the first token.

    A distinct convention, seen in Firstname-Lastname-ordered exports of
    run-in-pairs youth categories (D/H-12, D/H-14 night-run relays):
    'Firstname1/Firstname2 Lastname1/Lastname2', e.g. 'Jannis/Marie
    Binder/Egger' -> ['Jannis Binder', 'Marie Egger']. Unambiguous versus the
    convention above because that one never has a '/' in its first
    whitespace-separated group."""
    groups = name_text.split()
    if len(groups) == 2 and groups[0].count("/") == 1 and groups[1].count("/") == 1:
        firsts = groups[0].split("/")
        lasts = groups[1].split("/")
        return [f"{f} {l}" for f, l in zip(firsts, lasts)]
    parts = [p.strip() for p in re.split(r"\s*/\s*", name_text) if p.strip()]
    if len(parts) <= 1:
        return parts
    surname = parts[0].split()[0] if parts[0].split() else ""
    out = [parts[0]]
    for p in parts[1:]:
        out.append(f"{surname} {p}" if (len(p.split()) == 1 and surname) else p)
    return out


def split_hyphenated_pair_names(name_text):
    """'Albert-Adam Imriska-Imriska' -> ['Albert Imriska', 'Adam Imriska'];
    'Matilda-Annina Schreiber-Urbanek' -> ['Matilda Schreiber', 'Annina
    Urbanek']. Firstname-Lastname-ordered 'Firstname1-Firstname2
    Lastname1-Lastname2' convention, hyphen-joined instead of '/'-joined.
    Only called for confirmed pair-run categories (see PAIR_CATEGORY_RE) —
    a real hyphenated surname elsewhere must never reach here."""
    groups = name_text.split()
    if len(groups) == 2 and groups[0].count("-") == 1 and groups[1].count("-") == 1:
        firsts = groups[0].split("-")
        lasts = groups[1].split("-")
        return [f"{f} {l}" for f, l in zip(firsts, lasts)]
    return [name_text]
