#!/usr/bin/env python3
"""Compile raw ANNE snapshots + normalized legacy results into site/data/results.db.

Person identity: ANNE userId is authoritative (positive ids). Legacy results
without a userId are matched to existing persons by (normalized name, year of
birth) and otherwise get synthetic negative ids. Derived statistics (starters,
classified count, winner time) are computed in the category_stats view, never
stored.
"""
import csv
import gzip
import hashlib
import json
import os
import re
import sqlite3
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw" / "anne"
NORM = ROOT / "data" / "normalized"
DB_PATH = ROOT / "site" / "data" / "results.db"

SCHEMA = """
CREATE TABLE event (
    id INTEGER PRIMARY KEY,
    slug TEXT, title TEXT, short_title TEXT,
    date_from TEXT, date_to TEXT,
    location TEXT, country TEXT NOT NULL DEFAULT 'AUT',
    coordinates TEXT,
    competition_type TEXT, sport_type TEXT, event_type TEXT,
    url TEXT
);
CREATE TABLE stage (
    id INTEGER PRIMARY KEY,
    event_id INTEGER NOT NULL REFERENCES event(id),
    number INTEGER NOT NULL DEFAULT 1,
    title TEXT, date TEXT, location TEXT
);
CREATE TABLE person (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    name_key TEXT NOT NULL,
    year_of_birth INTEGER, nationality TEXT, iof_id TEXT
);
CREATE TABLE person_identifier (
    scheme TEXT NOT NULL,             -- anne_user_id|oefol_id|iof_id|club_internal
    identifier TEXT NOT NULL,
    person_id INTEGER NOT NULL REFERENCES person(id),
    verified INTEGER NOT NULL DEFAULT 0,
    verification_source TEXT NOT NULL,
    PRIMARY KEY (scheme, identifier, person_id)
);
CREATE TABLE person_alias (
    person_id INTEGER NOT NULL REFERENCES person(id),
    name TEXT NOT NULL,
    name_key TEXT NOT NULL,
    source TEXT NOT NULL,
    verified INTEGER NOT NULL DEFAULT 0,
    occurrences INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (person_id, name, source)
);
CREATE TABLE person_redirect (
    old_id INTEGER PRIMARY KEY,
    new_id INTEGER NOT NULL REFERENCES person(id)
);
CREATE TABLE source_document (
    id TEXT PRIMARY KEY,
    event_id INTEGER NOT NULL REFERENCES event(id),
    source_type TEXT NOT NULL,
    source_url TEXT,
    file_name TEXT,
    snapshot_path TEXT,
    snapshot_sha256 TEXT,
    normalized_path TEXT,
    normalized_sha256 TEXT,
    parser_version TEXT
);
CREATE TABLE result (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stage_id INTEGER NOT NULL REFERENCES stage(id),
    person_id INTEGER NOT NULL REFERENCES person(id),
    category TEXT NOT NULL,
    category_full TEXT,
    club TEXT,
    official_club TEXT,               -- club canonicalized to ANNE's /v1/club
                                       -- registry, for the Vereine section only
    rank INTEGER,
    status TEXT NOT NULL,            -- ok|dnf|dsq|mp|dns|nc|unknown
    time_s INTEGER,
    time_behind_s INTEGER,
    out_of_competition INTEGER NOT NULL DEFAULT 0,
    course_length_m INTEGER, course_climb_m INTEGER, course_controls INTEGER,
    result_kind TEXT NOT NULL DEFAULT 'individual',  -- individual|pair|relay
    note TEXT,                       -- e.g. "Partner: X" / "Staffel Y, Leg N"
    source TEXT NOT NULL,            -- anne-api|sportsoftware-html|...
    source_document_id TEXT REFERENCES source_document(id),
    observed_name TEXT,              -- source spelling before canonical identity resolution
    observed_club TEXT,              -- source spelling before club canonicalization
    observed_user_id TEXT,           -- source-supplied identity, never inferred
    identity_basis TEXT NOT NULL DEFAULT 'unknown',
    identity_confidence REAL NOT NULL DEFAULT 0.0,
    championship TEXT,               -- ÖM|ÖSTM, when this (stage, category)
                                      -- is a genuine Austrian championship
    national_rank INTEGER            -- placement among ONLY championship-
                                      -- eligible (Austrian) finishers, which
                                      -- can differ from the overall race
                                      -- `rank` when a foreign/ineligible
                                      -- competitor placed ahead - see the
                                      -- national-rank computation in main()
);
CREATE INDEX idx_result_person ON result(person_id);
CREATE INDEX idx_result_stage_cat ON result(stage_id, category);
CREATE INDEX idx_result_official_club ON result(official_club);
CREATE INDEX idx_person_name ON person(name_key);
CREATE INDEX idx_result_source_document ON result(source_document_id);
CREATE INDEX idx_person_alias_key ON person_alias(name_key);
CREATE INDEX idx_person_identifier_value ON person_identifier(scheme, identifier);
CREATE VIEW category_stats AS
SELECT stage_id, category,
       COUNT(*)                                       AS starters,
       SUM(status = 'ok')                             AS classified,
       MIN(CASE WHEN rank = 1 THEN time_s END)        AS winner_time_s
FROM result
WHERE status != 'dns' AND result_kind != 'relay'  -- relay leg times aren't comparable
GROUP BY stage_id, category;
"""

ANNE_STATUS = {
    "classified": "ok",
    "notClassified": "nc",
    "didNotFinish": "dnf",
    "disqualified": "dsq",
    "missingPunch": "mp",
    "didNotStart": "dns",
}

CLUB_JUNK_PREFIX_RE = re.compile(r"^(?:empty|leer|vacant|frei|\.)\s+", re.I)


def clean_club(name):
    """ANNE's own API sometimes concatenates an empty team/school-name field
    with the real club name, leaking a placeholder prefix through
    ('empty Naturfreunde Wien', '. OL Kufstein') - confirmed straight from
    clubName in the raw API response, not something our own parsing adds."""
    if not name:
        return name
    return CLUB_JUNK_PREFIX_RE.sub("", name).strip()


# ANNE's structured API tags every result row with its own 'championship'
# list, keyed by championshipShortName - not just ÖM/ÖSTM (the genuine
# Austrian Championship / Staatsmeisterschaft) but also regional/other ones
# (BMS, NÖ MS, NÖ LMS, Stadt-MS, SBG LMS, STM) that must NOT count here.
NATIONAL_CHAMPIONSHIPS = {"ÖM", "ÖSTM"}


def anne_championship(row):
    for c in (row.get("championship") or []):
        sn = c.get("championshipShortName")
        if sn in NATIONAL_CHAMPIONSHIPS:
            return sn
    return None


def anne_user_id(value):
    """Normalize ANNE user IDs to SQLite INTEGERs.

    Individual result rows currently use JSON numbers while teamMembers mostly
    use numeric strings.  Treating those as different key types fragments the
    same authoritative identity inside PersonRegistry.
    """
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def stable_synthetic_id(name, yob, salt=0):
    """Deterministic negative id for a legacy-only identity.

    It is based on the normalized source identity rather than encounter order,
    so adding an unrelated result no longer renumbers public runner URLs.
    """
    payload = f"olresults-person-v1\0{name_key(name)}\0{yob or ''}\0{salt}".encode()
    value = int.from_bytes(hashlib.sha256(payload).digest()[:8], "big") & ((1 << 62) - 1)
    return -(value + 1)


# Fallback for legacy events where no result row anywhere carries a champion
# annotation to detect at all (confirmed by hand: several real ÖM/ÖSTM-titled
# exports simply never print one) - the event's own title is the only signal
# left. ÖM = Österreichische Meisterschaft, ÖSTM = Österreichische
# Staatsmeisterschaft; a title spelling out both ("ÖSTM/ÖM ...", "ÖSTM &
# ÖM ...") or the parenthetical "Ö(ST)M" form grants both at once, while a
# title with only one grants only that one - confirmed against events that
# *do* have per-row annotations (e.g. "ÖSTM Mittel (6.AC Mittel)" only ever
# tags the Elite category ÖSTM, never ÖM elsewhere in that same race).
# Both abbreviations are sometimes hyphen/en-dash separated in an authored
# title ("Ö-M", "Ö–M-Nacht", "Ö–STM" - confirmed real: event 2274's ANNE
# stage title '"Night-Race". Ö–M-Nacht, 9.AC' and event 2422's 'Ö–STM, Ö–M,
# 6.AC (mittel)'), so an optional single separator right after the Ö is
# allowed; the boundaries still keep them from firing inside an ordinary word.
OESTM_TITLE_RE = re.compile(r"(?i)ö[–-]?\(?st\)?m")
OM_TITLE_RE = re.compile(r"(?i)(?<![a-zäöüß])ö[–-]?m(?![a-zäöüß])")
COMBINED_TITLE_RE = re.compile(r"(?i)ö\(st\)m")
# The championship can also be spelled out in full or ASCII-transliterated in
# an authored title/stage name, not just abbreviated - a per-stage title like
# "Österreichische Meisterschaft/9.Austriacup Langdistanz" or "4. AC + OEM
# Nachwuchs Sprint" must classify the same as its "ÖM"/"ÖSTM" shorthand, or
# the per-stage precedence in apply_title_championship_fallback can't tell a
# genuine championship stage apart from a plain Austria-Cup one. "Öster..."
# / "Staats..." prefixes are required so a regional "Wiener/NÖ/Landes-
# meisterschaft" never matches; the ASCII "oem"/"oestm" tokens are hyphen/
# word-bounded so they don't fire inside an unrelated word.
OESTM_SPELLED_RE = re.compile(r"(?i)\bstaats?meister|(?<![a-z0-9])oe?[–-]?stm(?![a-z0-9])")
OM_SPELLED_RE = re.compile(r"(?i)\böster(?:r|reich\w*)?\.?\s*meister|(?<![a-z0-9])oe[–-]?m(?![a-z0-9])")

# ANNE's own event-URL slug generator strips diacritics down to their plain
# ASCII base letter (ö -> o) rather than expanding them, confirmed
# consistently across many real slugs ("ÖSTM/ÖM Lang" -> "...-ostmom-...",
# "ÖSTM Sprint" -> "ostm-sprint", "SkiO ÖM/ÖStM..." -> "...-om-ostm-...") -
# so a slug carries "ÖM"/"ÖSTM" as plain, hyphen-bounded "om"/"ostm" tokens
# with no umlaut at all, never matching the title regexes above. A separate,
# ASCII-only pair used only against slugs, never against the title itself -
# that's authored text and always keeps its real umlauts, so applying this
# looser match there would only add false-positive risk for no benefit.
OESTM_SLUG_RE = re.compile(r"(?i)(?<![a-z0-9])ostm(?![a-z0-9])")
OM_SLUG_RE = re.compile(r"(?i)(?<![a-z0-9])om(?![a-z0-9])")


def classify_title_championships(title, slug=None):
    if not title:
        return set()
    if COMBINED_TITLE_RE.search(title):
        return {"ÖM", "ÖSTM"}
    types = set()
    if OESTM_TITLE_RE.search(title) or OESTM_SPELLED_RE.search(title):
        types.add("ÖSTM")
    if OM_TITLE_RE.search(title) or OM_SPELLED_RE.search(title):
        types.add("ÖM")
    if slug:
        if OESTM_SLUG_RE.search(slug):
            types.add("ÖSTM")
        if OM_SLUG_RE.search(slug):
            types.add("ÖM")
    return types


# Which categories are actually eligible, learned from every category that a
# real per-row annotation already confirmed (see the accompanying research):
# ÖSTM only ever lands on an Elite/near-elite category (D21E/H21E, D19-/H19-,
# "Allgemeine Klasse", "Staatsmeisterschaft Damen/Herren"); ÖM spans ordinary
# age classes starting at the "12 and under" bracket (D-12/H-12/D12 etc.) -
# never younger, and never non-competitive groupings (Bahn course listings,
# Neulinge/Familie/Hobby fun categories, school Mannschaft rosters, ...).
# The digit+E marker ("21E", "H21-E") needs a lookbehind that rejects only
# a preceding DIGIT (not a preceding letter): a gender-prefixed category
# often has no separator at all between the letter and the age ("M21E"),
# so a stricter "(?<![a-zäöü0-9])" lookbehind (rejecting any preceding
# letter too) silently never matches that shape - confirmed real: event
# 4220 ("3. AC Sprint (ÖM Sen.)"), where the senior Elite category
# "H21-E" was never recognized as Elite at all, so Jannis Bonek's ordinary
# ÖM-eligibility check let a category that should be ÖSTM-only through as
# plain ÖM. The optional hyphen ("21-E" vs "21E") covers both spellings
# seen in the data.
ELITE_CAT_RE = re.compile(
    r"(?i)\belite\b|allgemeine\s*klasse|staatsmeisterschaft|"
    r"(?<!\d)(1[6-9]|2[01])-?e(?![a-zäöü0-9])")
SPECIAL_OM_CAT_RE = re.compile(r"(?i)^allgemein$|mixed\s+(jugend|masters)")
CAT_AGE_NUM_RE = re.compile(r"(?<!\d)(\d{1,3})(?!\d)")  # isolated 1-3 digit
# numbers only, so a bare \d{1,3} scan doesn't fragment a 4-digit year
# ("2025" -> "202"+"5") into a bogus, wildly-too-young age match
EXCLUDE_CAT_RE = re.compile(
    r"(?i)\bbahn\b|neuling|familie|ultimate|hobby|schnupper|mannschaft|"
    r"\bak\b|gesamt(?!alter)|anf[aä]nger|\bdirekt\b|ohne\s*wertung|training|"
    r"einsteiger|jedermann|fun\b|\bkurz\b|\boffen\b|"
    # knock-out sprint qualification/consolation rounds ("H21-E -
    # Viertelfinale 5", "... Halbfinale B", "H55- - B-Finale"): none of
    # these are the real national ranking - only the event's own bare
    # "... - Finale" category (the A-final; unaffected by this pattern,
    # since "b-finale" requires the leading "b-") is. Confirmed real: event
    # 4792 wrongly picked up "2nd in Halbfinale B" as an ÖM medal once title
    # fallback started gating per category instead of per whole stage (see
    # apply_title_championship_fallback), and event 4254 ("ÖM KO-Sprint",
    # 2024-05-11) wrongly gave 2nd-in-the-B-Finale (the consolation bracket
    # for those already eliminated from medal contention) a silver medal.
    # "Viertelfinal" also appears without the trailing "e" when a letter
    # designator follows directly ("H21-E–Viertelfinal A") - German drops
    # the adjectival ending there; "viertelfinale" alone missed this shape
    # (confirmed real: event 4254, Jannis Bonek's real quarterfinal-heat
    # win at "H21-E–Viertelfinal A" wrongly counted as a gold).
    r"viertelfinal|halbfinale|b-finale|"
    # "D21-K"/"D21-L"/"H21-K"/"H21-L"/"D 18-K"/"H 18-K" etc: an abbreviated
    # Kurz/Lang course-length split of the open (non-Elite) adult class -
    # the exact same distinction as the already-excluded full-word "kurz"/
    # "lang" category names, just abbreviated to one letter after the age
    # number. Confirmed real: event 4245 ("ÖM Mittel"), where "D21-K"'s
    # plain #1 finisher (no per-row championship annotation in the source
    # HTML at all) wrongly picked up an ÖM gold via title fallback, since
    # neither EXCLUDE_CAT_RE nor ELITE_CAT_RE recognized the single-letter
    # suffix as meaning the same thing as spelled-out "Kurz"/"Lang".
    r"\d-[kl]\b")
# D/H-15-18, D/H-21-Kurz, D/H-21-Lang (every spelling/spacing variant
# found: "21K", "21-K", "21 Kurz", "H-21Kurz", "15-18", "ab 15 bis 18",
# ...) all overlap the real championship age ladder (-12,-14,-16E,-18E,
# -20E,21E,35-,40-,...,80-) without being part of it - confirmed by direct
# user instruction to always exclude them, backed by the event 4315
# Ausschreibung (explicitly lists D/H15-18, D/H21K, D/H21L under "Austria
# Cup" not "Meisterschaftskategorien") and dozens of already-tagged rows
# using these shapes, going back to 2007, that disagree with the club's
# own medal records. Kept as a standalone regex (not folded into
# EXCLUDE_CAT_RE) since it also needs to strip any REAL per-row
# annotation using these category shapes, not just gate the title
# fallback - see strip_age_overlap_categories(). The lookbehind rejects a
# preceding DIGIT but allows a preceding LETTER ('H21K' has no separator
# between the gender letter and '21'), mirroring ELITE_CAT_RE's fix for
# the same reason.
AGE_OVERLAP_EXCLUDE_RE = re.compile(
    r"(?i)(?<!\d)21\s*-?\s*(?:k(?:urz)?|l(?:ang)?)\b|"
    r"(?<!\d)15\s*-\s*18\b|\bab\s*15\s*bis\s*18\b")
# A "Jugend"-scoped title (confirmed real: event 4434, "5.AC" / slug
# "5-ac-om-jugend-sprint-alpen-adria-cup") restricts a title-fallback
# championship to youth categories only - it must not spill onto an
# unrelated senior/masters bracket that happens to also clear the >=12 age
# floor (Peter Bonek wrongly got a 2024 "Herren ab 60" ÖM gold from this
# event; the club's own records have no such medal for him there). Many
# other events instead say "Jugend UND Senioren" (both groups titled) -
# checked for loosely via a bare "sen" substring rather than a word-
# boundary match, since slug generation truncates "senioren"
# unpredictably (event 145: "...om-jugendundsen-6-austriacup..." with no
# separating hyphen at all) - so this only fires when "jugend" appears
# with no "sen" anywhere in the combined title+slug text.
#
# The event's own Ausschreibung (announcement PDF) for 4434 spells out the
# exact eligible set: "Österr. Meisterschaft: D/H-12, D/H-14, D/H-16E,
# D/H-18E, D/H-20E" - the fixed youth "bis NN[E]" ladder only. Notably
# NOT included: "D/H-10" (already excluded elsewhere by the >=12 age
# floor) and, less obviously, "D/H15-18" - a bounded non-Elite grouping
# ("ab 15 bis 18") that's Austria-Cup-only there despite covering youth
# ages, alongside the equivalent adult non-Elite splits ("ab 21 Kurz/
# Lang"). Every category on the confirmed ladder is named "bis NN[E]"
# (no "ab"); every excluded one - senior brackets and this bounded
# youth-non-Elite grouping alike - is named "ab NN[...]". So excluding
# anything with an "ab NN" clause, bounded or not, exactly reproduces the
# Ausschreibung's set without needing to special-case the bounded form.
JUGEND_ONLY_RE = re.compile(r"(?i)jugend")
AB_AGE_RE = re.compile(r"(?i)\bab\s*\d+\b")
SENIOR_ONLY_RE = re.compile(r"(?i)\bsen(?:ioren)?\b")
YOUTH_MARKER_RE = re.compile(r"(?i)jugend|nachwuchs|junioren")


