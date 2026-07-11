#!/usr/bin/env python3
"""Compile raw ANNE snapshots + normalized legacy results into site/data/results.db.

Person identity: ANNE userId is authoritative (positive ids). Legacy results
without a userId are matched to existing persons by (normalized name, year of
birth) and otherwise get synthetic negative ids. Derived statistics (starters,
classified count, winner time) are computed in the category_stats view, never
stored.
"""
import gzip
import json
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


# Fallback for legacy events where no result row anywhere carries a champion
# annotation to detect at all (confirmed by hand: several real ÖM/ÖSTM-titled
# exports simply never print one) - the event's own title is the only signal
# left. ÖM = Österreichische Meisterschaft, ÖSTM = Österreichische
# Staatsmeisterschaft; a title spelling out both ("ÖSTM/ÖM ...", "ÖSTM &
# ÖM ...") or the parenthetical "Ö(ST)M" form grants both at once, while a
# title with only one grants only that one - confirmed against events that
# *do* have per-row annotations (e.g. "ÖSTM Mittel (6.AC Mittel)" only ever
# tags the Elite category ÖSTM, never ÖM elsewhere in that same race).
OESTM_TITLE_RE = re.compile(r"(?i)ö\(?st\)?m")
OM_TITLE_RE = re.compile(r"(?i)(?<![a-zäöüß])öm(?![a-zäöüß])")
COMBINED_TITLE_RE = re.compile(r"(?i)ö\(st\)m")


def classify_title_championships(title):
    if not title:
        return set()
    if COMBINED_TITLE_RE.search(title):
        return {"ÖM", "ÖSTM"}
    types = set()
    if OESTM_TITLE_RE.search(title):
        types.add("ÖSTM")
    if OM_TITLE_RE.search(title):
        types.add("ÖM")
    return types


# Which categories are actually eligible, learned from every category that a
# real per-row annotation already confirmed (see the accompanying research):
# ÖSTM only ever lands on an Elite/near-elite category (D21E/H21E, D19-/H19-,
# "Allgemeine Klasse", "Staatsmeisterschaft Damen/Herren"); ÖM spans ordinary
# age classes starting at the "12 and under" bracket (D-12/H-12/D12 etc.) -
# never younger, and never non-competitive groupings (Bahn course listings,
# Neulinge/Familie/Hobby fun categories, school Mannschaft rosters, ...).
ELITE_CAT_RE = re.compile(
    r"(?i)\belite\b|allgemeine\s*klasse|staatsmeisterschaft|"
    r"(?<![a-zäöü0-9])(1[6-9]|2[01])e(?![a-zäöü0-9])")
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
    r"viertelfinale|halbfinale|b-finale")


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
TITLE_FALLBACK_EXCLUDE_EVENTS = {4783}

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
}

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
    cover them."""
    cur.execute("""
        SELECT DISTINCT s.id, r.category, e.title, e.id FROM result r
        JOIN stage s ON s.id = r.stage_id
        JOIN event e ON e.id = s.event_id
        WHERE r.status = 'ok' AND r.result_kind IN ('individual', 'relay')
          AND NOT EXISTS (SELECT 1 FROM result r2
                            WHERE r2.stage_id = s.id AND r2.category = r.category
                              AND r2.championship IS NOT NULL)
    """)
    candidates = cur.fetchall()
    n = 0
    for sid, category, title, eid in candidates:
        if eid in TITLE_FALLBACK_EXCLUDE_EVENTS:
            continue
        types = classify_title_championships(title)
        if not types:
            continue
        if "ÖSTM" in types and is_ostm_eligible_category(category):
            champ = "ÖSTM"
        elif "ÖM" in types and is_om_eligible_category(category):
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
                          AND result_kind IN ('individual', 'relay')""", (champ, sid, category))
        n += cur.rowcount
    return n


OFFICIAL_CLUBS_PATH = ROOT / "data" / "official_clubs.json"
CLUB_SUFFIX_NUM_RE = re.compile(r"^(.+)\s(\d)$")
CLUB_PREFIX_CODE_RE = re.compile(r"^([A-Za-zÄÖÜäöüß]{2,6})\s+(.+)$")


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
    while cur not in official:
        changed = False
        m = CLUB_SUFFIX_NUM_RE.match(cur)
        if m and m.group(1).strip() in official:
            cur = m.group(1).strip()
            changed = True
        if not changed:
            m = CLUB_PREFIX_CODE_RE.match(cur)
            if m and m.group(2).strip() in official:
                cur = m.group(2).strip()
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
        self.first_names = set()               # lowercased firstNames from the API
        self.last_names = set()                # lowercased lastNames from the API
        self.next_synthetic = -1

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
            pid = self.next_synthetic
            self.next_synthetic -= 1
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
             e.get("competitionType"), e.get("sportType"), e.get("eventType"),
             e.get("url")))
    return events