def category_min_age(category):
    nums = [int(n) for n in CAT_AGE_NUM_RE.findall(category)]
    return min(nums) if nums else None


def is_om_eligible_category(category):
    if EXCLUDE_CAT_RE.search(category):
        return False
    # some exports name the category after the championship itself
    # ("ÖSTM SKI-O Mittel 2025 Damen") rather than splitting by age at all -
    # trust that label directly rather than falling through to the age
    # heuristic, which has nothing to extract from a category like that
    types = classify_title_championships(category)
    if types:
        return True
    age = category_min_age(category)
    # The senior/open Elite bracket (D21E/H21E, "ab 21 Elite", "Allgemeine
    # Klasse", "Staatsmeisterschaft") is deliberately excluded here, even
    # though its age (21, or unstated for the name-only variants) would
    # otherwise clear the >= 12 floor below: that bracket can only ever earn
    # ÖSTM, never plain ÖM - a title-fallback event whose title carries ÖM
    # but not ÖSTM (e.g. "7.AC ÖM Mitteldistanz") still must not tag it ÖM
    # (see is_ostm_eligible_category for the ÖSTM side). Junior "bis 16/18/20
    # Elite" brackets are unaffected - they're genuinely ÖM-eligible - since
    # only the age-21-or-unstated (name-only) case is treated as senior here.
    if ELITE_CAT_RE.search(category) and (age is None or age >= 21):
        return False
    if SPECIAL_OM_CAT_RE.search(category):
        return True
    return age is not None and age >= 12


def is_ostm_eligible_category(category):
    if EXCLUDE_CAT_RE.search(category):
        return False
    if "ÖSTM" in classify_title_championships(category):
        return True
    age = category_min_age(category)
    # Same senior-only restriction as is_om_eligible_category's ELITE_CAT_RE
    # check, mirrored: a bare "Elite" match must not sweep in a YOUTH Elite
    # bracket ("Damen bis 16 Elite") just because the literal word is
    # present - confirmed false by hand (event 4837, "ÖSTM Mittel am 14.9.":
    # the excel record's only medals there are D21E/H21E, no "bis 16 Elite"
    # at all). age is None for the genuinely senior, un-aged spellings
    # ("Allgemeine Klasse", "Staatsmeisterschaft Damen/Herren"), so those
    # still pass.
    if ELITE_CAT_RE.search(category) and (age is None or age >= 21):
        return True
    # every real ÖSTM category confirmed by per-row detection is exactly age
    # 19 or 21 (D19-/H19-, D21E/H21E and the like) - not a range, because a
    # nearby age this dataset actually uses for a *different*, ÖM-only
    # bracket would otherwise slip in too: Ski-O's "bis 20"/"H-20" near-elite
    # youth class (confirmed ÖM, not ÖSTM, by real per-row detection at
    # event 4894) has age 20, which a bare 19-21 range would wrongly catch.
    # An upper bound matters at all (vs. a bare ">= 19") because a *relay*
    # category can be named after the TEAM's combined age ("Damen ab 120" -
    # three runners summing to 120+, a masters division, confirmed real but
    # ÖM not ÖSTM) rather than an individual's.
    return age in (19, 21)


# Events whose title claims ÖM/ÖSTM but whose actual attached results don't
# back it up - confirmed by hand, not something classify_title_championships
# can tell from the title alone. Event 4783 ("ÖM Nacht und 1.AC (Sprint)")
# was a two-day meet; only the non-championship 1.AC Sprint day produced any
# results at all, the ÖM Nacht half never happened / was never published.
#
# Event 3999 ("Austrian Finals - AC - Sprint") is the KO-Sprint's own
# QUALIFICATION round, not a championship result in its own right - its
# slug ("...qualifikation-om-knock-out-sprint") only mentions ÖM because
# it's naming the event it qualifies INTO, and the attached PDF's own
# title confirms this plainly ("Qualifikation-Graz"). The real ÖM medals
# for every one of these same age brackets already live at event 3998
# ("Austrian Finals - ÖM-Knock-Out-Sprint") under its proper "X A-Finale"
# categories (with "B-Finale"/qualifying heats correctly excluded there) -
# 3999's plain, non-bracket "Damen ab 35" etc. rows are just the qualifying
# heat times feeding into that final, confirmed real after the user flagged
# this file directly ("is no ÖM!").
TITLE_FALLBACK_EXCLUDE_EVENTS = {4783, 3999}

# Same idea, but for a two-day meet where only ONE day is the actual
# championship and the other genuinely has its own separate, unrelated
# results published - the event-level title/slug can't express that split,
# since classify_title_championships/apply_title_championship_fallback
# only look at the whole event, not per-stage. Confirmed real: event 4428
# ("AC Wochendende Lang/Mittel Strallegg", slug "...-2-tage-ostm-om-lang-
# und-ac-mittel" = "2-day: ÖSTM/ÖM for the Lang(distanz) day, AC for the
# Mittel(distanz) day") - stage 20442802 (2024-09-22, the Mittel day) is
# published under the title "9. AC & ASKÖ Bundesmeisterschaften" (a
# different federation's own title, unrelated to ÖFOL's ÖM/ÖSTM) with no
# championship marker of its own at all; only stage 20442801 (2024-09-21,
# Lang) is the real ÖSTM/ÖM race the event-level slug refers to.
TITLE_FALLBACK_EXCLUDE_STAGES = {20442802}

# The general rule (is_om_eligible_category) treats the senior/open Elite
# bracket as ÖSTM-only, never plain ÖM, learned from real per-row
# detections that were mostly Langdistanz races (Austria's traditional
# "Staatsmeisterschaft" distance). "ÖM Nacht" (night-O) races are a
# confirmed exception - twice over now: event 4315's own Ausschreibung
# (ac10_ausschreibung_2.0.pdf) lists "D/H21E" directly alongside the other
# ÖM-only age brackets ("D/H35-", "D/H40-", ...) under "Meisterschaftska-
# tegorien ÖM Nacht-OL" - a real published document, not a guess - and its
# official top-3 extract (om-nacht-meisterschaftswertung-2.pdf) confirms
# medals were actually awarded there ("1 österreichischer Meister" on the
# H21E/D21E winner's row); separately, event 4048's own "ÖM Nacht" stage
# (2023-04-29) is confirmed the same way by Naturfreunde Wien's own Excel
# medal sheet (Erik Bonek, bronze, Herren ab 21 Elite). Night-O's senior
# Elite is ÖM, not ÖSTM, unlike Langdistanz's.
#
# Scoped per STAGE, not per event: an "ÖM Nacht" race is very often only
# one day of a multi-day meet whose OTHER stages are Langdistanz/Sprint,
# where excluding senior Elite from ÖM is correct (event 4048 itself is
# exactly this - its Sunday "ÖM Sprint" and Monday "ÖM Lang" stages must
# NOT get this override just because their sibling Saturday stage does).
STAGE_ELITE_OM_OVERRIDE = {10004315, 30404801}

# Competitors confirmed by hand to be foreign/ineligible for the Austrian
# title despite carrying a championship tag, for cases the automated
# pipeline can't catch on its own: person.nationality is too sparse and
# occasionally wrong to trust (see the earlier "Vera Arbter" incident), and
# champion_rank only excludes someone ranked BETTER than the real champion -
# it has no way to know a finisher ranked BELOW the champion, and otherwise
# indistinguishable from a genuine Austrian silver/bronze medalist, is also
# ineligible. Keyed by (event_id, name exactly as that event's own results
# spell it) to rule out any risk of an unrelated same-named person elsewhere
# being caught by a broader match.
KNOWN_INELIGIBLE_RESULTS = {
    (4837, "Milja Väätäjä"),  # Paimion Rasti, Finland - ÖSTM Mittel, Damen ab 21 Elite, rank 2
    (4884, "Ivan Serafini"),  # ASD Team Sky Friul, Italy - ÖM MTBO, Herren ab 40, rank 3
    (4306, "Vojtech Teringl"),  # TJ OB Ceske Budejovice, Czechia - ÖM Sprint, H17, rank 1
    # Naturfreunde Wien member, but Ukrainian and hasn't yet had 3
    # uninterrupted years of primary residence in Austria - ÖFOL's own
    # championship rule (Staatsmeisterschaften/Meisterschaften Kriterium
    # 2) requires either Austrian citizenship or 3+ years' residence plus
    # ÖFOL license, not just club membership. Confirmed by hand: event
    # 4315 ("ÖM Nacht"), Damen bis 14 pair - her exclusion is why the
    # event's own official Meisterschaftswertung extract gives the bronze
    # to the next-placed eligible pair (Tandl/Asseg) instead.
    (4315, "Yelyzaveta Yevtushenko"),
}

# Single-row spelling typos confirmed present in the SOURCE document itself
# (not a parsing artifact - the raw PDF/HTML text literally has the wrong
# spelling), keyed the same way as KNOWN_INELIGIBLE_RESULTS. Left uncorrected,
# the misspelled row resolves to its own synthetic person instead of the
# runner's real identity, splitting one person's medal count across two
# person records. Confirmed real: event 3633 ("ÖSTM und ÖM Staffel" 2022),
# "Damen ab 19" relay leg 2 prints "Anita Gassner" where every other 2022
# result (including the very next day's) spells the same Naturfreunde Wien
# runner "Anika Gassner" - verified against the club's own Excel medal sheet.
KNOWN_NAME_TYPOS = {
    (3633, "Anita Gassner"): "Anika Gassner",
    # Naturfreunde Wien's own Excel medal sheet and her ANNE-resolved
    # identity both spell her "Matilda" (no h); these two source documents
    # spell her "Mathilda" instead, fragmenting her medal count onto a
    # separate synthetic person.
    (3851, "Buschek Mathilda"): "Buschek Matilda",
    (4690, "Mathilda Buschek"): "Matilda Buschek",
}

# Unambiguous non-Austrian club-name keywords (case-insensitive substring
# match against the raw `club` field), derived by systematically reviewing
# every club that appears in a current championship-tier row and fails to
# canonicalize to an official Austrian club (~690 distinct strings at the
# time this was built) - the majority of that list is genuine Austrian club
# truncation/spelling variants (fixed-width legacy exports cut names off,
# e.g. "Naturfreunde Villach - Oriente") or outright parsing garbage (a
# surname leaked into the club column from a misaligned row), NEITHER of
# which indicates foreign nationality at all - only entries with a clearly
# recognizable non-Austrian country name, national-team designation, or
# well-known Czech/Slovak/Slovenian/Croatian/Hungarian/Swiss/German/Italian
# city or club-type token went in this list. Deliberately excludes anything
# that could overlap an Austrian place name (Klagenfurt, Kufstein, Graz,
# Waldviertel, Kärnten, Steiermark, ...) or a club we've already confirmed
# has real Austrian-eligible members via the ANNE API despite a foreign-
# sounding name (OLT Transdanubien/HU - see USER_ELIGIBILITY_PATH).
FOREIGN_CLUB_KEYWORDS = [
    "italy", "italia", "hungary", "hungarian", "ukraine", " usa", "bulgaria", "bulgarien",
    "czech", "schweiz", "croatia", "slovenia", ", slo", "slovakia", "poland", "latvia",
    "philippines", "australia",
    "madona",
    " (lit)", " (hu)", " (d)", " (lat)",
    "praha", "praga", "prg", "brno", "plzen", "plzeň", "jihlava", "hradec kralov", "hradec králov",
    "strelka",
    "pardubice", "jicín", "jičín", "liberec", "šumperk", "sumperk", "vsetín", "vsetin",
    "bratislava", "zlín", "zlin", "nový bor", "novy bor", "blansko", "sokolov", "chrastava",
    "zilina",
    "dobríš", "dobris", "kamenice", "jilemnice", "smržovka", "smrzovka", "ceske budejovice",
    "ceské budejovice", "marianske lazne", "mariánské lázne", "rychnov", "kob ", "tj spartak",
    "tj tesla", "sk zabovresky", "sk zabrovesky", "sk zabrovresky", "skob", "vštj", "vstj",
    "oob tj", "dynamo malá skála", "kotlárka", "spartak vrchlabí", "lokomotiva plzen",
    "lokomotiva pardubice", "choceò", "nove mesto", "nové mest", "vrbne pod", "olomouc",
    "jizerky", "kladno", "vejprty", "žamberk", "zamberk", "melník", "melnik", "studenec",
    "bruntál", "bruntal", "jiskra",
    "orientacijski", "slovenj gradec", "komenda", "japetic", "mariborski", "karnika",
    "varaždin", "brežice", "kamniski", "pohorje",
    "delnice", "maksimir", "hrvatska",
    "zalaegerszeg", "szombathely", "szentendre", "alpokalja", "diósgyor", "diósgyori", "dvtk",
    "gerecse", "paksi", "veszprémi", "szegedi", "haladas", "hangya", "megalódusz",
    "tájfutó", "hun-o-team",
    "olg st. gallen", "olg kölliken", "olv hindelbank", "olv baselland", "olk fricktal",
    "olk argus", "ski-o swiss", "ski o swiss", "swiss o", "olg welsikon", "olg weisslingen",
    "olg thun", "olg säuliamt", "olg skandia", "olg schaffhausen", "olg goldau", "olg davos",
    "olg basel", "ol zimmerberg", "ol amriswil", "regio wil", "altdorf",
    "tu ilmenau", "tu dresden", "osnabrück", "bad harzburg", "ol team filder", "olv landshut",
    "oc münchen", "sachsen", "radebeul", "wannweil",
    "bussola", "g.s. pav", "gs pavione", "sportclub meran", "orientamento vincenza",
    "e.o.vizenca", "semiperdo", "ski-o fvg", "cordoba",
    "team israel", "team slovenia", "team italia", "team croatia", "team bayern",
    "national team", "military team", "mtbo italy", "mtbo hungary", "mtbo team",
    "italian mtbo", "hungarian mtbo", "czech mtbo", "czech dream team",
    "keravan", "puijon", "ifk lidingö", "sigulda", "polski zwiazek", "dnipro",
    "gronlait", "northwest orienteering", "city of trees", "club south london",
    "volkssport berlin", "rehab sc", "viking", "hammaren", "skogsfalken", "kangaroos",
    "perkunas",
]

# ÖM/ÖSTM medals require club membership, independent of nationality - a
# "vereinslos" (clubless) starter can compete and occupy a real finish
# position, but is never eligible for the title itself and doesn't block a
# club-affiliated runner from theirs. Confirmed by hand: Oleksandr
# Ievstafiev (UKR), vereinslos for the entire 10+ years he's raced in
# Austria - his ANNE-confirmed championshipEligibility override reflects
# his individual eligibility (citizenship/residency), a separate axis from
# this club-membership requirement, so it does NOT make him medal-eligible
# on its own. Matched by regex rather than an exact-string set since the
# same "no club" status is spelled a dozen ways across sources
# ("vereinslos", "Vereinlos", "kein Verein", "ohne Verein", "No Club",
# "Individuals/No club", ...), several with a stray name or number stuck to
# it from a parsing quirk elsewhere - the substring match still catches
# those correctly since the clubless marker itself stays intact.
CLUBLESS_CLUB_RE = re.compile(
    r"vereins?los|verienslos|kein\s*verein|ohne\s*verein|no\s*club|"
    r"club[- ]?less|individuals?\s*/\s*no\s*club", re.I)

# ANNE's own championshipEligibility flag (see ingest/anne_user_eligibility.py),
# cached per (ANNE userId, event id) - the authoritative signal, since
# person.nationality alone isn't one: several long-tenured Austrian club
# members are on record with a foreign passport nationality (marriage, dual
# citizenship, historical registration quirks - e.g. Vera Arbter/CHE, Marina
# Skern/RUS) yet hold an explicit eligibility override confirmed real by
# their club's own medal records. Keyed per event, not just per person,
# because eligibility isn't permanent - it's checked once, the first time a
# given event is seen, and locked to that event forever; a later status
# change only affects events synced after the change, never rewrites one
# already decided. The cache only contains (userId, eventId) pairs ANNE
# itself reported a non-Austrian nationality for, each mapped to True
# (explicit override granted - eligible despite the nationality) or None
# (no override - not eligible).
USER_ELIGIBILITY_PATH = RAW / "user_eligibility.json"

# Club "book of record": the authoritative member roster (ÖFOL-ID = the same
# id space as an ANNE userId / a positive person.id), used ONLY at build time
# to resolve result-name variants onto their real member identity. It is
# private (see .gitignore): its full birthdates/gender/roster never enter the
# public DB - only the derived effect (better result grouping + canonical
# display name + birth YEAR, all already public-grade) does. Absent (e.g. on
# CI, which doesn't have the private file) the whole member pass is skipped,
# same graceful-degrade contract as USER_ELIGIBILITY_PATH.
PRIVATE = ROOT / "data" / "private"
MEMBER_CSV_PATH = PRIVATE / "naturfreunde_wien_members.csv"
# The iterative decision ledger: confirmed name→ÖFOL-ID aliases for variant/
# typo spellings that name_key alone can't match, plus a "reviewed, NOT a
# member" set so genuine non-members aren't re-proposed every build. Also
# private, and append-only in spirit (grown via the review workflow).
MEMBER_MAPPING_PATH = PRIVATE / "member_mapping.json"
# The public, COMMITTED derived member index: ÖFOL-ID + name + birth YEAR, and
# only for roster members who actually appear in the (public) results - no full
# birthdates, no gender, no members who never raced. All already public-grade
# (those people are visible in published result lists), so it's safe to commit
# and lets CI reproduce the member matching without the private roster. Written
# on every local build (when the private CSV is present) and read as the
# registry source when it isn't.
MEMBER_INDEX_PATH = ROOT / "data" / "member_index" / "naturfreunde_wien.json"
# The public, COMMITTED copy of the decision ledger (aliases, internal members,
# club overrides, splits). Same public-grade content as the private working
# ledger minus the free-text `split_pending` to-do notes - it references only
# people already visible in public results and holds no birthdates/gender/
# non-racers. Written on every local build so it tracks the private ledger,
# and read as the mapping source on CI where the private one isn't present.
MEMBER_MAPPING_PUBLIC_PATH = ROOT / "data" / "member_index" / "naturfreunde_wien_mapping.json"
PERSON_REDIRECT_PATH = Path(os.environ.get(
    "OLRESULTS_PERSON_REDIRECT_PATH", ROOT / "data" / "person_id_redirects.json"))
# Build byproducts (private, regenerated each run): the review worklist and
# any BoR-vs-DB id conflicts worth a human look.
PENDING_REVIEW_PATH = PRIVATE / "pending_review.json"
MEMBER_CONFLICTS_PATH = PRIVATE / "member_conflicts.json"
# The official ANNE-clean club name this roster belongs to (extend later:
# one (csv, club-name) pair per club once other clubs are onboarded).
MEMBER_CLUB_NAME = "Naturfreunde Wien"
# Internal-only members (the club's own layer-1 membership, broader than and
# predating the official ÖFOL DB) have no ÖFOL-ID, so they get a stable id in
# this reserved high range - clear of both real ÖFOL-IDs (all well under 10^6)
# and the negative synthetic ids the resolver mints on the fly.
INTERNAL_ID_BASE = 90_000_000


def load_member_registry():
    """Parse the private book-of-record CSV into member records. Returns
    [] when the file isn't present (CI, or before it's been placed), so the
    caller degrades to today's behaviour. Each record carries the canonical
    'First Last' name, its name_key, birth year (year only - the full date
    stays private and never leaves this function), and the club name.

    When the private CSV isn't present (CI), falls back to the committed public
    member index (MEMBER_INDEX_PATH) - the same records minus roster members who
    never raced, so the member matching still reproduces on CI."""
    if not MEMBER_CSV_PATH.exists():
        if MEMBER_INDEX_PATH.exists():
            return [{"ofol_id": e["ofol_id"], "name": e["name"],
                     "name_key": name_key(e["name"]), "yob": e.get("yob"),
                     "club": MEMBER_CLUB_NAME}
                    for e in json.loads(MEMBER_INDEX_PATH.read_text())]
        return []
    members = []
    with MEMBER_CSV_PATH.open(encoding="utf-8") as f:
        for row in csv.DictReader(f, delimiter=";"):
            last = (row.get("Nachname") or "").strip()
            first = (row.get("Vorname") or "").strip()
            oid = (row.get("ID") or "").strip()
            if not oid.isdigit() or not (first or last):
                continue
            name = f"{first} {last}".strip()
            yob = (row.get("Geburtsdatum") or "").strip()[:4]
            members.append({
                "ofol_id": int(oid), "name": name, "name_key": name_key(name),
                "yob": int(yob) if yob.isdigit() else None, "club": MEMBER_CLUB_NAME})
    return members


def load_member_mapping():
    """Load the confirmed alias/non-member ledger. Shape:
        {"aliases": {"<name_key>": <ofol_id>, ...},
         "not_member": ["<name_key>", ...]}
    Reads the private working ledger when present (local dev, where it's
    hand-edited), else the committed public copy (CI). Missing both -> empty."""
    path = MEMBER_MAPPING_PATH if MEMBER_MAPPING_PATH.exists() else MEMBER_MAPPING_PUBLIC_PATH
    if not path.exists():
        return {"aliases": {}, "not_member": []}
    try:
        d = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {"aliases": {}, "not_member": []}
    d.setdefault("aliases", {})
    d.setdefault("not_member", [])
    d.setdefault("internal_member", {})
    # club_override: name_key -> the official club a person actually belongs to,
    # for runners whose results were attributed to this club by mistake (e.g. a
    # guest on one of its relay teams). null means clubless (vereinslos).
    d.setdefault("club_override", {})
    # split_override: name_key of a garbled row that crammed several runners into
    # one name field (relay/family/night-run pairs) -> the list of real runners
    # [{name, id}, ...] its results should be attributed to, one copy each.
    d.setdefault("split_override", {})
    return d


def prepare_verified_member_identities(cur, persons, members):
    """Apply the club's verified identity evidence before name reconciliation.

    ANNE occasionally attaches one member's userId to a row carrying another
    member's full name.  A global userId merge cannot safely repair that: it
    would merge both people and rewrite all of their otherwise-correct rows.
    Canonicalise known member ids first, then repair only source rows where the
    observed name uniquely and exactly identifies a *different* verified
    member or an independently established ANNE identity.  The conflicting
    observed_user_id deliberately remains on the result as provenance.

    Returns audit records for the private conflict report.
    """
    member_by_id = {m["ofol_id"]: m for m in members}
    members_by_name = defaultdict(list)
    for m in members:
        members_by_name[m["name_key"]].append(m)
    # Snapshot the first ANNE identity name before roster canonicalisation.
    # It is useful as independent evidence for repairing one crossed source
    # row (including when the correctly named target is not in our club).
    anne_ids_by_name = defaultdict(list)
    for pid, (_name, nk, _yob, _nat, _iof) in persons.by_id.items():
        if pid > 0:
            anne_ids_by_name[nk].append(pid)

    # The book of record fixes the canonical identity before duplicate-account
    # reconciliation groups people by name.  Otherwise the first bad ANNE row
    # can give a verified id another member's name and cause both ids to merge.
    for oid, member in member_by_id.items():
        existing = persons.by_id.get(oid)
        if not existing:
            continue
        if set(existing[1].split()) & set(member["name_key"].split()):
            persons.reconciliation_name_keys[oid].add(existing[1])
        persons.by_id[oid] = (
            member["name"], member["name_key"],
            member["yob"] if member["yob"] is not None else existing[2],
            existing[3], existing[4],
        )
        persons._link(oid, member["name"], member["yob"])

    corrections = []
    rows = cur.execute(
        """SELECT id, person_id, observed_name, observed_user_id
           FROM result
           WHERE source = 'anne-api' AND observed_user_id IS NOT NULL
           ORDER BY id""").fetchall()
    for result_id, person_id, observed_name, observed_user_id in rows:
        try:
            source_id = int(observed_user_id)
        except (TypeError, ValueError):
            continue
        source_member = member_by_id.get(source_id)
        if source_member is None:
            continue
        observed_key = name_key(clean_name(observed_name or ""))
        source_tokens = set(source_member["name_key"].split())
        if source_tokens & set(observed_key.split()):
            continue

        verified_targets = [m for m in members_by_name.get(observed_key, [])
                            if m["ofol_id"] != source_id]
        if len(verified_targets) == 1:
            target_member = verified_targets[0]
            target_id = target_member["ofol_id"]
            target_name = target_member["name"]
        else:
            anne_targets = [pid for pid in anne_ids_by_name.get(observed_key, [])
                            if pid != source_id]
            if len(verified_targets) > 1 or len(anne_targets) != 1:
                continue
            target_member = None
            target_id = anne_targets[0]
            target_name = persons.by_id[target_id][0]

        existing = persons.by_id.get(target_id)
        if existing is None:
            persons.by_id[target_id] = (
                target_member["name"], target_member["name_key"],
                target_member["yob"], None, None,
            )
            persons._link(target_id, target_member["name"], target_member["yob"])

        cur.execute(
            """UPDATE result
               SET person_id = ?, identity_basis = 'club-book-of-record',
                   identity_confidence = 1.0
               WHERE id = ?""",
            (target_id, result_id),
        )

        cleaned = clean_name(observed_name or "")
        for counters in (persons.name_seen, persons.name_auth):
            source_counts = counters.get(person_id)
            if source_counts and source_counts.get(cleaned, 0):
                source_counts[cleaned] -= 1
                if source_counts[cleaned] <= 0:
                    del source_counts[cleaned]
            counters[target_id][cleaned] += 1

        corrections.append({
            "result_id": result_id,
            "observed_user_id": source_id,
            "observed_name": observed_name,
            "source_identity": source_member["name"],
            "assigned_person_id": target_id,
            "assigned_identity": target_name,
        })
    return corrections


def duplicate_identity_merge_edges(persons, verified_member_ids=()):
    """Return safe duplicate/synthetic-id merges grouped by canonical name.

    A verified member id is preferred over an unverified duplicate ANNE
    account and is never merged into another id.  If several verified ids
    share the same name and cannot be separated by birth year, no positive-id
    merge is made; uncertainty is safer than destroying two identities.
    """
    by_name_group = defaultdict(list)
    for pid, (_name, nk, _yob, _nat, _iof) in persons.by_id.items():
        keys = {nk, *persons.reconciliation_name_keys.get(pid, set())}
        for grouping_key in keys:
            by_name_group[grouping_key].append(pid)

    verified_member_ids = set(verified_member_ids)
    merge_map = {}
    for ids in by_name_group.values():
        anne_ids = [i for i in ids if i > 0]
        synth_ids = [i for i in ids if i < 0]
        if not anne_ids:
            continue
        distinct_yobs = {persons.by_id[a][2] for a in anne_ids
                         if persons.by_id[a][2] is not None}
        protected = [a for a in anne_ids if a in verified_member_ids]
        if len(distinct_yobs) <= 1 and len(protected) <= 1:
            target = protected[0] if protected else min(anne_ids)
            for anne_id in anne_ids:
                if anne_id != target:
                    merge_map[anne_id] = target
            target_yob = next(iter(distinct_yobs), None)
            if target_yob is not None and persons.by_id[target][2] is None:
                current = persons.by_id[target]
                persons.by_id[target] = (
                    current[0], current[1], target_yob, current[3], current[4])
            for synth_id in synth_ids:
                synth_yob = persons.by_id[synth_id][2]
                if synth_yob is None or target_yob is None or synth_yob == target_yob:
                    merge_map[synth_id] = target
        else:
            by_yob = defaultdict(list)
            for anne_id in anne_ids:
                by_yob[persons.by_id[anne_id][2]].append(anne_id)
            for synth_id in synth_ids:
                synth_yob = persons.by_id[synth_id][2]
                matches = by_yob.get(synth_yob)
                if synth_yob is not None and matches and len(matches) == 1:
                    merge_map[synth_id] = matches[0]
    return merge_map


def strip_age_overlap_categories(cur):
    """Unconditionally null championship for any INDIVIDUAL row whose
    category matches AGE_OVERLAP_EXCLUDE_RE (D/H-15-18, D/H-21-Kurz,
    D/H-21-Lang) - unlike the same check inside is_om/is_ostm_eligible_
    category, which only gates the title *fallback*, this also catches a
    real per-row annotation using one of these category shapes (an older
    legacy export occasionally marks a Kurz/Lang stage winner the same
    way as a genuine champion), since the category itself was never a
    real ÖM/ÖSTM bracket regardless of how the source decided to write it
    up. Scoped to result_kind='individual' only: a genuinely different,
    legitimate bracket can share this exact age-range name in a RELAY
    context - confirmed real: event 4588 ("Ö(ST)M Staffel..."), whose
    "Herren ab 15 bis 18" relay category is a real championship division
    (Lauri Urbanek/Mika Asenbauer/Fabian Kolar's gold there is genuine,
    already confirmed by the club's own records) even though the
    identically-named INDIVIDUAL "ab 15 bis 18" category elsewhere never
    is."""
    cur.execute("""SELECT DISTINCT category FROM result
                   WHERE championship IS NOT NULL AND result_kind = 'individual'""")
    bad = [(cat,) for (cat,) in cur.fetchall() if AGE_OVERLAP_EXCLUDE_RE.search(cat)]
    if not bad:
        return 0
    cur.executemany(
        """UPDATE result SET championship = NULL
           WHERE category = ? AND championship IS NOT NULL AND result_kind = 'individual'""",
        bad)
    return len(bad)


def apply_championship_eligibility_overrides(cur):
    """Strip championship from a runner's rows in one specific event that
    ANNE itself reports as foreign-nationality with no eligibility override
    for that event. Only touches (person, event) pairs present in the cache
    - i.e. only ones ANNE told us are non-Austrian; everyone else (Austrian
    by default, or a synthetic person id with no ANNE account to check via
    this API at all - about half of all medal-tier people, mostly pre-ANNE
    legacy results) is left untouched, since guessing either way there
    would be worse than doing nothing."""
    if not USER_ELIGIBILITY_PATH.exists():
        return 0
    cache = json.loads(USER_ELIGIBILITY_PATH.read_text())
    n = 0
    for uid, by_event in cache.items():
        for eid, eligibility in by_event.items():
            if eligibility is True or eligibility == "error":
                continue  # eligible, or a transient fetch failure - not evidence either way
            cur.execute("""UPDATE result SET championship = NULL
                            WHERE championship IS NOT NULL AND person_id = ?
                              AND stage_id IN (SELECT id FROM stage WHERE event_id = ?)""",
                        (int(uid), int(eid)))
            n += cur.rowcount
    return n


def apply_title_championship_fallback(cur):
    """Only touches a (stage, category) with zero rows already carrying a
    championship tag (i.e. no per-row annotation was found anywhere in that
    category) - including relay categories, e.g. a PDF "Staffel" export
    whose team-announcement text (if any) parse_relay_pdf doesn't itself
    capture. Gated per category, not per whole stage: a stage can merge
    several source files with different, partial per-row-detection coverage
    (e.g. event 4894 - one file real-annotates some brackets, a second,
    cleaner file for other brackets has no annotation mechanism of its own
    at all), so "some category in this stage already has a real tag"
    doesn't mean every OTHER category in it does too. The same age-
    eligibility heuristic learned from real per-row detections applies; it's
    known to miss a handful of relay-specific exceptions with a lower age
    floor (a youth relay can carry full ÖSTM status - see
    is_ostm_eligible_category's docstring) - those already have real per-row
    detection wherever the data supports it, so this fallback never needs to
    cover them.

    Also covers 'pair' rows (D/H-12/-14 run-in-pairs night events, e.g.
    event 4315 "ÖM Nacht") - a pair category with no real per-row
    annotation of its own used to fall through this fallback entirely
    despite a clearly ÖM-titled event, because the original SELECT/UPDATE
    only looked at 'individual'/'relay' and forgot 'pair' existed. Same
    story for 'team' (e.g. event 3831, "ÖM Mannschaft", 2023) - a named-
    individual team result (not the anonymous surname-only roster kind
    load_legacy_results also tags 'team') that never got a championship
    at all despite a clearly ÖM-titled event and real gold/silver/bronze
    teams underneath, because 'team' wasn't in this list either."""
    # A multi-race event whose individual stages carry their own descriptive
    # titles ("6.AC Mittel", "ÖM Sprint (7.AC)", "Sparkassensprint", "8.AC
    # Mittel verl.") is telling us precisely WHICH stage is the championship;
    # the event-level title ("ÖM 3Tage-4Läufe") names the whole meet and
    # over-generalizes to every stage. So whenever ANY stage of an event has a
    # title that itself classifies to a championship, switch that entire event
    # to per-stage classification: each stage is judged only by its own title,
    # and a stage whose title names no championship (a plain Austria-Cup leg)
    # gets nothing - even though the event title does. Gated on "at least one
    # stage's title classifies" so an event whose stages are all generic
    # ("Etappe 1"/"Verfolgung") still falls back to the event title for every
    # stage, exactly as before. Confirmed real: event 5278, where only stage 2
    # ("ÖM Sprint") of 4 is an actual ÖM race (ANNE's own per-row championship
    # field is null across all 4, so nothing but the stage titles distinguishes
    # them). Also makes a legacy multi-day meet precise where only one day is
    # the championship (e.g. 3938's Sunday "ÖSM/ÖM Middle" vs Saturday sprint).
    stage_title_by_id = {}
    per_stage_events = set()
    for sid_, eid_, stitle in cur.execute(
            "SELECT id, event_id, title FROM stage WHERE title IS NOT NULL").fetchall():
        stage_title_by_id[sid_] = stitle
        if classify_title_championships(stitle):
            per_stage_events.add(eid_)

    cur.execute("""
        SELECT DISTINCT s.id, r.category, e.title, e.id, e.slug, r.result_kind, e.sport_type FROM result r
        JOIN stage s ON s.id = r.stage_id
        JOIN event e ON e.id = s.event_id
        WHERE r.status = 'ok' AND r.result_kind IN ('individual', 'relay', 'pair', 'team')
          AND NOT EXISTS (SELECT 1 FROM result r2
                            WHERE r2.stage_id = s.id AND r2.category = r.category
                              AND r2.championship IS NOT NULL)
    """)
    candidates = cur.fetchall()
    n = 0
    for sid, category, title, eid, slug, result_kind, sport_type in candidates:
        if eid in TITLE_FALLBACK_EXCLUDE_EVENTS or sid in TITLE_FALLBACK_EXCLUDE_STAGES:
            continue
        stage_title = stage_title_by_id.get(sid)
        if eid in per_stage_events:
            # per-stage mode (see the per_stage_events comment above): a stage
            # is a championship only if ITS OWN title says so. A title naming a
            # championship keeps everything BOTH it and the event title grant
            # (union) - a stage title is often an abbreviated form that drops
            # one of the two (e.g. 2675's "3. AC + ÖSTM Sprint" omits the ÖM
            # its event's "...ÖM/ÖStM Sprint" names), and must not lose it. A
            # descriptive title naming NO championship (a plain "6.AC Mittel"/
            # "Sparkassensprint" leg), OR no title at all (a legacy day whose
            # doc-title yielded none - almost always a non-championship leg of a
            # meet whose championship days DID get titled), gets nothing. That
            # precision - not blanketing every stage of an "ÖM …"-titled meet -
            # is the whole point (confirmed real: event 5278, only stage 2 of 4
            # is a real ÖM race; event 2675, only its sprint day, not its Long).
            if not stage_title:
                continue
            stage_types = classify_title_championships(stage_title)
            if not stage_types:
                continue
            types = stage_types | classify_title_championships(title, slug)
            combined = f"{stage_title} {title} {slug or ''}"
        else:
            # ANNE sometimes only exposes a terse shortTitle ("5.AC") with the
            # actual championship claim living solely in the event's own URL
            # slug (confirmed real: event 4434, title "5.AC", slug "5-ac-om-
            # jugend-sprint-alpen-adria-cup").
            types = classify_title_championships(title, slug)
            combined = f"{title} {slug or ''}"
            if not types:
                continue
        if (JUGEND_ONLY_RE.search(combined) and "sen" not in combined.lower()
                and AB_AGE_RE.search(category)):
            continue
        # Mirror image of the Jugend-only case above: a "(ÖM Sen.)"-scoped
        # title (confirmed real: event 4220, "3. AC Sprint (ÖM Sen.)",
        # slug "...-om-sprint-sen-ko-sprint-qualifikation") restricts the
        # title-fallback championship to masters categories (age >= 35)
        # only - it must not spill onto a youth/near-elite category that
        # also clears the ordinary >=12 floor (Corinna Biel D20-E, Matilda
        # Buschek/Anna Skern D-14, and Jannis Bonek H21-E each wrongly
        # picked up a spurious extra 2024 medal from this event). Distinct
        # from the many OTHER events titled "... Nachwuchs UND Senioren"
        # (both groups covered) - SENIOR_ONLY_RE fires on a bare "Sen."/
        # "Senioren" marker, but only once YOUTH_MARKER_RE rules out a
        # "Nachwuchs"/"Jugend"/"Junioren" co-marker being present too.
        if SENIOR_ONLY_RE.search(combined) and not YOUTH_MARKER_RE.search(combined):
            age = category_min_age(category)
            if age is None or age < 35:
                continue
        # D/H-15-18, D/H-21-Kurz, D/H-21-Lang are always Austria-Cup-only
        # for an INDIVIDUAL race (confirmed by direct user instruction and
        # the event 4315 Ausschreibung). Scoped to result_kind=='individual'
        # only: the identical age-range name can legitimately be a real
        # RELAY division instead (event 4588, "Ö(ST)M Staffel..." - its
        # "Herren ab 15 bis 18" relay category is a genuine championship
        # bracket, confirmed by the club's own records), so this must not
        # be folded into is_om/is_ostm_eligible_category, which have no
        # notion of result_kind and are shared by both.
        if result_kind == "individual" and AGE_OVERLAP_EXCLUDE_RE.search(category):
            continue
        if "ÖSTM" in types and is_ostm_eligible_category(category):
            champ = "ÖSTM"
        elif "ÖM" in types and (is_om_eligible_category(category)
                                 or (sid in STAGE_ELITE_OM_OVERRIDE
                                     and is_ostm_eligible_category(category))):
            champ = "ÖM"
        elif (sport_type == "mountainbikeOrienteering" and types == {"ÖSTM"}
                and is_om_eligible_category(category)):
            # MTBO's "ÖSTM"-only titles (no "ÖM" mentioned at all) still
            # cover the non-Elite age brackets as ÖM, unlike foot-O where an
            # ÖSTM-only title is genuinely Elite-only (confirmed explicitly
            # for foot-O: event 3830 "ÖSTM Langdistanz", masters categories
            # correctly get nothing). Confirmed for MTBO by cross-referencing
            # sibling events with real per-row annotations under the exact
            # same "MTBO ÖSTM Sprint/Mittel/Lang (+ N. AC)" title shape and
            # no "ÖM" token anywhere (events 3820, 3821, 3955, 3957, 4835,
            # 4956) - every one of them genuinely tags non-Elite categories
            # ÖM via a real "und Österreichischer Meister" row annotation,
            # so the same-shaped event 3962 ("MTBO ÖSTM Sprint und NÖ & St
            # LM") missing that annotation in its own source file is a gap
            # in that file's printing, not a real difference in the event -
            # Barbara Kastner's "Damen ab 50" gold there was invisible to us
            # entirely without this MTBO-specific inference.
            champ = "ÖM"
        else:
            continue
        # rank IS NOT NULL: a relay/pair leg can be status='ok' (that runner
        # personally punched every control) while the TEAM still has no rank
        # at all, because a teammate on another leg mispunched - the whole
        # team is unplaced then, and none of its members should carry a
        # championship tag regardless of their own leg's status.
        cur.execute("""UPDATE result SET championship = ?
                        WHERE stage_id = ? AND category = ? AND status = 'ok'
                          AND rank IS NOT NULL
                          AND result_kind IN ('individual', 'relay', 'pair', 'team')""", (champ, sid, category))
        n += cur.rowcount
    return n