RESULT_COLS = ("stage_id", "person_id", "category", "category_full", "club", "official_club",
               "rank", "status", "time_s", "time_behind_s", "out_of_competition",
               "course_length_m", "course_climb_m", "course_controls",
               "result_kind", "note", "source", "championship")


def insert_result(cur, **kw):
    kw.setdefault("out_of_competition", 0)
    kw.setdefault("result_kind", "individual")
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


def load_anne_results(cur, events, persons, stage_ids):
    n = 0
    for path in sorted((RAW / "results").glob("*.json")):
        eid = int(path.stem)
        event = events.get(eid)
        if not event:
            continue
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
                n += insert_anne_relay(cur, persons, sid, cat, r)
                continue
            name = clean_name(f"{r.get('firstName') or ''} {r.get('lastName') or ''}".strip())
            # some old imports carry bib/SI numbers or 'empty' placeholders
            if not is_valid_name(name) or "empty" in name.lower():
                continue
            uid = r.get("userId")
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
                          source="anne-api", championship=anne_championship(r))
            n += 1
    return n


def insert_anne_relay(cur, persons, sid, cat, team):
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
        pid = persons.from_legacy(nm, None)
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
        insert_result(cur, stage_id=sid, person_id=pid, category=cat,
                      category_full=team.get("categoryTitle"), club=relay_club,
                      official_club=canonicalize_official_club(relay_club, OFFICIAL_CLUBS),
                      rank=team.get("rank"),
                      status=ANNE_STATUS.get(m.get("classification")
                                             or team.get("classification"), "unknown"),
                      time_s=leg_time, result_kind="relay",
                      note=" · ".join(note_bits), source="anne-api",
                      championship=championship)
        n += 1
    return n