OFFICIAL_CLUBS_PATH = ROOT / "data" / "official_clubs.json"
CLUB_SUFFIX_NUM_RE = re.compile(r"^(.+)\s(\d)$")
CLUB_PREFIX_CODE_RE = re.compile(r"^([A-Za-zÄÖÜäöüß]{2,6})\s+(.+)$")
# "NF" is a widespread abbreviation for "Naturfreunde" across many of that
# federation's clubs in legacy exports (NF Wien, NF Linz, NF Kitzbühel,
# NF Pasching, NF Seekirchen, NF Steiermark, ...), confirmed real: event
# 4317's relay export used "NF Wien 1", which the suffix-number strip
# alone reduces to "NF Wien" - not an official name on its own - silently
# dropping Marina Skern's and Wolfgang Waldhäusl's official_club match
# (and with it, their medal from the club's own cross-check) entirely.
# The space after "NF" is optional - some exports run it straight into the
# city name with none at all ("NFWien", "NFSteuerberg") - confirmed real:
# event 3615 ("5.AustriaCup Schi-O ÖSTM/ÖM Sprint"), club field "NFWien",
# which the space-requiring form didn't match at all, so his real ÖM gold
# there showed up on his own runner page (no official_club filter) but
# silently vanished from the club's own Medaillenspiegel (which filters on
# official_club). Safe to relax unconditionally: the final match still
# requires the expanded name to be an exact, already-known official club,
# so a string that only coincidentally starts with "NF" can never resolve
# to a wrong club - worst case it just still resolves to nothing, same as
# before.
NF_ABBREV_RE = re.compile(r"^NF\s*(.+)$")


def load_official_clubs():
    """ANNE's own /v1/club registry (type=='club' only - regional sub-
    federations excluded), fetched by build_club_dict.py. Used only to give
    the Vereine section one unambiguous name per real club - the `club`
    column shown on an individual result stays exactly as the source wrote
    it, since some events genuinely used a non-official name."""
    if not OFFICIAL_CLUBS_PATH.exists():
        return set()
    return {c["name"] for c in json.loads(OFFICIAL_CLUBS_PATH.read_text())}


def canonicalize_official_club(name, official):
    """Map a raw club string to the official club it's a variant of, or None
    if it doesn't resolve to one. Handles a relay/pair team-number suffix
    ('Naturfreunde Wien 2'), a trailing '*' marker, and a leaked short code
    or first-name prefix ('NWN Naturfreunde Wien', 'Boris Naturfreunde Wien')
    - each only accepted when stripping it lands exactly on an official name,
    never on a guessed/partial match, so an official club whose own name
    happens to start with a short word (e.g. 'OC Fürstenfeld') is never
    mistaken for a prefixed variant of some other, unrelated bare name."""
    if not name:
        return None
    cur = name.rstrip("*").strip()
    # a team-number suffix ('NF Wien 1') can stack with the NF abbreviation
    # - strip the suffix first (unconditionally; a lone trailing digit is
    # never part of a real club name) so the NF-expansion below sees the
    # bare club name underneath it, rather than requiring each transform to
    # land on an official name in a single step on its own.
    m = CLUB_SUFFIX_NUM_RE.match(cur)
    if m:
        cur = m.group(1).strip()
    while cur not in official:
        changed = False
        m = CLUB_PREFIX_CODE_RE.match(cur)
        if m and m.group(2).strip() in official:
            cur = m.group(2).strip()
            changed = True
        if not changed:
            m = NF_ABBREV_RE.match(cur)
            if m and f"Naturfreunde {m.group(1).strip()}" in official:
                cur = f"Naturfreunde {m.group(1).strip()}"
                changed = True
        if not changed:
            return None
    return cur


OFFICIAL_CLUBS = load_official_clubs()


def name_key(name):
    """Normalized identity key: lowercase, accent-stripped, sorted tokens."""
    n = unicodedata.normalize("NFKD", name)
    n = "".join(c for c in n if not unicodedata.combining(c)).lower()
    return " ".join(sorted(re.findall(r"[a-zäöüß]+", n)))


def clean_name(name):
    """Strip artifacts that leaked into the name column across sources, so the
    same runner isn't fragmented into separate identities per race. Handles
    Excel '#NAME?' import errors and a leading rank that some result layouts
    glue onto the name, with ('8 Robert') or without ('1Löwenstein') a space."""
    name = re.sub(r"^#NAME\?\s*", "", name.strip())
    name = re.sub(r"^A\.?\s?K\.?\s+", "", name)  # 'A.K.' = außer Konkurrenz marker
    name = re.sub(r"^\([^)]*\)\s*", "", name)   # leading note, e.g. "(Csala) Judit Resch"
    m = re.match(r"^\d{1,3}\s+(\D.*)$", name) or re.match(r"^\d{1,3}([A-Za-zÀ-ÿ].*)$", name)
    if m:
        name = m.group(1).strip()
    # PDF extraction sometimes splits the first letter off ("A lexander Grill")
    name = re.sub(r"^([A-ZÀ-Þ]) ([a-zà-ÿ])", r"\1\2", name)
    return name


# SportSoftware appends championship/title notes ("... und Österreichischer
# Meister") that some layouts drop onto their own line; misparsed category or
# event-title lines also surface. Both are non-persons.
ANNOTATION_RE = re.compile(
    r"(?i)(^&|^und\b|\bcup\b|\b(19|20)\d\d\b|"
    r"(staats|sprint|österr|öster|nieder|steir|kärnt|tirol|salzb|vorarl|"
    r"burgenl|wr\.?|nö|wien|jugend|schüler)\w*\.?\s*(sprint)?meister)")


# Characters that never occur in a real person name but do in the various
# parser failure modes: HTML markup (<>=;{}), relay separators (/), score/
# placeholder junk (#&%|?*).
INVALID_NAME_CHARS = set("<>={}[]|;#&%\\/?*")



# Czech/Slovak (and Russian/Bulgarian) feminine surnames are formed by adding
# "-ová"/"-ova" to the family name (e.g. Komárek -> Komárková): a grammatical
# marker that never occurs in a given name, so it identifies the surname
# reliably even for foreign guest runners with no ANNE firstName on record.
SLAVIC_SURNAME_SUFFIX_RE = re.compile(r"ov[áa]$", re.IGNORECASE)


def _order_signal(a, b, first_names):
    """+1 if the pair (a, b) looks like 'Lastname Firstname' (i.e. flipping to
    'b a' would fix it), -1 if it already looks like 'Firstname Lastname', 0
    if neither token gives any evidence. `first_names` may be None to use only
    the (source-independent, always-safe) Slavic-suffix signal - used for a
    single blind name with no corroborating evidence to protect it, since a
    handful of firstName/lastName swaps at the ANNE source (e.g. one row
    giving firstName='Meier' for someone actually named Thomas Meier) would
    otherwise poison an unrelated, already-correct '... Meier' name."""
    al, bl = a.lower(), b.lower()
    if first_names is not None:
        if bl in first_names and al not in first_names:
            return 1
        if al in first_names and bl not in first_names:
            return -1
    if SLAVIC_SURNAME_SUFFIX_RE.search(a) and not SLAVIC_SURNAME_SUFFIX_RE.search(b):
        return 1
    if SLAVIC_SURNAME_SUFFIX_RE.search(b) and not SLAVIC_SURNAME_SUFFIX_RE.search(a):
        return -1
    return 0


def reorder_first_last(name):
    """Fallback for a two-token name that no document-level decision covered
    (e.g. a source document too small to vote decisively). Only acts on the
    Slavic feminine-surname suffix (so 'Komárková Ondřejka' -> 'Ondřejka
    Komárková') - deliberately not the ANNE first-names set, which is safe to
    use for the dominance-checked, many-names-at-once vote in
    detect_lastname_firstname_doc but not for a single blind name with no
    corroborating evidence."""
    toks = name.split()
    if len(toks) == 2 and _order_signal(*toks, None) > 0:
        return f"{toks[1]} {toks[0]}"
    return name


def detect_lastname_firstname_doc(categories, first_names, min_votes=3, dominance=4):
    """A single SportSoftware result list uses one name-column convention
    throughout - almost always 'Lastname Firstname', but some exports (e.g.
    newer OE12 configurations) already print 'Firstname Lastname'. Per-name
    heuristics alone miss entries where neither token individually matches a
    known first name (a foreign guest with no ANNE record, say) - but their
    document-mates usually do, so vote across every 2-token name in the
    document and decide the convention for the whole list at once."""
    last_first = first_last = 0
    for cat in categories:
        for r in cat.get("results", []):
            toks = clean_name(r.get("name") or "").split()
            if len(toks) != 2:
                continue
            sig = _order_signal(*toks, first_names)
            if sig > 0:
                last_first += 1
            elif sig < 0:
                first_last += 1
    return last_first >= min_votes and last_first > dominance * first_last


def is_valid_name(name):
    """Reject non-person artifacts: score-O course lines, header/title
    fragments, relay bib strings, championship annotations, leaked HTML, etc."""
    if not re.search(r"[A-Za-zÀ-ÿ]{2,}", name):
        return False
    if re.match(r"^[\d+\-.,]", name):        # real names don't start digit/punct
        return False
    if any(c in INVALID_NAME_CHARS for c in name):
        return False
    if re.search(r"\d,\d{3}|\bkm\b", name):  # "4,950", "2,250 km" course artifacts
        return False
    if ANNOTATION_RE.search(name):
        return False
    return True


class PersonRegistry:
    """Identity resolution across sources.

    ANNE userIds are authoritative but inconsistent: the same real person
    can show up with yearOfBirth=None on older events and a real value on
    newer ones, since ANNE only started capturing DOB at some point. Legacy
    (scraped) results virtually never carry a birth year at all. So matching
    can't rely on (name, yob) being stable per person — it has to track every
    yob variant ever seen for a name and prefer an existing ANNE identity
    over minting a new synthetic one.
    """

    def __init__(self):
        self.by_id = {}
        self.by_key = {}     # (name_key, yob) -> pid
        self.by_name = {}    # name_key -> [pid, ...], insertion order, no dupes
        self.name_seen = defaultdict(Counter)  # pid -> Counter(name -> occurrences)
        self.name_auth = defaultdict(Counter)  # pid -> Counter of API-form names
        self.reconciliation_name_keys = defaultdict(set)
        self.first_names = set()               # lowercased firstNames from the API
        self.last_names = set()                # lowercased lastNames from the API
        self.anne_ids = set()                   # source-supplied ANNE identifiers

    def add_first_name(self, name):
        """Record a firstName seen in the ANNE API, for reorder_first_last's
        heuristic. Skip anything carrying the Slavic feminine-surname suffix
        outright - that's structurally never a given name, so it can only be
        here because of a firstName/lastName swap at the ANNE source."""
        name = name.strip().lower()
        if name and not SLAVIC_SURNAME_SUFFIX_RE.search(name):
            self.first_names.add(name)

    def add_last_name(self, name):
        self.last_names.add(name.strip().lower())

    def finalize_first_names(self):
        """A handful of ANNE rows have firstName/lastName swapped at the
        source (not something we can detect or fix per-row) - e.g. one row
        gives firstName='Meier' for someone actually named Thomas Meier. Left
        alone, that single bad row would poison the set into mis-flipping
        every unrelated correctly-ordered '... Meier' name back to front.
        A name legitimately used as both given name and surname is rare, so
        drop anything seen as both - cheap insurance against a handful of
        known source typos generalizing into many wrong flips."""
        self.first_names -= self.last_names

    def record(self, pid, name, authoritative=False):
        """Track every spelling of a name seen for a person so the display name
        can later be set to the most frequent one. ANNE sometimes ties one
        userId to several names — typos ('Erich'/'Erik'), maiden/married names,
        or data errors mixing two people; the majority keeps the common case
        right. `authoritative` marks names composed from the API's explicit
        firstName + lastName (reliably 'First Last' order), which win over the
        'Lastname Firstname' spellings SportSoftware exports use."""
        self.name_seen[pid][name] += 1
        if authoritative:
            self.name_auth[pid][name] += 1

    def _new(self, name, yob, nationality=None, iof_id=None, pid=None):
        if pid is None:
            salt = 0
            pid = stable_synthetic_id(name, yob, salt)
            while pid in self.by_id and self.by_id[pid][1:3] != (name_key(name), yob):
                salt += 1
                pid = stable_synthetic_id(name, yob, salt)
        self.by_id[pid] = (name, name_key(name), yob, nationality, iof_id)
        self._link(pid, name, yob)
        return pid

    def _link(self, pid, name, yob):
        nk = name_key(name)
        self.by_key[(nk, yob)] = pid
        lst = self.by_name.setdefault(nk, [])
        if pid not in lst:
            lst.append(pid)

    def from_anne(self, user_id, name, yob, nationality, iof_id):
        self.anne_ids.add(user_id)
        if user_id in self.by_id:
            self._link(user_id, name, yob)
            if yob is not None and self.by_id[user_id][2] is None:
                cur = self.by_id[user_id]
                self.by_id[user_id] = (cur[0], cur[1], yob, cur[3] or nationality, cur[4] or iof_id)
            return user_id
        return self._new(name, yob, nationality, iof_id, pid=user_id)

    def from_legacy(self, name, yob):
        nk = name_key(name)
        if (nk, yob) in self.by_key:
            return self.by_key[(nk, yob)]

        candidates = self.by_name.get(nk, [])
        anne_candidates = [c for c in candidates if c > 0]
        if anne_candidates:
            # trust the real ANNE identity over a mismatched/missing legacy yob
            pid = anne_candidates[0]
        elif yob is None and candidates:
            pid = candidates[0]
        elif candidates and all(self.by_id[c][2] in (None, yob) for c in candidates):
            # no candidate has a conflicting *known* birth year -> same person
            pid = candidates[0]
        else:
            return self._new(name, yob)

        self._link(pid, name, yob)
        return pid


def is_bewertung_clone(event):
    """'Bewertung' events are ANNE's compensated/handicap-scoring view of a
    race that's already ingested as its own event: same runners, same
    times, same categories, just re-published under a second event id
    (e.g. id 5511 'Bewertung - ... Langdistanz' duplicates stage 733 of
    event 5301 row for row). They're not separate competitions and would
    double-count every runner who ran the underlying race."""
    return "bewertung" in (event.get("shortTitle") or "").lower()


# ANNE's own sportType metadata is occasionally wrong - or, for a handful of
# older/thinly-tracked events, simply absent (None) - at the source.
# Confirmed real: event 4317's shortTitle is literally "ÖM und ÖSTM Sprint
# Mixed Staffel in SkiO", but ANNE reports sportType 'footOrienteering'; events
# 4626/4114/2605/2436/2437 (all "O-Festival" editions, ordinary summer foot-O)
# and 3376 ("MTB-O Festival", named as such) carry sportType None outright.
# This matters beyond mislabeling: (1) the Nov/Dec-belongs-to-next-season
# "Wertungsjahr" rule (site/app.js seasonYear()) only shifts a date forward
# for sportType 'skiOrienteering', so a Ski-O event misfiled as footO would
# silently stay attributed to the wrong (calendar, not season) year
# everywhere on the site; (2) the site's OL/SkiO/MTBO discipline filter can
# only filter on sportType at all - a None value always passes every filter
# state, so a genuinely MTBO event stuck at None never disappears when a
# user filters it OUT, looking like a misclassification even though it's
# really just missing data.
EVENT_SPORT_TYPE_OVERRIDES = {
    4317: "skiOrienteering",
    4626: "footOrienteering",   # O-Festival 2025
    4114: "footOrienteering",   # O-Festival 2023
    2605: "footOrienteering",   # O-Festival 2019 E03
    2437: "footOrienteering",   # O-Festival 2019 Etappe 02
    2436: "footOrienteering",   # O-Festival 2019 Etappe 01
    3376: "mountainbikeOrienteering",  # 38. MTB-O Festival
}


def load_events(cur):
    events = {e["id"]: e for e in json.loads((RAW / "events.json").read_text())
              if not is_bewertung_clone(e)}
    for e in events.values():
        cur.execute(
            "INSERT INTO event VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (e["id"], e.get("slug"), e.get("shortTitle"), e.get("shortTitle"),
             (e.get("dateFrom") or "")[:10] or None,
             (e.get("dateTo") or "")[:10] or None,
             e.get("location"), "AUT", e.get("coordinates"),
             e.get("competitionType"),
             EVENT_SPORT_TYPE_OVERRIDES.get(e["id"], e.get("sportType")),
             e.get("eventType"), e.get("url")))
    return events


PARSER_FILES = {
    "sportsoftware-html": ROOT / "ingest" / "parse_sportsoftware_html.py",
    "sportsoftware-pdf": ROOT / "ingest" / "parse_sportsoftware_pdf.py",
    "sportsoftware-text": ROOT / "ingest" / "parse_sportsoftware_text.py",
    "club-table": ROOT / "ingest" / "parse_club_table.py",
    "liveresultat": ROOT / "ingest" / "parse_liveresultat.py",
    "anne-api": Path(__file__),
}


def file_sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path and path.exists() else None


def parser_version(source):
    paths = [PARSER_FILES.get(source)]
    if source != "anne-api":
        paths.append(ROOT / "ingest" / "sportsoftware_common.py")
    digest = hashlib.sha256()
    for path in paths:
        if path and path.exists():
            digest.update(path.read_bytes())
    return digest.hexdigest() if any(path and path.exists() for path in paths) else None


def repo_path(path):
    return path.relative_to(ROOT).as_posix() if path else None


def register_anne_document(cur, event_id, path):
    document_id = f"anne-results:{event_id}"
    cur.execute(
        """INSERT OR IGNORE INTO source_document
           (id, event_id, source_type, source_url, file_name, snapshot_path,
            snapshot_sha256, normalized_path, normalized_sha256, parser_version)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (document_id, event_id, "anne-api",
         f"https://anne-api.oefol.at/v1/event/{event_id}/results", path.name,
         repo_path(path), file_sha256(path), None, None,
         parser_version("anne-api")))
    return document_id


def register_legacy_document(cur, doc):
    normalized_path = ROOT / doc["_normalizedPath"]
    stem = normalized_path.stem
    source = doc["source"]
    document_id = f"legacy:{stem}"
    cur.execute(
        """INSERT OR IGNORE INTO source_document
           (id, event_id, source_type, source_url, file_name, snapshot_path,
            snapshot_sha256, normalized_path, normalized_sha256, parser_version)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (document_id, doc["eventId"], source, doc.get("sourceUrl"),
         doc.get("fileName"), None, None,
         repo_path(normalized_path), file_sha256(normalized_path),
         parser_version(source)))
    return document_id


RESULT_COLS = ("stage_id", "person_id", "category", "category_full", "club", "official_club",
               "rank", "status", "time_s", "time_behind_s", "out_of_competition",
               "course_length_m", "course_climb_m", "course_controls",
               "result_kind", "note", "source", "source_document_id",
               "observed_name", "observed_club", "observed_user_id",
               "identity_basis", "identity_confidence", "championship")


def insert_result(cur, **kw):
    kw.setdefault("out_of_competition", 0)
    kw.setdefault("result_kind", "individual")
    kw.setdefault("identity_basis", "unknown")
    kw.setdefault("identity_confidence", 0.0)
    vals = [kw.get(c) for c in RESULT_COLS]
    cur.execute(f"INSERT INTO result ({','.join(RESULT_COLS)}) "
                f"VALUES ({','.join('?' * len(RESULT_COLS))})", vals)


def default_stage(cur, event, stage_ids):
    """Stage id for single-stage events: synthetic id = event id + offset."""
    sid = 10_000_000 + event["id"]
    if sid not in stage_ids:
        cur.execute("INSERT INTO stage VALUES (?,?,?,?,?,?)",
                    (sid, event["id"], 1, None,
                     (event.get("dateFrom") or "")[:10] or None,
                     event.get("location")))
        stage_ids.add(sid)
    return sid


def dated_stage(cur, event, stage_ids, date, number):
    """One stage per distinct legacy-file date within an event that actually
    spans multiple real days under a single ANNE id (e.g. an Austria-Cup
    weekend: Lang one day, Mittel the next, both filed under one event) -
    otherwise same-named categories on different days ('Herren ab 55' both
    days) collide into default_stage()'s single synthetic stage and one
    day's results get silently dropped from the dedup in load_legacy_results.
    """
    sid = 20_000_000 + event["id"] * 100 + number
    if sid not in stage_ids:
        cur.execute("INSERT INTO stage VALUES (?,?,?,?,?,?)",
                    (sid, event["id"], number, None, date, event.get("location")))
        stage_ids.add(sid)
    return sid


def anne_mapped_stage(cur, event, stage_ids, info):
    """A legacy result file mapped to a specific ANNE stage by its Etappe
    number (see map_docs_to_anne_stages) - gets that stage's authoritative
    number, date AND title straight from ANNE. Unlike dated_stage this can
    tell two races run on the SAME day apart (event 2274: a "Chicken-Race"
    and the "Night-Race"/ÖM-Nacht, both on the 20th), and the ANNE title is
    the precise per-stage championship signal apply_title_championship_
    fallback needs."""
    num = info["number"]
    sid = 30_000_000 + event["id"] * 100 + num
    if sid not in stage_ids:
        cur.execute("INSERT INTO stage VALUES (?,?,?,?,?,?)",
                    (sid, event["id"], num, info.get("title"), info.get("date"),
                     event.get("location")))
        stage_ids.add(sid)
    return sid


def load_anne_results(cur, events, persons, stage_ids):
    n = 0
    for path in sorted((RAW / "results").glob("*.json")):
        eid = int(path.stem)
        event = events.get(eid)
        if not event:
            continue
        source_document_id = register_anne_document(cur, eid, path)
        stages_path = RAW / "stages" / f"{eid}.json"
        if stages_path.exists():
            for s in json.loads(stages_path.read_text()):
                if s["id"] not in stage_ids:
                    cur.execute("INSERT INTO stage VALUES (?,?,?,?,?,?)",
                                (s["id"], eid, s.get("number", 1), s.get("title"),
                                 s.get("dateFrom"), s.get("location")))
                    stage_ids.add(s["id"])
        rows = json.loads(path.read_text())
        # the API can return live and official lists side by side:
        # keep only the most authoritative type per stage+category
        priority = {"official": 0, "unofficial": 1, "live": 2}
        best = {}
        for r in rows:
            key = (r.get("eventStageId"), r.get("categoryShortTitle"))
            p = priority.get(r.get("resultType"), 3)
            best[key] = min(best.get(key, 3), p)
        rows = [r for r in rows
                if priority.get(r.get("resultType"), 3)
                == best[(r.get("eventStageId"), r.get("categoryShortTitle"))]]
        for r in rows:
            sid = r.get("eventStageId") or default_stage(cur, event, stage_ids)
            cat = r.get("categoryShortTitle") or r.get("categoryTitle")
            if r.get("teamMembers"):
                n += insert_anne_relay(cur, persons, sid, cat, r, source_document_id)
                continue
            observed_name = f"{r.get('firstName') or ''} {r.get('lastName') or ''}".strip()
            name = clean_name(observed_name)
            # some old imports carry bib/SI numbers or 'empty' placeholders
            if not is_valid_name(name) or "empty" in name.lower():
                continue
            uid = anne_user_id(r.get("userId"))
            if uid:
                pid = persons.from_anne(uid, name, r.get("yearOfBirth"),
                                        r.get("nationality"), r.get("iofId"))
            else:
                pid = persons.from_legacy(name, r.get("yearOfBirth"))
            if r.get("firstName"):
                persons.add_first_name(r["firstName"])
            if r.get("lastName"):
                persons.add_last_name(r["lastName"])
            persons.record(pid, name, authoritative=bool(r.get("firstName") and r.get("lastName")))
            course = r.get("course") or {}
            club = clean_club(r.get("clubName"))
            insert_result(cur, stage_id=sid, person_id=pid, category=cat,
                          category_full=r.get("categoryTitle"), club=club,
                          official_club=canonicalize_official_club(club, OFFICIAL_CLUBS),
                          rank=r.get("rank"),
                          status=ANNE_STATUS.get(r.get("classification"), "unknown"),
                          time_s=r.get("time"), time_behind_s=r.get("timeBehind"),
                          out_of_competition=1 if r.get("outOfCompetition") else 0,
                          course_length_m=course.get("length"),
                          course_climb_m=course.get("climb"),
                          course_controls=course.get("controlCount"),
                          source="anne-api", source_document_id=source_document_id,
                          observed_name=observed_name, observed_club=r.get("clubName"),
                          observed_user_id=str(uid) if uid is not None else None,
                          identity_basis="anne-user-id" if uid is not None
                                         else ("legacy-name-yob" if r.get("yearOfBirth")
                                               else "legacy-name"),
                          identity_confidence=1.0 if uid is not None
                                              else (0.75 if r.get("yearOfBirth") else 0.55),
                          championship=anne_championship(r))
            n += 1
    return n


def insert_anne_relay(cur, persons, sid, cat, team, source_document_id=None):
    """Explode a structured relay team into one result per leg runner, sharing
    the team's rank/club, with the runner's own leg time and a note naming the
    team and teammates. Leg time is the cumulative team time at that leg minus
    the previous leg's (the API only gives cumulative 'overall' times)."""
    members = team["teamMembers"]
    names = []
    for m in members:
        nm = clean_name(f"{m.get('firstName') or ''} {m.get('lastName') or ''}".strip())
        names.append(nm if is_valid_name(nm) else None)

    team_name = clean_club(team.get("teamName") or team.get("clubName") or "")
    championship = anne_championship(team)
    n = 0
    prev_cum = 0
    for m, nm in zip(members, names):
        ov = m.get("overall") or {}
        cum = ov.get("time")
        leg_time = (cum - prev_cum) if (cum is not None) else None
        if cum is not None:
            prev_cum = cum
        if not nm:
            continue
        uid = anne_user_id(m.get("userId"))
        if uid is not None:
            pid = persons.from_anne(uid, nm, m.get("yearOfBirth"),
                                    m.get("nationality"), m.get("iofId"))
        else:
            pid = persons.from_legacy(nm, m.get("yearOfBirth"))
        if m.get("firstName"):
            persons.add_first_name(m["firstName"])
        if m.get("lastName"):
            persons.add_last_name(m["lastName"])
        persons.record(pid, nm, authoritative=bool(m.get("firstName") and m.get("lastName")))
        mates = list(dict.fromkeys(o for o in names if o and o != nm))
        note_bits = [f"Staffel: {team_name}".strip(),
                     f"Leg {m.get('leg')}/{len(members)}"]
        if mates:
            note_bits.append("Team: " + ", ".join(mates))
        relay_club = clean_club(team.get("clubName"))
        observed_name = f"{m.get('firstName') or ''} {m.get('lastName') or ''}".strip()
        insert_result(cur, stage_id=sid, person_id=pid, category=cat,
                      category_full=team.get("categoryTitle"), club=relay_club,
                      official_club=canonicalize_official_club(relay_club, OFFICIAL_CLUBS),
                      rank=team.get("rank"),
                      status=ANNE_STATUS.get(m.get("classification")
                                             or team.get("classification"), "unknown"),
                      time_s=leg_time, result_kind="relay",
                      note=" · ".join(note_bits), source="anne-api",
                      source_document_id=source_document_id,
                      observed_name=observed_name, observed_club=team.get("clubName"),
                      observed_user_id=str(uid) if uid is not None else None,
                      identity_basis="anne-user-id" if uid is not None
                                     else ("legacy-name-yob" if m.get("yearOfBirth")
                                           else "legacy-name"),
                      identity_confidence=1.0 if uid is not None
                                          else (0.75 if m.get("yearOfBirth") else 0.55),
                      championship=championship)
        n += 1
    return n


# A SportSoftware PDF/HTML header only records when that file was last
# (re)generated, not the actual competition date - guess_doc_date() falls
# back to reading it anyway for a legacy file with no "ergDDMMYY..."
# filename convention of its own. For a multi-day event, ANNE's own
# per-stage metadata (data/raw/anne/stages/{eid}.json - see anne_sync.py)
# has the real date for each day, just not tied to a specific attachment
# file. Both a stage's title ("5.AC Sprint", "ÖSM 6.AC Middle Nassereith")
# and the matching attachment's filename ("...ergebnis-5-ac-2023.pdf")
# share the same "N.AC" Austria-Cup round number though, which is enough
# to line them up. Confirmed real: event 4114 ("O-Festival 2023") - all 3
# of its separate result files were reprinted on the same later day, so
# guess_doc_date() gave them all the identical wrong date, collapsing 3
# real stages (27/28/29 May) into 1.
AC_ROUND_RE = re.compile(r"(\d+)[\s.-]*ac\b", re.I)
# A second, independent way to recover the real date: some legacy
# filenames spell it out directly ("...19-5-2019-ergebnisse.html") in a
# D-M-YYYY/D.M.YYYY shape that doesn't match FILENAME_DATE_RE's stricter
# 6-digit "ergDDMMYY" SportSoftware convention. Only trusted when the
# resulting date is one of the event's own known stage dates - a random
# D-M-YYYY-shaped number run in an unrelated filename must not get
# promoted to a real date on a guess. Confirmed necessary: event 2675
# ("2. AC Long und 3. AC/ÖM/ÖStM Sprint 2019") - its main results file
# has no "N.AC" round number in its name at all (only its split-times
# sibling does), so without this it would keep its wrong guessed date
# while the split file - a stand-in with no club/rank data of its own,
# see the relay/leg-times dedup-priority comment above - took over the
# real one, leaving the real results stranded on a phantom stage.
LEGACY_FILENAME_DATE_RE = re.compile(r"(\d{1,2})[.-](\d{1,2})[.-](\d{4})")
# Some multi-race festivals number their result files by stage
# ("...-e1.pdf"/"...-e2.pdf"/"...-e3.pdf" for Etappe 1/2/3), which lines up
# directly with ANNE's own ordered stages - see map_docs_to_anne_stages,
# which uses this to give each file its true stage identity (date AND title)
# from ANNE even when two races share a day, which a date-only split can't.
ETAPPE_FILENAME_RE = re.compile(r"(?<![a-z])e[\s.-]*(\d{1,2})(?![0-9])", re.I)
# Split (Zwischenzeiten) files and cumulative "standings so far" files are
# never a race's own result list - a split file has no rank/club/team of its
# own (see the relay/leg-times dedup-priority comment above), and a
# "results-after-eNN"/"over-all-results-after-eNN" file just re-totals every
# earlier stage into one running score. Both need dropping before
# map_docs_to_anne_stages ever sees them: ETAPPE_FILENAME_RE happily matches
# the "eNN" inside "...-after-e02.pdf" too, so left in, a cumulative file
# would collide with that same stage's own real result file and merge into
# it. Confirmed real: event 4626 ("O-Festival 2025") - "over-all-results-
# after-e02.pdf" mapping onto Etappe 2 alongside the real "results-e02.pdf".
JUNK_DOC_FILENAME_RE = re.compile(r"-split\.|results-after-e\d+", re.I)