def load_legacy_results(cur, events, persons, stage_ids, anne_event_ids):
    n = 0
    canonical = re.compile(r"^\d+-(?:club)?\d+\.json$")
    docs = [json.loads(p.read_text())
            for p in sorted(NORM.glob("*.json")) if canonical.match(p.name)]
    # plain result lists before split-time lists, so duplicates resolve
    # in favour of the cleaner source
    docs.sort(key=lambda d: (d["eventId"], "split" in d["fileName"].lower()))
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
        if eid in anne_event_ids:
            continue  # structured API data wins over legacy files
        # team (Mannschaft) result lists give only member surnames + a club +
        # a single team time — no first names, so members can't be resolved to
        # individual runners (a surname+club match linked only ~17%). Keep them
        # as one team-level row each; they're shown on event pages and excluded
        # from the runner directory.
        is_team = event.get("competitionType") == "team"
        event_dates = sorted(dates_by_event.get(eid) or [])
        if len(event_dates) > 1 and doc.get("docDate") in event_dates:
            sid = dated_stage(cur, event, stage_ids, doc["docDate"],
                               event_dates.index(doc["docDate"]) + 1)
        else:
            sid = default_stage(cur, event, stage_ids)
        flip_doc = detect_lastname_firstname_doc(doc["categories"], persons.first_names)
        for cat in doc["categories"]:
            for r in cat["results"]:
                if r.get("status") == "dns":
                    continue
                # a parsed row may carry several runners (a pair): the parser
                # emits one entry per runner already, each with its own name
                # and a note; treat them uniformly here
                name = clean_name(r["name"])
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
                elif is_team and len(name.split()) >= 3:
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
                              championship=r.get("championship"))
                n += 1
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
    by_name_group = {}
    for pid, (name, nk, yob, nat, iof) in persons.by_id.items():
        by_name_group.setdefault(nk, []).append(pid)

    merge_map = {}
    for nk, ids in by_name_group.items():
        anne_ids = [i for i in ids if i > 0]
        synth_ids = [i for i in ids if i < 0]
        if not anne_ids:
            continue  # no authoritative identity to arbitrate against
        distinct_yobs = {persons.by_id[a][2] for a in anne_ids
                          if persons.by_id[a][2] is not None}
        if len(distinct_yobs) <= 1:
            target = min(anne_ids)
            for a in anne_ids:
                if a != target:
                    merge_map[a] = target
            target_yob = next(iter(distinct_yobs), None)
            if target_yob is not None and persons.by_id[target][2] is None:
                cur_p = persons.by_id[target]
                persons.by_id[target] = (cur_p[0], cur_p[1], target_yob, cur_p[3], cur_p[4])
            for s in synth_ids:
                s_yob = persons.by_id[s][2]
                if s_yob is None or target_yob is None or s_yob == target_yob:
                    merge_map[s] = target
        else:
            by_yob = {}
            for a in anne_ids:
                by_yob.setdefault(persons.by_id[a][2], []).append(a)
            for s in synth_ids:
                s_yob = persons.by_id[s][2]
                match = by_yob.get(s_yob)
                if s_yob is not None and match and len(match) == 1:
                    merge_map[s] = match[0]

    # merge_map can chain (a synthetic id may map to an id that itself got
    # merged); resolve to final targets before applying
    def resolve(pid):
        while pid in merge_map:
            pid = merge_map[pid]
        return pid

    final_names = defaultdict(Counter)
    final_auth = defaultdict(Counter)
    for pid, counts in persons.name_seen.items():
        final_names[resolve(pid)].update(counts)
    for pid, counts in persons.name_auth.items():
        final_auth[resolve(pid)].update(counts)

    for old in list(merge_map):
        new = resolve(old)
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
        cur.execute("INSERT INTO person VALUES (?,?,?,?,?,?)",
                    (pid, name, key, yob, nat, iof))

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

    n_eligibility = apply_championship_eligibility_overrides(cur)

    # KNOWN_INELIGIBLE_RESULTS remains for the cases the API can't cover:
    # someone with no ANNE account at all (a genuinely one-off foreign
    # guest, e.g. Milja Väätäjä/Ivan Serafini - confirmed by hand to not
    # exist in ANNE's user database whatsoever).
    for eid, pname in KNOWN_INELIGIBLE_RESULTS:
        cur.execute("""
            UPDATE result SET championship = NULL
            WHERE championship IS NOT NULL
              AND stage_id IN (SELECT id FROM stage WHERE event_id = ?)
              AND person_id IN (SELECT id FROM person WHERE name = ?)
        """, (eid, pname))

    # national_rank: placement among ONLY the finishers still championship-
    # tagged after the exclusions above, which is what the medal table (Gold/
    # Silber/Bronze) should key off instead of the overall race `rank` - a
    # foreign/ineligible finisher who placed ahead no longer shifts the real
    # champion down to "silver". Deliberately no id-based tiebreak for equal
    # `rank`: a relay/pair team's members all share one identical rank (they
    # ARE the same result, not separate competitors), so counting only
    # STRICTLY lower ranks as "ahead" gives every teammate the same, correct
    # national_rank - an id-based tiebreak previously split them apart,
    # arbitrarily bumping one teammate to "silver" for a gold-medal team.
    # COUNT(DISTINCT r2.rank), not COUNT(*): a relay category tags every row
    # of every team (so the medal table's rank<=3 filter can still find a
    # team ranked 3rd overall even though most rows aren't medal-relevant at
    # all), so counting raw ROWS ahead - 3 members each for however many
    # teams placed better - triples/n-tuples the count instead of counting
    # the number of teams (a plain COUNT(*) put a 3rd-place trio's own
    # national_rank at 7, not 3, once two 3-person teams outranked them).
    # rank IS NOT NULL on both sides: a NULL rank (unplaced - e.g. a relay
    # team with a mispunched leg) must never compute a national_rank at all
    # ('r2.rank < result.rank' with either side NULL is neither true nor
    # false in SQL, so it silently drops out of the COUNT rather than
    # erroring - a bare championship-tagged, unranked row would otherwise
    # get national_rank = 1, "beating" everyone, since COUNT(...) = 0 + 1).
    cur.execute("""
        UPDATE result SET national_rank = (
            SELECT COUNT(DISTINCT r2.rank) + 1 FROM result r2
            WHERE r2.stage_id = result.stage_id AND r2.category = result.category
              AND r2.status = 'ok' AND r2.championship IS NOT NULL
              AND r2.rank IS NOT NULL AND r2.rank < result.rank)
        WHERE championship IS NOT NULL AND status = 'ok' AND rank IS NOT NULL
    """)

    # compute time_behind for legacy rows from winner time per category
    cur.execute("""
        UPDATE result SET time_behind_s = time_s - (
            SELECT winner_time_s FROM category_stats cs
            WHERE cs.stage_id = result.stage_id AND cs.category = result.category)
        WHERE time_behind_s IS NULL AND time_s IS NOT NULL AND status = 'ok'
    """)

    con.commit()
    for table in ("event", "stage", "person", "result"):
        print(table, cur.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    print(f"api results: {n_api}, legacy results: {n_legacy}, "
          f"championship rows from title fallback: {n_title_fallback}, "
          f"championship rows stripped by eligibility check: {n_eligibility}")
    cur.execute("VACUUM")
    con.close()
    gz_path = DB_PATH.with_suffix(".db.gz")
    gz_path.write_bytes(gzip.compress(DB_PATH.read_bytes(), 9))
    print(f"wrote {DB_PATH} ({DB_PATH.stat().st_size / 1e6:.1f} MB, "
          f"gz {gz_path.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