def map_docs_to_anne_stages(docs):
    """Attach each legacy result file to a specific ANNE stage. ANNE knows the
    true structure of a multi-race meet - each stage's number, date and
    descriptive title ("ÖSTM Sprint + 3.AC" vs "4. AC") - which the legacy
    files themselves don't reliably carry: their printed export date is often
    identical across races, and their own titles are inconsistent or absent
    (PDFs). Matched by, in order of confidence:
      1. an Etappe number in the filename ("...-e2.pdf") -> the Nth stage;
      2. an Austria-Cup round in the filename or the file's own race-name
         title ("...-6-ac...", ".../ - Ergebnis - 4. Austriacup") -> the stage
         whose ANNE title carries that same round;
      3. an exact competition-date match, when only one stage ran that day.
    Applied only when the result is >= 2 distinct stages (so we're genuinely
    reshaping a multi-race meet, never re-homing a lone file). Once mapped,
    any leftover file is a duplicate or a cumulative cup standing that just
    re-lists the same runners (event 1637's "austria-cup-wertung", event
    2375's redundant sprint copy) - dropped so it can't spawn a phantom stage.
    Confirmed real: events 2274 (two races the same day, only ANNE tells them
    apart), 2422 (undated PDFs, titles only in ANNE), 2375 (four files, two
    near-duplicate pairs, for two stages)."""
    by_event = defaultdict(list)
    for d in docs:
        by_event[d["eventId"]].append(d)
    for eid, event_docs in by_event.items():
        stages_path = RAW / "stages" / f"{eid}.json"
        stages = None
        if stages_path.exists():
            try:
                stages = json.loads(stages_path.read_text())
            except (json.JSONDecodeError, OSError):
                stages = None
        if not stages or len(stages) < 2:
            # No usable ANNE stage data at all for this event - fall back to
            # the filenames' own Etappe numbering alone, when at least 2
            # distinct numbers show up (own date as each stage's date, no
            # ANNE title to attach). Confirmed real: event 4626 ("O-Festival
            # 2025") - ANNE's own stageCount is 0 for this event despite it
            # being a real 4-day meet, so every one of its result files
            # collapsed onto a single synthetic stage; "results-e01" through
            # "e04" in the filenames is the only stage structure available.
            enum = [(int(m.group(1)), d) for d in event_docs
                    if (m := ETAPPE_FILENAME_RE.search(d.get("fileName") or ""))]
            numbers = sorted({n for n, _ in enum})
            if len(numbers) >= 2:
                mapped = {id(d) for _, d in enum}
                for n, d in enum:
                    d["_anneStage"] = {"number": n, "date": d.get("docDate"), "title": None}
                for d in event_docs:
                    if id(d) not in mapped:
                        d["_skip"] = True
            continue

        def info(i):
            s = stages[i]
            return {"number": s.get("number") or i + 1,
                    "date": (s.get("dateFrom") or "")[:10] or None,
                    "title": (s.get("title") or "").strip() or None}

        # (1) Etappe-numbered files map 1:1 onto ANNE's ordered stages.
        enum = []
        for d in event_docs:
            m = ETAPPE_FILENAME_RE.search(d.get("fileName") or "")
            if m and 1 <= int(m.group(1)) <= len(stages):
                enum.append((int(m.group(1)) - 1, d))
        if len({i for i, _ in enum}) >= 2:
            mapped = {id(d) for _, d in enum}
            for i, d in enum:
                d["_anneStage"] = info(i)
            for d in event_docs:
                if id(d) not in mapped:
                    d["_skip"] = True
            continue

        # (2)/(3) round- or date-match each file to a stage.
        anne_round, anne_date = {}, {}
        for i, s in enumerate(stages):
            m = AC_ROUND_RE.search(s.get("title") or "")
            if m:
                anne_round.setdefault(int(m.group(1)), i)
            dt = (s.get("dateFrom") or "")[:10]
            if dt:
                anne_date.setdefault(dt, []).append(i)

        def match(d):
            m = AC_ROUND_RE.search(d.get("fileName") or "")
            if not m:
                m = AC_ROUND_RE.search(derive_stage_title(d.get("docTitle")) or "")
            if m and int(m.group(1)) in anne_round:
                return anne_round[int(m.group(1))]
            same = anne_date.get(d.get("docDate") or "")
            return same[0] if same and len(same) == 1 else None

        matched = [(match(d), d) for d in event_docs]
        if len({i for i, _ in matched if i is not None}) >= 2:
            for i, d in matched:
                if i is not None:
                    d["_anneStage"] = info(i)
                elif d.get("listType") in ("race", "relay"):
                    d["_skip"] = True
    return docs


def correct_legacy_stage_dates(docs, events):
    by_event = defaultdict(list)
    for d in docs:
        by_event[d["eventId"]].append(d)
    n = 0
    for eid, event_docs in by_event.items():
        stages_path = RAW / "stages" / f"{eid}.json"
        if not stages_path.exists():
            continue
        try:
            stages = json.loads(stages_path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if len(stages) < 2:
            continue
        valid_dates = {s["dateFrom"][:10] for s in stages if s.get("dateFrom")}
        round_to_date = {}
        for s in stages:
            m = AC_ROUND_RE.search(s.get("title") or s.get("shortTitle") or "")
            if m and s.get("dateFrom"):
                round_to_date[int(m.group(1))] = s["dateFrom"][:10]
        if not round_to_date and not valid_dates:
            continue
        for d in event_docs:
            fn = d.get("fileName") or ""
            corrected = None
            m = AC_ROUND_RE.search(fn)
            if m and int(m.group(1)) in round_to_date:
                corrected = round_to_date[int(m.group(1))]
            if not corrected:
                dm = LEGACY_FILENAME_DATE_RE.search(fn)
                if dm:
                    day, month, year = dm.groups()
                    try:
                        iso = f"{year}-{int(month):02d}-{int(day):02d}"
                    except ValueError:
                        iso = None
                    if iso in valid_dates:
                        corrected = iso
            if corrected and d.get("docDate") != corrected:
                d["docDate"] = corrected
                n += 1
    return n


def drop_cross_event_duplicate_docs(docs):
    """Some events publish a byte-identical result file under a SECOND,
    separate ANNE event id - confirmed real by hand with ANNE's own
    support (event 4048, "OL Südbgld.", a whole-weekend listing bundling
    all 3 days, vs. 4129/4130/4131, "ÖM Nacht"/"ÖM Lang"/"ÖM Sprint", one
    dedicated event per day, each publishing the exact same file as the
    corresponding day of 4048) - every result in a shared file was
    counting as a medal twice, once under each event id. Detected by
    hashing each doc's own parsed categories (not the raw file - two
    independently-parsed copies of the identical source normalize to the
    identical dict either way, and this avoids re-reading raw files
    here). Confirmed real, not a coincidence: an accidental hash
    collision between two genuinely different competitions would need
    every runner's name, club, rank and time to match exactly.

    Once duplicate content is confirmed, which copy to keep is decided by
    which event covers the BROADEST span of it - most distinct dates
    among its own docs, then most attachments, then (deterministic final
    tiebreak, arbitrary otherwise) the lower event id - since the whole
    point is that the loser's content is, by construction, a full subset
    of the winner's, so nothing unique is ever actually lost. This
    prefers the single multi-stage event (4048) over the 3 separate
    single-stage ones - the reverse of preferring by title/slug ÖM/ÖSTM
    marker, which would keep the 3 harder-to-find single-day events
    instead: confirmed by hand that only 4048 shows up when browsing
    ANNE's own site at all."""
    groups = defaultdict(list)
    for d in docs:
        h = hashlib.md5(json.dumps(d.get("categories"), sort_keys=True, default=str)
                         .encode()).hexdigest()
        groups[h].append(d)
    by_event = defaultdict(list)
    for d in docs:
        by_event[d["eventId"]].append(d)

    def completeness(eid):
        ds = by_event[eid]
        return (len({d.get("docDate") for d in ds if d.get("docDate")}), len(ds))

    drop = set()
    for group in groups.values():
        eids = set(d["eventId"] for d in group)
        if len(eids) < 2:
            continue
        keep_eid = max(eids, key=lambda eid: (completeness(eid), -eid))
        for d in group:
            if d["eventId"] != keep_eid:
                drop.add(id(d))
    if not drop:
        return docs, 0
    kept = [d for d in docs if id(d) not in drop]
    return kept, len(docs) - len(kept)


# SportSoftware's own generated <title> comes in two shapes, both anchored on
# the word "Ergebnis" (optionally "Ergebnis nach Kategorien/Bahnen"):
#   "{meet name} - Ergebnis - {race name}"  ("OL Weekend Südburgenland -
#      Ergebnis - ÖM Nacht & 2. AC")  -> the SUFFIX after "Ergebnis" is the
#      specific race that this file is; the prefix just names the whole meet.
#   "{race name} - Ergebnis"          ("ÖM Mitteldistanz 2016 - Ergebnis")
#      -> nothing after "Ergebnis", so the PREFIX is itself the race name.
# The suffix is authoritative when present - taking the prefix instead would
# wrongly inherit a championship named for a DIFFERENT day of the same meet
# (confirmed real: event 3797, "3. + 4. Austriacup + OEM Nachwuchs Sprint -
# Ergebnis - 3. AC Mitteldistanz" - this file is the plain Mitteldistanz, the
# ÖM sprint is a separate day/event, so its title must classify to nothing).
# This derived race name is stored as the stage's title (dated_stage()/
# default_stage() otherwise insert NULL) and drives per-stage championship
# classification in apply_title_championship_fallback.
STAGE_TITLE_RE = re.compile(
    r"^(?P<prefix>.*?)\s-\s*Ergebnis(?:\s+nach\s+\w+)?\b\s*-?\s*(?P<suffix>.*)$", re.I)


def derive_stage_title(doc_title):
    m = STAGE_TITLE_RE.search(doc_title or "")
    if not m:
        return None
    return m.group("suffix").strip() or m.group("prefix").strip() or None


def load_legacy_results(cur, events, persons, stage_ids, anne_event_ids):
    n = 0
    # Each legacy file's own SportSoftware <title> names the specific race it
    # holds ("... - Ergebnis - ÖM Sprint (7.AC)"); collected per stage here and
    # turned into the stage's own title (derive_stage_title), which is then the
    # authoritative per-stage championship signal in
    # apply_title_championship_fallback - see its per_stage_events logic.
    # Preserve the already-established document priority below. A set made the
    # chosen title process-random whenever several attachments named the same
    # stage differently, which changed 14 stage titles across identical builds.
    stage_doc_titles = defaultdict(list)
    canonical = re.compile(r"^\d+-(?:club)?\d+\.json$")
    docs = []
    for path in sorted(NORM.glob("*.json")):
        if not canonical.match(path.name):
            continue
        doc = json.loads(path.read_text())
        doc["_normalizedPath"] = repo_path(path)
        docs.append(doc)
    docs = [d for d in docs if not JUNK_DOC_FILENAME_RE.search(d.get("fileName") or "")]
    docs, _n_dropped = drop_cross_event_duplicate_docs(docs)
    correct_legacy_stage_dates(docs, events)
    map_docs_to_anne_stages(docs)
    # plain result lists before split-time lists, so duplicates resolve
    # in favour of the cleaner source. Also prefer a dedicated relay
    # ("Staffel") export over a same-event 'race'-listType file covering
    # the identical categories: that combination is really a per-leg
    # times report in disguise - no club, no rank, no team grouping - the
    # relay-shaped equivalent of a split-times file, just not named
    # "split" (confirmed real: event 4317, "ÖM/ÖSTM Sprint Mixed Staffel"
    # - "ergebnisse.html" processed first and silently shadowed every
    # runner's real team/rank/club from "erg101223-staffel.html" via the
    # (stage, category, name) dedup key below, dropping Marina Skern's and
    # Wolfgang Waldhäusl's silver from the medal count entirely).
    #
    # Also prefer a dedicated "-oem"/"-oestm" championship extract over
    # the same event's general results file: the extract is pre-filtered
    # by the organizers to eligible entrants only, so its own rank number
    # already IS the correct national placement, while the general file's
    # rank counts everyone including foreign guests. Confirmed real: event
    # 4346 ("2. AC SkiO Mittel"), "Herren ab 45" - the general file placed
    # Wolfgang Waldhäusl 5th (behind four foreign/guest runners folded
    # into that count), but "...-results-oem.pdf" already excludes them
    # and correctly has him 3rd; the general file happened to sort first
    # and silently won the dedup, costing him a bronze.
    oem_extract_re = re.compile(r"-oe?stm\.|-oem\.", re.I)
    docs.sort(key=lambda d: (d["eventId"], "split" in d["fileName"].lower(),
                              d.get("listType") != "relay",
                              not oem_extract_re.search(d["fileName"])))
    # only split into per-date stages for events ANNE itself says span
    # multiple days (stageCount >= 2, or a distinct dateTo) - otherwise a
    # single-day event's own split-times file (same race, just guesses a
    # different "docDate" off its own filename/content than the plain
    # results file) gets a stage of its own instead of deduping against the
    # plain file's stage as intended, duplicating every result on that date
    multiday_events = {
        eid for eid, e in events.items()
        if (e.get("stageCount") or 0) >= 2 or (e.get("dateTo") or "")[:10] not in ("", (e.get("dateFrom") or "")[:10])
    }
    dates_by_event = defaultdict(set)
    for d in docs:
        if d.get("docDate") and d["eventId"] in multiday_events:
            dates_by_event[d["eventId"]].add(d["docDate"])
    seen = set()
    for doc in docs:
        eid = doc["eventId"]
        event = events.get(eid)
        if not event or doc.get("listType") not in ("race", "relay"):
            continue
        if doc.get("_skip"):
            continue  # redundant cumulative standing - see map_docs_to_anne_stages
        if eid in anne_event_ids:
            continue  # structured API data wins over legacy files
        source_document_id = register_legacy_document(cur, doc)
        # team (Mannschaft) result lists give only member surnames + a club +
        # a single team time — no first names, so members can't be resolved to
        # individual runners (a surname+club match linked only ~17%). Keep them
        # as one team-level row each; they're shown on event pages and excluded
        # from the runner directory.
        is_team = event.get("competitionType") == "team"
        # A file whose own name says "team" (English, distinct from the
        # ubiquitous German "Mannschaft" that both a team-standings AND an
        # individual-runners file for the same event carry) holds ONLY team
        # rows - every row in it is a team, never mixed with individual leg
        # times, so the surname-count heuristic below (>= 3 tokens) doesn't
        # need to guess for it. That heuristic alone missed real 2-word team
        # names (confirmed real: event 3507, "ergebnis-teams-mannschaft.pdf" -
        # "ASKÖ Henndorf"/"OLC Graz"-shaped team names have only 2 tokens,
        # indistinguishable by word count from an ordinary "Firstname
        # Lastname" individual, so they fell through to result_kind=
        # 'individual' with the team name misread as a person).
        doc_is_team_only = is_team and "team" in (doc.get("fileName") or "").lower()
        event_dates = sorted(dates_by_event.get(eid) or [])
        if doc.get("_anneStage"):
            sid = anne_mapped_stage(cur, event, stage_ids, doc["_anneStage"])
        elif len(event_dates) > 1 and doc.get("docDate") in event_dates:
            sid = dated_stage(cur, event, stage_ids, doc["docDate"],
                               event_dates.index(doc["docDate"]) + 1)
        else:
            sid = default_stage(cur, event, stage_ids)
        if doc.get("docTitle") and doc["docTitle"] not in stage_doc_titles[sid]:
            stage_doc_titles[sid].append(doc["docTitle"])
        flip_doc = detect_lastname_firstname_doc(doc["categories"], persons.first_names)
        for cat in doc["categories"]:
            for r in cat["results"]:
                if r.get("status") == "dns":
                    continue
                # a parsed row may carry several runners (a pair): the parser
                # emits one entry per runner already, each with its own name
                # and a note; treat them uniformly here
                observed_name = r["name"]
                name = clean_name(observed_name)
                name = KNOWN_NAME_TYPOS.get((eid, name), name)
                if not is_valid_name(name):
                    continue
                if flip_doc:
                    toks = name.split()
                    if len(toks) == 2:
                        name = f"{toks[1]} {toks[0]}"
                key = (sid, cat["name"], name_key(name))
                if key in seen:
                    continue
                seen.add(key)
                # newer team tables are already split per member by the parser
                # (resultKind=team, full names, note set). For the older surname-
                # only roster format, a roster row is a run of >=3 surnames;
                # 2-token "Lastname Firstname" rows are the individual (Einzel)
                # categories these events also contain.
                parser_kind = r.get("resultKind")
                if parser_kind:
                    kind, note = parser_kind, r.get("note")
                elif doc_is_team_only or (is_team and len(name.split()) >= 3):
                    kind, note = "team", "Mannschaft"
                else:
                    kind, note = "individual", r.get("note")
                pid = persons.from_legacy(name, r.get("yearOfBirth"))
                persons.record(pid, name)
                insert_result(cur, stage_id=sid, person_id=pid, category=cat["name"],
                              category_full=cat["name"], club=r.get("club"),
                              official_club=canonicalize_official_club(r.get("club"), OFFICIAL_CLUBS),
                              rank=r.get("rank"), status=r.get("status", "unknown"),
                              time_s=r.get("timeS"),
                              course_length_m=cat.get("courseLengthM"),
                              course_climb_m=cat.get("courseClimbM"),
                              course_controls=cat.get("courseControls"),
                              result_kind=kind, note=note, source=doc["source"],
                              source_document_id=source_document_id,
                              observed_name=observed_name, observed_club=r.get("club"),
                              identity_basis="legacy-name-yob" if r.get("yearOfBirth")
                                             else "legacy-name",
                              identity_confidence=0.75 if r.get("yearOfBirth") else 0.55,
                              championship=r.get("championship"))
                n += 1

    for sid, titles in stage_doc_titles.items():
        for t in titles:
            label = derive_stage_title(t)
            if label:
                cur.execute("UPDATE stage SET title = ? WHERE id = ? AND title IS NULL", (label, sid))
                break

    return n


def main():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    DB_PATH.unlink(missing_ok=True)
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.executescript(SCHEMA)

    events = load_events(cur)
    persons = PersonRegistry()
    stage_ids = set()

    def has_usable_names(path):
        """Mirrors the exact validity check load_anne_results() applies per
        row (is_valid_name + the 'empty' placeholder check), not just a bare
        letters-present regex - a file where every single row is the literal
        placeholder 'empty' as lastName (seen for real, e.g. event 4884)
        would otherwise pass a looser check, wrongly marking the event as
        having usable ANNE data and blocking the legacy-attachment fallback
        even though load_anne_results goes on to discard every one of those
        rows anyway, leaving the event with zero results either way."""
        try:
            rows = json.loads(path.read_text())
        except Exception:
            return False
        if not rows or any(r.get("teamMembers") for r in rows):
            return True  # empty, or a relay/team (handled via teamMembers)
        return any(
            is_valid_name(name := clean_name(f"{r.get('firstName') or ''} {r.get('lastName') or ''}".strip()))
            and "empty" not in name.lower()
            for r in rows)

    # a few events flagged hasOfficialResults=True actually carry unusable API
    # data (SI-card numbers as names, e.g. event 1127) — fall back to their
    # legacy attachment instead of letting the empty/junk API snapshot win
    anne_event_ids = {int(p.stem) for p in (RAW / "results").glob("*.json")
                      if has_usable_names(p)}
    n_api = load_anne_results(cur, events, persons, stage_ids)
    persons.finalize_first_names()
    n_legacy = load_legacy_results(cur, events, persons, stage_ids, anne_event_ids)

    # Load verified club identities before the general duplicate-account pass.
    # This ordering is a correctness boundary: a bad source row must not get
    # the chance to rename and merge two independently verified people first.
    members = load_member_registry()
    source_identity_corrections = prepare_verified_member_identities(
        cur, persons, members) if members else []

    # Some API results have no userId (old events) or field-order quirks;
    # they're matched via from_legacy and can mint a synthetic identity
    # before the file carrying the real ANNE userId for the same person is
    # even processed (files are read in filename-string order, not
    # chronological). Some people also hold two separate ANNE accounts
    # outright. Reconcile both after the fact, grouped by name:
    #  - ANNE ids agreeing on every *known* birth year are one duplicated
    #    account -> merged into the lowest id.
    #  - ANNE ids with genuinely conflicting birth years are treated as
    #    different people; a synthetic id only joins one of them if its own
    #    birth year exactly and unambiguously matches.
    merge_map = duplicate_identity_merge_edges(
        persons, (m["ofol_id"] for m in members))

    # merge_map can chain (a synthetic id may map to an id that itself got
    # merged); resolve to final targets before applying
    def resolve(pid):
        while pid in merge_map:
            pid = merge_map[pid]
        return pid

    # --- Book-of-record member pass ---------------------------------------
    # Resolve result-name variants onto their real club-member identity using
    # the private roster + confirmed-alias ledger. A member's ÖFOL-ID is the
    # same id space as an ANNE userId, so this reuses the exact merge machinery
    # above: it just adds more edges to merge_map (variant/legacy id -> member
    # ÖFOL-ID) and, where the member never had an ANNE-linked result of their
    # own, mints the member identity so their scattered legacy rows have a real
    # home to collect onto. Only members who actually appear in results are
    # ever created - the roster of members with no races (and every full
    # birthdate/gender) stays private, never entering the public DB.
    member_canonical = {}   # surviving pid -> (canonical name, birth year)
    pending_review, conflicts = [], list(source_identity_corrections)
    split_override = {}
    if members:
        ledger = load_member_mapping()
        split_override = ledger["split_override"]
        not_member = set(ledger["not_member"])
        member_by_nk = defaultdict(list)         # exact-roster index (ÖFOL only)
        for m in members:
            member_by_nk[m["name_key"]].append(m)

        # Every target identity (member) keyed by its id, unifying the two
        # membership layers: an official ÖFOL member (id = ÖFOL-ID, positive)
        # and an internal-only member (real club member with no ÖFOL-ID, given
        # a stable id in a reserved high range - see INTERNAL_ID_BASE). Both
        # merge and canonicalise identically; the only difference is the id
        # space they live in.
        id_meta = {m["ofol_id"]: {"name": m["name"], "name_key": m["name_key"],
                                  "yob": m["yob"]} for m in members}
        # combined confirmed-alias map: a variant name_key -> its target id,
        # whether that target is an ÖFOL member or an internal-only one
        alias_target = dict(ledger["aliases"])
        for iid_str, info in ledger.get("internal_member", {}).items():
            iid = int(iid_str)
            id_meta[iid] = {"name": info["name"], "name_key": name_key(info["name"]),
                            "yob": info.get("yob")}
            for nk in info.get("aliases", []):
                alias_target[nk] = iid

        # every name_key ever seen for each surviving identity, so a variant
        # spelling recorded on any one of its merged rows can still match; also
        # keep the pre-merge person_ids behind each identity, for club_override
        resolved_nks = defaultdict(set)
        sid_pids = defaultdict(list)
        for p in list(persons.by_id):
            sid = resolve(p)
            sid_pids[sid].append(p)
            resolved_nks[sid].add(persons.by_id[p][1])
            for nm in persons.name_seen.get(p, {}):
                resolved_nks[sid].add(name_key(nm))

        # club_override: a runner wrongly attributed to this club (a guest on a
        # relay team, a mis-canonicalised club string) actually belongs to
        # another club - or none. Rewrite their club-attributed results BEFORE
        # the association/pending step below, so they correctly drop out of the
        # club entirely. null override -> clubless (vereinslos).
        for sid, nks in resolved_nks.items():
            ov_key = next((nk for nk in sorted(nks) if nk in ledger["club_override"]), None)
            if ov_key is None:
                continue
            newclub = ledger["club_override"][ov_key] or None   # null/"" -> clubless
            cur.executemany(
                "UPDATE result SET official_club = ? WHERE person_id = ? AND official_club = ?",
                [(newclub, p, MEMBER_CLUB_NAME) for p in sid_pids[sid]])

        # which surviving identities actually have results, and whether any of
        # those results ran under the club's official name
        assoc = defaultdict(lambda: [0, 0])  # sid -> [nfw_rows, total_rows]
        for person_id, nfw, n in cur.execute(
                "SELECT person_id, SUM(official_club = ?), COUNT(*) FROM result GROUP BY person_id",
                (MEMBER_CLUB_NAME,)).fetchall():
            sid = resolve(person_id)
            assoc[sid][0] += nfw or 0
            assoc[sid][1] += n

        def match_member(sid):
            # sorted() for determinism: a person can carry several name spellings
            # (ANNE occasionally stamps one userId onto rows that are really other
            # people - confirmed real: userId 1665 "Peter Bonek" also has stray
            # "Barbara Kastner"/"Ylvi Kastner" rows), and set-iteration order is
            # per-process-random, so an unsorted first-match would merge such a
            # person into a DIFFERENT member on some builds and not others.
            nks = sorted(resolved_nks.get(sid, set()))
            for nk in nks:                       # confirmed alias (ÖFOL or internal) wins
                if nk in alias_target:
                    return alias_target[nk]
            sid_yob = persons.by_id[sid][2]
            for nk in nks:                       # exact roster name_key (ÖFOL only)
                cands = member_by_nk.get(nk)
                if not cands:
                    continue
                if len(cands) == 1:
                    return cands[0]["ofol_id"]
                yobm = [m for m in cands if sid_yob is not None and m["yob"] == sid_yob]
                if len(yobm) == 1:               # same name, disambiguated by year
                    return yobm[0]["ofol_id"]
            return None

        for sid, (nfw_rows, _tot) in assoc.items():
            # a person who already holds their own ÖFOL-ID IS that member - never
            # remap them onto a different one just because a stray mis-tagged
            # name spelling of theirs happens to match another member's name
            # (the userId-1665 case above). Canonicalise in place instead.
            oid = sid if (sid > 0 and sid in id_meta) else match_member(sid)
            if oid is None:
                # surface only genuine club runners we couldn't place and
                # haven't already dismissed as non-members
                if nfw_rows and not (resolved_nks.get(sid, set()) & not_member):
                    pending_review.append({
                        "person_id": sid, "name": persons.by_id[sid][0],
                        "name_key": persons.by_id[sid][1],
                        "year_of_birth": persons.by_id[sid][2],
                        "nfw_results": nfw_rows})
                continue
            m = id_meta[oid]
            # conflict: the target id is already a genuinely DIFFERENT person in
            # our data - an ANNE result stamped that userId onto an unrelated
            # name (confirmed real: id 10344, roster "Le Blanc" but ANNE
            # "Kollndorfer"). Keyed on DISJOINT name tokens, so a mere nickname/
            # spelling difference under the same surname (Willi/Wilhelm
            # Tiefenböck, or a garbled "Luna+Lorenz Vesely" that's really
            # Herbert) is NOT a conflict - those correctly merge and take the
            # roster's canonical name.
            existing = persons.by_id.get(oid)
            if existing and oid != sid and assoc.get(oid, [0, 0])[1] > 0 \
                    and not (set(name_key(existing[0]).split()) & set(m["name_key"].split())):
                conflicts.append({"target_id": oid, "roster_name": m["name"],
                                  "db_name": existing[0], "matched_from": persons.by_id[sid][0]})
                continue
            if oid not in persons.by_id:         # member with only legacy rows: mint them
                persons.by_id[oid] = (m["name"], m["name_key"], m["yob"], None, None)
            target = resolve(oid)
            if sid != target:
                merge_map[sid] = target
            member_canonical[target] = (m["name"], m["yob"])

        # a surviving id can be both processed as unmatched (added to pending)
        # AND later become a member's merge target (another row matched it) -
        # a member target is placed, so drop it from the review worklist; also
        # drop garbled rows already scheduled to be split apart below
        pending_review = [e for e in pending_review
                          if resolve(e["person_id"]) not in member_canonical
                          and e["name_key"] not in split_override]
    # ----------------------------------------------------------------------

    final_names = defaultdict(Counter)
    final_auth = defaultdict(Counter)
    for pid, counts in persons.name_seen.items():
        final_names[resolve(pid)].update(counts)
    for pid, counts in persons.name_auth.items():
        final_auth[resolve(pid)].update(counts)

    for old in list(merge_map):
        new = resolve(old)
        if new in member_canonical:
            cur.execute(
                """UPDATE result
                   SET identity_basis = 'club-book-of-record', identity_confidence = 1.0
                   WHERE person_id = ?""", (old,))
        cur.execute("UPDATE result SET person_id = ? WHERE person_id = ?", (new, old))
        persons.by_id.pop(old, None)

    for pid, (name, key, yob, nat, iof) in persons.by_id.items():
        # prefer the API's authoritative 'First Last' spelling; otherwise the
        # most-frequent spelling, flipped to 'First Last' when it's a legacy
        # 'Lastname Firstname' form we can recognise
        auth = final_auth.get(pid)
        counts = final_names.get(pid)
        if auth:
            name = auth.most_common(1)[0][0]
            key = name_key(name)
        elif counts:
            name = reorder_first_last(counts.most_common(1)[0][0])
            key = name_key(name)
        # the book of record is authoritative for a confirmed member's display
        # name and birth year (overriding whatever spelling the results used) -
        # only the year, never the private full date
        if pid in member_canonical:
            m_name, m_yob = member_canonical[pid]
            name, key = m_name, name_key(m_name)
            if m_yob is not None:
                yob = m_yob
        cur.execute("INSERT INTO person VALUES (?,?,?,?,?,?)",
                    (pid, name, key, yob, nat, iof))

    # split_override: a garbled row that crammed several runners into one name
    # field (relay/family/night-run pairs, e.g. "Anna+Selina Skern") - give each
    # of its results to every real runner it named, then drop the garbled
    # identity. Runs before stats/national_rank so those see the split rows.
    # Confirmed non-championship rows only (night runs / family categories), so
    # no medal impact. Copy each result once per extra runner (while the garbled
    # person still owns them), then hand the originals to the first runner.
    _copy_cols = ("stage_id, category, category_full, club, official_club, rank, "
                  "status, time_s, time_behind_s, out_of_competition, course_length_m, "
                  "course_climb_m, course_controls, result_kind, note, source, "
                  "source_document_id, observed_name, observed_club, observed_user_id, "
                  "identity_basis, identity_confidence, championship")
    for gnk, targets in split_override.items():
        tids = [t["id"] for t in targets]
        if len(tids) < 2:
            continue
        for (gid,) in cur.execute("SELECT id FROM person WHERE name_key = ?", (gnk,)).fetchall():
            if gid in tids:                 # a runner's own row, don't self-split
                continue
            for tid in tids[1:]:
                cur.execute(f"INSERT INTO result (person_id, {_copy_cols}) "
                            f"SELECT ?, {_copy_cols} FROM result WHERE person_id = ?", (tid, gid))
            cur.execute("UPDATE result SET person_id = ? WHERE person_id = ?", (tids[0], gid))
            cur.execute("DELETE FROM person WHERE id = ?", (gid,))

    # Publish identity evidence separately from the canonical person row.  An
    # ANNE id is a strong source-supplied identifier; only this club's private
    # book of record marks an ÖFOL id as independently verified membership.
    surviving_people = {pid for (pid,) in cur.execute("SELECT id FROM person")}
    for anne_id in sorted(persons.anne_ids):
        target = resolve(anne_id)
        if target in surviving_people:
            cur.execute(
                "INSERT OR REPLACE INTO person_identifier VALUES (?,?,?,?,?)",
                ("anne_user_id", str(anne_id), target, 0, "anne-api"))
    for member in members:
        target = resolve(member["ofol_id"])
        if target in surviving_people:
            cur.execute(
                "INSERT OR REPLACE INTO person_identifier VALUES (?,?,?,?,?)",
                ("oefol_id", str(member["ofol_id"]), target, 1,
                 "naturfreunde-wien-book-of-record"))
    person_rows = cur.execute(
        "SELECT id, name, name_key, year_of_birth, nationality, iof_id FROM person").fetchall()
    for pid, _name, _key, _yob, _nat, iof_id in person_rows:
        if iof_id:
            cur.execute(
                "INSERT OR IGNORE INTO person_identifier VALUES (?,?,?,?,?)",
                ("iof_id", str(iof_id), pid, 0, "anne-api"))
        if pid >= INTERNAL_ID_BASE:
            cur.execute(
                "INSERT OR IGNORE INTO person_identifier VALUES (?,?,?,?,?)",
                ("club_internal", str(pid), pid, 1, "naturfreunde-wien-book-of-record"))

    for pid, counts in final_names.items():
        if pid not in surviving_people:
            continue
        auth_names = final_auth.get(pid, {})
        for alias, occurrences in counts.items():
            source = "anne-api" if alias in auth_names else "result-observation"
            cur.execute(
                "INSERT OR REPLACE INTO person_alias VALUES (?,?,?,?,?,?)",
                (pid, alias, name_key(alias), source, 0, occurrences))
    for pid, (canonical_name, _yob) in member_canonical.items():
        if pid in surviving_people:
            cur.execute(
                "INSERT OR REPLACE INTO person_alias VALUES (?,?,?,?,?,?)",
                (pid, canonical_name, name_key(canonical_name),
                 "naturfreunde-wien-book-of-record", 1, 1))

    if PERSON_REDIRECT_PATH.exists():
        redirects = json.loads(PERSON_REDIRECT_PATH.read_text())
        for old_id, new_id in redirects.items():
            old_id, new_id = int(old_id), int(new_id)
            if new_id not in surviving_people:
                raise RuntimeError(f"person redirect target does not exist: {old_id} -> {new_id}")
            if old_id != new_id:
                cur.execute("INSERT INTO person_redirect VALUES (?, ?)", (old_id, new_id))

    # SportSoftware prints the national champion's rank ("1") on a separate
    # "… österreichischer Meister" annotation line, so the winning row parses
    # with no rank and the list appears to start at 2. Where a category has
    # ranked finishers but none ranked 1, assign rank 1 to the fastest unranked
    # finisher(s) — for a team/pair all members share the winning time.
    cur.execute("""
        UPDATE result SET rank = 1
        WHERE status = 'ok' AND rank IS NULL AND time_s IS NOT NULL
          AND (stage_id, category, time_s) IN (
            SELECT r.stage_id, r.category, MIN(r.time_s)
            FROM result r
            WHERE r.status = 'ok'
            GROUP BY r.stage_id, r.category
            HAVING SUM(r.rank = 1) = 0 AND SUM(r.rank IS NOT NULL) > 0
          )
    """)

    # Anyone ranked BETTER (a lower number) than the confirmed national
    # champion within the same (stage, category) is presumptively foreign/
    # ineligible for the Austrian title - that's exactly why the source
    # numbered the champion "2." or "3." instead of "1." in the first place
    # (see parse_champion_annotation). Snapshot that boundary before
    # propagating, so a foreign finisher who beat the champion doesn't
    # inherit the tag too.
    cur.execute("""
        CREATE TEMP TABLE champion_rank AS
        SELECT stage_id, category, MIN(rank) AS champ_rank
        FROM result
        WHERE championship IS NOT NULL AND status = 'ok' AND rank IS NOT NULL
        GROUP BY stage_id, category
    """)

    # A legacy export only tags the ONE row carrying the champion annotation
    # with its ÖM/ÖSTM classification; ANNE's structured API already tags
    # every row of a championship category individually. Either way, the
    # medal table needs the whole category marked, so fan the tag out to
    # every other "ok" row sharing the same (stage, category) at or below
    # the champion's own rank once any one of them has it - a no-op for
    # ANNE categories, which are already fully tagged.
    cur.execute("""
        UPDATE result SET championship = (
            SELECT r2.championship FROM result r2
            WHERE r2.stage_id = result.stage_id AND r2.category = result.category
              AND r2.championship IS NOT NULL LIMIT 1)
        WHERE status = 'ok' AND championship IS NULL
          AND rank >= COALESCE((SELECT champ_rank FROM champion_rank cr
                                 WHERE cr.stage_id = result.stage_id AND cr.category = result.category), 1)
          AND EXISTS (
            SELECT 1 FROM result r3
            WHERE r3.stage_id = result.stage_id AND r3.category = result.category
              AND r3.championship IS NOT NULL)
    """)

    cur.execute("DROP TABLE champion_rank")

    n_title_fallback = apply_title_championship_fallback(cur)

    strip_age_overlap_categories(cur)

    n_eligibility = apply_championship_eligibility_overrides(cur)

    # Every non-nationality-API exclusion source, collected once into a temp
    # table and reused for (1) stripping championship tags below and (2) the
    # eligible-starter count further down - a DNF/MP/DSQ row never got a
    # championship tag to strip in the first place, so it needs checking
    # against this table directly, not just already-tagged rows.
    cur.execute("CREATE TEMP TABLE ineligible_starter (event_id INTEGER, person_id INTEGER)")

    # KNOWN_INELIGIBLE_RESULTS: cases the API can't cover - someone with no
    # ANNE account at all (a genuinely one-off foreign guest, e.g. Milja
    # Väätäjä/Ivan Serafini - confirmed by hand to not exist in ANNE's user
    # database whatsoever).
    for eid, pname in KNOWN_INELIGIBLE_RESULTS:
        cur.execute("""
            INSERT INTO ineligible_starter (event_id, person_id)
            SELECT ?, id FROM person WHERE name = ?
        """, (eid, pname))
    if USER_ELIGIBILITY_PATH.exists():
        for uid, by_event in json.loads(USER_ELIGIBILITY_PATH.read_text()).items():
            for eid, eligibility in by_event.items():
                if eligibility is True or eligibility == "error":
                    continue
                cur.execute("INSERT INTO ineligible_starter (event_id, person_id) VALUES (?, ?)",
                            (int(eid), int(uid)))

    # Foreign guest-team club codes with an unambiguous, non-Austrian naming
    # convention - confirmed real: event 4434, an ÖM round hosted jointly
    # with the international Alpe-Adria Cup, where most of the field is
    # "AA <region>" regional team codes or Italian federation "NNNN -
    # <club>" numeric-prefixed codes (0392 - A.S.D. SEMIPER...), none with
    # an ANNE account to check via the API. This is deliberately a narrow,
    # literal-prefix match rather than a blanket "no ANNE account" or
    # "unmatched club" rule - both tried and rejected earlier for wrongly
    # catching real Austrians (Vera Arbter by nationality; separately,
    # "Cleo Machold" by a malformed club field on one legacy row, unlinked
    # from her real ANNE account for a data-quality reason that has nothing
    # to do with actually being foreign). "AA " itself isn't purely
    # foreign, either - the Alpe-Adria Cup fields Austria's own bordering
    # provinces alongside genuinely foreign regions (confirmed real: the
    # full "AA <region>" list in this dataset has "AA Team Kärnten" and
    # "AA Team Steiermark" - both Austrian - next to Bayern/DE,
    # Trentino-Südtirol/Veneto/Friuli/Lombardia/IT, Hrvatska/HR,
    # Slovenia/SI, Ticino/CH, Baranya/Somogy/Vas/Zala/HU), so those two are
    # carved back out.
    cur.execute("""
        INSERT INTO ineligible_starter (event_id, person_id)
        SELECT DISTINCT s.event_id, r.person_id FROM result r JOIN stage s ON s.id = r.stage_id
        WHERE (r.club LIKE 'AA %' AND r.club NOT LIKE 'AA %Kärnten%'
               AND r.club NOT LIKE 'AA %Steiermark%')
           OR r.club GLOB '[0-9][0-9][0-9][0-9] - *'
    """)

    # Broader pass: FOREIGN_CLUB_KEYWORDS (see its own docstring for how it
    # was derived and why it stays deliberately conservative). Matched in
    # Python rather than one giant SQL OR chain, since a ~90-keyword list is
    # far more maintainable as a plain list than as SQL string literals.
    cur.execute("""
        SELECT DISTINCT s.event_id, r.person_id, r.club FROM result r
        JOIN stage s ON s.id = r.stage_id
        WHERE r.club IS NOT NULL AND r.club != ''
    """)
    for eid, pid, club in cur.fetchall():
        lc = club.lower()
        if any(k in lc for k in FOREIGN_CLUB_KEYWORDS):
            cur.execute("INSERT INTO ineligible_starter (event_id, person_id) VALUES (?, ?)",
                        (eid, pid))

    # "vereinslos" (clubless) starters, regardless of nationality - see
    # CLUBLESS_CLUB_RE's own docstring.
    cur.execute("""
        SELECT DISTINCT s.event_id, r.person_id, r.club FROM result r
        JOIN stage s ON s.id = r.stage_id
        WHERE r.club IS NOT NULL AND r.club != ''
    """)
    for eid, pid, club in cur.fetchall():
        if CLUBLESS_CLUB_RE.search(club):
            cur.execute("INSERT INTO ineligible_starter (event_id, person_id) VALUES (?, ?)",
                        (eid, pid))

    for eid, pid in cur.execute(
            "SELECT DISTINCT event_id, person_id FROM ineligible_starter").fetchall():
        cur.execute("""
            UPDATE result SET championship = NULL
            WHERE championship IS NOT NULL AND person_id = ?
              AND stage_id IN (SELECT id FROM stage WHERE event_id = ?)
        """, (pid, eid))

    # A pair/relay/team result stands or falls together: one ineligible
    # member (foreign guest, insufficient residency, ...) taints the whole
    # unit's placement, not just their own row - the ELIGIBLE partner
    # doesn't get to keep the medal alone. Confirmed real: event 4315
    # ("ÖM Nacht"), Damen bis 14 pair Cleo Machold/Yelyzaveta Yevtushenko -
    # Yevtushenko doesn't meet the 3-years-residency criterion, and the
    # event's own official Meisterschaftswertung extract shows the whole
    # pair skipped, with the next-placed eligible pair (Tandl/Asseg)
    # promoted to bronze instead - not Machold alone keeping it.
    cur.execute("""
        UPDATE result SET championship = NULL
        WHERE championship IS NOT NULL
          AND result_kind IN ('relay', 'pair', 'team')
          AND EXISTS (
            SELECT 1 FROM result r2 JOIN stage s2 ON s2.id = r2.stage_id
            WHERE r2.stage_id = result.stage_id AND r2.category = result.category
              AND r2.rank = result.rank
              AND EXISTS (SELECT 1 FROM ineligible_starter i
                          WHERE i.event_id = s2.event_id AND i.person_id = r2.person_id))
    """)

    # A podium needs at least 3 ELIGIBLE starters - fewer than that and no
    # official ÖM/ÖSTM medal is awarded at all, gold included (confirmed
    # real: event 4884, "Damen bis 17" had only 2 starters total, no
    # medals). "Starters", not "finishers": a DNF/MP/DSQ still counts (they
    # started), only DNS (never started at all) doesn't (confirmed real:
    # event 4306, H17 - Anton Greiner/AUT DNF still counts as one of the 3
    # eligible starters alongside Ochenbauer and Urbanek, even without a
    # rank of his own). "Eligible", not "raw": every exclusion collected
    # above applies here too - none of the ineligible_starter rows count
    # toward the 3, so a category still clears the threshold on its
    # remaining real Austrians even if the excluded ones were part of its
    # nominal field.
    # This rule applies to every result_kind, relay/team/pair included -
    # confirmed real: event 4829's "Herren ab 210" relay only LOOKS like it
    # has 2 starting teams if you count distinct ranks (a DNF team gets no
    # rank at all), but it genuinely had 3 teams on the start line
    # (Naturfreunde Wien 1, OLG Ströck Wien 1, and ASKÖ Henndorf
    # Orienteering 1 - whose own member mispunched, DNF, no rank), clearing
    # the threshold same as any individual category would. The unit being
    # counted just isn't the same for every kind: one "starter" is one
    # PERSON for an individual race, but one TEAM for relay/team/pair (its
    # members all share one identical `club` value, so COUNT(DISTINCT
    # person_id) would count a single team as 2-4 "starters" instead of 1 -
    # counting DISTINCT club_ instead of DISTINCT person_id for those kinds
    # fixes that).
    cur.execute("""
        UPDATE result SET championship = NULL
        WHERE championship IS NOT NULL
          AND (stage_id, category) IN (
            SELECT r.stage_id, r.category FROM result r JOIN stage s ON s.id = r.stage_id
            WHERE r.status IN ('ok', 'dnf', 'mp', 'dsq')
              AND NOT EXISTS (SELECT 1 FROM ineligible_starter i
                              WHERE i.event_id = s.event_id AND i.person_id = r.person_id)
            GROUP BY r.stage_id, r.category
            HAVING COUNT(DISTINCT CASE WHEN r.result_kind = 'individual'
                                       THEN 'p' || r.person_id ELSE 'c' || r.club END) < 3)
    """)
    cur.execute("DROP TABLE ineligible_starter")

    # national_rank: placement among ONLY the finishers still championship-
    # tagged after the exclusions above, which is what the medal table (Gold/
    # Silber/Bronze) should key off instead of the overall race `rank` - a
    # foreign/ineligible finisher who placed ahead no longer shifts the real
    # champion down to "silver".
    #
    # Counts DISTINCT COMPETITOR UNITS strictly ahead, not distinct RANK
    # VALUES - those differ exactly when two separate competitors/teams tie
    # for the same raw rank, which an early version of this query got wrong.
    # Counting distinct rank VALUES was meant to solve a real problem (a
    # relay/pair team's members all share one identical rank - they're the
    # SAME result, not separate competitors, so raw ROWS ahead would triple/
    # n-tuple the count - a plain COUNT(*) put a 3rd-place trio's own
    # national_rank at 7, not 3, once two 3-person teams outranked them), but
    # it silently swallowed a much rarer case too: when two DIFFERENT teams
    # genuinely tie for a place, they share one rank VALUE just as much as
    # one team's own members do, so counting distinct rank values collapsed
    # them into a single "ahead" unit instead of two - confirmed real: event
    # 4048 ("OL Südbgld." ÖM Nacht), "Damen bis 14" pair category, where two
    # different pairs tied for gold (4 people, one rank value) and the next
    # pair down wrongly computed to silver instead of bronze, because "how
    # many rank values beat me" (1) undercounts "how many competitors beat
    # me" (2) whenever a tie is involved - this is also why simply reusing
    # rank's own already-correct skip-numbering doesn't work: rank counts
    # ALL starters including ineligible ones, and national_rank's whole
    # point is to close the gap left by REMOVING them, which only a fresh
    # unit count (not an offset from rank) gets right.
    #
    # A per-kind unit count, not a per-kind unit IDENTIFIER: an individual
    # row already IS one unit (COUNT it directly); a relay/team row's shared
    # `club` is already a synthesized, genuinely-unique-per-instance team
    # name ("Naturfreunde Wien 1" vs "...2"), so COUNT(DISTINCT club) safely
    # collapses each team's several member-rows back to one - both reused
    # from the identical starter-count pattern just above. A 'pair' row's
    # `club` is NOT similarly unique, though - it's just the shared OFFICIAL
    # club name, and two DIFFERENT pairs from the same club (unremarkable -
    # confirmed real in this exact category: event 4048, two separate "OC
    # Fürstenfeld" pairs at different, non-tied ranks) would collapse into
    # one club-identified "unit" despite never actually tying with each
    # other.
    #
    # A first attempt divided the count of 'pair' rows by a fixed 2, on the
    # assumption a pair always has exactly 2 members. That's true for the
    # run-in-pairs bis-12/14 categories the result_kind was built for, but
    # 'pair' also gets reused for 3-person "Gesamtalter" family/veteran team
    # categories (confirmed real: event 4515's "Gesamtalter Damen ab 165") -
    # dividing 3-member teams' row counts by 2 still under/over-counts. Real
    # team identity - built once into the pair_unit temp table below, not
    # inline in this query - fixes both at once: every pair/team member's
    # own `note` ("Partner: X, Y") already lists every OTHER member by name,
    # so the FULL member set (own name + partners) is reconstructable per
    # row regardless of team size, and its sorted form is a stable, self-
    # consistent identity - the same string for every member of one team,
    # different from any other team's even if they share a club or tie on
    # rank - without ever needing to guess which specific rows paired up.
    # rank IS NOT NULL on both sides: a NULL rank (unplaced - e.g. a relay
    # team with a mispunched leg) must never compute a national_rank at all
    # ('r2.rank < result.rank' with either side NULL is neither true nor
    # false in SQL, so it silently drops out of the COUNT rather than
    # erroring - a bare championship-tagged, unranked row would otherwise
    # get national_rank = 1, "beating" everyone, since COUNT(...) = 0 + 1).
    cur.execute("CREATE TEMP TABLE pair_unit (result_id INTEGER PRIMARY KEY, unit_key TEXT)")
    cur.execute("SELECT r.id, p.name, r.note FROM result r JOIN person p ON p.id = r.person_id "
                "WHERE r.result_kind = 'pair'")
    pair_units = []
    for rid, name, note in cur.fetchall():
        partners = note[len("Partner: "):].split(", ") if note and note.startswith("Partner: ") else []
        # name_key(), not the raw strings: `note`'s partner names are frozen
        # in whatever raw order the source printed them ("Eichmüller Maya"),
        # while `person.name` gets reordered to "Firstname Lastname" during
        # identity resolution ("Emma Frey") - comparing the raw strings for
        # the SAME two people never matches, silently splitting one pair
        # into two fake half-units (confirmed real: event 4048, doubled
        # every pair's own count and wrecked every national_rank downstream
        # of it). name_key() is already this codebase's order/case/accent-
        # insensitive identity key for exactly this kind of comparison.
        key = "|".join(sorted(name_key(n) for n in [name, *partners])) if partners else f"solo-{rid}"
        pair_units.append((rid, key))
    cur.executemany("INSERT INTO pair_unit VALUES (?, ?)", pair_units)

    cur.execute("""
        UPDATE result SET national_rank = (
            SELECT COUNT(CASE WHEN r2.result_kind = 'individual' THEN 1 END)
                 + COUNT(DISTINCT CASE WHEN r2.result_kind = 'pair' THEN pu2.unit_key END)
                 + COUNT(DISTINCT CASE WHEN r2.result_kind IN ('relay', 'team')
                                        THEN r2.club END)
                 + 1
            FROM result r2
            LEFT JOIN pair_unit pu2 ON pu2.result_id = r2.id
            WHERE r2.stage_id = result.stage_id AND r2.category = result.category
              AND r2.status = 'ok' AND r2.championship IS NOT NULL
              AND r2.rank IS NOT NULL AND r2.rank < result.rank)
        WHERE championship IS NOT NULL AND status = 'ok' AND rank IS NOT NULL
    """)
    cur.execute("DROP TABLE pair_unit")

    # compute time_behind for legacy rows from winner time per category
    cur.execute("""
        UPDATE result SET time_behind_s = time_s - (
            SELECT winner_time_s FROM category_stats cs
            WHERE cs.stage_id = result.stage_id AND cs.category = result.category)
        WHERE time_behind_s IS NULL AND time_s IS NOT NULL AND status = 'ok'
    """)

    cur.execute("PRAGMA user_version = 2")
    con.commit()
    for table in ("event", "stage", "person", "person_identifier", "person_alias",
                  "person_redirect", "source_document", "result"):
        print(table, cur.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    print(f"api results: {n_api}, legacy results: {n_legacy}, "
          f"championship rows from title fallback: {n_title_fallback}, "
          f"championship rows stripped by eligibility check: {n_eligibility}")

    # Member-mapping build byproducts (private files, regenerated each run).
    if members:
        # suggest likely roster candidates for each unplaced club runner by
        # shared name tokens, so the review step is pick-from-a-shortlist
        member_tokens = [(m, set(m["name_key"].split())) for m in members]
        for e in pending_review:
            toks = set(e["name_key"].split())
            scored = sorted(
                ((len(toks & mt), m) for m, mt in member_tokens if toks & mt),
                key=lambda x: -x[0])[:4]
            e["candidates"] = [{"ofol_id": m["ofol_id"], "name": m["name"],
                                "year_of_birth": m["yob"]} for _, m in scored]
        pending_review.sort(key=lambda e: -e["nfw_results"])
        # data/private/ is gitignored, so a fresh checkout (CI) never has it -
        # only local dev, where the private CSV/ledger were placed by hand,
        # happens to have created it already. Confirmed real: this crashed
        # every CI build once the committed member index gave it members to
        # process, since load_member_registry() no longer short-circuits to
        # [] there.
        PENDING_REVIEW_PATH.parent.mkdir(parents=True, exist_ok=True)
        PENDING_REVIEW_PATH.write_text(json.dumps(pending_review, ensure_ascii=False, indent=1))
        MEMBER_CONFLICTS_PATH.write_text(json.dumps(conflicts, ensure_ascii=False, indent=1))
        print(f"members: {len(members)} in roster, {len(member_canonical)} matched to results, "
              f"{len(pending_review)} club runners pending review, {len(conflicts)} id conflicts "
              f"-> {PENDING_REVIEW_PATH.name}, {MEMBER_CONFLICTS_PATH.name}")

        # Regenerate the committed public member index from the private roster:
        # ÖFOL-ID + name + birth YEAR, only for members who actually raced (their
        # ÖFOL-ID = person.id ended up with >=1 result). No full birthdates, no
        # gender, no members without results - all public-grade. Only when built
        # from the CSV (locally); a CI build already reads this file, so it must
        # not overwrite it with a subset of itself.
        if MEMBER_CSV_PATH.exists():
            idx = [{"ofol_id": m["ofol_id"], "name": m["name"], "yob": m["yob"]}
                   for m in sorted(members, key=lambda m: m["ofol_id"])
                   if cur.execute("SELECT 1 FROM result WHERE person_id = ?",
                                  (m["ofol_id"],)).fetchone()]
            MEMBER_INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
            MEMBER_INDEX_PATH.write_text(json.dumps(idx, ensure_ascii=False, indent=1))
            print(f"member index: {len(idx)} members with results -> {MEMBER_INDEX_PATH}")

        # Keep the committed public ledger in sync with the private working one
        # (drop only the free-text split_pending to-do notes). Public-grade, so
        # CI can apply the same decisions without the private file.
        if MEMBER_MAPPING_PATH.exists():
            public_ledger = {k: v for k, v in ledger.items() if k != "split_pending"}
            MEMBER_MAPPING_PUBLIC_PATH.write_text(
                json.dumps(public_ledger, ensure_ascii=False, indent=1))

    cur.execute("VACUUM")
    con.close()
    gz_path = DB_PATH.with_suffix(".db.gz")
    gz_path.write_bytes(gzip.compress(DB_PATH.read_bytes(), 9))
    print(f"wrote {DB_PATH} ({DB_PATH.stat().st_size / 1e6:.1f} MB, "
          f"gz {gz_path.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
