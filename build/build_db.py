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
USER_INDEX_PATH = RAW / "user_index.json"
NORM = ROOT / "data" / "normalized"
DB_PATH = ROOT / "site" / "data" / "results.db"
REVIEW_DECISIONS_PATH = ROOT / "data" / "review" / "verification.json"
CHAMPIONSHIP_CATALOG_PATH = ROOT / "data" / "review" / "championship_catalog.json"
CLUB_JURISDICTIONS_PATH = ROOT / "data" / "club_jurisdictions.json"
EXCLUDED_EVENTS_PATH = ROOT / "data" / "review" / "excluded_events.json"


def load_event_exclusions(path=EXCLUDED_EVENTS_PATH):
    """Return event IDs deliberately omitted from the published database.

    Raw and normalized evidence stays in the repository so a later recovered
    official source can reverse the decision.  The exclusion acts before the
    event row is inserted, so stages, results and derived person/statistic
    rows cannot leak into the published model.
    """
    if not path.exists():
        return {}
    return {int(event_id): value for event_id, value
            in json.loads(path.read_text()).items()}


EXCLUDED_EVENTS = load_event_exclusions()

# Dedicated national result sources discovered in ANNE's attachment catalog.
# ``supplemental`` means the document is a real stage missing from ANNE's
# structured result snapshot; ``evidence`` means it overlaps existing rows and
# is retained only as provenance/verification, never duplicated publicly.
CHAMPIONSHIP_SOURCE_CONFIG = {
    "4315-2": {"mode": "evidence", "championship": "ÖM",
               "scope": "medal_places_only", "explicit_eligibility": True},
    "5203-1": {"mode": "supplemental", "championship": "ÖSTM",
               "scope": "full_field", "explicit_eligibility": True},
    "5396-2": {"mode": "supplemental", "championship": "ÖM",
               "scope": "full_field", "explicit_eligibility": True,
               "stage_title": r"Verfolgung"},
    "5396-3": {"mode": "supplemental", "championship": "ÖM",
               "scope": "full_field", "explicit_eligibility": False,
               "stage_title": r"Verfolgung"},
    "5437-0": {"mode": "evidence", "championship": "ÖSTM",
               "scope": "full_field", "explicit_eligibility": False,
               "stage_title": r"ÖSTM\s+Sprint$"},
    "5437-2": {"mode": "evidence", "championship": "ÖSTM",
               "scope": "full_field", "explicit_eligibility": False,
               "stage_title": r"Knock\s*Out"},
}

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
    year_of_birth INTEGER, nationality TEXT
);
CREATE TABLE person_identifier (
    scheme TEXT NOT NULL,             -- oefol_id
    identifier TEXT NOT NULL,
    person_id INTEGER NOT NULL REFERENCES person(id),
    identifier_state TEXT NOT NULL,   -- authoritative|independently_confirmed|redirected
    source TEXT NOT NULL,             -- anne-user-registry|club-book-of-record|result-observation
    observed_at TEXT,
    PRIMARY KEY (scheme, identifier, person_id, source)
);
CREATE TABLE person_club_membership (
    person_id INTEGER NOT NULL REFERENCES person(id),
    club TEXT NOT NULL,
    sport_type TEXT NOT NULL,
    valid_from TEXT NOT NULL DEFAULT '',
    valid_to TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    source TEXT NOT NULL,             -- anne-user-registry
    observed_at TEXT,
    PRIMARY KEY (person_id, club, sport_type, valid_from, source)
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
CREATE TABLE person_tombstone (
    old_id INTEGER PRIMARY KEY,
    reason TEXT NOT NULL
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
CREATE TABLE result_list (
    id TEXT PRIMARY KEY,
    stage_id INTEGER NOT NULL REFERENCES stage(id),
    source_document_id TEXT NOT NULL REFERENCES source_document(id),
    category TEXT NOT NULL,
    category_full TEXT,
    declared_starters INTEGER,
    parsed_entries INTEGER NOT NULL DEFAULT 0,
    parsed_rows INTEGER NOT NULL DEFAULT 0,
    ranking_basis TEXT NOT NULL DEFAULT 'time', -- time|score|other
    course_length_m INTEGER,
    course_climb_m INTEGER,
    course_controls INTEGER,
    input_fingerprint TEXT NOT NULL,
    UNIQUE (stage_id, source_document_id, category)
);
CREATE TABLE result (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stage_id INTEGER NOT NULL REFERENCES stage(id),
    person_id INTEGER REFERENCES person(id),
    result_list_id TEXT REFERENCES result_list(id),
    category TEXT NOT NULL,
    category_full TEXT,
    club TEXT,
    official_club TEXT,               -- club canonicalized to ANNE's /v1/club
                                       -- registry, for the Vereine section only
    rank INTEGER,
    status TEXT NOT NULL,            -- ok|dnf|dsq|mp|dns|unknown
    time_s INTEGER,
    time_behind_s INTEGER,
    out_of_competition INTEGER NOT NULL DEFAULT 0,
    course_length_m INTEGER, course_climb_m INTEGER, course_controls INTEGER,
    result_kind TEXT NOT NULL DEFAULT 'individual',  -- individual|pair|relay|team|family
    note TEXT,                       -- e.g. "Partner: X" / "Staffel Y, Leg N"
    team_number TEXT,                -- source start/bib number, stable within a list
    team_name TEXT,                  -- source team label, not canonicalized club
    leg_number INTEGER,
    leg_count INTEGER,
    individual_status TEXT,          -- this leg/member only
    team_status TEXT,                -- overall relay/team classification
    team_time_s INTEGER,
    observed_team_time TEXT,
    source TEXT NOT NULL,            -- anne-api|sportsoftware-html|...
    source_document_id TEXT REFERENCES source_document(id),
    observed_name TEXT,              -- source spelling before canonical identity resolution
    observed_club TEXT,              -- source spelling before club canonicalization
    observed_user_id TEXT,           -- source-supplied identity, never inferred
    observed_category TEXT,
    observed_rank TEXT,
    observed_status TEXT,
    observed_time TEXT,
    identity_basis TEXT NOT NULL DEFAULT 'unknown',
    identity_confidence REAL NOT NULL DEFAULT 0.0,
    identity_state TEXT NOT NULL DEFAULT 'provisional',
    championship TEXT,               -- ÖM|ÖSTM, when this (stage, category)
                                      -- is a genuine Austrian championship
    championship_eligibility_state TEXT NOT NULL DEFAULT 'unknown',
                                      -- eligible|ineligible|provisional|unknown
    championship_eligibility_basis TEXT NOT NULL DEFAULT 'none',
                                      -- event-time evidence used for this row
    championship_source_scope TEXT NOT NULL DEFAULT 'inferred',
                                      -- full_field|medal_places_only|winner_only|inferred
    national_rank INTEGER,           -- placement among ONLY championship-
                                      -- eligible (Austrian) finishers, which
                                      -- can differ from the overall race
                                      -- `rank` when a foreign/ineligible
                                      -- competitor placed ahead - see the
                                      -- national-rank computation in main()
    observed_nation TEXT             -- raw PDF/HTML Nat/Country cell; in a
                                      -- joint Landes-MS this can be W/NÖ/B/St
);
CREATE TABLE championship_source_entry (
    id TEXT PRIMARY KEY,
    stage_id INTEGER NOT NULL REFERENCES stage(id),
    source_document_id TEXT NOT NULL REFERENCES source_document(id),
    category TEXT NOT NULL,
    category_key TEXT NOT NULL,
    observed_name TEXT NOT NULL,
    observed_name_key TEXT NOT NULL,
    observed_club TEXT,
    observed_rank INTEGER,
    observed_status TEXT,
    result_id INTEGER REFERENCES result(id),
    championship_type TEXT NOT NULL, -- ÖM|ÖSTM
    evidence_kind TEXT NOT NULL,     -- official_championship_inclusion|official_championship_field
    source_scope TEXT NOT NULL       -- full_field|medal_places_only
);
CREATE TABLE verification_assertion (
    scope_type TEXT NOT NULL,        -- result_list|championship
    scope_key TEXT NOT NULL,
    dimension TEXT NOT NULL,         -- completeness|parsing|identity|ranking|rules
    state TEXT NOT NULL,             -- confirmed|flagged|not_applicable
    input_fingerprint TEXT NOT NULL,
    reviewer TEXT,
    reviewed_at TEXT,
    note TEXT,
    PRIMARY KEY (scope_type, scope_key, dimension)
);
CREATE TABLE audit_issue (
    id TEXT PRIMARY KEY,
    result_list_id TEXT REFERENCES result_list(id),
    result_id INTEGER REFERENCES result(id),
    code TEXT NOT NULL,
    severity TEXT NOT NULL,          -- blocker|warning|info
    message TEXT NOT NULL,
    auto_resolvable INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE championship_rule_set (
    id TEXT PRIMARY KEY,
    jurisdiction TEXT NOT NULL,
    version INTEGER NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL,            -- active|draft
    description TEXT
);
CREATE TABLE championship_jurisdiction (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    short_name TEXT NOT NULL,
    level TEXT NOT NULL              -- national|regional
);
CREATE TABLE club_jurisdiction (
    club TEXT PRIMARY KEY,
    jurisdiction TEXT NOT NULL REFERENCES championship_jurisdiction(code),
    valid_from TEXT,
    valid_to TEXT,
    evidence TEXT NOT NULL
);
CREATE TABLE championship_instance (
    id TEXT PRIMARY KEY,
    jurisdiction TEXT NOT NULL REFERENCES championship_jurisdiction(code),
    stage_id INTEGER NOT NULL REFERENCES stage(id),
    category TEXT NOT NULL,
    category_key TEXT NOT NULL,
    championship_type TEXT NOT NULL,
    rule_set_id TEXT NOT NULL REFERENCES championship_rule_set(id),
    state TEXT NOT NULL,             -- confirmed|candidate|rejected
    detection_basis TEXT NOT NULL,
    input_fingerprint TEXT NOT NULL,
    UNIQUE (jurisdiction, stage_id, category_key, championship_type)
);
CREATE TABLE regional_category_mapping (
    id TEXT PRIMARY KEY,
    result_list_id TEXT NOT NULL REFERENCES result_list(id),
    jurisdiction TEXT NOT NULL REFERENCES championship_jurisdiction(code),
    source_category TEXT NOT NULL,
    canonical_category TEXT NOT NULL,
    category_key TEXT NOT NULL,
    state TEXT NOT NULL,             -- confirmed|candidate|rejected
    evidence_kind TEXT NOT NULL,     -- category|document|event_title
    evidence_text TEXT NOT NULL,
    confidence REAL NOT NULL,
    partition_required INTEGER NOT NULL DEFAULT 1,
    input_fingerprint TEXT NOT NULL,
    UNIQUE (result_list_id, jurisdiction, category_key)
);
CREATE TABLE championship_entry (
    id TEXT PRIMARY KEY,
    championship_instance_id TEXT NOT NULL REFERENCES championship_instance(id),
    stage_id INTEGER NOT NULL REFERENCES stage(id),
    competitor_key TEXT NOT NULL,
    regional_rank INTEGER,
    eligibility_state TEXT NOT NULL, -- eligible|provisional|unknown|ineligible
    eligibility_basis TEXT NOT NULL,
    state TEXT NOT NULL,             -- derived|provisional|verified
    source_result_list_id TEXT NOT NULL REFERENCES result_list(id),
    input_fingerprint TEXT NOT NULL,
    UNIQUE (championship_instance_id, competitor_key)
);
CREATE TABLE championship_entry_result (
    championship_entry_id TEXT NOT NULL REFERENCES championship_entry(id),
    result_id INTEGER NOT NULL REFERENCES result(id),
    PRIMARY KEY (championship_entry_id, result_id),
    UNIQUE (result_id)
);
CREATE TABLE award (
    id TEXT PRIMARY KEY,
    championship_instance_id TEXT NOT NULL REFERENCES championship_instance(id),
    result_id INTEGER NOT NULL REFERENCES result(id),
    medal TEXT NOT NULL,              -- gold|silver|bronze
    award_rank INTEGER NOT NULL,
    state TEXT NOT NULL,              -- derived|provisional|verified
    UNIQUE (championship_instance_id, result_id)
);
CREATE INDEX idx_result_person ON result(person_id);
CREATE INDEX idx_result_stage_cat ON result(stage_id, category);
CREATE INDEX idx_result_official_club ON result(official_club);
CREATE INDEX idx_person_name ON person(name_key);
CREATE INDEX idx_result_source_document ON result(source_document_id);
CREATE INDEX idx_result_list ON result(result_list_id);
CREATE INDEX idx_champ_source_stage ON championship_source_entry(stage_id, category_key);
CREATE INDEX idx_champ_source_result ON championship_source_entry(result_id);
CREATE INDEX idx_audit_list ON audit_issue(result_list_id, severity);
CREATE INDEX idx_championship_stage ON championship_instance(stage_id, category);
CREATE INDEX idx_regional_mapping_list ON regional_category_mapping(result_list_id);
CREATE INDEX idx_regional_mapping_jurisdiction ON regional_category_mapping(jurisdiction, state);
CREATE INDEX idx_championship_entry_instance ON championship_entry(championship_instance_id);
CREATE INDEX idx_championship_entry_stage ON championship_entry(stage_id, competitor_key);
CREATE INDEX idx_championship_entry_result ON championship_entry_result(result_id);
CREATE INDEX idx_award_instance ON award(championship_instance_id);
CREATE INDEX idx_person_alias_key ON person_alias(name_key);
CREATE INDEX idx_person_identifier_value ON person_identifier(scheme, identifier);
CREATE INDEX idx_person_membership_club ON person_club_membership(club, sport_type, active);
CREATE VIEW category_stats AS
SELECT stage_id, category,
       COUNT(*)                                       AS starters,
       SUM(status = 'ok')                             AS classified,
       MIN(CASE WHEN rank = 1 THEN time_s END)        AS winner_time_s
FROM result
WHERE result_kind NOT IN ('relay', 'family')
GROUP BY stage_id, category;

-- Source-faithful relay rows stay in result: one row per leg.  Person- and
-- club-level consumers need one participation per person and team instead.
-- The earliest leg is a stable representative; different teams in the same
-- list remain separate even if the runner appears in both.
CREATE VIEW person_result AS
SELECT r.* FROM result r
WHERE r.person_id IS NOT NULL
  AND (r.result_kind != 'relay'
       OR COALESCE(r.team_number, r.team_name, r.club) IS NULL
       OR NOT EXISTS (
           SELECT 1 FROM result prior
           WHERE prior.id < r.id
             AND prior.result_kind = 'relay'
             AND prior.person_id = r.person_id
             AND prior.result_list_id = r.result_list_id
             AND COALESCE('n:' || prior.team_number,
                          't:' || prior.team_name,
                          'c:' || prior.club) =
                 COALESCE('n:' || r.team_number,
                          't:' || r.team_name,
                          'c:' || r.club)));
"""

ANNE_STATUS = {
    "classified": "ok",
    "notClassified": "unknown",
    "didNotFinish": "dnf",
    "disqualified": "dsq",
    "missingPunch": "mp",
    "didNotStart": "dns",
    # ANNE/IOF distinguishes exceeding the maximum time from an ordinary
    # finish. The public model intentionally uses the smaller status set
    # shared by all sources; its equivalent is the existing time-limit DSQ.
    "overTime": "dsq",
}

VALID_STATUSES = {"ok", "dnf", "dsq", "mp", "dns", "unknown"}
OOC_STATUS_TEXT_RE = re.compile(
    r"^(?:ak|au(?:ß|ss)er konkurrenz|ohne wertung|wertungsfrei)$", re.I)
OOC_TIME_TEXT_RE = re.compile(r"^\(\s*\d{1,3}:\d{2}(?::\d{2})?\s*\)$")
OOC_NAME_PREFIX_RE = re.compile(r"^A\.?\s?K\.?\s+", re.I)
FAMILY_CATEGORY_RE = re.compile(
    r"(?:\bfam(?:ilie|ily|iliy|iliy|iliy)?\b|familien|rahmenbewerb\s+familie)", re.I)
AMBIGUOUS_FAMILY_CATEGORY_RE = re.compile(r"^(?:AT-)?F$", re.I)

# These historic result documents use only the one-letter class ``F`` and do
# not expose ANNE's long category title. Inspection of the complete source
# lists confirms that F is the Family class, not a female/course label.
KNOWN_LEGACY_FAMILY_CATEGORIES = {
    (4220, "f"),
    (4245, "f"),
    (4254, "f"),
}
# The same one-letter spelling can also be an ordinary open/course class.
# Event 4248's complete source has 19 separately ranked individual runners
# (men and women), so it is definitively not a Family result.
KNOWN_LEGACY_ORDINARY_CATEGORIES = {
    (4248, "f"),
}


def classify_family_category(category, category_full=None, event_id=None):
    """Return ``family``, ``ambiguous`` or ``ordinary``.

    Full words and common misspellings are safe to classify automatically.
    Historic one-letter classes such as ``F`` are deliberately review work:
    depending on the event they can mean Family, female, or a course label.
    """
    value = re.sub(r"\s+", " ", (category or "").strip())
    full_value = re.sub(r"\s+", " ", (category_full or "").strip())
    if FAMILY_CATEGORY_RE.search(full_value):
        return "family"
    if (event_id is not None and
            (int(event_id), value.casefold()) in KNOWN_LEGACY_ORDINARY_CATEGORIES):
        return "ordinary"
    if (event_id is not None and
            (int(event_id), value.casefold()) in KNOWN_LEGACY_FAMILY_CATEGORIES):
        return "family"
    if AMBIGUOUS_FAMILY_CATEGORY_RE.fullmatch(value):
        return "ambiguous"
    return "family" if FAMILY_CATEGORY_RE.search(value) else "ordinary"


def normalize_status(status, raw_text=None, out_of_competition=False):
    """Normalize status while keeping OOC as an orthogonal flag.

    Old committed parser output used ``nc`` for the international equivalent
    of AK/OOC. Keep that source meaning as the orthogonal OOC flag and expose
    the normalized sporting status as ``ok``.
    """
    raw = str(raw_text if raw_text is not None else status or "").strip()
    ooc = (bool(out_of_competition) or bool(OOC_STATUS_TEXT_RE.fullmatch(raw))
           or bool(OOC_TIME_TEXT_RE.fullmatch(raw)))
    if str(status or "").casefold() == "nc" or raw.casefold() == "nc":
        ooc = True
    normalized = status if status in VALID_STATUSES else "unknown"
    # Compatibility for committed parser output created before these source
    # spellings were normalized. This repairs old cached rows at build time
    # without requiring an expensive reparse of unrelated PDFs.
    if normalized == "unknown":
        legacy_status_patterns = (
            (r"omt\.?", "dns"),
            (r"(?:n\.?\s*)?ang\.?", "dns"),
            (r"missing\s+punch", "mp"),
            (r"\d+\s+posten\s+fehl(?:t|en)", "mp"),
            (r"ziel\s+fehlt", "mp"),
            (r"not\s+finish(?:ed)?", "dnf"),
            (r"verletzt", "dnf"),
            (r"dis\.?", "dsq"),
            (r"teilgenommen", "ok"),
        )
        for pattern, mapped in legacy_status_patterns:
            if re.fullmatch(pattern, raw, re.I):
                normalized = mapped
                break
    if ooc and normalized == "unknown":
        normalized = "ok"
    return normalized, int(ooc)


def is_active_anne_result(row):
    """Whether an ANNE row represents a result rather than a removed draft.

    During category changes ANNE can retain an ``inactive`` live row next to
    the runner's later official result. It is provenance inside ANNE, not a
    DNS/DNF result and must not become an OLResults competitor entry.
    """
    if str(row.get("classification") or "").casefold() == "inactive":
        return False
    # One migrated Tirol-Cup payload contains a second, corrupt copy of every
    # genuine result.  In those copies category is literally ``empty``, all
    # rows are labelled disqualified, and the club column contains a rendered
    # elapsed value such as ``27:16:00 0``.  The proper rows coexist in their
    # real categories with the real club and classification.  This signature
    # occurs nowhere else in the complete snapshot and is data transport
    # debris, not a legitimate DSQ observation.
    corrupt_club = re.fullmatch(
        r"\d{1,3}:\d{2}:\d{2}\s+\d+", str(row.get("clubName") or "").strip())
    if ((row.get("categoryShortTitle") or "").strip().casefold() == "empty"
            and str(row.get("classification") or "").casefold() == "disqualified"
            and corrupt_club):
        return False
    return True


def deduplicate_anne_rows(rows):
    """Remove byte-semantic duplicates returned under different ANNE ids.

    Some migrated events contain a second copy of every result.  Only the
    transport metadata differs (``id`` and occasionally ``updatedAt``); all
    competition fields are identical.  Confirmed real: events 448 and 3438.
    Comparing the complete remaining payload is deliberately stricter than a
    name/time heuristic and therefore retains distinct starts, stages, teams,
    bib numbers, championship annotations, and classifications.
    """
    seen = set()
    unique = []
    for row in rows:
        payload = {key: value for key, value in row.items()
                   if key not in ("id", "updatedAt")}
        key = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), default=str)
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    return unique


def repair_mojibake(value):
    """Repair the common UTF-8-decoded-as-Latin-1 source corruption."""
    if not value or not any(marker in value for marker in ("Ã", "Â")):
        return value
    try:
        repaired = value.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value
    return repaired

CLUB_JUNK_PREFIX_RE = re.compile(r"^(?:empty|leer|vacant|frei|\.)\s+", re.I)


def clean_club(name):
    """ANNE's own API sometimes concatenates an empty team/school-name field
    with the real club name, leaking a placeholder prefix through
    ('empty Naturfreunde Wien', '. OL Kufstein') - confirmed straight from
    clubName in the raw API response, not something our own parsing adds."""
    if not name:
        return name
    return CLUB_JUNK_PREFIX_RE.sub("", repair_mojibake(name)).strip()


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
    r"(?i)^cz-|\(cze\)|"
    r"\bbahn\b|neuling|familie|ultimate|hobby|schnupper|mannschaft|"
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

# Joint foreign-host events can publish one combined field without ANNE's
# per-person championshipEligibility having been fetched yet.  In that
# situation a national medal still requires an identifiable ÖFOL club; an
# unrecognised foreign club must not inherit an Austrian rank merely because
# the event title contains "ÖM".  Event 5280 is the 2026 ÖM Mittel embedded
# in the Pannon MTBO race in Márkó (HUN), whose source has generic M/W classes
# shared by Austrian and Hungarian competitors rather than AT/HU prefixes.
FOREIGN_HOST_REQUIRE_OFFICIAL_CLUB_EVENTS = {5280}

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
    # The fixed-width 2013 Nachtlauf export splits the last three letters
    # across the Name/Verein boundary: ``Christina Hell | man OK gittis...``.
    # The full physical source line unambiguously spells Christina Hellman.
    (856, "Christina Hell"): "Christina Hellman",
    (3633, "Anita Gassner"): "Anika Gassner",
    # Naturfreunde Wien's own Excel medal sheet and her ANNE-resolved
    # identity both spell her "Matilda" (no h); these two source documents
    # spell her "Mathilda" instead, fragmenting her medal count onto a
    # separate synthetic person.
    (3851, "Buschek Mathilda"): "Buschek Matilda",
    (4690, "Mathilda Buschek"): "Matilda Buschek",
    # The PDF embeds Á through a broken CID mapping. The ANNE user registry
    # confirms the exact spelling and ÖFOL identity (8661).
    (4477, "Ã(cid:129)gnes Vajda-Kovács"): "Ágnes Vajda-Kovács",
    # The 2014 Krems text export contains irrecoverable replacement bytes,
    # but the intact surrounding letters and the ANNE surname spelling make
    # these two repairs unambiguous.
    (1110, "Ams�üss Birgit"): "Amsüss Birgit",
    # Both Austrian event sources lost the same character. The Slovak
    # federation runner register (RBA6051) confirms the spelling.
    (1583, "Ta�jana Jánošková"): "Taťjana Jánošková",
    (1584, "Ta�jana Jánošková"): "Taťjana Jánošková",
    (1583, "Jánošková Ta�jana"): "Taťjana Jánošková",
    (1584, "Jánošková Ta�jana"): "Taťjana Jánošková",
    # Event 4482's otherwise valid Tirol-Cup rows carry a literal question
    # mark for ü. Intact ANNE results for the same people confirm all three
    # spellings (Uwe and Sabine also have stable ÖFOL identities).
    (4482, "Maya Eichm?ller"): "Maya Eichmüller",
    (4482, "Uwe Waldh?tter"): "Uwe Waldhütter",
    (4482, "Sabine Scholl-B?rgi"): "Sabine Scholl-Bürgi",
}

# Quellenspezifische Vereinszellen, die im offiziellen Resultat sichtbar auf
# ein mehrdeutiges erstes Wort abgeschnitten sind.  Die Personenidentität und
# ihre vollständigen Vereinsangaben in unmittelbar benachbarten 2022-Quellen
# wurden gegengeprüft.  ``observed_club`` behält unten weiterhin den rohen
# Wert; nur die normalisierte sportliche Zuordnung wird berichtigt.
KNOWN_RESULT_CLUB_OVERRIDES = {
    (856, "Christina Hellman"): "Orienteering Klosterneuburg",
    # The same damaged fixed-width row leaks ``man`` (the tail of
    # Hellman) into the Verein column for all three pair members.
    (856, "Maria Reil"): "Orienteering Klosterneuburg",
    (856, "Laura"): "Orienteering Klosterneuburg",
    (3847, "Thomas Radon"): "Naturfreunde Wien",
    (3847, "Nikolaus Euler-Rolle"): "Naturfreunde Wien",
    (3847, "Michael Grill"): "Naturfreunde Wien",
    (3847, "Thomas Neuhold"): "Orienteering Klosterneuburg",
    (3847, "Barbara Kastner"): "Naturfreunde Wien",
    (3847, "Natalia Machold"): "Naturfreunde Wien",
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
                     "name_key": name_key(e["name"]),
                     "yob": None if e.get("yob") in (1900, 1901) else e.get("yob"),
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
            yob = int(yob) if yob.isdigit() else None
            members.append({
                "ofol_id": int(oid), "name": name, "name_key": name_key(name),
                "yob": None if yob in (1900, 1901) else yob,
                "club": MEMBER_CLUB_NAME})
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


def prepare_verified_member_identities(cur, persons, members, confirmed_aliases=None):
    """Repair crossed ANNE source IDs before global name reconciliation.

    ANNE occasionally attaches one person's userId to a row carrying another
    person's full name.  A global userId merge cannot safely repair that: it
    would merge both people and rewrite all of their otherwise-correct rows.
    Canonicalise known club-member ids first, then repair only non-Family
    source rows where the observed name independently identifies a
    *different* verified member, a unique /user profile, or (for completely
    disjoint names) a name/club fallback person. Family rows are excluded
    because their labels need not identify one person. The conflicting
    observed_user_id deliberately remains on the result as provenance.

    Returns audit records for the private conflict report.
    """
    confirmed_aliases = confirmed_aliases or {}
    member_by_id = {m["ofol_id"]: m for m in members}
    members_by_name = defaultdict(list)
    for m in members:
        members_by_name[m["name_key"]].append(m)
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
            existing[3],
        )
        persons._link(oid, member["name"], member["yob"])

    corrections = []
    rows = cur.execute(
        """SELECT id, person_id, observed_name, observed_user_id,
                  observed_club, official_club
           FROM result
           WHERE source = 'anne-api' AND observed_user_id IS NOT NULL
             AND result_kind != 'family'
           ORDER BY id""").fetchall()
    for (result_id, person_id, observed_name, observed_user_id,
         observed_club, observed_official_club) in rows:
        try:
            source_id = int(observed_user_id)
        except (TypeError, ValueError):
            continue
        source_member = member_by_id.get(source_id)
        source_profile = persons.anne_profiles.by_id.get(source_id)
        source_identity = source_member or source_profile
        if source_identity is None:
            continue
        observed_key = name_key(clean_name(observed_name or ""))
        source_key = source_identity["name_key"]
        if observed_key == source_key:
            continue

        verified_target_ids = {
            m["ofol_id"] for m in members_by_name.get(observed_key, [])
            if m["ofol_id"] != source_id
        }
        alias_target = confirmed_aliases.get(observed_key)
        if alias_target in member_by_id and alias_target != source_id:
            verified_target_ids.add(alias_target)
        if len(verified_target_ids) == 1:
            registry_proves_target = False
            target_member = member_by_id[next(iter(verified_target_ids))]
            target_id = target_member["ofol_id"]
            target_name = target_member["name"]
            target_basis = "club-book-of-record"
            target_confidence = 1.0
            target_state = "resolved"
        else:
            target_member = None
            profile_matches, _profile_basis = persons.anne_profiles.match(
                clean_name(observed_name or ""), None,
                observed_official_club or observed_club)
            profile_matches = [profile for profile in profile_matches
                               if profile["oefol_id"] != source_id]
            if len(profile_matches) == 1:
                registry_proves_target = True
                profile = profile_matches[0]
                target_id = persons.from_anne(
                    profile["oefol_id"], profile["name"],
                    profile["year_of_birth"], profile["nationality"])
                target_name = profile["name"]
                target_basis = "anne-registry-name-club"
                target_confidence = 0.95
                target_state = "resolved"
            elif not (set(source_key.split()) & set(observed_key.split())):
                registry_proves_target = False
                # Completely disjoint authoritative and observed names are a
                # crossed source ID even when the real runner has no /user
                # profile. Preserve the source ID as provenance, but attach
                # only this row to a name/club fallback identity.
                target_id, target_basis, target_confidence, target_state = \
                    persons.from_legacy(clean_name(observed_name or ""), None,
                                        observed_official_club or observed_club)
                target_name = persons.by_id[target_id][0]
                if target_id == source_id:
                    continue
            else:
                continue

        # A shared token can be an ordinary spelling/name change, so it needs
        # a second independent proof before overriding a source-supplied ID.
        # Exact book-of-record aliases are already reviewed proof. Otherwise
        # require the row's official club to agree with the exact target
        # member. This catches crossed IDs such as Thomas Hnilica/Hlosta and
        # Herwig/Ute Hierzegger without treating a surname overlap as enough.
        source_tokens = set(source_key.split())
        if source_tokens & set(observed_key.split()):
            alias_proves_target = confirmed_aliases.get(observed_key) == target_id
            target_club = (canonicalize_official_club(
                target_member.get("club"), OFFICIAL_CLUBS)
                if target_member else None)
            if not registry_proves_target and not alias_proves_target and (
                    target_club is None or observed_official_club != target_club):
                continue

        existing = persons.by_id.get(target_id)
        if existing is None:
            persons.by_id[target_id] = (
                target_member["name"], target_member["name_key"],
                target_member["yob"], None,
            )
            persons._link(target_id, target_member["name"], target_member["yob"])

        cur.execute(
            """UPDATE result
               SET person_id = ?, identity_basis = ?,
                   identity_confidence = ?, identity_state = ?
               WHERE id = ?""",
            (target_id, target_basis, target_confidence, target_state, result_id),
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
            "source_identity": source_identity["name"],
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
    for pid, (_name, nk, _yob, _nat) in persons.by_id.items():
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
            # Prefer the club book of record, then a registry profile carrying
            # a genuine birth year, then ANNE's verification bit. A lower
            # numeric ID is only the final deterministic tie-break. This is
            # crucial for duplicate accounts whose placeholder year 1900/1901
            # has been normalised to unknown: the valid higher ID must not be
            # redirected into the placeholder profile.
            def target_quality(anne_id):
                profile = persons.anne_profiles.by_id.get(anne_id, {})
                return (
                    anne_id in verified_member_ids,
                    persons.by_id[anne_id][2] is not None,
                    bool(profile.get("anne_is_verified")),
                    -anne_id,
                )
            target = protected[0] if protected else max(anne_ids, key=target_quality)
            for anne_id in anne_ids:
                if anne_id != target:
                    merge_map[anne_id] = target
            target_yob = next(iter(distinct_yobs), None)
            if target_yob is not None and persons.by_id[target][2] is None:
                current = persons.by_id[target]
                persons.by_id[target] = (
                    current[0], current[1], target_yob, current[3])
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


def registry_identifier_merge_conflicts(cur, profile_index):
    """Return incompatible authoritative /user IDs sharing one person.

    Exact duplicate ANNE accounts can legitimately collapse when their current
    registry name and every known birth year agree. Different names or two
    different known birth years are independent people and must never share a
    canonical person merely because one historic result carried a crossed ID.
    """
    conflicts = []
    groups = cur.execute(
        """SELECT person_id, GROUP_CONCAT(DISTINCT identifier)
           FROM person_identifier
           WHERE scheme = 'oefol_id' AND source = 'anne-user-registry'
           GROUP BY person_id HAVING COUNT(DISTINCT identifier) > 1""").fetchall()
    for person_id, identifiers in groups:
        profiles = [profile_index.by_id.get(int(value))
                    for value in identifiers.split(",")]
        profiles = [profile for profile in profiles if profile]
        names = {profile["name_key"] for profile in profiles}
        years = {profile["year_of_birth"] for profile in profiles
                 if profile["year_of_birth"] is not None}
        if len(names) > 1 or len(years) > 1:
            conflicts.append({
                "person_id": person_id,
                "identifiers": sorted(profile["oefol_id"] for profile in profiles),
                "names": sorted(profile["name"] for profile in profiles),
                "years": sorted(years),
            })
    return conflicts


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
            cur.execute("""UPDATE result
                            SET championship = NULL,
                                championship_eligibility_state = 'ineligible',
                                championship_eligibility_basis = 'anne_foreign_no_override'
                            WHERE championship IS NOT NULL AND person_id = ?
                              AND championship_eligibility_basis != 'official_championship_ranking'
                              AND stage_id IN (SELECT id FROM stage WHERE event_id = ?)""",
                        (int(uid), int(eid)))
            n += cur.rowcount
    return n


def apply_championship_eligibility_evidence(cur):
    """Resolve the best event-time eligibility evidence available per row.

    ANNE nationality is authoritative only when an ÖFOL identity exists:
    AUT is eligible without consulting the foreign-nationality override.
    Non-AUT decisions are frozen per event in USER_ELIGIBILITY_PATH and are
    applied by apply_championship_eligibility_overrides (negative) or here
    (positive).  An explicit championship row/annotation keeps the eligible
    state assigned at insertion.  Everything else can only become a
    provisional club inference; club membership is useful evidence but is not
    equivalent to nationality/residency eligibility.
    """
    cur.execute("""
        UPDATE result
        SET championship_eligibility_state = 'eligible',
            championship_eligibility_basis = 'anne_aut_nationality'
        WHERE championship IS NOT NULL AND person_id > 0
          AND championship_eligibility_basis != 'official_championship_ranking'
          AND person_id IN (SELECT id FROM person WHERE nationality = 'AUT')
    """)
    if USER_ELIGIBILITY_PATH.exists():
        cache = json.loads(USER_ELIGIBILITY_PATH.read_text())
        for uid, by_event in cache.items():
            for eid, eligibility in by_event.items():
                if eligibility is not True:
                    continue
                cur.execute("""
                    UPDATE result
                    SET championship_eligibility_state = 'eligible',
                        championship_eligibility_basis = 'anne_foreign_override'
                    WHERE championship IS NOT NULL AND person_id = ?
                      AND championship_eligibility_basis != 'official_championship_ranking'
                      AND stage_id IN (SELECT id FROM stage WHERE event_id = ?)
                """, (int(uid), int(eid)))
    cur.execute("""
        UPDATE result
        SET championship_eligibility_state = 'provisional',
            championship_eligibility_basis = 'oefol_club_inference'
        WHERE championship IS NOT NULL AND official_club IS NOT NULL
          AND championship_eligibility_state IN ('unknown', 'provisional')
    """)
    cur.execute("""
        UPDATE result
        SET championship_eligibility_state = 'unknown',
            championship_eligibility_basis = 'no_verified_eligibility_evidence'
        WHERE championship IS NOT NULL AND official_club IS NULL
          AND championship_eligibility_state = 'provisional'
    """)


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
        WHERE r.status = 'ok' AND r.person_id IS NOT NULL
          AND r.result_kind IN ('individual', 'relay', 'pair', 'team')
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
        cur.execute("""UPDATE result
                        SET championship = ?,
                            championship_eligibility_state = 'provisional',
                            championship_eligibility_basis = 'title_category_inference',
                            championship_source_scope = 'inferred'
                        WHERE stage_id = ? AND category = ? AND status = 'ok'
                          AND rank IS NOT NULL AND person_id IS NOT NULL
                          AND result_kind IN ('individual', 'relay', 'pair', 'team')""", (champ, sid, category))
        n += cur.rowcount
    return n


OFFICIAL_CLUBS_PATH = ROOT / "data" / "official_clubs.json"
HISTORICAL_OFFICIAL_CLUBS_PATH = ROOT / "data" / "historical_official_clubs.json"
CLUB_SUFFIX_NUM_RE = re.compile(r"^(.+)\s(\d+)$")
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
CLUB_SOURCE_ALIASES = {
    # Historic/source spellings versus ANNE's current official registry.
    "FUN-OL NÖ": "FUN.O NOe",
    "FUN-OL NÖe": "FUN.O NOe",
    "FUN-OL NOE": "FUN.O NOe",
    "OLG DKB": "SKV OLG Deutsch Kaltenbrunn",
    "WAT": "WAT-OL",
    "WAT.OL": "WAT-OL",
    "WAT OL": "WAT-OL",
    "WAT-OL WAT-OL": "WAT-OL",
    "GO Harzberg": "GO_Harzberg/Bad_Voeslau",
    "HSV Villach": "HSV OL Villach",
    "HSV OL Wr. Neustadt": "HSV OL Wiener Neustadt",
    "HSV OL Wr.Neustadt": "HSV OL Wiener Neustadt",
    "HSV Wr. Neustadt": "HSV OL Wiener Neustadt",
    "HSV Wr Neustadt": "HSV OL Wiener Neustadt",
    "HSV Wr.Neustadt": "HSV OL Wiener Neustadt",
    "HSV Wiener Neustadt": "HSV OL Wiener Neustadt",
    "LK Innsbruck": "Laufklub Kompass Innsbruck",
    "NF Villach Orienteering": "Naturfreunde Villach - Orienteering",
    "NF Villach": "Naturfreunde Villach - Orienteering",
    "ASKÖ– Henndorf": "ASKÖ Henndorf Orienteering",
    "ASKÖ- Henndorf": "ASKÖ Henndorf Orienteering",
    "HSV Absam": "HSV Absam OL",
    "Leibnitzer AC Orientierungslau": "Leibnitzer AC OLG",
    "LAC Leibnitz": "Leibnitzer AC OLG",
    "Leibnitzer Athletik Club-OLGem": "Leibnitzer AC OLG",
    "Leibnitzer Athletik Club - Ori": "Leibnitzer AC OLG",
    "Leibnitzer Athletik Club": "Leibnitzer AC OLG",
    # Literal replacement character in old ANNE migrations.
    "Naturfreunde Kitzb?hel": "Naturfreunde Kitzbühel",
    # Historical name of today's Fürstenfeld club in old STOLV exports.
    "TV Fürstenfeld": "OC Fürstenfeld",
    "TV Fuerstenfeld": "OC Fürstenfeld",
    # Name used by the Klosterneuburg club in older result lists.
    "OK Gittis Klosterneuburg": "Orienteering Klosterneuburg",
    "OK Klosterneuburg": "Orienteering Klosterneuburg",
    # A paired night-result row starts the extracted club cell inside the
    # preceding runner name. The surviving suffix is still unique and was
    # checked against the source's full club label.
    "ner  OK gittis Klosterneubu": "Orienteering Klosterneuburg",
    "GOs Harzberg": "GO_Harzberg/Bad_Voeslau",
}


def load_official_clubs():
    """Current ANNE clubs plus deliberately curated historical ÖFOL clubs.

    A retired club remains a real, separate club in historical result lists:
    it must not be silently remapped to a successor or a club that received
    some of its former members. The source spelling on an individual result
    remains untouched; this set only powers the canonical club index.
    """
    clubs = set()
    if OFFICIAL_CLUBS_PATH.exists():
        clubs.update(c["name"] for c in json.loads(OFFICIAL_CLUBS_PATH.read_text()))
    if HISTORICAL_OFFICIAL_CLUBS_PATH.exists():
        clubs.update(c["name"] for c in json.loads(HISTORICAL_OFFICIAL_CLUBS_PATH.read_text()))
    return clubs


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
    source_aliases = {source.casefold(): target
                      for source, target in CLUB_SOURCE_ALIASES.items()}
    cur = source_aliases.get(cur.casefold(), cur)
    # Club spelling in historical exports often differs only in upper/lower
    # case (for example "LZ Omaha" vs the ANNE registry's "LZ OMAHA").
    # This is an exact case-insensitive match, not a fuzzy club guess.
    official_casefold = {candidate.casefold(): candidate for candidate in official}
    cur = official_casefold.get(cur.casefold(), cur)
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
            # Some team exports concatenate a short/source label and the
            # full official club ("NF Wien Naturfreunde Wien", "SUSO SU
            # Schöckl Orienteering").  Accept this only if exactly one full
            # official name occurs inside the field; combined-club relay
            # labels consequently remain ambiguous and are not collapsed.
            embedded_matches = [candidate for candidate in official
                                if len(candidate) >= 8
                                and candidate.casefold() in cur.casefold()]
            if len(embedded_matches) == 1:
                cur = embedded_matches[0]
                changed = True
        if not changed:
            # Fixed-width PDF columns truncate long club names. Accept only a
            # sufficiently long prefix that identifies exactly one official
            # club; this repairs "ASKÖ Henndorf Orientee" and "HSV OL Wiener
            # Neustad" without guessing among ambiguous short fragments.
            prefix_matches = [candidate for candidate in official
                              if len(cur) >= 10 and candidate.casefold().startswith(cur.casefold())]
            if len(prefix_matches) == 1:
                cur = prefix_matches[0]
                changed = True
        if not changed:
            return None
    return cur


def source_club_for_team(raw_club, team_name, kind):
    """Separate a source team label from its underlying club association."""
    club = (raw_club or "").strip()
    if kind not in ("relay", "team"):
        return club or None
    # Relay exports commonly put the squad number straight onto the club
    # ("Naturfreunde Wien 2"). Mannschaft parsers now already keep club and
    # generated team name separate, so only strip when both source values are
    # the same label.
    if club and team_name and club == team_name:
        match = CLUB_SUFFIX_NUM_RE.match(club)
        if match:
            club = match.group(1).strip()
    return club or None


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
    name = repair_mojibake(name.strip())
    name = re.sub(r"^#NAME\?\s*", "", name)
    name = re.sub(r"^A\.?\s?K\.?\s+", "", name)  # 'A.K.' = außer Konkurrenz marker
    name = re.sub(r"^\([^)]*\)\s*", "", name)   # leading note, e.g. "(Csala) Judit Resch"
    m = re.match(r"^\d{1,3}\s+(\D.*)$", name) or re.match(r"^\d{1,3}([A-Za-zÀ-ÿ].*)$", name)
    if m:
        name = m.group(1).strip()
    # PDF extraction sometimes splits the first letter off ("A lexander Grill")
    name = re.sub(r"^([A-ZÀ-Þ]) ([a-zà-ÿ])", r"\1\2", name)
    return name


def clean_result_name(event_id, observed_name):
    """Clean a source name and then apply narrowly verified event repairs.

    Applying the repair here, before name validation, is important: a literal
    question mark from a broken legacy character encoding would otherwise make
    an otherwise valid ANNE result disappear before it can be corrected.
    """
    name = clean_name(observed_name)
    return KNOWN_NAME_TYPOS.get((event_id, name), name)


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
SOURCE_CHROME_NAME_RE = re.compile(
    r"(?i)^(?:seite\s+\d+|©?\s*stephan\s+kr[äa]mer|sportsoftware(?:\s+\d+)?)$")



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
    if SOURCE_CHROME_NAME_RE.fullmatch(name.strip()):
        return False
    if re.search(r"\d,\d{3}|\bkm\b", name):  # "4,950", "2,250 km" course artifacts
        return False
    if ANNOTATION_RE.search(name):
        return False
    if re.fullmatch(r"(?i)(?:vakant|vacant|n\.?\s*n\.?)", name.strip()):
        return False
    return True


def is_relay_placeholder_name(name):
    """True for explicit empty relay slots, never for a real person."""
    compact = re.sub(r"\s+", " ", (name or "").strip())
    return bool(re.fullmatch(
        r"(?i)n\.?\s*n\.?(?:\s+(?:n\.?\s*n\.?|n\s*ang))?", compact))


def club_match_key(name):
    """Loose key only for supporting a legacy identity candidate with a
    *current* ANNE membership.  It is never used to overwrite an observed
    historic result club or to prove an identity on its own."""
    value = unicodedata.normalize("NFKD", name or "")
    value = "".join(c for c in value if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def relay_club_match_key(name):
    """Comparison key for abbreviated clubs inside a mixed relay label.

    This is intentionally narrower than general club canonicalisation.  It
    is only used as supporting evidence for an already exact, unique ANNE
    name match, and handles abbreviations actually printed in relay sources
    (``NF Kitzb.``, ``HSV Wr. Neust.``, ``LK Innsbruck``).
    """
    value = unicodedata.normalize("NFKD", name or "")
    value = "".join(c for c in value if not unicodedata.combining(c)).casefold()
    value = re.sub(r"\bnaturfreunde\b", "nf", value)
    value = re.sub(r"\blaufklub\s+kompass\b", "lk", value)
    value = re.sub(r"\basko\s+henndorf\s+orienteering\b", "ahdo", value)
    value = re.sub(r"\bwiener\b", "wr", value)
    value = re.sub(r"\bneustadt\b", "neust", value)
    value = re.sub(r"\bkitzbuhl\b", "kitzbuhel", value)
    value = re.sub(r"\borienteering\b|\boriente\b", "", value)
    value = re.sub(r"\bol\b", "", value)
    value = re.sub(r"\boc\s+furstenfeld\b", "ocff", value)
    return re.sub(r"[^a-z0-9]", "", value)


def relay_club_component_matches(component, membership):
    component_key = relay_club_match_key(component)
    membership_key = relay_club_match_key(membership)
    if not component_key or not membership_key:
        return False
    if component_key == membership_key:
        return True
    shorter, longer = sorted((component_key, membership_key), key=len)
    return len(shorter) >= 5 and longer.startswith(shorter)


class AnneProfileIndex:
    """Private, complete ANNE /user snapshot used only while building.

    The index is intentionally not copied into the public SQLite database.
    It can resolve a result's direct ÖFOL-ID and safely promote a unique exact
    name+birth-year or name+club legacy match. Club matching is deliberately
    limited to one unambiguous ANNE profile; duplicate matches remain open.
    """

    def __init__(self, profiles=(), fetched_at=None):
        self.fetched_at = fetched_at
        self.by_id = {}
        self.by_name_yob = defaultdict(list)
        self.by_name = defaultdict(list)
        for profile in profiles:
            try:
                oefol_id = int(profile["oefol_id"])
            except (KeyError, TypeError, ValueError):
                continue
            # The private snapshot has occasionally contained UTF-8 names
            # decoded as Latin-1 (``SchÃ¼tz``).  Result names already pass
            # through clean_name(); do the same for the authoritative ANNE
            # profile or a direct ÖFOL-ID would publish the damaged spelling.
            first = repair_mojibake((profile.get("first_name") or "").strip())
            last = repair_mojibake((profile.get("last_name") or "").strip())
            name = clean_name(f"{first} {last}".strip())
            if oefol_id <= 0 or not is_valid_name(name):
                continue
            yob = profile.get("year_of_birth")
            # Defensive compatibility with snapshots created before the
            # importer normalised ANNE's 1900/1901 placeholder years to null.
            yob = yob if isinstance(yob, int) and yob not in (1900, 1901) else None
            normalised = {
                "oefol_id": oefol_id,
                "name": name,
                "name_key": name_key(name),
                "year_of_birth": yob,
                "nationality": profile.get("nationality") or None,
                "anne_is_verified": bool(profile.get("anne_is_verified")),
                "memberships": tuple(
                    club_match_key((m.get("club") or {}).get("name"))
                    for m in profile.get("active_memberships", [])
                    if isinstance(m, dict) and isinstance(m.get("club"), dict)
                ),
                "membership_names": tuple(dict.fromkeys(
                    (m.get("club") or {}).get("name").strip()
                    for m in profile.get("active_memberships", [])
                    if isinstance(m, dict) and isinstance(m.get("club"), dict)
                    and isinstance((m.get("club") or {}).get("name"), str)
                    and (m.get("club") or {}).get("name").strip()
                )),
                # Preserve only the active-membership fields needed by the
                # public club roster.  The complete /user snapshot remains a
                # private build input; later we publish these rows only for
                # profiles that already occur in the result database.
                "active_memberships": tuple(
                    {
                        "club": (m.get("club") or {}).get("name").strip(),
                        "sport_type": (m.get("sport_type") or "").strip(),
                        "valid_from": (m.get("date_from") or "").strip(),
                        "valid_to": (m.get("date_to") or "").strip() or None,
                        "active": m.get("active") is not False,
                    }
                    for m in profile.get("active_memberships", [])
                    if isinstance(m, dict) and isinstance(m.get("club"), dict)
                    and isinstance((m.get("club") or {}).get("name"), str)
                    and (m.get("club") or {}).get("name").strip()
                    and isinstance(m.get("sport_type"), str)
                    and m.get("sport_type").strip()
                ),
            }
            self.by_id[oefol_id] = normalised
            self.by_name[normalised["name_key"]].append(normalised)
            if yob is not None:
                self.by_name_yob[(normalised["name_key"], yob)].append(normalised)

    @classmethod
    def empty(cls):
        return cls()

    def match(self, name, yob, club=None):
        nk = name_key(name)
        if yob is not None:
            matches = self.by_name_yob.get((nk, yob), [])
            if matches:
                return matches, "name-yob"
            # Result feeds occasionally contain a wrong year of birth even
            # when the printed name and club are correct (notably structured
            # ANNE relay members).  A failed year match must not suppress the
            # independent, exact name+club proof below.  It is still promoted
            # only when that combination identifies exactly one profile.
        canonical_club = canonicalize_official_club(club, OFFICIAL_CLUBS)
        club_key = club_match_key(canonical_club or club)
        if club_key:
            matches = [profile for profile in self.by_name.get(nk, [])
                       if club_key in profile["memberships"]]
            return matches, "name-club"
        return [], None

    def relay_member_club(self, name, team_label):
        """Resolve one member's club from a slash-separated relay label.

        The source observes only a combined team label.  A club is returned
        only when exact ANNE name + printed component select exactly one
        profile membership.  Thus two people with the same name but distinct
        clubs can still be disambiguated safely.
        """
        if "/" not in (team_label or ""):
            return None
        profiles = self.by_name.get(name_key(name), [])
        components = [part.strip() for part in team_label.split("/") if part.strip()]
        matches = {
            (profile["oefol_id"], membership)
            for profile in profiles
            for membership in profile.get("membership_names", ())
            if any(relay_club_component_matches(component, membership)
                   for component in components)
        }
        return next(iter(matches))[1] if len(matches) == 1 else None


def load_anne_profile_index():
    if not USER_INDEX_PATH.exists():
        return AnneProfileIndex.empty()
    try:
        snapshot = json.loads(USER_INDEX_PATH.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"warning: private ANNE user index is unreadable: {exc}")
        return AnneProfileIndex.empty()
    if snapshot.get("schema_version") != 1 or not isinstance(snapshot.get("users"), list):
        print("warning: private ANNE user index has unsupported shape")
        return AnneProfileIndex.empty()
    index = AnneProfileIndex(snapshot["users"], snapshot.get("fetched_at"))
    print(f"loaded private ANNE identity index: {len(index.by_id)} ÖFOL profiles"
          + (f" (snapshot {index.fetched_at})" if index.fetched_at else ""))
    return index


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

    def __init__(self, anne_profiles=None):
        self.by_id = {}
        self.by_key = {}     # (name_key, yob) -> pid
        self.by_name = {}    # name_key -> [pid, ...], insertion order, no dupes
        self.name_seen = defaultdict(Counter)  # pid -> Counter(name -> occurrences)
        self.name_auth = defaultdict(Counter)  # pid -> Counter of API-form names
        self.reconciliation_name_keys = defaultdict(set)
        self.first_names = set()               # lowercased firstNames from the API
        self.last_names = set()                # lowercased lastNames from the API
        self.anne_ids = set()                   # source-supplied ANNE identifiers
        self.anne_profiles = anne_profiles or AnneProfileIndex.empty()

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

    def _new(self, name, yob, nationality=None, pid=None):
        yob = self._identity_year(yob)
        if pid is None:
            salt = 0
            pid = stable_synthetic_id(name, yob, salt)
            while pid in self.by_id and self.by_id[pid][1:3] != (name_key(name), yob):
                salt += 1
                pid = stable_synthetic_id(name, yob, salt)
        self.by_id[pid] = (name, name_key(name), yob, nationality)
        self._link(pid, name, yob)
        return pid

    @staticmethod
    def _identity_year(value):
        try:
            value = int(value) if value is not None else None
        except (TypeError, ValueError):
            return None
        return None if value in (1900, 1901) else value

    def _link(self, pid, name, yob):
        nk = name_key(name)
        self.by_key[(nk, yob)] = pid
        lst = self.by_name.setdefault(nk, [])
        if pid not in lst:
            lst.append(pid)

    def from_anne(self, user_id, name, yob, nationality=None):
        yob = self._identity_year(yob)
        profile = self.anne_profiles.by_id.get(user_id)
        if profile:
            name = profile["name"] or name
            yob = profile["year_of_birth"] or yob
            nationality = profile["nationality"] or nationality
        self.anne_ids.add(user_id)
        if user_id in self.by_id:
            self._link(user_id, name, yob)
            if yob is not None and self.by_id[user_id][2] is None:
                cur = self.by_id[user_id]
                self.by_id[user_id] = (cur[0], cur[1], yob, cur[3] or nationality)
            if profile and profile["name"]:
                self.record(user_id, profile["name"], authoritative=True)
            return user_id
        pid = self._new(name, yob, nationality, pid=user_id)
        if profile and profile["name"]:
            self.record(pid, profile["name"], authoritative=True)
        return pid

    def from_legacy(self, name, yob, club=None):
        yob = self._identity_year(yob)
        nk = name_key(name)
        profile_matches, profile_basis = self.anne_profiles.match(name, yob, club)
        if len(profile_matches) == 1:
            profile = profile_matches[0]
            pid = self.from_anne(profile["oefol_id"], profile["name"],
                                  profile["year_of_birth"], profile["nationality"])
            self._link(pid, name, yob)
            if profile_basis == "name-yob":
                return pid, "anne-registry-name-yob", 0.99, "resolved"
            return pid, "anne-registry-name-club", 0.95, "resolved"
        if (nk, yob) in self.by_key:
            return self.by_key[(nk, yob)], ("legacy-name-yob" if yob else "legacy-name"), \
                (0.75 if yob else 0.55), "candidate"

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
            return self._new(name, yob), ("legacy-name-yob" if yob else "legacy-name"), \
                (0.75 if yob else 0.55), "candidate"

        self._link(pid, name, yob)
        return pid, ("legacy-name-yob" if yob else "legacy-name"), \
            (0.75 if yob else 0.55), "candidate"


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
              if not is_bewertung_clone(e) and e["id"] not in EXCLUDED_EVENTS}
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
    "sportident-center": ROOT / "ingest" / "parse_sportident_center.py",
    "anne-entry-recovery": (
        ROOT / "ingest" / "build_source_omission_recoveries.py"),
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
    snapshot_path = next(iter((RAW / "files").glob(f"{stem}.*")), None)
    cur.execute(
        """INSERT OR IGNORE INTO source_document
           (id, event_id, source_type, source_url, file_name, snapshot_path,
            snapshot_sha256, normalized_path, normalized_sha256, parser_version)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (document_id, doc["eventId"], source, doc.get("sourceUrl"),
         doc.get("fileName"), repo_path(snapshot_path) if snapshot_path else None,
         file_sha256(snapshot_path),
         repo_path(normalized_path), file_sha256(normalized_path),
         parser_version(source)))
    return document_id


def championship_category_key(category):
    """Normalize ANNE codes and spelled-out PDF classes to one join key."""
    text = unicodedata.normalize("NFKD", category or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).casefold()
    text = re.sub(r"\s+", " ", text).strip()
    gender = ("d" if re.match(r"^(?:d(?:amen)?)(?:\b|[-\d])", text) else
              "h" if re.match(r"^(?:h(?:erren)?)(?:\b|[-\d])", text) else None)
    if gender:
        elite = bool(re.search(r"\belite\b|21\s*-?\s*e\b", text))
        age_match = (re.search(r"\bbis\s*(\d{1,3})\b", text)
                     or re.search(r"\bab\s*(\d{1,3})\b", text)
                     or re.search(r"^[dh]\s*-?\s*(\d{1,3})", text))
        if age_match:
            age = int(age_match.group(1))
            return f"{gender}{age}{'e' if elite else ''}"
        if elite or text in {"damen", "herren"}:
            return f"{gender}21e"
    return re.sub(r"[^a-z0-9]+", "", text)


def configured_championship_stage(cur, event, stage_ids, config):
    pattern = config.get("stage_title")
    stages = cur.execute(
        "SELECT id, title FROM stage WHERE event_id = ? ORDER BY number, id",
        (event["id"],)).fetchall()
    if pattern:
        matches = [sid for sid, title in stages
                   if re.search(pattern, title or "", re.I)]
        if len(matches) == 1:
            return matches[0]
    if len(stages) == 1:
        return stages[0][0]
    return default_stage(cur, event, stage_ids)


def register_championship_source_entries(cur, doc, stage_id, source_document_id,
                                         config):
    """Persist source observations without duplicating overlapping results."""
    stem = Path(doc["_normalizedPath"]).stem
    evidence_kind = ("official_championship_inclusion"
                     if config.get("explicit_eligibility")
                     else "official_championship_field")
    inserted = 0
    for category in doc.get("categories") or []:
        category_name = category.get("name") or ""
        category_key = championship_category_key(category_name)
        for index, row in enumerate(category.get("results") or []):
            observed_name = (row.get("name") or "").strip()
            cleaned = clean_name(observed_name)
            if not is_valid_name(cleaned):
                continue
            entry_id = "champ-source:" + hashlib.sha256(
                f"{stem}\0{category_name}\0{index}\0{name_key(cleaned)}".encode()
            ).hexdigest()[:24]
            cur.execute(
                """INSERT OR REPLACE INTO championship_source_entry
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (entry_id, stage_id, source_document_id, category_name,
                 category_key, observed_name, name_key(cleaned), row.get("club"),
                 row.get("rank"), row.get("status"), None,
                 config["championship"], evidence_kind, config["scope"]))
            inserted += 1
    return inserted


def apply_championship_source_entries(cur):
    """Join official attachment observations onto canonical result rows.

    A specifically filtered ``Meisterschaftswertung`` is direct event-time
    eligibility evidence. A general result sheet headed ÖM/ÖSTM confirms the
    championship category and its source coverage, but does not by itself
    prove that every foreign/clubless guest was eligible.
    """
    candidates = defaultdict(list)
    ranked_candidates = defaultdict(list)
    for row in cur.execute(
            """SELECT r.id, r.stage_id, r.category, p.name_key, r.observed_name,
                      r.observed_club, r.championship, r.rank
               FROM result r LEFT JOIN person p ON p.id = r.person_id""").fetchall():
        (rid, stage_id, category, person_key, observed_name, observed_club,
         championship, rank) = row
        keys = {key for key in (person_key,
                                name_key(clean_name(observed_name)) if observed_name else None)
                if key}
        for key in keys:
            candidates[(stage_id, championship_category_key(category), key)].append(
                (rid, category, observed_club, championship, rank))
        if rank is not None:
            ranked_candidates[(stage_id, championship_category_key(category), rank)].append(
                (rid, category, observed_club, championship, rank))

    matched = 0
    entries = cur.execute(
        """SELECT id, stage_id, category_key, observed_name_key, observed_club,
                  observed_rank, championship_type, evidence_kind, source_scope
           FROM championship_source_entry""").fetchall()
    for (entry_id, stage_id, category_key, observed_name_key, observed_club,
         observed_rank, championship_type, evidence_kind, source_scope) in entries:
        options = candidates.get((stage_id, category_key, observed_name_key), [])
        # Pair-only medal sheets sometimes compress two names into one token
        # (``Nora-Sophia Tandl-Asseg``).  The official rank + category + club
        # identifies the source unit and intentionally maps to both member
        # rows in the full result list.
        if (not options and evidence_kind == "official_championship_inclusion"
                and observed_rank is not None):
            options = ranked_candidates.get(
                (stage_id, category_key, observed_rank), [])
        if not options:
            continue
        if observed_club:
            clean_observed_club = re.sub(
                r"(?i)^x\s*\d+\s+", "", observed_club).strip()
            club_key = canonicalize_official_club(
                clean_observed_club, OFFICIAL_CLUBS)
            club_matches = [option for option in options
                            if canonicalize_official_club(option[2], OFFICIAL_CLUBS) == club_key]
            if club_matches:
                options = club_matches
        rid, result_category, _club, existing_championship, _rank = options[0]
        cur.execute(
            "UPDATE championship_source_entry SET result_id = ? WHERE id = ?",
            (rid, entry_id))
        eligible_category = (
            is_ostm_eligible_category(result_category)
            if championship_type == "ÖSTM" else is_om_eligible_category(result_category))
        if evidence_kind == "official_championship_inclusion":
            eligible_category = True  # the filtered source itself is authoritative
            cur.executemany(
                """UPDATE result
                   SET championship = ?, championship_eligibility_state = 'eligible',
                       championship_eligibility_basis = 'official_championship_ranking',
                       championship_source_scope = ?
                   WHERE id = ?""",
                [(championship_type, source_scope, option[0]) for option in options])
        elif existing_championship is not None or eligible_category:
            cur.execute(
                """UPDATE result
                   SET championship = COALESCE(championship, ?),
                       championship_eligibility_state = CASE
                         WHEN championship_eligibility_state = 'unknown'
                         THEN 'provisional' ELSE championship_eligibility_state END,
                       championship_eligibility_basis = CASE
                         WHEN championship_eligibility_basis = 'none'
                         THEN 'official_championship_field'
                         ELSE championship_eligibility_basis END,
                       championship_source_scope = ?
                   WHERE id = ?""",
                (championship_type, source_scope, rid))
        matched += 1
    return matched


def result_list_id(stage_id, source_document_id, category):
    raw = f"{stage_id}\0{source_document_id}\0{category}".encode()
    return "list:" + hashlib.sha256(raw).hexdigest()[:24]


def normalized_source_unit_count(rows):
    """Count units in parser output before cross-source result deduplication.

    A complete PDF can overlap a narrower championship PDF. Results are
    intentionally deduplicated in the public result table, but that must not
    turn the complete source's 26 parsed rows into a false 26-vs-16 parser
    blocker. This count belongs to the source-list snapshot itself.
    """
    keys = []
    for index, row in enumerate(rows or []):
        if row.get("excludedFromDeclaredCount"):
            continue
        kind = row.get("resultKind") or "individual"
        if kind in ("individual", "family"):
            key = ("row", index)
        elif kind == "pair" and row.get("teamNumber"):
            key = (kind, "number", row["teamNumber"])
        elif kind == "pair":
            partner_note = row.get("note") or ""
            if partner_note.startswith("Partner: "):
                members = [row.get("name") or ""] + [
                    member.strip() for member in partner_note[9:].split(",")
                    if member.strip()
                ]
                key = (kind, "members", tuple(sorted({
                    member.casefold() for member in members})))
            else:
                key = (kind, row.get("rank"), row.get("status"),
                       row.get("timeS"), row.get("club"))
        else:
            key = (kind, row.get("teamNumber") or row.get("teamName")
                   or row.get("note"))
        keys.append(key)
    return len(set(keys))


def reserve_source_row_key(
        seen, current_list_occurrences, base_key, source_signature):
    """Reserve a dedup key without dropping repeated rows in one source list.

    ``seen`` deliberately removes overlapping copies from different official
    documents.  A single document can nevertheless list the same person more
    than once in one class, for example a ranked KO-sprint start followed by
    ``Name (2) – DNS``.  Such a second observation is real source data and
    must survive even though both labels resolve to the same person identity.
    """
    signatures = current_list_occurrences[base_key]
    if source_signature in signatures:
        return None
    occurrence = len(signatures)
    key = (base_key if occurrence == 0
           else (*base_key, "repeated-source-row", occurrence))
    if key in seen:
        return None
    seen.add(key)
    signatures.add(source_signature)
    return key


def register_result_list(cur, stage_id, source_document_id, category, category_full,
                         declared_starters, rows, course=None):
    """Create the stable review unit for one category in one source list."""
    list_id = result_list_id(stage_id, source_document_id, category)
    course = course or {}
    source_unit_count = course.get("sourceUnitCount")
    fingerprint = hashlib.sha256(json.dumps(
        {"rows": rows, "sourceUnitCount": source_unit_count},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str).encode()).hexdigest()
    cur.execute(
        """INSERT OR IGNORE INTO result_list
           (id, stage_id, source_document_id, category, category_full,
            declared_starters, parsed_entries, parsed_rows, ranking_basis,
            course_length_m, course_climb_m, course_controls, input_fingerprint)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (list_id, stage_id, source_document_id, category, category_full,
         declared_starters, (source_unit_count if source_unit_count is not None
                             else normalized_source_unit_count(rows)), len(rows or []),
         ("other" if (any(row.get("rankingBasis") == "other" for row in (rows or []))
                      # Cup end standings expose one representative stage
                      # time per row, but rank by the accumulated points over
                      # several races.  Their category header enumerates the
                      # stages explicitly (``1.Lauf ... 2.Lauf ...``).
                      or len(re.findall(r"\b\d+\.\s*Lauf\b", category or "", re.I)) >= 2)
          else "score" if any(row.get("rankingBasis") == "score"
                              or row.get("scoreText") not in (None, "")
                              or row.get("scorePoints") not in (None, "")
                              # Some old MeOS Score PDFs placed ``640 p.`` in
                              # the reconstructed club cell.  It is still an
                              # unambiguous source-native points column.
                              or re.search(r"\b-?\d+\s*p\.\s*$",
                                           row.get("club") or "", re.I)
                              for row in (rows or []))
          else "time"),
         course.get("length") or course.get("courseLengthM"),
         course.get("climb") or course.get("courseClimbM"),
         course.get("controlCount") or course.get("courseControls"), fingerprint))
    return list_id


def normalize_qualitative_result_ranks(categories):
    """Qualitative U10 results (``gut``/participated) are never ranked.

    One historic PDF contains a stray ``1`` in the placement column for a
    single child while every row in both U10 classes is intentionally reported
    only as ``gut``. Preserve the participation status but do not invent a
    winner from that isolated source artifact (event 657).
    """
    for category in categories or []:
        for row in category.get("results") or []:
            if re.search(
                    r"(?i)(?:(?:sehr\s+)?gut|teilg|(?:erfolgreich\s+)?teilgenommen)\s*$",
                    row.get("timeText") or ""):
                row.pop("rank", None)
    return categories


RESULT_COLS = ("stage_id", "person_id", "result_list_id", "category", "category_full", "club", "official_club",
               "rank", "status", "time_s", "time_behind_s", "out_of_competition",
               "course_length_m", "course_climb_m", "course_controls",
               "result_kind", "note", "team_number", "team_name", "leg_number", "leg_count",
               "individual_status", "team_status", "team_time_s", "observed_team_time",
               "source", "source_document_id",
               "observed_name", "observed_club", "observed_user_id", "observed_category",
               "observed_rank", "observed_status", "observed_time",
               "identity_basis", "identity_confidence", "identity_state", "championship",
               "championship_eligibility_state", "championship_eligibility_basis",
               "championship_source_scope", "observed_nation")


def insert_result(cur, **kw):
    kw.setdefault("out_of_competition", 0)
    kw.setdefault("result_kind", "individual")
    kw.setdefault("identity_basis", "unknown")
    kw.setdefault("identity_confidence", 0.0)
    # ANNE and a few legacy exports use negative elapsed values as internal
    # sentinels for MP/DNS rather than as durations. Keep the raw observation
    # for audit/display, but never expose a negative value as a measured time.
    for duration_field in ("time_s", "time_behind_s", "team_time_s"):
        value = kw.get(duration_field)
        if isinstance(value, (int, float)) and value < 0:
            kw[duration_field] = None
    # A numeric placement is itself an explicit classification. Several
    # score, school-cup and browser-to-PDF result lists intentionally omit the
    # elapsed-time column, while old normalized snapshots consequently stored
    # these ranked finishers as ``unknown``. Do not override an explicit API
    # classification such as ``notClassified``; this compatibility rule is
    # limited to parser output whose raw status was absent/unknown.
    if (kw.get("status") == "unknown" and kw.get("rank") is not None
            and kw.get("observed_status") in (None, "", "unknown")):
        kw["status"] = "ok"
    kw.setdefault("identity_state", "resolved" if kw.get("identity_confidence", 0) >= 1.0
                  else ("candidate" if kw.get("person_id") is not None else "unresolved"))
    kw.setdefault("championship_eligibility_state", "unknown")
    kw.setdefault("championship_eligibility_basis", "none")
    kw.setdefault("championship_source_scope", "inferred")
    if kw.get("championship") is not None and kw["championship_eligibility_state"] == "unknown":
        if kw.get("source") == "anne-api":
            kw["championship_eligibility_state"] = "eligible"
            kw["championship_eligibility_basis"] = "official_anne_championship"
            kw["championship_source_scope"] = "full_field"
        else:
            kw["championship_eligibility_state"] = "eligible"
            kw["championship_eligibility_basis"] = "official_champion_annotation"
            kw["championship_source_scope"] = "winner_only"
    vals = [kw.get(c) for c in RESULT_COLS]
    cur.execute(f"INSERT INTO result ({','.join(RESULT_COLS)}) "
                f"VALUES ({','.join('?' * len(RESULT_COLS))})", vals)


TEAM_STATUS_PRIORITY = {
    "unknown": 0, "ok": 1, "dns": 2, "dnf": 3, "mp": 4, "dsq": 5,
}


def aggregate_team_status(declared_status, member_statuses):
    """Compute one classification for a whole relay/team unit."""
    known = [s for s in [declared_status, *(member_statuses or [])]
             if s in TEAM_STATUS_PRIORITY and s != "unknown"]
    return max(known, key=TEAM_STATUS_PRIORITY.get) if known else "unknown"


def relay_metadata(row, kind):
    """Read explicit relay fields, with notes as a compatibility fallback."""
    if kind not in ("relay", "team", "pair"):
        return {}
    note = row.get("note") or ""
    team_name = row.get("teamName")
    if not team_name:
        match = re.search(r"Staffel:\s*([^·]+)", note)
        team_name = match.group(1).strip() if match else row.get("club")
    leg_number = row.get("leg")
    leg_count = row.get("legCount")
    if leg_number is None:
        match = re.search(r"Leg\s+(\d+)(?:/(\d+))?", note)
        if match:
            leg_number = int(match.group(1))
            leg_count = leg_count or (int(match.group(2)) if match.group(2) else None)
    return {
        "team_number": str(row.get("teamNumber")) if row.get("teamNumber") not in (None, "") else None,
        "team_name": team_name,
        "leg_number": int(leg_number) if leg_number not in (None, "") else None,
        "leg_count": int(leg_count) if leg_count not in (None, "") else None,
        "individual_status": row.get("individualStatus"),
        "team_status": row.get("teamStatus") or row.get("status"),
        "team_time_s": row.get("teamTimeS"),
        "observed_team_time": row.get("teamTimeText"),
    }


def legacy_result_unit_identity(row, kind, metadata=None,
                                preserve_repeated_relay_leg=False):
    """Stable within-list identity used before cross-source deduplication.

    Most overlapping HTML/PDF relay sources disagree about leg metadata, so
    adding the leg globally would duplicate thousands of otherwise identical
    observations. It is essential only when the *same source category*
    actually contains the same person/team more than once.
    """
    metadata = metadata or relay_metadata(row, kind)
    team_identity = metadata.get("team_number") or metadata.get("team_name")
    if (kind == "relay" and preserve_repeated_relay_leg
            and metadata.get("leg_number") is not None):
        return team_identity, metadata["leg_number"]
    if kind == "pair":
        return row.get("note") or (
            row.get("rank"), row.get("status"), row.get("timeS"), row.get("club"))
    return team_identity


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
    # When structured results exist, load_anne_results() has already created
    # ANNE's real stage row.  A supplemental attachment for an otherwise empty
    # stage must reuse that id instead of minting a parallel synthetic stage.
    existing = cur.execute(
        "SELECT id FROM stage WHERE event_id = ? AND number = ? ORDER BY id LIMIT 1",
        (event["id"], num)).fetchone()
    if existing:
        return existing[0]
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
        rows = deduplicate_anne_rows([
            row for row in json.loads(path.read_text())
            if is_active_anne_result(row)
        ])
        # A small family of old ANNE migrations stores elapsed time as a
        # whole number of *minutes* (for example 42 for an exact 42:41 in the
        # attached official result list), while the current API contract and
        # every normal result use seconds.  They are also conspicuous at the
        # document level: a substantial completed event, no source rank at
        # all, and no elapsed value reaching five minutes.  Scale those values
        # for display/calculation, but retain the source integer separately
        # and mark the loss of precision for the audit model.  Inferring ranks
        # from row order would be unsafe because minute ties are not real ties.
        if anne_results_have_minute_precision(rows):
            converted = []
            for source_row in rows:
                row = dict(source_row)
                row["_observedMinuteTime"] = row.get("time")
                if isinstance(row.get("time"), (int, float)):
                    row["time"] = round(row["time"] * 60)
                row["_timePrecisionNote"] = "ANNE-Altimport: Zeit nur minutengenau"
                converted.append(row)
            rows = converted
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
        list_ids = {}
        rows_by_list = defaultdict(list)
        for row in rows:
            sid = row.get("eventStageId") or default_stage(cur, event, stage_ids)
            cat = row.get("categoryShortTitle") or row.get("categoryTitle")
            rows_by_list[(sid, cat)].append(row)
        for (sid, cat), list_rows in rows_by_list.items():
            sample = list_rows[0]
            list_ids[(sid, cat)] = register_result_list(
                cur, sid, source_document_id, cat, sample.get("categoryTitle"),
                None, list_rows, sample.get("course") or {})
        for r in rows:
            sid = r.get("eventStageId") or default_stage(cur, event, stage_ids)
            cat = r.get("categoryShortTitle") or r.get("categoryTitle")
            list_id = list_ids[(sid, cat)]
            family_state = classify_family_category(
                cat, r.get("categoryTitle"), event.get("id"))
            if family_state == "family":
                members = r.get("teamMembers") or []
                member_names = [
                    clean_name(f"{m.get('firstName') or ''} {m.get('lastName') or ''}".strip())
                    for m in members]
                observed_name = (r.get("teamName") or
                                 " + ".join(n for n in member_names if n) or
                                 f"{r.get('firstName') or ''} {r.get('lastName') or ''}".strip())
                raw_status = r.get("classification")
                status, ooc = normalize_status(
                    ANNE_STATUS.get(raw_status, "unknown"), raw_status,
                    r.get("outOfCompetition"))
                course = r.get("course") or {}
                club = clean_club(r.get("clubName"))
                insert_result(
                    cur, stage_id=sid, person_id=None, result_list_id=list_id,
                    category=cat, category_full=r.get("categoryTitle"), club=club,
                    official_club=canonicalize_official_club(club, OFFICIAL_CLUBS),
                    rank=r.get("rank"), status=status, time_s=r.get("time"),
                    time_behind_s=r.get("timeBehind"), out_of_competition=ooc,
                    course_length_m=course.get("length"), course_climb_m=course.get("climb"),
                    course_controls=course.get("controlCount"), result_kind="family",
                    note=" · ".join(filter(None, [
                        "Family-Ergebnis ohne Personenzuordnung",
                        r.get("_timePrecisionNote")
                    ])), source="anne-api",
                    source_document_id=source_document_id, observed_name=observed_name,
                    observed_club=r.get("clubName"), observed_user_id=str(r.get("userId"))
                    if r.get("userId") not in (None, "") else None,
                    observed_category=cat, observed_rank=str(r.get("rank"))
                    if r.get("rank") is not None else None,
                    observed_status=raw_status,
                    observed_time=str(r.get("_observedMinuteTime", r.get("time")))
                    if r.get("time") is not None else None,
                    identity_basis="not-applicable-family", identity_confidence=1.0,
                    identity_state="not_applicable", championship=None)
                n += 1
                continue
            if r.get("teamMembers"):
                n += insert_anne_relay(cur, persons, sid, cat, r, source_document_id, list_id)
                continue
            observed_name = f"{r.get('firstName') or ''} {r.get('lastName') or ''}".strip()
            name = clean_result_name(eid, observed_name)
            # some old imports carry bib/SI numbers or 'empty' placeholders
            if not is_valid_name(name) or "empty" in name.lower():
                continue
            uid = anne_user_id(r.get("userId"))
            club = clean_club(r.get("clubName"))
            if uid:
                pid = persons.from_anne(uid, name, r.get("yearOfBirth"), r.get("nationality"))
                identity_basis, identity_confidence, identity_state = \
                    "source-oefol-id", 1.0, "resolved"
            else:
                pid, identity_basis, identity_confidence, identity_state = persons.from_legacy(
                    name, r.get("yearOfBirth"), club)
            if r.get("firstName"):
                persons.add_first_name(r["firstName"])
            if r.get("lastName"):
                persons.add_last_name(r["lastName"])
            persons.record(pid, name, authoritative=bool(r.get("firstName") and r.get("lastName")))
            course = r.get("course") or {}
            raw_status = r.get("classification")
            status, ooc = normalize_status(
                ANNE_STATUS.get(raw_status, "unknown"), raw_status,
                r.get("outOfCompetition"))
            insert_result(cur, stage_id=sid, person_id=pid, result_list_id=list_id, category=cat,
                          category_full=r.get("categoryTitle"), club=club,
                          official_club=canonicalize_official_club(club, OFFICIAL_CLUBS),
                          rank=r.get("rank"),
                          status=status,
                          time_s=r.get("time"), time_behind_s=r.get("timeBehind"),
                          out_of_competition=ooc,
                          course_length_m=course.get("length"),
                          course_climb_m=course.get("climb"),
                          course_controls=course.get("controlCount"),
                          note=r.get("_timePrecisionNote"), source="anne-api",
                          source_document_id=source_document_id,
                          observed_name=observed_name, observed_club=r.get("clubName"),
                          observed_user_id=str(uid) if uid is not None else None,
                          observed_category=cat,
                          observed_rank=str(r.get("rank")) if r.get("rank") is not None else None,
                          observed_status=raw_status,
                          observed_time=str(r.get("_observedMinuteTime", r.get("time")))
                          if r.get("time") is not None else None,
                          identity_basis=identity_basis,
                          identity_confidence=identity_confidence,
                          identity_state=identity_state,
                          championship=anne_championship(r))
            n += 1
    return n


def anne_results_have_minute_precision(rows):
    """Identify ANNE legacy payloads whose elapsed unit is whole minutes.

    The threshold is deliberately document-wide and conservative.  A single
    very short category must never trigger it; the source must contain at
    least twenty timed classified rows, no ranks anywhere, and every positive
    elapsed value must be below five minutes.  Real sprint events still have
    finishers above that bound, whereas the confirmed migrations contain
    hundreds of values such as 17, 42 and 84.
    """
    timed = [
        row.get("time") for row in rows or []
        if row.get("classification") == "classified"
        and isinstance(row.get("time"), (int, float))
        and row.get("time") > 0
    ]
    return (len(timed) >= 20
            and not any(row.get("rank") is not None for row in rows or [])
            and max(timed, default=300) < 300)


def insert_anne_relay(cur, persons, sid, cat, team, source_document_id=None,
                      result_list_id_value=None):
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
    raw_team_status = team.get("classification")
    declared_team_status, team_ooc = normalize_status(
        ANNE_STATUS.get(raw_team_status, "unknown"), raw_team_status,
        team.get("outOfCompetition"))
    member_statuses = []
    for member in members:
        raw_member_status = (member.get("classification") or
                             (member.get("overall") or {}).get("classification"))
        member_statuses.append(normalize_status(
            ANNE_STATUS.get(raw_member_status, "unknown"), raw_member_status)[0])
    team_status = aggregate_team_status(declared_team_status, member_statuses)
    team_number = team.get("bibNumber") or team.get("startNumber")
    team_time = team.get("time")
    n = 0
    prev_cum = 0
    for member_index, (m, nm) in enumerate(zip(members, names), start=1):
        ov = m.get("overall") or {}
        cum = ov.get("time")
        leg_time = (cum - prev_cum) if (cum is not None) else None
        if cum is not None:
            prev_cum = cum
        if not nm:
            continue
        uid = anne_user_id(m.get("userId"))
        relay_club = clean_club(team.get("clubName"))
        if uid is not None:
            pid = persons.from_anne(uid, nm, m.get("yearOfBirth"), m.get("nationality"))
            identity_basis, identity_confidence, identity_state = \
                "source-oefol-id", 1.0, "resolved"
        else:
            pid, identity_basis, identity_confidence, identity_state = persons.from_legacy(
                nm, m.get("yearOfBirth"), relay_club)
        if m.get("firstName"):
            persons.add_first_name(m["firstName"])
        if m.get("lastName"):
            persons.add_last_name(m["lastName"])
        persons.record(pid, nm, authoritative=bool(m.get("firstName") and m.get("lastName")))
        mates = list(dict.fromkeys(o for o in names if o and o != nm))
        note_bits = [f"Staffel: {team_name}".strip(),
                     f"Leg {m.get('leg') or member_index}/{len(members)}"]
        if mates:
            note_bits.append("Team: " + ", ".join(mates))
        observed_name = f"{m.get('firstName') or ''} {m.get('lastName') or ''}".strip()
        raw_status = (m.get("classification") or
                      (m.get("overall") or {}).get("classification"))
        individual_status = member_statuses[member_index - 1]
        insert_result(cur, stage_id=sid, person_id=pid,
                      result_list_id=result_list_id_value, category=cat,
                      category_full=team.get("categoryTitle"), club=relay_club,
                      official_club=canonicalize_official_club(relay_club, OFFICIAL_CLUBS),
                      rank=team.get("rank"),
                      status=team_status, out_of_competition=team_ooc,
                      time_s=leg_time, result_kind="relay",
                      note=" · ".join(note_bits),
                      team_number=str(team_number) if team_number not in (None, "") else None,
                      team_name=team_name, leg_number=m.get("leg") or member_index,
                      leg_count=len(members), individual_status=individual_status,
                      team_status=team_status, team_time_s=team_time,
                      observed_team_time=str(team_time) if team_time is not None else None,
                      source="anne-api",
                      source_document_id=source_document_id,
                      observed_name=observed_name, observed_club=team.get("clubName"),
                      observed_user_id=str(uid) if uid is not None else None,
                      observed_category=cat,
                      observed_rank=str(team.get("rank")) if team.get("rank") is not None else None,
                      observed_status=raw_status,
                      observed_time=str(leg_time) if leg_time is not None else None,
                      identity_basis=identity_basis,
                      identity_confidence=identity_confidence,
                      identity_state=identity_state,
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
# ("...-e1.pdf", "RESULT2.html", or "Ergebnisse_1Etappe.html"), which lines up
# directly with ANNE's own ordered stages - see map_docs_to_anne_stages,
# which uses this to give each file its true stage identity (date AND title)
# from ANNE even when two races share a day, which a date-only split can't.
ETAPPE_FILENAME_RE = re.compile(
    r"(?:\b(?:results?|ergebnisse?)[\s._-]*|(?<![a-z])e[\s.-]*)(\d{1,2})(?![0-9])",
    re.I)
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
COURSE_VIEW_FILENAME_RE = re.compile(
    r"(?:nach[\s._-]*bahnen|by[\s._-]*courses?)", re.I)

# ANNE describes these historic umbrella events as single-stage even though
# their independent result documents prove multiple competition dates. Keep
# the exception explicit: inferred ``docDate`` is often only a publication
# timestamp, so automatically treating every date disagreement as a stage
# would duplicate provisional/final and category/course views.
#
# 4089, Schul Olympics: full BM result lists dated 24 and 25 May 2023, each
# with its own complete rankings. Without this override the shared stage-level
# dedup retained only the first day and silently discarded the second.
LEGACY_MULTIDAY_EVENT_OVERRIDES = {4089}


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
            explicit = [
                d for d in event_docs
                if d.get("_multistageSource") and d.get("_anneStage")
            ]
            explicit_numbers = {
                d["_anneStage"].get("number") for d in explicit
            }
            if len(explicit_numbers) >= 2:
                by_date = defaultdict(list)
                for d in explicit:
                    stage_date = d["_anneStage"].get("date")
                    if stage_date:
                        by_date[stage_date].append(d["_anneStage"])
                for d in event_docs:
                    if d in explicit or d.get("_anneStage"):
                        continue
                    matches = by_date.get(d.get("docDate")) or []
                    if len(matches) == 1:
                        d["_anneStage"] = dict(matches[0])
                    elif d.get("listType") in ("race", "relay"):
                        d["_skip"] = True
                continue
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
            # A dedicated championship attachment often omits the AC round
            # but names the discipline (for example event 2541's
            # ``...oestm...ski-lang...pdf``).  When exactly one ANNE stage
            # carries the same discipline, that is an unambiguous mapping.
            source_text = " ".join(filter(None, (
                d.get("fileName"), derive_stage_title(d.get("docTitle")))))
            discipline_matches = []
            for discipline in ("sprint", "lang", "mittel", "nacht", "staffel",
                               "verfolgung", "knock out", "ko-sprint"):
                if re.search(rf"\b{re.escape(discipline)}\b", source_text, re.I):
                    matching_stages = [
                        i for i, stage in enumerate(stages)
                        if re.search(rf"\b{re.escape(discipline)}\b",
                                     stage.get("title") or "", re.I)
                    ]
                    if len(matching_stages) == 1:
                        discipline_matches.extend(matching_stages)
            if len(set(discipline_matches)) == 1:
                return discipline_matches[0]
            same = anne_date.get(d.get("docDate") or "")
            return same[0] if same and len(same) == 1 else None

        matched = [(match(d), d) for d in event_docs]
        if len({i for i, _ in matched if i is not None}) >= 2:
            for i, d in matched:
                if i is not None:
                    if not d.get("_multistageSource"):
                        d["_anneStage"] = info(i)
                elif d.get("listType") in ("race", "relay"):
                    d["_skip"] = True
    return docs


def drop_redundant_course_views(docs):
    """Drop a second, course-grouped view of the same stage's results.

    SportSoftware often publishes both ``Ergebnis nach Kategorien`` and
    ``Ergebnis nach Bahnen``.  They contain the same starts grouped
    differently, so importing both duplicates every runner and also creates
    artificial ``Bahn N`` result lists.  Keep the course view when it is the
    only available source; discard it only when a normal race/relay result
    document exists for the exact same event and mapped stage/date.

    Confirmed real: events 5129 (Herbst Cup) and 5245 (Sommer Cup).  This is
    deliberately stage-aware: a category file for Etappe 1 must never suppress
    the only available course file for Etappe 2.
    """
    def stage_key(doc):
        mapped = doc.get("_anneStage") or {}
        if mapped:
            return (doc["eventId"], mapped.get("number"), mapped.get("date"))
        return (doc["eventId"], None, doc.get("docDate"))

    preferred = {
        stage_key(doc)
        for doc in docs
        if doc.get("listType") in ("race", "relay")
        and not doc.get("_skip")
        and not COURSE_VIEW_FILENAME_RE.search(doc.get("fileName") or "")
    }
    return [
        doc for doc in docs
        if not (
            doc.get("listType") in ("race", "relay")
            and COURSE_VIEW_FILENAME_RE.search(doc.get("fileName") or "")
            and stage_key(doc) in preferred
        )
    ]


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


def legacy_document_quality(doc):
    """Return row, rank and exact-time coverage for one attachment."""
    rows = [
        row for category in doc.get("categories") or []
        for row in category.get("results") or []
        if not row.get("excludedFromDeclaredCount")
    ]
    times = [
        row.get("timeS") for row in rows
        if isinstance(row.get("timeS"), (int, float))
    ]
    return {
        "rows": len(rows),
        "ranked": sum(row.get("rank") is not None for row in rows),
        "max_time": max(times, default=None),
    }


def expand_multistage_normalized_document(document, normalized_path):
    """Expand one physical source into stage-scoped logical documents.

    Some official exports place E1/E2/E3 in columns of one Gesamt-Ergebnis.
    Keeping one source_document preserves provenance, while each logical
    child carries an explicit ANNE-stage identity and an independent category
    set.  Ordinary normalized documents remain unchanged.
    """
    stage_documents = document.get("stageDocuments")
    if not stage_documents:
        child = dict(document)
        child["_normalizedPath"] = normalized_path
        return [child]

    shared = {
        key: value for key, value in document.items()
        if key not in {"categories", "stageDocuments"}
    }
    expanded = []
    for stage_document in stage_documents:
        number = stage_document.get("stageNumber")
        categories = stage_document.get("categories") or []
        if not isinstance(number, int) or number < 1 or not categories:
            continue
        child = dict(shared)
        child.update({
            "listType": stage_document.get("listType") or "race",
            "docDate": stage_document.get("stageDate"),
            "docTitle": stage_document.get("stageTitle"),
            "categories": categories,
            "_anneStage": {
                "number": number,
                "date": stage_document.get("stageDate"),
                "title": stage_document.get("stageTitle"),
            },
            "_normalizedPath": normalized_path,
            "_multistageSource": True,
        })
        expanded.append(child)
    return expanded


def replace_minute_precision_anne_with_legacy(cur, stage_id, doc):
    """Prefer a complete exact official attachment over a lossy ANNE import.

    This is intentionally stricter than a generic HTML-over-API preference.
    It applies only when every persisted API row is explicitly marked as the
    confirmed minute-precision migration, ANNE provides no ranks, and the
    attachment covers at least 90% as many rows with both real ranks and
    second-resolution elapsed values. Narrow championship extracts can
    therefore never erase a complete API field.
    """
    api_rows, minute_rows, api_ranked = cur.execute(
        """SELECT COUNT(*),
                  SUM(COALESCE(note, '') LIKE '%ANNE-Altimport: Zeit nur minutengenau%'),
                  SUM(rank IS NOT NULL)
             FROM result
            WHERE stage_id = ? AND source = 'anne-api'""",
        (stage_id,)).fetchone()
    api_rows = api_rows or 0
    minute_rows = minute_rows or 0
    api_ranked = api_ranked or 0
    quality = legacy_document_quality(doc)
    if not (api_rows >= 20 and minute_rows == api_rows and api_ranked == 0
            and quality["rows"] >= max(20, int(api_rows * 0.9))
            and quality["ranked"] >= 5
            and (quality["max_time"] or 0) >= 300):
        return False

    api_lists = [row[0] for row in cur.execute(
        """SELECT rl.id
             FROM result_list rl
             JOIN source_document sd ON sd.id = rl.source_document_id
            WHERE rl.stage_id = ? AND sd.source_type = 'anne-api'""",
        (stage_id,)).fetchall()]
    cur.execute(
        "DELETE FROM result WHERE stage_id = ? AND source = 'anne-api'",
        (stage_id,))
    if api_lists:
        cur.executemany("DELETE FROM result_list WHERE id = ?",
                        [(list_id,) for list_id in api_lists])
    return True


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
        for expanded in expand_multistage_normalized_document(
                doc, repo_path(path)):
            normalize_qualitative_result_ranks(expanded.get("categories"))
            docs.append(expanded)
    docs = [d for d in docs if not JUNK_DOC_FILENAME_RE.search(d.get("fileName") or "")]
    docs, _n_dropped = drop_cross_event_duplicate_docs(docs)
    correct_legacy_stage_dates(docs, events)
    map_docs_to_anne_stages(docs)
    docs = drop_redundant_course_views(docs)
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
        if ((e.get("stageCount") or 0) >= 2
            or (e.get("dateTo") or "")[:10]
               not in ("", (e.get("dateFrom") or "")[:10])
            or eid in LEGACY_MULTIDAY_EVENT_OVERRIDES)
    }
    dates_by_event = defaultdict(set)
    for d in docs:
        if d.get("docDate") and d["eventId"] in multiday_events:
            dates_by_event[d["eventId"]].add(d["docDate"])
    seen = set()
    for doc in docs:
        eid = doc["eventId"]
        event = events.get(eid)
        source_config = CHAMPIONSHIP_SOURCE_CONFIG.get(
            Path(doc["_normalizedPath"]).stem)
        if not event or doc.get("listType") not in ("race", "relay"):
            continue
        if doc.get("_skip") and not source_config:
            continue  # redundant cumulative standing - see map_docs_to_anne_stages
        source_document_id = None
        configured_sid = None
        if source_config:
            source_document_id = register_legacy_document(cur, doc)
            configured_sid = configured_championship_stage(
                cur, event, stage_ids, source_config)
            register_championship_source_entries(
                cur, doc, configured_sid, source_document_id, source_config)
            if source_config["mode"] == "evidence":
                continue
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
        if configured_sid is not None:
            sid = configured_sid
        elif doc.get("_anneStage"):
            sid = anne_mapped_stage(cur, event, stage_ids, doc["_anneStage"])
        elif eid in anne_event_ids:
            # With structured data, an unmapped legacy document is safe only
            # for an unambiguous one-stage event.  In a multi-stage event it
            # could be a duplicate, cumulative standing, or belong to any
            # stage, so never guess.  Number/date/round mapping above must
            # identify it first.
            existing_stages = cur.execute(
                "SELECT id FROM stage WHERE event_id = ? ORDER BY number, id",
                (eid,)).fetchall()
            if len(existing_stages) != 1:
                continue
            sid = existing_stages[0][0]
        elif len(event_dates) > 1 and doc.get("docDate") in event_dates:
            sid = dated_stage(cur, event, stage_ids, doc["docDate"],
                               event_dates.index(doc["docDate"]) + 1)
        else:
            sid = default_stage(cur, event, stage_ids)
        if eid in anne_event_ids and not source_config and cur.execute(
                "SELECT 1 FROM result WHERE stage_id = ? AND source = 'anne-api' LIMIT 1",
                (sid,)).fetchone():
            if not replace_minute_precision_anne_with_legacy(cur, sid, doc):
                continue  # structured API data normally wins within the same stage
        source_document_id = source_document_id or register_legacy_document(cur, doc)
        if doc.get("docTitle") and doc["docTitle"] not in stage_doc_titles[sid]:
            stage_doc_titles[sid].append(doc["docTitle"])
        flip_doc = detect_lastname_firstname_doc(doc["categories"], persons.first_names)
        for cat in doc["categories"]:
            current_list_occurrences = defaultdict(set)
            list_id = register_result_list(
                cur, sid, source_document_id, cat["name"],
                cat.get("sourceCategory") or cat["name"],
                cat.get("declaredStarters"), cat.get("results") or [], {
                    "courseLengthM": cat.get("courseLengthM"),
                    "courseClimbM": cat.get("courseClimbM"),
                    "courseControls": cat.get("courseControls"),
                    "sourceUnitCount": cat.get("sourceUnitCount"),
                })
            if classify_family_category(
                    cat["name"], cat.get("sourceCategory"), eid) == "family":
                grouped = []
                group_index = {}
                for idx, row in enumerate(cat["results"]):
                    kind = row.get("resultKind") or "individual"
                    if kind in ("pair", "team"):
                        key = (kind, row.get("rank"), row.get("status"), row.get("timeS"),
                               row.get("club"))
                    else:
                        key = ("row", idx)
                    if key not in group_index:
                        group_index[key] = len(grouped)
                        grouped.append([])
                    grouped[group_index[key]].append(row)
                for unit in grouped:
                    first = unit[0]
                    names = list(dict.fromkeys(
                        (row.get("name") or "").strip() for row in unit
                        if (row.get("name") or "").strip()))
                    label = " + ".join(names)
                    raw_status = first.get("status", "unknown")
                    status, ooc = normalize_status(
                        raw_status, first.get("timeText") or raw_status,
                        first.get("outOfCompetition") or
                        bool(OOC_NAME_PREFIX_RE.match(first.get("name") or "")))
                    insert_result(
                        cur, stage_id=sid, person_id=None, result_list_id=list_id,
                        category=cat["name"], category_full=cat["name"],
                        club=first.get("club"),
                        official_club=canonicalize_official_club(
                            first.get("club"), OFFICIAL_CLUBS),
                        rank=first.get("rank"), status=status, time_s=first.get("timeS"),
                        out_of_competition=ooc,
                        course_length_m=cat.get("courseLengthM"),
                        course_climb_m=cat.get("courseClimbM"),
                        course_controls=cat.get("courseControls"),
                        result_kind="family", note="Family-Ergebnis ohne Personenzuordnung",
                        source=doc["source"], source_document_id=source_document_id,
                        observed_name=label, observed_club=first.get("club"),
                        observed_category=cat["name"],
                        observed_rank=str(first.get("rank"))
                        if first.get("rank") is not None else None,
                        observed_status=raw_status, observed_time=first.get("timeText"),
                        observed_nation=first.get("sourceNat"),
                        identity_basis="not-applicable-family", identity_confidence=1.0,
                        identity_state="not_applicable", championship=None)
                    n += 1
                continue
            for r in cat["results"]:
                # a parsed row may carry several runners (a pair): the parser
                # emits one entry per runner already, each with its own name
                # and a note; treat them uniformly here
                parser_kind = r.get("resultKind")
                if r.get("identityExcluded"):
                    # Preserve a ranked/timed source row whose Name cell is
                    # explicitly unusable as a person identifier (for example
                    # an SI-card number). It stays visible on the event but
                    # cannot enter the runner index or a championship.
                    observed_name = (r.get("name") or "").strip()
                    key = (sid, cat["name"], observed_name,
                           "unidentified", None)
                    if key in seen:
                        continue
                    seen.add(key)
                    raw_status = r.get("status", "unknown")
                    club_value = KNOWN_RESULT_CLUB_OVERRIDES.get(
                        (eid, clean_result_name(eid, observed_name)),
                        r.get("club"))
                    status, ooc = normalize_status(
                        raw_status, r.get("timeText") or raw_status,
                        r.get("outOfCompetition"))
                    insert_result(
                        cur, stage_id=sid, person_id=None,
                        result_list_id=list_id, category=cat["name"],
                        category_full=cat["name"], club=club_value,
                        official_club=canonicalize_official_club(
                            club_value, OFFICIAL_CLUBS),
                        rank=r.get("rank"), status=status,
                        time_s=r.get("timeS"), out_of_competition=ooc,
                        course_length_m=cat.get("courseLengthM"),
                        course_climb_m=cat.get("courseClimbM"),
                        course_controls=cat.get("courseControls"),
                        result_kind=r.get("resultKind") or "individual",
                        note=(r.get("note") or
                              "Quellzeile ohne verwendbaren Personenbezeichner"),
                        source=doc["source"],
                        source_document_id=source_document_id,
                        observed_name=observed_name,
                        observed_club=r.get("club"),
                        observed_category=cat["name"],
                        observed_rank=str(r.get("rank"))
                        if r.get("rank") is not None else None,
                        observed_status=raw_status,
                        observed_time=r.get("timeText"),
                        observed_nation=r.get("sourceNat"),
                        identity_basis="not-applicable-unidentified-source",
                        identity_confidence=1.0,
                        identity_state="not_applicable",
                        championship=None)
                    n += 1
                    continue
                if r.get("memberlessTeam") and parser_kind in ("relay", "team", "pair"):
                    # A DNS team can exist in the official result list without
                    # any participant/leg rows. Persist the team observation,
                    # but deliberately create no person identity.
                    kind, note = parser_kind, r.get("note")
                    metadata = relay_metadata(r, kind)
                    unit_identity = legacy_result_unit_identity(r, kind, metadata)
                    key = (sid, cat["name"], "", kind, unit_identity)
                    if key in seen:
                        continue
                    seen.add(key)
                    club_value = source_club_for_team(
                        r.get("club"), metadata.get("team_name"), kind)
                    raw_status = r.get("status", "unknown")
                    status, ooc = normalize_status(
                        raw_status, r.get("timeText") or raw_status,
                        r.get("outOfCompetition"))
                    metadata["individual_status"] = None
                    metadata["team_status"] = status
                    insert_result(
                        cur, stage_id=sid, person_id=None, result_list_id=list_id,
                        category=cat["name"], category_full=cat["name"],
                        club=club_value,
                        official_club=canonicalize_official_club(
                            club_value, OFFICIAL_CLUBS),
                        rank=r.get("rank"), status=status, time_s=None,
                        out_of_competition=ooc,
                        course_length_m=cat.get("courseLengthM"),
                        course_climb_m=cat.get("courseClimbM"),
                        course_controls=cat.get("courseControls"),
                        result_kind=kind, note=note, source=doc["source"],
                        source_document_id=source_document_id,
                        observed_name=None, observed_club=r.get("club"),
                        observed_category=cat["name"],
                        observed_rank=str(r.get("rank"))
                        if r.get("rank") is not None else None,
                        observed_status=raw_status,
                        observed_time=r.get("timeText"),
                        observed_nation=r.get("sourceNat"),
                        identity_basis="not-applicable-memberless-team",
                        identity_confidence=1.0, identity_state="not_applicable",
                        championship=None, **metadata)
                    n += 1
                    continue
                observed_name = r["name"]
                name = clean_result_name(eid, observed_name)
                if parser_kind == "relay" and is_relay_placeholder_name(name):
                    metadata = relay_metadata(r, "relay")
                    unit_identity = legacy_result_unit_identity(
                        r, "relay", metadata,
                        preserve_repeated_relay_leg=bool(
                            r.get("preserveRepeatedRelayLeg")))
                    key = (sid, cat["name"], "relay-placeholder", "relay", unit_identity)
                    if key in seen:
                        continue
                    seen.add(key)
                    club_value = source_club_for_team(
                        r.get("club"), metadata.get("team_name"), "relay")
                    raw_status = r.get("status", "unknown")
                    status, ooc = normalize_status(
                        raw_status, r.get("timeText") or raw_status,
                        r.get("outOfCompetition"))
                    individual_raw = metadata.get("individual_status")
                    metadata["individual_status"] = normalize_status(
                        individual_raw or "unknown",
                        r.get("timeText") or individual_raw or "unknown")[0]
                    metadata["team_status"] = status
                    insert_result(
                        cur, stage_id=sid, person_id=None, result_list_id=list_id,
                        category=cat["name"], category_full=cat["name"],
                        club=club_value,
                        official_club=canonicalize_official_club(
                            club_value, OFFICIAL_CLUBS),
                        rank=r.get("rank"), status=status, time_s=r.get("timeS"),
                        out_of_competition=ooc,
                        course_length_m=cat.get("courseLengthM"),
                        course_climb_m=cat.get("courseClimbM"),
                        course_controls=cat.get("courseControls"),
                        result_kind="relay", note=r.get("note"),
                        source=doc["source"], source_document_id=source_document_id,
                        observed_name=observed_name, observed_club=r.get("club"),
                        observed_category=cat["name"],
                        observed_rank=str(r.get("rank"))
                        if r.get("rank") is not None else None,
                        observed_status=raw_status, observed_time=r.get("timeText"),
                        observed_nation=r.get("sourceNat"),
                        identity_basis="not-applicable-relay-placeholder",
                        identity_confidence=1.0, identity_state="not_applicable",
                        championship=None, **metadata)
                    n += 1
                    continue
                if not is_valid_name(name):
                    continue
                if flip_doc:
                    toks = name.split()
                    if len(toks) == 2:
                        name = f"{toks[1]} {toks[0]}"
                # newer team tables are already split per member by the parser
                # (resultKind=team, full names, note set). For the older surname-
                # only roster format, a roster row is a run of >=3 surnames;
                # 2-token "Lastname Firstname" rows are the individual (Einzel)
                # categories these events also contain.
                if parser_kind:
                    kind, note = parser_kind, r.get("note")
                elif (doc_is_team_only
                      or (is_team and "einzel" not in cat["name"].casefold()
                          and len(name.split()) >= 3)):
                    kind, note = "team", "Mannschaft"
                else:
                    kind, note = "individual", r.get("note")
                metadata = relay_metadata(r, kind)
                # The same runner can legitimately appear in two distinct
                # Mannschaft rows of one category (for example once ranked
                # and once in a DNS reserve team). Team identity therefore
                # belongs in the dedup key; name alone silently dropped those
                # real source rows.
                unit_identity = legacy_result_unit_identity(
                    r, kind, metadata,
                    preserve_repeated_relay_leg=bool(
                        r.get("preserveRepeatedRelayLeg")))
                # A youth night-runner can appear once in a ranked pair and
                # again as a DNS/AK reserve pair in the same category.  The
                # partner note is the persisted identity of that pair; using
                # it in the dedup key retains both real observations instead
                # of silently dropping the second appearance of (for example)
                # Fuchs Max.
                base_key = (
                    sid, cat["name"], name_key(name), kind, unit_identity)
                source_signature = (
                    observed_name, r.get("rank"), r.get("status"),
                    r.get("timeS"), r.get("timeText"), r.get("club"),
                    r.get("note"))
                if reserve_source_row_key(
                        seen, current_list_occurrences, base_key,
                        source_signature) is None:
                    continue
                club_value = source_club_for_team(
                    r.get("club"), metadata.get("team_name") if metadata else None, kind)
                if kind == "relay" and metadata:
                    # Mixed SkiO relays print one combined team label (for
                    # example ``NF Kitzb./HSV Wr. Neust.``), not a club on
                    # each leg.  Keep that original label in observed_club
                    # and team_name, but use an unambiguous exact ANNE name +
                    # matching label component for the person's own club.
                    club_value = (persons.anne_profiles.relay_member_club(
                        name, metadata.get("team_name")) or club_value)
                club_value = KNOWN_RESULT_CLUB_OVERRIDES.get(
                    (eid, name), club_value)
                uid = anne_user_id(r.get("userId"))
                if uid:
                    pid = persons.from_anne(
                        uid, name, r.get("yearOfBirth"),
                        r.get("nationality"))
                    identity_basis, identity_confidence, identity_state = (
                        "source-oefol-id", 1.0, "resolved")
                    persons.record(pid, name, authoritative=True)
                else:
                    pid, identity_basis, identity_confidence, identity_state = (
                        persons.from_legacy(
                            name, r.get("yearOfBirth"), club_value))
                    persons.record(pid, name)
                raw_status = r.get("status", "unknown")
                observed_raw_status = raw_status
                if doc.get("source") == "liveresultat" and r.get("rawStatusCode") == 5:
                    # LiveResults' official code 5 is OT (over maximum time).
                    # Old committed snapshots called it unknown; normalize it
                    # reproducibly without requiring another network fetch.
                    raw_status = "dsq"
                    observed_raw_status = "overTime"
                elif (doc.get("source") == "liveresultat"
                      and doc.get("eventId") == 4292
                      and raw_status == "unknown"):
                    # The old snapshot predates rawStatusCode persistence.
                    # The completed competition's public API was rechecked:
                    # every one of these nine rows is code 9, Not Started Yet.
                    raw_status = "dns"
                    observed_raw_status = "notStartedYet"
                status, ooc = normalize_status(
                    raw_status, r.get("timeText") or raw_status,
                    r.get("outOfCompetition") or
                    bool(OOC_NAME_PREFIX_RE.match(observed_name)))
                if metadata:
                    if (kind == "team"
                            and not metadata.get("individual_status")):
                        # A Mannschaft runs together with one SI card: there
                        # is no individual leg classification to invent.
                        # Scored school teams are also represented as
                        # ``team`` but can carry a real per-member MP; retain
                        # that explicit source status.
                        metadata["individual_status"] = None
                    else:
                        individual_raw = metadata.get("individual_status")
                        metadata["individual_status"] = normalize_status(
                            individual_raw or raw_status,
                            r.get("timeText") or individual_raw or raw_status)[0]
                    metadata["team_status"] = status
                insert_result(cur, stage_id=sid, person_id=pid, result_list_id=list_id,
                              category=cat["name"],
                              category_full=cat["name"], club=club_value,
                              official_club=canonicalize_official_club(club_value, OFFICIAL_CLUBS),
                              rank=r.get("rank"), status=status,
                              time_s=r.get("timeS"),
                              out_of_competition=ooc,
                              course_length_m=cat.get("courseLengthM"),
                              course_climb_m=cat.get("courseClimbM"),
                              course_controls=cat.get("courseControls"),
                              result_kind=kind, note=note, source=doc["source"],
                              source_document_id=source_document_id,
                              observed_name=observed_name, observed_club=r.get("club"),
                              observed_user_id=(
                                  str(uid) if uid is not None else None),
                              observed_category=cat["name"],
                              observed_rank=str(r.get("rank"))
                              if r.get("rank") is not None else None,
                              observed_status=observed_raw_status,
                              observed_time=r.get("timeText"),
                              observed_nation=r.get("sourceNat"),
                              identity_basis=identity_basis,
                              identity_confidence=identity_confidence,
                              identity_state=identity_state,
                              championship=r.get("championship"), **metadata)
                n += 1

    for sid, titles in stage_doc_titles.items():
        for t in titles:
            label = derive_stage_title(t)
            if label:
                cur.execute("UPDATE stage SET title = ? WHERE id = ? AND title IS NULL", (label, sid))
                break

    return n


def _issue_id(list_id, result_id, code, message):
    raw = f"{list_id or ''}\0{result_id or ''}\0{code}\0{message}".encode()
    return "issue:" + hashlib.sha256(raw).hexdigest()[:24]


def add_audit_issue(cur, list_id, code, severity, message, result_id=None,
                    auto_resolvable=False):
    cur.execute(
        "INSERT OR IGNORE INTO audit_issue VALUES (?,?,?,?,?,?,?)",
        (_issue_id(list_id, result_id, code, message), list_id, result_id,
         code, severity, message, int(auto_resolvable)))


def stale_verification_requires_review(cur, list_id, assertion_state):
    """Whether an old manual assertion still needs to block the clean queue.

    A confirmed category whose row fingerprint changed but still passes every
    deterministic blocker/warning gate is covered by the UI's reproducible
    automatic confirmation.  Keeping a synthetic stale blocker there would
    contradict that model and force a no-op click.  Manual flags and lists
    with any current finding remain open for an explicit re-check.
    """
    if assertion_state == "flagged":
        return True
    return bool(cur.execute(
        """SELECT 1 FROM audit_issue
            WHERE result_list_id = ? AND severity IN ('blocker', 'warning')
              AND code != 'provisional_championship_identity'
            LIMIT 1""", (list_id,)).fetchone())


def normalize_tied_individual_ranks(cur):
    """Fill the rank suppressed on subsequent rows of an exact-time tie.

    Several source families (SportSoftware, liveresultat and hand-made PDFs)
    print the shared placement only on the first tied row. The blank value is
    not missing source data. Keep ``observed_rank`` untouched for provenance,
    but expose the derived shared rank to ranking and quality consumers.
    """
    rows = cur.execute(
        """SELECT id, result_list_id, rank, status, time_s,
                  out_of_competition, result_kind
             FROM result ORDER BY result_list_id, id"""
    ).fetchall()
    updates = []
    active_list = None
    previous_time = previous_rank = None
    for rid, list_id, rank, status, time_s, ooc, kind in rows:
        if list_id != active_list:
            active_list = list_id
            previous_time = previous_rank = None
        classified = (kind == "individual" and status == "ok"
                      and time_s is not None and not ooc)
        if not classified:
            previous_time = previous_rank = None
            continue
        if rank is not None:
            previous_time, previous_rank = time_s, rank
        elif previous_rank is not None and time_s == previous_time:
            updates.append((previous_rank, rid))
            # Keep the same active tie for a third or later blank-rank row.
        else:
            previous_time, previous_rank = time_s, None
    cur.executemany("UPDATE result SET rank = ? WHERE id = ?", updates)
    return len(updates)


def normalize_team_results(cur):
    """Backfill explicit team/leg fields and enforce one status per team.

    This is deliberately a build-time invariant in addition to parser logic:
    old normalized snapshots and ANNE's structured relay rows then obey the
    same semantics, and no UI/audit consumer has to infer teams from rank or
    member status again.
    """
    rows = cur.execute(
        """SELECT id, stage_id, result_list_id, category, result_kind, rank,
                  status, individual_status, team_status, team_number,
                  team_name, leg_number, leg_count, club, note
           FROM result WHERE result_kind IN ('relay', 'team') ORDER BY id"""
    ).fetchall()
    groups = defaultdict(list)
    prepared = []
    for row in rows:
        (rid, stage_id, list_id, category, kind, rank, status,
         individual_status, team_status, team_number, team_name,
         leg_number, leg_count, club, note) = row
        note = note or ""
        if not team_name:
            match = re.search(r"(?:Staffel|Mannschaft):\s*([^·]+)", note)
            team_name = match.group(1).strip() if match else club
        if leg_number is None:
            match = re.search(r"Leg\s+(\d+)(?:/(\d+))?", note)
            if match:
                leg_number = int(match.group(1))
                leg_count = leg_count or (int(match.group(2)) if match.group(2) else None)
        if kind == "relay":
            individual_status = individual_status or status
        # A scored team can retain an explicit per-member status even though
        # the best-N team result itself remains valid.  A classic one-chip
        # Mannschaft has no such value and therefore stays NULL.
        scope = list_id or f"{stage_id}:{category}"
        identity = (f"n:{team_number}" if team_number else
                    f"t:{(team_name or '').strip().casefold()}" if team_name else
                    f"legacy:{rank}:{(club or '').strip().casefold()}")
        key = (scope, kind, identity)
        item = {
            "id": rid, "kind": kind, "status": status,
            "team_status": team_status,
            "individual_status": individual_status, "team_name": team_name,
            "leg_number": leg_number, "leg_count": leg_count,
        }
        groups[key].append(item)
        prepared.append(item)

    for members in groups.values():
        statuses = [m["individual_status"] for m in members if m["individual_status"]]
        declared = aggregate_team_status(
            None, [m["team_status"] or m["status"] for m in members])
        overall = (declared if members[0]["kind"] == "team" else
                   aggregate_team_status(declared, statuses))
        inferred_leg_count = max(
            [m["leg_count"] or 0 for m in members] +
            [m["leg_number"] or 0 for m in members]) or None
        for m in members:
            cur.execute(
                """UPDATE result SET status = ?, team_status = ?,
                          individual_status = ?, team_name = ?, leg_number = ?,
                          leg_count = ? WHERE id = ?""",
                (overall, overall, m["individual_status"], m["team_name"],
                 m["leg_number"], m["leg_count"] or inferred_leg_count, m["id"]))


def competitor_unit_key(row):
    """Stable unit key for entry counts after pair/relay member expansion."""
    if len(row) == 10:
        rid, observed_name, kind, rank, status, time_s, club, note, team_number, team_name = row
    else:
        rid, kind, rank, status, time_s, club, note, team_number, team_name = row
        observed_name = ""
    if kind in ("individual", "family"):
        return f"row:{rid}"
    if kind == "pair" and team_number:
        return f"pair:number:{team_number}"
    if kind == "pair":
        if note and note.startswith("Partner: "):
            # Persisted rows do not carry their observed partner as a
            # separate relation.  The source note still provides a stable
            # roster key and prevents several same-club MP pairs from being
            # collapsed into one competitor unit.
            members = ([observed_name] if observed_name else []) + [
                member.strip() for member in note[9:].split(",") if member.strip()
            ]
            return "pair:members:" + ":".join(sorted({
                member.casefold() for member in members}))
        return f"pair:{rank}:{status}:{time_s}:{club or ''}"
    if team_number:
        return f"{kind}:number:{team_number}"
    if team_name:
        return f"{kind}:name:{team_name.strip().casefold()}"
    team = note or ""
    m = re.search(r"Staffel:\s*([^·]+)", team)
    legacy_name = m.group(1).strip() if m else club or team
    return f"{kind}:legacy:{legacy_name.strip().casefold()}"


OBSERVED_TIME_RE = re.compile(r"^\d{1,3}:\d{2}(?::\d{2})?$")
KNOWN_SOURCE_VALUE_CORRUPTIONS = {
    # Excel's print export lets the long runner name overwrite the adjacent
    # cell. These fragments are visibly all that remains in the official PDF;
    # there is no hidden exact leg time for the parser to recover.
    (5204, "er 11"),
    (5204, "ht 95"),
}

# Exact source values whose visible original and all available official
# alternatives have been reviewed.  They remain visible as ``unknown`` (or as
# an unknown relay-leg value), but no longer belong in the open repair queue.
# A new value that merely looks similar is still emitted as a warning.
KNOWN_CONFIRMED_SOURCE_UNREADABLE_VALUES = {
    (2865, "Vereinsmeisterschaft", "Slávka Cahlová", "???"),
    (4837, "Familie", "Leonhardt Tano", "-32:10"),
    (5287, "Kerzen", "Emmanuiele", "-11:25:57"),
    (5287, "Krippe", "Serge", "-11:25:53"),
    (5287, "Krippe", "Michaela", "-11:23:41"),
    (5204, "Mixed Staffel bis 17", "Pia Aspalter", "er 11"),
    (5204, "Mixed Staffel ab 18", "Lisa Habenicht", "ht 95"),
}

# Confirmed contradictions between a published category header and the number
# of visible competitor units below it.  Keep this list exact: a newly parsed
# extra row must block publication until somebody has compared it with the
# original source instead of inheriting a broad "source defect" exemption.
KNOWN_SOURCE_COUNT_ANOMALIES = {
    (853, "Premium", 54, 55),
    (1167, "Offen 19-", 14, 15),
    (1249, "Damen 65-", 2, 3),
    (1367, "Herren B", 29, 30),
    (3134, "DB-Kurz", 4, 5),
    (3713, "C", 10, 11),
}

# A compact result attachment may omit DNS names which another official
# attachment for the same event supplies.  These exact cases remain guarded
# by an aggregate row-count check below: if the supplemental parser stops
# recovering the rows, they automatically return to the unresolved warning
# queue instead of inheriting a permanent exemption.
KNOWN_RECOVERED_SOURCE_OMISSIONS = {
    (633, "Damen E", 5, 4),
    (856, "Herren E", 9, 8),
    (1909, "Herren Elite", 15, 13),
    (1909, "Herren/Damen -14", 7, 6),
    (1909, "Herren 60", 16, 13),
    (1909, "Herren 70", 4, 3),
    (4995, "Damen A", 11, 10),
    (4995, "Damen B", 17, 16),
    (4995, "Herren A", 32, 31),
    (4995, "Herren B", 35, 33),
    (4995, "Herren D", 9, 8),
}

# The complete result source and every official alternative exposed by ANNE
# were compared for these exact class/count combinations.  The header number
# has no corresponding named result rows to recover.  Keep the limitation in
# the audit trail as information, but do not repeatedly present it as open
# parser work. Any changed count falls out of this exact allowlist and becomes
# a warning again.
KNOWN_CONFIRMED_SOURCE_OMISSIONS = {
    (853, "Ultimate", 63, 62),
    (1114, "D1", 8, 5),
    (1114, "D2", 10, 7),
    (1114, "D3", 10, 7),
    (1114, "H1", 14, 11),
    (1114, "H2", 33, 28),
    (1114, "H3", 5, 3),
    (1677, "Herren 21-E", 15, 14),
    (1967, "Damen 2", 16, 15),
    (1967, "Herren 3", 11, 10),
    (1967, "Schnupperer", 20, 19),
    (3366, "Damen 21 Elite", 20, 18),
    (3366, "Herren 21 Elite", 36, 34),
    (4254, "H-12–Finale", 7, 6),
    (4254, "H21-E–B-Finale", 38, 34),
    (4254, "Offen", 25, 23),
    (4364, "H 35-", 6, 5),
    (4364, "H 65-", 4, 3),
}

# Named rows whose official result cell is visibly empty. No sporting status
# can safely be inferred from an empty cell. These reviewed cases remain in
# the audit trail as information; future blank values return to the
# parser-repair queue.
KNOWN_SOURCE_MISSING_VALUES = {
    (1672, "Damen 10", "Veitsberger Miriam"),
    (1672, "Herren 10", "Ehrlich Lilly"),
    (1947, "NO H45", "Schuller Georg"),
    (2020, "Family", "Annika Springer"),
    (2375, "Family", "Böhm Niklas"),
}

# Confirmed defects in the published source itself, not parser output. The
# PDFs visibly print the same inverted numeric ranks which the parser stores.
KNOWN_SOURCE_RANK_ANOMALIES = {
    (1734, "B"),
    (1941, "Bahn B"),
    (2839, "HDS"),
}

# ANNE's migrated payload for this event supplies the literal category
# ``empty`` for every result and publishes no attachment from which the real
# classes could be recovered. Keep the rows visible, but report a source
# limitation instead of sending it to the parser-repair queue.
KNOWN_ANNE_CATEGORY_OMISSIONS = {3438}


def source_value_is_unreadable(event_id, value):
    value = (value or "").strip()
    return bool(re.fullmatch(r"\?+", value)
                or re.fullmatch(r"-\d{1,3}:\d{2}(?::\d{2})?", value)
                or
                (event_id, value) in KNOWN_SOURCE_VALUE_CORRUPTIONS)


def source_value_is_confirmed_unreadable(event_id, category, name, value):
    return (
        event_id,
        (category or "").strip(),
        (name or "").strip(),
        (value or "").strip(),
    ) in KNOWN_CONFIRMED_SOURCE_UNREADABLE_VALUES


def populate_quality_model(cur):
    """Compute review units, deterministic findings and current assertions."""
    lists = cur.execute(
        """SELECT rl.id, rl.stage_id, rl.category, rl.category_full,
                  rl.declared_starters, rl.input_fingerprint,
                  rl.parsed_entries, rl.parsed_rows, sd.source_type, rl.ranking_basis,
                  s.event_id
             FROM result_list rl
             JOIN source_document sd ON sd.id = rl.source_document_id
             JOIN stage s ON s.id = rl.stage_id"""
    ).fetchall()
    # Keep the result rows which survived cross-source deduplication for
    # row-level audits.  Entry completeness, however, must use the counts
    # captured directly from each normalized source in register_result_list:
    # a second official document can overlap the first one and consequently
    # persist only a subset (or no rows at all) without being misparsed.
    list_rows = {}
    for (list_id, _stage_id, _category, _category_full, _declared, _fingerprint,
         _source_entries, _source_rows, _source_type, _ranking_basis,
         _event_id) in lists:
        rows = cur.execute(
            """SELECT id, observed_name, result_kind, rank, status, time_s, club, note,
                      team_number, team_name
               FROM result WHERE result_list_id = ? ORDER BY id""", (list_id,)).fetchall()
        persisted_entries = len({competitor_unit_key(row) for row in rows})
        list_rows[list_id] = (rows, persisted_entries)

    for (list_id, stage_id, category, category_full, declared, fingerprint,
         source_entries, source_rows, source_type, ranking_basis, event_id) in lists:
        rows, persisted_entries = list_rows[list_id]
        family_state = classify_family_category(category, category_full, event_id)
        if family_state == "ambiguous":
            add_audit_issue(
                cur, list_id, "ambiguous_family_category", "warning",
                f"Kurzklasse {category!r}: Family-Kategorie oder reguläre Klasse bestätigen.")
        if (persisted_entries > 0
                and (category or "").strip().casefold() in ("", "empty", "unknown")):
            missing_category_code = (
                "source_category_missing" if event_id in KNOWN_ANNE_CATEGORY_OMISSIONS
                else "anne_missing_category")
            add_audit_issue(
                cur, list_id, missing_category_code, "warning",
                "Die Quelle enthält Ergebniszeilen, aber keine verwertbare "
                "Klassenbezeichnung; eine Kategoriezuordnung ist nicht ableitbar.")
        if declared is not None and declared != source_entries:
            unexplained_extra = cur.execute(
                """SELECT COUNT(*) FROM result
                   WHERE result_list_id = ? AND rank IS NULL AND status = 'ok'
                     AND out_of_competition = 0
                     AND result_kind IN ('individual', 'family')""",
                (list_id,)).fetchone()[0]
            if source_entries < declared:
                # SportSoftware's number in parentheses is often the number
                # of registrations, while its result section omits entrants
                # who never started and have no DNS row.  That source-level
                # omission remains important context, but it is not evidence
                # that a visible result row failed to parse.  Parser failures
                # stay blocking through the row/value/ranking checks below.
                recovery_key = (
                    event_id, category, declared, source_entries)
                recovered_total = cur.execute(
                    """SELECT COUNT(*) FROM result
                       WHERE stage_id = ? AND category = ?
                         AND result_kind = 'individual'""",
                    (stage_id, category)).fetchone()[0]
                if (recovery_key in KNOWN_RECOVERED_SOURCE_OMISSIONS
                        and recovered_total >= declared):
                    add_audit_issue(
                        cur, list_id, "source_omission_recovered", "info",
                        f"Die kompakte Quelle enthält {source_entries} von "
                        f"{declared} Einträgen; eine ergänzende offizielle "
                        "ANNE-Quelle liefert die fehlenden benannten Zeilen.")
                elif recovery_key in KNOWN_CONFIRMED_SOURCE_OMISSIONS:
                    add_audit_issue(
                        cur, list_id, "source_declared_omission_confirmed", "info",
                        f"Klassenkopf nennt {declared} Meldungen, der vollständige "
                        f"Ergebnisbereich und die verfügbaren offiziellen "
                        f"Alternativquellen enthalten {source_entries} benannte "
                        "Einträge. Für die Differenz sind keine Namen "
                        "veröffentlicht; es werden keine DNS-Personen erfunden.")
                else:
                    difference = declared - source_entries
                    add_audit_issue(
                        cur, list_id, "source_declared_omission", "warning",
                        f"Klassenkopf nennt {declared} Meldungen, der Ergebnisbereich "
                        f"enthält {source_entries} sichtbare Einträge; {difference} "
                        f"gemeldete {'Person wird' if difference == 1 else 'Personen werden'} "
                        "in dieser Quelle nicht als Ergebniszeile angeführt.")
            elif (source_entries > declared and unexplained_extra == 0
                  and (event_id, category, declared, source_entries)
                  in KNOWN_SOURCE_COUNT_ANOMALIES):
                # The source itself can print more fully classified rows than
                # its category header claims (confirmed examples: ``DB-Kurz
                # (4)`` followed by ranks 1..5, and relay headers omitting an
                # MP/DNF team). Exact, visually reviewed contradictions remain
                # in the audit trail as information; a new occurrence remains
                # a blocker below.
                add_audit_issue(
                    cur, list_id, "source_count_anomaly", "info",
                    f"Klassenkopf nennt {declared} Starts, die Quelle enthält aber "
                    f"{source_entries} vollständig klassifizierte Ergebnis-Einträge; "
                    "der veröffentlichte Quellwiderspruch wurde geprüft.")
            else:
                add_audit_issue(
                    cur, list_id, "entry_count_mismatch", "blocker",
                    f"Quelle nennt {declared} Starts, erfasst sind {source_entries} "
                    "Ergebnis-Einträge. Originalquelle auf nicht angeführte DNS/DNF "
                    "oder eine Parserlücke prüfen.")

        timed_rows, ranked_rows = cur.execute(
            """SELECT SUM(time_s IS NOT NULL), SUM(rank IS NOT NULL)
               FROM result WHERE result_list_id = ?
                 AND out_of_competition = 0 AND result_kind = 'individual'""",
            (list_id,)).fetchone()
        # A full timed result list with no placement at all is a parser/data
        # quality problem, not a reviewer decision.  It used to be invisible
        # because the ranking audit only looked for a *partial* rank set.
        # Keep family/team semantics out of this signal: their ranking is
        # represented by their competitor unit and is handled elsewhere.
        ranking_not_applicable = (
            ranking_basis == "other"
            or bool(re.search(
                r"(?i)\b(?:annulliert|annulled|cancelled|canceled)\b",
                category_full or "",
            ))
        )
        course_only_list = bool(re.match(
            r"(?i)^(?:bahn|course)\s+\d+\b", category.strip()))
        if (source_type != "anne-api" and (timed_rows or 0) >= 3
                and (ranked_rows or 0) == 0 and family_state == "ordinary"
                and not ranking_not_applicable and not course_only_list):
            add_audit_issue(
                cur, list_id, "missing_ranking", "blocker",
                "Quelle enthält Zeiten, aber keine einzige Platzierung wurde gelesen.")

        minute_precision_rows = cur.execute(
            """SELECT COUNT(*) FROM result
               WHERE result_list_id = ?
                 AND COALESCE(note, '') LIKE
                     '%ANNE-Altimport: Zeit nur minutengenau%'""",
            (list_id,)).fetchone()[0]
        if minute_precision_rows:
            add_audit_issue(
                cur, list_id, "anne_minute_precision", "warning",
                "ANNE-Altimport enthält nur minutengenaue Zeiten und keine "
                "Quellränge; Zeitwerte sind als ungefähre volle Minuten dargestellt.")

        unknown = cur.execute(
            """SELECT id, observed_name, observed_status, observed_time FROM result
               WHERE result_list_id = ? AND status = 'unknown'""",
            (list_id,)).fetchall()
        for result_id, observed_name, observed_status, observed_time in unknown:
            if source_value_is_unreadable(event_id, observed_time):
                severity = (
                    "info"
                    if source_value_is_confirmed_unreadable(
                        event_id, category, observed_name, observed_time)
                    else "warning"
                )
                add_audit_issue(
                    cur, list_id, "source_value_unreadable", severity,
                    f"Die Quelle zeigt für diesen Ergebniswert nur "
                    f"{(observed_time or '').strip()!r}; ein genauer Status "
                    "oder Zeitwert ist nicht rekonstruierbar"
                    f"{'; der Quellenfehler wurde geprüft' if severity == 'info' else ''}.",
                    result_id)
                continue
            # This is a parser failure, not an ambiguous sporting status: a
            # source time such as 114:08 exists but never became seconds.
            # Surface it separately so the review queue points at the parser
            # signature rather than asking a reviewer to interpret "unknown".
            if OBSERVED_TIME_RE.fullmatch((observed_time or "").strip()):
                add_audit_issue(
                    cur, list_id, "time_text_unparsed", "blocker",
                    f"Zeit {observed_time!r} ist in der Quelle vorhanden, wurde aber nicht als Zeit gelesen.",
                    result_id, auto_resolvable=True)
                continue
            if ((event_id, category, observed_name)
                    in KNOWN_SOURCE_MISSING_VALUES):
                # The parser retained the named source row correctly, but the
                # source itself leaves its result cell blank or uses a bare
                # dash. There is no defensible automatic DNS/MP inference.
                add_audit_issue(
                    cur, list_id, "source_value_missing", "info",
                    "Die Quelle führt den Eintrag an, lässt Rang, Zeit und Status "
                    "aber leer; die leere Originalzelle wurde geprüft.",
                    result_id)
                continue
            if ((observed_status or "").strip().casefold() in ("", "unknown")
                    and not (observed_time or "").strip()):
                add_audit_issue(
                    cur, list_id, "unknown_status", "blocker",
                    "Rang, Zeit und Status sind leer; Originalquelle auf "
                    "eine Parserlücke oder einen tatsächlich leeren Quellwert prüfen.",
                    result_id)
                continue
            add_audit_issue(
                cur, list_id, "unknown_status", "blocker",
                f"Status {observed_status or '(leer)'} ist nicht eindeutig normalisiert.",
                result_id)

        # A parser can occasionally mark a row as OK even though its source
        # result cell is neither a time nor a known qualitative result. This
        # was previously invisible (for example broken PDF glyphs such as
        # ``er 11``). Keep it in the review queue rather than silently showing
        # a ranked result with an empty time.
        if source_type != "anne-api" and ranking_basis == "time":
            for result_id, observed_name, observed_time in cur.execute(
                    """SELECT id, observed_name, observed_time FROM result
                       WHERE result_list_id = ? AND status = 'ok'
                         AND COALESCE(individual_status, 'ok') = 'ok'
                         AND time_s IS NULL
                         AND TRIM(COALESCE(observed_time, '')) != ''""",
                    (list_id,)).fetchall():
                value = (observed_time or "").strip()
                if source_value_is_unreadable(event_id, value):
                    severity = (
                        "info"
                        if source_value_is_confirmed_unreadable(
                            event_id, category, observed_name, value)
                        else "warning"
                    )
                    add_audit_issue(
                        cur, list_id, "source_value_unreadable", severity,
                        f"Die Quelle zeigt für diesen Ergebniswert nur "
                        f"{value!r}; ein genauer Leg-Zeitwert ist nicht "
                        "rekonstruierbar"
                        f"{'; der Quellenfehler wurde geprüft' if severity == 'info' else ''}.",
                        result_id)
                    continue
                if (OBSERVED_TIME_RE.fullmatch(value)
                        or re.search(
                            r"(?i)(?:(?:sehr\s+)?gut|super\s+gelaufen!?|ok|teilg\.?|"
                            r"(?:erfolgreich\s+)?teilgenommen)\s*$", value)):
                    continue
                add_audit_issue(
                    cur, list_id, "result_value_unparsed", "blocker",
                    f"Ergebniswert {value!r} wurde weder als Zeit noch als Status gelesen.",
                    result_id, auto_resolvable=True)

        # A mixed category with some ranked finishers and many timed but
        # unranked ordinary entries is rarely intentional.  It is not a hard
        # blocker (AK/foreign classifications do exist), but it is exactly
        # the kind of layout shift a reviewer should see before confirming a
        # category. Relay/team members are excluded because their shared team
        # rank is deliberately stored only on the team unit.
        ranked_count, timed_count = cur.execute(
            """SELECT
                   SUM(rank IS NOT NULL),
                   SUM(status = 'ok' AND time_s IS NOT NULL)
               FROM result
               WHERE result_list_id = ? AND out_of_competition = 0
                 AND result_kind = 'individual'""", (list_id,)).fetchone()
        ranked_count, timed_count = ranked_count or 0, timed_count or 0
        if (source_type != "anne-api"
                and timed_count >= 3 and ranked_count > 0 and ranked_count < timed_count
                and not course_only_list and not ranking_not_applicable):
            add_audit_issue(
                cur, list_id, "partial_ranking_coverage", "warning",
                f"Nur {ranked_count} von {timed_count} klassifizierten Einträgen haben einen Rang; Ranking in der Quelle prüfen.")

        provisional = cur.execute(
            """SELECT id, observed_name FROM result
               WHERE result_list_id = ? AND person_id IS NOT NULL
                 AND identity_state NOT IN ('resolved', 'not_applicable')
                 AND championship IS NOT NULL""", (list_id,)).fetchall()
        for result_id, observed_name in provisional:
            add_audit_issue(
                cur, list_id, "provisional_championship_identity", "warning",
                f"Meisterschaftsidentität von {observed_name} ist noch nicht aufgelöst.",
                result_id)

        ranked = cur.execute(
            """SELECT rank, MIN(time_s) FROM result
               WHERE result_list_id = ? AND status = 'ok' AND rank IS NOT NULL
                 AND time_s IS NOT NULL AND out_of_competition = 0
                 AND result_kind NOT IN ('relay', 'team', 'family')
               GROUP BY rank ORDER BY rank""", (list_id,)).fetchall()
        # Score/points races are intentionally ranked by points, often with a
        # time-limit penalty.  A faster elapsed time can therefore have a
        # worse rank and is not evidence of a parser inversion.
        # ANNE's rank is source-native authoritative data rather than a
        # parser interpretation.  Special series and team scoring can rank
        # by rules not exposed in the result payload, so a time inversion is
        # not an actionable parser finding there.
        best_so_far = (None if ranking_basis == "time" and source_type != "anne-api"
                       else False)
        for rank, time_s in ranked:
            if best_so_far is False:
                break
            if best_so_far is not None and time_s < best_so_far:
                issue_code = ("source_rank_anomaly"
                              if (event_id, category) in KNOWN_SOURCE_RANK_ANOMALIES
                              else "rank_time_inversion")
                severity = (
                    "info" if issue_code == "source_rank_anomaly" else "warning")
                add_audit_issue(
                    cur, list_id, issue_code, severity,
                    f"Rang {rank} ist schneller als ein besser gereihter Eintrag"
                    f"{'; der veröffentlichte Quellrang wurde geprüft' if severity == 'info' else ''}.")
                break
            best_so_far = time_s if best_so_far is None else max(best_so_far, time_s)

    if not REVIEW_DECISIONS_PATH.exists():
        return
    payload = json.loads(REVIEW_DECISIONS_PATH.read_text())
    assertions = payload.get("assertions", []) if isinstance(payload, dict) else payload
    fingerprints = {
        list_id: fingerprint
        for (list_id, _stage_id, _cat, _category_full, _declared, fingerprint,
             _source_entries, _source_rows, _source_type, _ranking_basis,
             _event_id) in lists
    }
    for assertion in assertions:
        if not isinstance(assertion, dict):
            continue
        scope_type = assertion.get("scope_type", "result_list")
        scope_key = assertion.get("scope_key")
        expected = fingerprints.get(scope_key) if scope_type == "result_list" else None
        supplied = assertion.get("input_fingerprint")
        if scope_type == "result_list" and expected != supplied:
            if (scope_key in fingerprints and stale_verification_requires_review(
                    cur, scope_key, assertion.get("state"))):
                add_audit_issue(
                    cur, scope_key, "stale_verification", "blocker",
                    "Die Quelle oder Parserlogik hat sich seit der Bestätigung geändert.")
            continue
        cur.execute(
            """INSERT OR REPLACE INTO verification_assertion
               (scope_type, scope_key, dimension, state, input_fingerprint,
                reviewer, reviewed_at, note) VALUES (?,?,?,?,?,?,?,?)""",
            (scope_type, scope_key, assertion.get("dimension"), assertion.get("state"),
             supplied or "", assertion.get("reviewer"), assertion.get("reviewed_at"),
             assertion.get("note")))


REGIONAL_JURISDICTIONS = {
    "WIEN": ("Wien", "Wiener MS"),
    "NOE": ("Niederösterreich", "NÖ MS"),
    "BGLD": ("Burgenland", "Bgld. MS"),
    "STMK": ("Steiermark", "Stmk. MS"),
    "OOE": ("Oberösterreich", "OÖ MS"),
    "SBG": ("Salzburg", "Sbg. MS"),
    "TIR": ("Tirol", "Tiroler MS"),
    "KTN": ("Kärnten", "Kärntner MS"),
    "VBG": ("Vorarlberg", "Vbg. MS"),
}

REGIONAL_LONG_PATTERNS = {
    "WIEN": r"\b(?:wien(?:er|erinnen)?|wr\.?)\b",
    "NOE": r"\b(?:n(?:ö|oe)\.?|niederösterreich(?:isch(?:e[rsn]?)?)?)\b",
    "BGLD": r"\b(?:bgld\.?|burgenländ(?:isch(?:e[rsn]?)?)?|burgenland)\b",
    "STMK": r"\b(?:stmk\.?|steir(?:isch(?:e[rsn]?)?)?|steiermark)\b",
    "OOE": r"\b(?:o(?:ö|oe)\.?|oberösterreich(?:isch(?:e[rsn]?)?)?)\b",
    "SBG": r"\b(?:sbg\.?|salzburg(?:er|isch(?:e[rsn]?)?)?)\b",
    "TIR": r"\b(?:tirol(?:er|erisch(?:e[rsn]?)?)?)\b",
    "KTN": r"\b(?:ktn\.?|kärnt(?:en|ner|nerisch(?:e[rsn]?)?))\b",
    "VBG": r"\b(?:vbg\.?|vorarlberg(?:er|isch(?:e[rsn]?)?)?)\b",
}

REGIONAL_FRAME_RE = re.compile(
    r"(?:rahmen(?:bewerb)?|gäste?|offen|neu(?:ling(?:e)?)?|fam(?:il(?:ie|ien|y)|iliy)?|kids?|kinderfähnchen|bahn|lyceum)",
    re.I)
REGIONAL_CHAMPIONSHIP_RE = re.compile(
    r"(?:\b(?:lm|lms|ms|km)\b|meisterschaft|landesmeister|landes[- ]?ms)", re.I)
REGIONAL_FOREIGN_CATEGORY_RE = re.compile(
    r"(?:^|[-_/()\s])(?:SLO|SVN|CZE|CZ|SVK|HUN|GER|DEU|ITA|FIN|POL|"
    r"CRO|HRV|SUI|CHE)(?:$|[-_/()\s])", re.I)
REGIONAL_NON_STATE_CATEGORY_RE = re.compile(
    # International M/W gender classes in joint foreign events. A real
    # compact state prefix is followed by an explicit Austrian D/H class.
    r"^\s*[MW]\s*-?\s*\d|"
    # Common novice/open categories from ANNE's AT-prefixed Ski-O exports.
    r"^\s*N\s*$|^\s*AT-(?:N|F|OFF(?:-L)?)\s*$", re.I)

REGIONAL_COMPACT_CODES = {
    "W": "WIEN", "N": "NOE", "B": "BGLD", "ST": "STMK",
    "O": "OOE", "S": "SBG", "T": "TIR", "K": "KTN", "V": "VBG",
}


def extract_regional_jurisdictions(text, compact=False):
    """Return unambiguous state associations named in source text.

    Single-letter codes are accepted only for category fragments in a known
    regional context.  That prevents an ordinary ``D``/``H`` category or the
    city in ``Wiener Neustadt`` from becoming a championship assignment.
    """
    value = text or ""
    safe = re.sub(r"Wiener\s+Neust(?:adt|ädter)", "", value, flags=re.I)
    # National title abbreviations are not compact state codes: the O/ST in
    # ``Ö(ST)M`` means Österreichische (Staats-)Meisterschaft, not OÖ/Stmk.
    safe = re.sub(
        r"\b(?:Ö|OE)\s*\(\s*ST\s*\)\s*M\b|"
        r"\b(?:Ö|OE)\s*ST\s*M\b|\b(?:Ö|OE)\s*M\b",
        "", safe, flags=re.I)
    # The discipline suffix in Ski-O/MTB-O is just "orienteering". Without
    # this guard its detached O would look exactly like the historical OÖ
    # shorthand. State names in a separate comparison-match clause likewise
    # do not define the championship advertised elsewhere in the title.
    safe = re.sub(r"\b(?:Ski|MTB)\s*[- ]\s*O\b", "", safe, flags=re.I)
    safe = re.sub(r"\bLänder(?:vergleich|kampf)\b[^,/;]*", "", safe, flags=re.I)
    # Historical PDFs often print state abbreviations with an inner dot.
    # Collapse that dot before the compact-token pass so N.Ö. cannot become
    # the two unrelated codes N and O.
    safe = re.sub(r"\bN\s*\.\s*Ö\b", "NÖ", safe, flags=re.I)
    safe = re.sub(r"\bO\s*\.\s*Ö\b", "OÖ", safe, flags=re.I)
    found = {code for code, pattern in REGIONAL_LONG_PATTERNS.items()
             if re.search(pattern, safe, re.I)}
    if compact:
        folded = unicodedata.normalize("NFKD", safe)
        folded = "".join(ch for ch in folded if not unicodedata.combining(ch)).upper()
        tokens = {token.upper() for token in re.findall(
            r"(?<![A-Za-z])(?:W|N|ST|O|S|T|K|V)(?![A-Za-z])|(?<![A-Za-z0-9])B(?![A-Za-z])",
            folded)}
        found.update(REGIONAL_COMPACT_CODES[token] for token in tokens)
    return found


def extract_regional_category_compact_states(segment):
    """Return only compact codes that occur in a state-like category slot.

    A generic token scan is too broad for result classes: ``W -14`` is the
    international Women category, a lone ``N`` is commonly Neulinge, and
    ``AT-N`` is an Austrian open/novice class. Real state codes occur before
    an explicit D/H category, after a D/H age class, or inside the state-list
    parentheses of a shared course.
    """
    value = segment or ""
    folded = unicodedata.normalize("NFKD", value)
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch)).upper()
    code_pattern = r"(?:ST|W|N|B|O|S|T|K|V)"
    tokens = []
    # ``N D-12`` / ``W H45``: state prefix followed by an actual gender.
    prefix = re.match(
        rf"^\s*({code_pattern})\s+(?=(?:D(?:AMEN)?|H(?:ERREN)?)(?:\b|[-\d]))",
        folded)
    if prefix:
        tokens.append(prefix.group(1))
    # ``D45-W``, ``H19 N`` and compact concatenations such as ``D19ST``.
    suffix = re.search(
        rf"(?:D(?:AMEN)?|H(?:ERREN)?)\s*-?\s*\d[^/]*?[-, ]\s*({code_pattern})\s*$",
        folded)
    if suffix:
        tokens.append(suffix.group(1))
    concatenated = re.search(
        rf"(?:D(?:AMEN)?|H(?:ERREN)?)\s*-?\s*\d+\s*({code_pattern})\s*$",
        folded)
    if concatenated:
        tokens.append(concatenated.group(1))
    # Shared courses put their state list in parentheses: H35(W,NÖ).
    for inner in re.findall(r"\(([^)]*)\)", folded):
        tokens.extend(re.findall(
            rf"(?<![A-Z0-9])({code_pattern})(?![A-Z])", inner))
    return {REGIONAL_COMPACT_CODES[token] for token in tokens}


def is_vienna_championship_candidate(title, stage_title=None):
    """Backward-compatible title predicate used by older callers/tests."""
    text = f"{title or ''} {stage_title or ''}"
    if re.search(r"Wiener\s+Neust(?:adt|ädter)|Schul|Vereinsmeister", text, re.I):
        return False
    return ("WIEN" in extract_regional_jurisdictions(text)
            and bool(REGIONAL_CHAMPIONSHIP_RE.search(text)))


def _regional_segments(category):
    """Split only where a slash starts a new gender/age category.

    ``D19-Bgld/NÖ`` is one shared class, while
    ``D40-(St,B)/D45-(NÖ,W)`` contains two canonical age classes.
    """
    return [part.strip() for part in re.split(
        r"/(?=\s*(?:D(?:amen)?|H(?:erren)?)\s*-?\s*\d)", category or "") if part.strip()]


def _canonical_regional_category(segment, explicit_states=None,
                                 compact_states=None):
    value = segment or ""
    explicit_states = set(explicit_states or ())
    compact_states = set(compact_states or ())

    # Parentheses in joint championships normally contain only jurisdiction
    # codes. Remove them only when every code was actually accepted in the
    # event context; otherwise a class marker such as ``(B)`` must survive.
    def strip_state_parenthesis(match):
        inner = match.group(1)
        states = extract_regional_jurisdictions(inner, compact=True)
        return "" if states and states.issubset(explicit_states) else match.group(0)

    value = re.sub(r"\(([^)]*)\)", strip_state_parenthesis, value)
    for state in explicit_states:
        pattern = REGIONAL_LONG_PATTERNS.get(state)
        if pattern:
            value = re.sub(pattern, "", value, flags=re.I)

    accepted_codes = [code for code, state in REGIONAL_COMPACT_CODES.items()
                      if state in compact_states]
    if accepted_codes:
        code_pattern = "|".join(sorted(accepted_codes, key=len, reverse=True))
        value = re.sub(
            rf"^\s*(?:{code_pattern})\s+(?=(?:D(?:amen)?|H(?:erren)?)(?:\b|[-\d]))",
            "", value, flags=re.I)
        value = re.sub(
            rf"(?<![A-Za-z0-9])(?:{code_pattern})(?![A-Za-z])",
            "", value, flags=re.I)
        value = re.sub(
            rf"(?<=\d)\s*-?\s*(?:{code_pattern})\s*$", "", value, flags=re.I)
    value = re.sub(r"\s*[,/]+\s*$", "", value)
    value = re.sub(r"\s+", " ", value).strip(" -/,()")
    value = re.sub(r"^(?:Damen\s*\+?&?\s*Herren|Herren\s*\+?&?\s*Damen)\b",
                   "DH", value, flags=re.I)
    value = re.sub(r"^Damen(?=[\s\d-])", "D", value, flags=re.I)
    value = re.sub(r"^Herren(?=[\s\d-])", "H", value, flags=re.I)
    value = re.sub(r"^(?:D\s*\+\s*H|H\s*\+\s*D)\b", "DH", value, flags=re.I)
    return value or (segment or "").strip()


def regional_category_key(category):
    key = championship_category_key(category)
    division = re.search(r"\d\s*-?\s*([AB])(?:\s*-|\s*$)", category or "", re.I)
    return f"{key}{division.group(1).lower()}" if division and key else key


def regional_mappings_for_list(category, event_title="", stage_title="", file_name=""):
    """Detect regional championship scopes with source-level provenance.

    A mapping may fan one shared source category out to several jurisdictions;
    the later entry builder partitions the actual competitors by their club's
    state association.  A single-state category/document is authoritative and
    does not need that partition.
    """
    category = (category or "").strip()
    title_context = f"{event_title or ''} {stage_title or ''}"
    full_context = f"{title_context} {file_name or ''}"
    if (not category or REGIONAL_FRAME_RE.search(category)
            or REGIONAL_FOREIGN_CATEGORY_RE.search(category)
            or REGIONAL_NON_STATE_CATEGORY_RE.search(category)
            or re.match(r"^\s*R(?:\s|[-_])", category, re.I)):
        return []
    if re.search(r"Schul|Vereinsmeister", full_context, re.I):
        return []

    # Compact codes are ambiguous in categories (K=Kurz/Kärnten,
    # B=B-Klasse/Burgenland, W=Women/Wien). They are therefore accepted only
    # when the championship title establishes the same state context.
    title_states = extract_regional_jurisdictions(title_context, compact=True)
    if re.search(r"\bLM\s+Nacht\s*\(?\s*Ost\s*\)?", title_context, re.I):
        title_states.update({"WIEN", "NOE", "BGLD", "STMK"})
    document_states = set()
    document_text = file_name or ""
    if re.search(r"wien(?:er)?[-_ ]?wertung|wr[-_ ]?wertung", document_text, re.I):
        document_states.add("WIEN")
    if re.search(r"n(?:ö|oe)[-_ ]?wertung|niederösterreich[-_ ]?wertung", document_text, re.I):
        document_states.add("NOE")
    if re.search(r"bgld[-_ ]?wertung|burgenland[-_ ]?wertung", document_text, re.I):
        document_states.add("BGLD")

    regional_context = (bool(REGIONAL_CHAMPIONSHIP_RE.search(full_context))
                        or bool(document_states))
    if not regional_context:
        return []

    mappings = []
    segments = _regional_segments(category)
    for segment in segments:
        long_explicit = extract_regional_jurisdictions(segment, compact=False)
        compact_explicit = extract_regional_category_compact_states(segment)
        compact_explicit &= (title_states | document_states)
        explicit = long_explicit | compact_explicit
        # R means Rahmen in the compact W/N/R source convention.
        if not explicit and re.search(r"(?<=\d)\s*-?\s*R\s*$", segment, re.I):
            continue
        canonical = _canonical_regional_category(
            segment, explicit_states=explicit, compact_states=compact_explicit)
        category_key = regional_category_key(canonical)
        if not category_key:
            continue
        if document_states and not explicit:
            states, basis, state, confidence = document_states, "document", "confirmed", 1.0
        elif explicit:
            states, basis, state, confidence = explicit, "category", "confirmed", 1.0
        elif title_states:
            states, basis, state, confidence = title_states, "event_title", "candidate", 0.55
        else:
            continue
        # A category label is a scope signal, not sufficient proof that every
        # row in it is medal-eligible: historical exports frequently retain
        # guests in the same printed ranking. Only a dedicated official state
        # ranking document is authoritative for row inclusion on its own.
        partition_required = basis != "document"
        for jurisdiction in sorted(states):
            mappings.append({
                "jurisdiction": jurisdiction,
                "canonical_category": canonical,
                "category_key": category_key,
                "state": state,
                "evidence_kind": basis,
                "evidence_text": (segment if basis == "category" else
                                  document_text if basis == "document" else title_context.strip()),
                "confidence": confidence,
                "partition_required": partition_required,
            })
    # A slash can assign different canonical age classes to different state
    # associations while all rows still live in one physical source list.
    # Even a segment naming only one state (``H40 B`` in
    # ``H40 B/H45 NÖ,W``) must therefore be partitioned by competitor club.
    if len(segments) > 1 or len({m["jurisdiction"] for m in mappings}) > 1:
        for mapping in mappings:
            mapping["partition_required"] = True
    return mappings


def _championship_id(jurisdiction, stage_id, category, championship_type):
    raw = f"{jurisdiction}\0{stage_id}\0{category}\0{championship_type}".encode()
    return "champ:" + hashlib.sha256(raw).hexdigest()[:24]


def _regional_mapping_id(list_id, jurisdiction, category_key):
    raw = f"{list_id}\0{jurisdiction}\0{category_key}".encode()
    return "regional-map:" + hashlib.sha256(raw).hexdigest()[:24]


def _regional_entry_id(instance_id, competitor_key):
    raw = f"{instance_id}\0{competitor_key}".encode()
    return "regional-entry:" + hashlib.sha256(raw).hexdigest()[:24]


def load_club_jurisdictions(cur):
    if not CLUB_JURISDICTIONS_PATH.exists():
        return {}
    payload = json.loads(CLUB_JURISDICTIONS_PATH.read_text())
    result = {}
    for item in payload.get("clubs", []):
        club, jurisdiction = item.get("club"), item.get("jurisdiction")
        if not club or jurisdiction not in REGIONAL_JURISDICTIONS:
            continue
        result[club] = jurisdiction
        cur.execute(
            "INSERT INTO club_jurisdiction VALUES (?,?,?,?,?)",
            (club, jurisdiction, item.get("valid_from"), item.get("valid_to"),
             item.get("evidence", "curated-oefol-club-catalog")))
    return result


def _regional_unit_key(row):
    """Stable competitor key within one physical stage and result source."""
    (rid, person_id, kind, team_number, team_name, club, _official_club,
     rank, _status, time_s, _ooc, *_provenance) = row
    if kind in {"relay", "team", "pair"}:
        if team_number:
            label = f"n:{str(team_number).strip()}:t:{name_key(team_name or club or '')}"
        else:
            label = f"t:{name_key(team_name or club or '')}:r:{rank}:time:{time_s}"
        return f"{kind}:{label}"
    return f"person:{person_id}" if person_id is not None else f"result:{rid}"


REGIONAL_SOURCE_NAT_CODES = {
    "w": "WIEN",
    "nö": "NOE",
    "noe": "NOE",
    "b": "BGLD",
    "st": "STMK",
}


def source_nat_jurisdiction(value):
    """Map an exact Landes-MS value from a source ``Nat`` column.

    The one-letter codes are intentionally not interpreted globally. Callers
    use this evidence only after the event/category has independently been
    detected as a regional championship, so an international Country column
    or an ordinary Women/B-class marker cannot create a state medal entry.
    """
    token = re.sub(r"\s+", "", str(value or "")).casefold().rstrip(".")
    return REGIONAL_SOURCE_NAT_CODES.get(token)


def _regional_source_states(unit_rows):
    return {
        state for row in unit_rows
        if (state := source_nat_jurisdiction(row[11] if len(row) > 11 else None))
    }


def _regional_club_states(unit_rows, club_jurisdictions):
    states = set()
    for row in unit_rows:
        # Prefer the already canonical club, but retry the observed team/club
        # strings here: older relay exports can carry an official club inside
        # a longer team label that the base result deliberately preserves.
        for candidate in (row[6], row[5], row[4]):
            canonical = (candidate if candidate in club_jurisdictions else
                         canonicalize_official_club(candidate, OFFICIAL_CLUBS))
            jurisdiction = club_jurisdictions.get(canonical)
            if jurisdiction:
                states.add(jurisdiction)
                break
    return states


def _regional_membership_states(unit_rows, person_memberships, stage_date):
    """Resolve a unit through event-time ANNE club memberships.

    Team labels such as ``Naturfreunde 1`` intentionally remain team names,
    not guessed official clubs.  When every identified member nevertheless
    has a valid membership in the same state on the race date, that is
    stronger evidence than the generic team label and can safely partition a
    joint Landes-MS.
    """
    states = set()
    for row in unit_rows:
        person_id = row[1]
        if person_id is None:
            continue
        for jurisdiction, valid_from, valid_to in person_memberships.get(person_id, ()):
            if stage_date:
                if valid_from and valid_from > stage_date:
                    continue
                if valid_to and valid_to < stage_date:
                    continue
            states.add(jurisdiction)
    return states


def _regional_unit_has_unresolved_club(unit_rows, club_jurisdictions):
    """Whether a unit carries a plausible club that still needs mapping.

    Clubless starters are known to be ineligible for a state ranking; they
    are not an unresolved historical membership. The same applies once any
    candidate string resolves to a known state. Unknown non-empty Austrian-
    looking labels remain reviewable rather than being guessed.
    """
    if _regional_club_states(unit_rows, club_jurisdictions):
        return False
    candidates = {
        str(candidate).strip()
        for row in unit_rows for candidate in (row[6], row[5], row[4])
        if candidate and str(candidate).strip()
    }
    if not candidates:
        return False
    meaningful = []
    for candidate in candidates:
        if CLUBLESS_CLUB_RE.search(candidate):
            continue
        folded = candidate.casefold()
        if any(keyword in folded for keyword in FOREIGN_CLUB_KEYWORDS):
            continue
        meaningful.append(candidate)
    return bool(meaningful)


def populate_regional_championships(cur, catalog, club_jurisdictions):
    """Build nationwide regional mappings, instances and competitor entries."""
    person_memberships = defaultdict(list)
    for person_id, club, valid_from, valid_to in cur.execute(
            """SELECT person_id, club, valid_from, valid_to
                 FROM person_club_membership""").fetchall():
        canonical = (club if club in club_jurisdictions else
                     canonicalize_official_club(club, OFFICIAL_CLUBS))
        jurisdiction = club_jurisdictions.get(canonical)
        if jurisdiction:
            person_memberships[person_id].append(
                (jurisdiction, valid_from or "", valid_to))

    lists = cur.execute(
        """SELECT rl.id, rl.stage_id, rl.category, rl.input_fingerprint,
                  e.title, s.title, sd.file_name
           FROM result_list rl JOIN stage s ON s.id = rl.stage_id
           JOIN event e ON e.id = s.event_id
           JOIN source_document sd ON sd.id = rl.source_document_id""").fetchall()
    detected = []
    for list_id, stage_id, category, fingerprint, event_title, stage_title, file_name in lists:
        for mapping in regional_mappings_for_list(
                category, event_title, stage_title, file_name):
            # Event title + an exact row-level Nat code is direct source
            # evidence, not a mere title candidate. Promote only the state
            # actually printed in this result list; sibling states with no
            # entrant remain candidates unless category/document evidence
            # independently confirms them.
            source_nat_values = [row[0] for row in cur.execute(
                "SELECT DISTINCT observed_nation FROM result WHERE result_list_id = ?",
                (list_id,)).fetchall()]
            source_nat_states = {
                state for value in source_nat_values
                if (state := source_nat_jurisdiction(value))
            }
            if mapping["jurisdiction"] in source_nat_states:
                mapping = dict(mapping)
                mapping.update({
                    "state": "confirmed",
                    "evidence_kind": "source_nat",
                    "evidence_text": "Nat: " + ", ".join(sorted(
                        str(value) for value in source_nat_values
                        if source_nat_jurisdiction(value) == mapping["jurisdiction"])),
                    "confidence": 1.0,
                    "partition_required": True,
                })
            mapping_id = _regional_mapping_id(
                list_id, mapping["jurisdiction"], mapping["category_key"])
            cur.execute(
                """INSERT OR REPLACE INTO regional_category_mapping
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (mapping_id, list_id, mapping["jurisdiction"], category,
                 mapping["canonical_category"], mapping["category_key"],
                 mapping["state"], mapping["evidence_kind"], mapping["evidence_text"],
                 mapping["confidence"], int(mapping["partition_required"]), fingerprint))
            detected.append((stage_id, mapping, fingerprint))

    # A physical class can have several source documents.  Promote an
    # instance to confirmed as soon as one explicit category/document proves
    # it; a title-only candidate never overrides that stronger evidence.
    grouped = defaultdict(list)
    for stage_id, mapping, fingerprint in detected:
        grouped[(mapping["jurisdiction"], stage_id,
                 mapping["category_key"])].append((mapping, fingerprint))
    for (jurisdiction, stage_id, category_key), observations in grouped.items():
        observations.sort(key=lambda item: (
            item[0]["state"] == "confirmed",
            {"source_nat": 4, "document": 3, "category": 2,
             "event_title": 1}[item[0]["evidence_kind"]]),
            reverse=True)
        best = observations[0][0]
        instance_id = _championship_id(jurisdiction, stage_id, category_key, "LMS")
        digest = hashlib.sha256("\0".join(sorted(
            fingerprint for _mapping, fingerprint in observations)).encode()).hexdigest()
        decision = catalog.get(instance_id, {})
        decision_is_current = decision.get("input_fingerprint") == digest
        state = decision.get("state", best["state"]) if decision_is_current else best["state"]
        if state not in {"candidate", "confirmed", "rejected"}:
            state = best["state"]
        cur.execute(
            """INSERT INTO championship_instance
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (instance_id, jurisdiction, stage_id, best["canonical_category"], category_key,
             "LMS", f"{jurisdiction.lower()}-regional-v1", state,
             best["evidence_kind"], digest))

    mapping_rows = cur.execute(
        """SELECT m.result_list_id, m.jurisdiction, m.category_key, m.state,
                  m.evidence_kind, m.partition_required, m.input_fingerprint,
                  rl.stage_id, ci.id, ci.state, s.date
           FROM regional_category_mapping m
           JOIN result_list rl ON rl.id = m.result_list_id
           JOIN stage s ON s.id = rl.stage_id
           JOIN championship_instance ci
             ON ci.jurisdiction = m.jurisdiction AND ci.stage_id = rl.stage_id
            AND ci.category_key = m.category_key AND ci.championship_type = 'LMS'
           WHERE m.state != 'rejected' AND ci.state != 'rejected'
           ORDER BY (ci.state = 'confirmed') DESC,
                    CASE m.evidence_kind WHEN 'document' THEN 3
                         WHEN 'source_nat' THEN 4
                         WHEN 'category' THEN 2 ELSE 1 END DESC""").fetchall()
    claimed = {}
    claimed_results = {}
    unresolved_mappings = {}
    for (list_id, jurisdiction, category_key, mapping_state, evidence_kind,
         partition_required, fingerprint, stage_id, instance_id, instance_state,
         stage_date) in mapping_rows:
        rows = cur.execute(
            """SELECT id, person_id, result_kind, team_number, team_name, club,
                      official_club, rank, status, time_s, out_of_competition,
                      observed_nation
               FROM result WHERE result_list_id = ? ORDER BY id""", (list_id,)).fetchall()
        units = defaultdict(list)
        for row in rows:
            if row[2] == "family":
                continue
            units[_regional_unit_key(row)].append(row)
        for unit_key, unit_rows in units.items():
            if any(row[10] for row in unit_rows):
                continue
            source_states = _regional_source_states(unit_rows)
            club_states = _regional_club_states(unit_rows, club_jurisdictions)
            membership_states = _regional_membership_states(
                unit_rows, person_memberships, stage_date)
            if partition_required:
                # A printed Nat column in a joint Landes-MS is direct
                # event-time evidence and outranks a current/historical club
                # inference. This is essential when four state rankings share
                # one physical course and one overall placing column.
                if source_states:
                    if source_states != {jurisdiction}:
                        continue
                    eligibility_basis = "explicit-source-nat"
                elif club_states == {jurisdiction}:
                    eligibility_basis = "event-time-club-jurisdiction"
                elif not club_states and membership_states == {jurisdiction}:
                    eligibility_basis = "event-time-person-club-membership"
                else:
                    if (not club_states and not membership_states
                            and _regional_unit_has_unresolved_club(
                                unit_rows, club_jurisdictions)):
                        issue_key = (list_id, mapping_state, jurisdiction, instance_id)
                        unresolved_mappings[issue_key] = (
                            unresolved_mappings.get(issue_key, False)
                            or any(row[8] == "ok" for row in unit_rows))
                    continue
            else:
                eligibility_basis = f"explicit-regional-{evidence_kind}"
            claim_key = (stage_id, unit_key)
            previous = claimed.get(claim_key)
            if previous and previous != jurisdiction:
                # The same person can be present in an overlapping dedicated
                # state-ranking document and in the general result document.
                # Those are duplicate observations, not two performances.
                # Flag only when the exact same source row is claimed twice.
                if any(claimed_results.get(row[0]) not in {None, jurisdiction}
                       for row in unit_rows):
                    add_audit_issue(
                        cur, list_id, "regional_double_assignment", "blocker",
                        f"Eine Quellleistung würde zugleich {previous} und {jurisdiction} zugeordnet.")
                continue
            if previous:
                continue
            claimed[claim_key] = jurisdiction
            claimed_results.update({row[0]: jurisdiction for row in unit_rows})
            entry_id = _regional_entry_id(instance_id, unit_key)
            eligibility_state = "eligible" if instance_state == "confirmed" else "provisional"
            entry_state = "derived" if instance_state == "confirmed" else "provisional"
            cur.execute(
                """INSERT OR IGNORE INTO championship_entry
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (entry_id, instance_id, stage_id, unit_key, None, eligibility_state,
                 eligibility_basis, entry_state, list_id, fingerprint))
            cur.executemany(
                "INSERT OR IGNORE INTO championship_entry_result VALUES (?,?)",
                [(entry_id, row[0]) for row in unit_rows])

    for (list_id, mapping_state, jurisdiction, instance_id), has_ok_result \
            in unresolved_mappings.items():
        assigned = cur.execute(
            "SELECT COUNT(*) FROM championship_entry WHERE championship_instance_id = ?",
            (instance_id,)).fetchone()[0]
        add_audit_issue(
            cur, list_id, "regional_membership_unresolved",
            "warning" if (mapping_state == "confirmed" and not assigned
                           and has_ok_result) else "info",
            (f"{jurisdiction}: " + ("Keine Leistung" if not assigned else "Mindestens eine Leistung")
             + " kann ohne historische Vereinszugehörigkeit nicht sicher getrennt werden."))

    # Regional ranks are ranks among the assigned competitor units, not the
    # shared source rank. Exact source-rank ties retain a shared placement.
    for (instance_id,) in cur.execute(
            "SELECT id FROM championship_instance WHERE championship_type = 'LMS'").fetchall():
        entries = cur.execute(
            """SELECT ce.id, MIN(r.rank), MIN(r.time_s)
               FROM championship_entry ce
               JOIN championship_entry_result cer ON cer.championship_entry_id = ce.id
               JOIN result r ON r.id = cer.result_id
               WHERE ce.championship_instance_id = ? AND r.status = 'ok'
               GROUP BY ce.id ORDER BY MIN(r.rank) IS NULL, MIN(r.rank), MIN(r.time_s), ce.id""",
            (instance_id,)).fetchall()
        position = 0
        previous_order = None
        previous_rank = None
        for entry_id, source_rank, time_s in entries:
            position += 1
            order = (source_rank, time_s)
            regional_rank = previous_rank if previous_order == order else position
            cur.execute("UPDATE championship_entry SET regional_rank = ? WHERE id = ?",
                        (regional_rank, entry_id))
            previous_order, previous_rank = order, regional_rank


def compute_national_ranks(cur):
    """Rank only competitors with positive championship evidence.

    ``unknown`` is deliberately excluded: being ranked in the general race
    is not evidence that a foreign or otherwise unidentified competitor is
    entitled to an Austrian championship medal.  ``provisional`` remains in
    the calculation because it is the explicit, reviewable ÖFOL-club fallback
    for historical rows without stronger person evidence.
    """
    cur.execute("UPDATE result SET national_rank = NULL")
    cur.execute("CREATE TEMP TABLE pair_unit (result_id INTEGER PRIMARY KEY, unit_key TEXT)")
    cur.execute("SELECT r.id, p.name, r.note FROM result r JOIN person p ON p.id = r.person_id "
                "WHERE r.result_kind = 'pair'")
    pair_units = []
    for rid, name, note in cur.fetchall():
        partners = note[len("Partner: "):].split(", ") if note and note.startswith("Partner: ") else []
        key = "|".join(sorted(name_key(n) for n in [name, *partners])) if partners else f"solo-{rid}"
        pair_units.append((rid, key))
    cur.executemany("INSERT INTO pair_unit VALUES (?, ?)", pair_units)

    cur.execute("""
        UPDATE result SET national_rank = (
            SELECT COUNT(CASE WHEN r2.result_kind = 'individual' THEN 1 END)
                 + COUNT(DISTINCT CASE WHEN r2.result_kind = 'pair' THEN pu2.unit_key END)
                 + COUNT(DISTINCT CASE WHEN r2.result_kind IN ('relay', 'team')
                                        THEN COALESCE('n:' || r2.team_number,
                                                      't:' || r2.team_name,
                                                      'c:' || r2.club) END)
                 + 1
            FROM result r2
            LEFT JOIN pair_unit pu2 ON pu2.result_id = r2.id
            WHERE r2.stage_id = result.stage_id AND r2.category = result.category
              AND r2.status = 'ok' AND r2.championship IS NOT NULL
              AND r2.championship_eligibility_state IN ('eligible', 'provisional')
              AND r2.rank IS NOT NULL AND r2.rank < result.rank)
        WHERE championship IS NOT NULL AND status = 'ok' AND rank IS NOT NULL
          AND championship_eligibility_state IN ('eligible', 'provisional')
    """)
    cur.execute("DROP TABLE pair_unit")


def compute_time_behind(cur):
    """Derive a non-negative gap only for classified time rankings.

    Score/series lists can rank by points while still carrying a representative
    elapsed value, and OOC/unranked performances can legitimately be faster
    than the official winner. Neither case has a meaningful ``time behind``.
    Explicit source/API gaps remain untouched.
    """
    cur.execute("""
        UPDATE result AS current
           SET time_behind_s = current.time_s - (
               SELECT MIN(winner.time_s)
                 FROM result winner
                WHERE winner.result_list_id = current.result_list_id
                  AND winner.rank = 1
                  AND winner.status = 'ok'
                  AND winner.out_of_competition = 0
                  AND winner.time_s IS NOT NULL)
         WHERE current.time_behind_s IS NULL
           AND current.time_s IS NOT NULL
           AND current.status = 'ok'
           AND current.rank IS NOT NULL
           AND current.out_of_competition = 0
           AND EXISTS (
               SELECT 1 FROM result_list source_list
                WHERE source_list.id = current.result_list_id
                  AND source_list.ranking_basis = 'time')
           AND current.time_s >= (
               SELECT MIN(winner.time_s)
                 FROM result winner
                WHERE winner.result_list_id = current.result_list_id
                  AND winner.rank = 1
                  AND winner.status = 'ok'
                  AND winner.out_of_competition = 0
                  AND winner.time_s IS NOT NULL)
    """)


def populate_championship_model(cur):
    """Publish national awards and all nine regional championship layers."""
    cur.execute("INSERT INTO championship_jurisdiction VALUES (?,?,?,?)",
                ("AUT", "Österreich", "ÖM/ÖSTM", "national"))
    cur.executemany(
        "INSERT INTO championship_jurisdiction VALUES (?,?,?,?)",
        [(code, name, short_name, "regional")
         for code, (name, short_name) in REGIONAL_JURISDICTIONS.items()])
    cur.execute(
        "INSERT INTO championship_rule_set VALUES (?,?,?,?,?,?)",
        ("aut-national-v1", "AUT", 1, "ÖM/ÖSTM Bestandslogik", "active",
         "Eligibility, Mindeststarter und nationale Rangberechnung des Bestandsmodells."))
    cur.executemany(
        "INSERT INTO championship_rule_set VALUES (?,?,?,?,?,?)",
        [(f"{code.lower()}-regional-v1", code, 1,
          f"{name} Landesmeisterschaft – Quellenmodell", "draft",
          "Explizite Landeswertungen werden übernommen; Titel- und historische Vereinsableitungen bleiben prüfbar.")
         for code, (name, _short_name) in REGIONAL_JURISDICTIONS.items()])

    national = cur.execute(
        """SELECT r.stage_id, r.category, r.championship,
                  GROUP_CONCAT(DISTINCT rl.input_fingerprint)
           FROM result r LEFT JOIN result_list rl ON rl.id = r.result_list_id
           WHERE r.championship IS NOT NULL
           GROUP BY r.stage_id, r.category, r.championship""").fetchall()
    for stage_id, category, champ_type, fingerprints in national:
        category_key = championship_category_key(category)
        instance_id = _championship_id("AUT", stage_id, category_key, champ_type)
        digest = hashlib.sha256((fingerprints or "").encode()).hexdigest()
        cur.execute(
            "INSERT OR IGNORE INTO championship_instance VALUES (?,?,?,?,?,?,?,?,?,?)",
            (instance_id, "AUT", stage_id, category, category_key,
             champ_type, "aut-national-v1",
             "confirmed", "result-championship", digest))
        for result_id, rank, eligibility_state in cur.execute(
                """SELECT id, national_rank, championship_eligibility_state
                   FROM person_result
                   WHERE stage_id = ? AND category = ? AND championship = ?
                     AND national_rank BETWEEN 1 AND 3 AND status = 'ok'
                     AND out_of_competition = 0
                     AND championship_eligibility_state IN ('eligible', 'provisional')""",
                (stage_id, category, champ_type)).fetchall():
            medal = {1: "gold", 2: "silver", 3: "bronze"}[rank]
            award_id = "award:" + hashlib.sha256(
                f"{instance_id}\0{result_id}".encode()).hexdigest()[:24]
            cur.execute("INSERT INTO award VALUES (?,?,?,?,?,?)",
                        (award_id, instance_id, result_id, medal, rank,
                         "derived" if eligibility_state == "eligible" else "provisional"))

    catalog = {item.get("id"): item for item in json.loads(
        CHAMPIONSHIP_CATALOG_PATH.read_text()).get("instances", [])
        if isinstance(item, dict)} if CHAMPIONSHIP_CATALOG_PATH.exists() else {}
    club_jurisdictions = load_club_jurisdictions(cur)
    populate_regional_championships(cur, catalog, club_jurisdictions)


def main():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    DB_PATH.unlink(missing_ok=True)
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.executescript(SCHEMA)

    events = load_events(cur)
    persons = PersonRegistry(load_anne_profile_index())
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
    normalize_team_results(cur)
    normalize_tied_individual_ranks(cur)

    # Load verified club identities before the general duplicate-account pass.
    # This ordering is a correctness boundary: a bad source row must not get
    # the chance to rename and merge two independently verified people first.
    members = load_member_registry()
    member_ledger = load_member_mapping() if members else {
        "aliases": {}, "not_member": [], "internal_member": {},
        "club_override": {}, "split_override": {},
    }
    source_identity_corrections = prepare_verified_member_identities(
        cur, persons, members, member_ledger["aliases"]) if members else []

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
        ledger = member_ledger
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
                """SELECT person_id, SUM(official_club = ?), COUNT(*) FROM result
                   WHERE person_id IS NOT NULL GROUP BY person_id""",
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
            # A current /user profile is already an authoritative person. A
            # stray result name may move that ONE source row above, but must
            # never remap the profile and all of its otherwise-correct history
            # onto a club member. Verified NFW profiles still canonicalise in
            # place through id_meta; other official profiles remain separate.
            if (sid > 0 and sid in persons.anne_profiles.by_id
                    and sid not in id_meta):
                continue
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
                persons.by_id[oid] = (m["name"], m["name_key"], m["yob"], None)
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
                       , identity_state = 'resolved'
                   WHERE person_id = ?""", (old,))
        cur.execute("UPDATE result SET person_id = ? WHERE person_id = ?", (new, old))
        persons.by_id.pop(old, None)

    for pid, (name, key, yob, nat) in persons.by_id.items():
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
        cur.execute("INSERT INTO person VALUES (?,?,?,?,?)",
                    (pid, name, key, yob, nat))

    # split_override: a garbled row that crammed several runners into one name
    # field (relay/family/night-run pairs, e.g. "Anna+Selina Skern") - give each
    # of its results to every real runner it named, then drop the garbled
    # identity. Runs before stats/national_rank so those see the split rows.
    # Confirmed non-championship rows only (night runs / family categories), so
    # no medal impact. Copy each result once per extra runner (while the garbled
    # person still owns them), then hand the originals to the first runner.
    _copy_cols = ("stage_id, result_list_id, category, category_full, club, official_club, rank, "
                  "status, time_s, time_behind_s, out_of_competition, course_length_m, "
                  "course_climb_m, course_controls, result_kind, note, team_number, team_name, "
                  "leg_number, leg_count, individual_status, team_status, team_time_s, "
                  "observed_team_time, source, "
                  "source_document_id, observed_name, observed_club, observed_user_id, "
                  "observed_category, observed_rank, observed_status, observed_time, "
                  "identity_basis, identity_confidence, identity_state, championship, "
                  "championship_eligibility_state, championship_eligibility_basis, "
                  "championship_source_scope")
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
    # ÖFOL ID from the private ANNE registry is authoritative identity
    # evidence. The Naturfreunde Wien roster is an independent second source,
    # not a competing identifier scheme. Internal club IDs and IOF IDs are
    # intentionally excluded from this public model.
    surviving_people = {pid for (pid,) in cur.execute("SELECT id FROM person")}

    # A profile present in the private paginated /user snapshot is stronger
    # evidence than a userId merely observed on one result.  Publish that
    # distinction for every surviving result person found in the registry,
    # including a person introduced through the verified club roster rather
    # than a structured ANNE result.
    for anne_id, profile in sorted(persons.anne_profiles.by_id.items()):
        target = resolve(anne_id)
        if target in surviving_people:
            cur.execute(
                "INSERT OR REPLACE INTO person_identifier VALUES (?,?,?,?,?,?)",
                ("oefol_id", str(anne_id), target, "authoritative",
                 "anne-user-registry", persons.anne_profiles.fetched_at))
            # Keep every duplicate ID as identity provenance, but publish
            # current memberships only from the canonical surviving profile.
            # Otherwise an unverified 1900/1901 placeholder account would make
            # one real person appear as an active member of two clubs.
            if anne_id != target:
                continue
            for membership in profile.get("active_memberships", ()):
                raw_club = re.sub(r"\s+", " ", membership["club"]).strip()
                club = canonicalize_official_club(raw_club, OFFICIAL_CLUBS) or raw_club
                cur.execute(
                    """INSERT OR REPLACE INTO person_club_membership
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (target, club, membership["sport_type"],
                     membership["valid_from"], membership["valid_to"],
                     1 if membership["active"] else 0,
                     "anne-user-registry", persons.anne_profiles.fetched_at))

    # Keep source-supplied positive IDs that are absent from today's /user
    # snapshot as result observations, but do not let them into the official
    # runner/member directory.
    for anne_id in sorted(persons.anne_ids - persons.anne_profiles.by_id.keys()):
        target = resolve(anne_id)
        if target in surviving_people:
            cur.execute(
                "INSERT OR REPLACE INTO person_identifier VALUES (?,?,?,?,?,?)",
                ("oefol_id", str(anne_id), target, "authoritative",
                 "result-observation", None))
    for member in members:
        target = resolve(member["ofol_id"])
        if target in surviving_people:
            cur.execute(
                "INSERT OR REPLACE INTO person_identifier VALUES (?,?,?,?,?,?)",
                ("oefol_id", str(member["ofol_id"]), target,
                 "independently_confirmed", "naturfreunde-wien-book-of-record", None))

    registry_merge_conflicts = registry_identifier_merge_conflicts(
        cur, persons.anne_profiles)
    if registry_merge_conflicts:
        examples = "; ".join(
            f"person {item['person_id']}: IDs {item['identifiers']} "
            f"({item['names']}, years {item['years']})"
            for item in registry_merge_conflicts[:5])
        raise RuntimeError(
            f"{len(registry_merge_conflicts)} incompatible ANNE /user identity merges: "
            + examples)

    for pid, counts in final_names.items():
        if pid not in surviving_people:
            continue
        auth_names = final_auth.get(pid, {})
        for alias, occurrences in counts.items():
            source = "anne-user-registry" if alias in auth_names else "result-observation"
            cur.execute(
                "INSERT OR REPLACE INTO person_alias VALUES (?,?,?,?,?,?)",
                (pid, alias, name_key(alias), source, 0, occurrences))
    for pid, (canonical_name, _yob) in member_canonical.items():
        if pid in surviving_people:
            cur.execute(
                "INSERT OR REPLACE INTO person_alias VALUES (?,?,?,?,?,?)",
                (pid, canonical_name, name_key(canonical_name),
                 "naturfreunde-wien-book-of-record", 1, 1))

    # A merged positive ID may still live in old bookmarks or API links. Keep
    # it as identifier provenance above, and also make it an explicit route
    # redirect to the surviving canonical person.
    for old_id in sorted(merge_map):
        new_id = resolve(old_id)
        if old_id > 0 and old_id != new_id and new_id in surviving_people:
            cur.execute(
                "INSERT OR REPLACE INTO person_redirect VALUES (?, ?)",
                (old_id, new_id))

    if PERSON_REDIRECT_PATH.exists():
        redirects = json.loads(PERSON_REDIRECT_PATH.read_text())
        for old_id, new_id in redirects.items():
            old_id, new_id = int(old_id), int(new_id)
            if new_id not in surviving_people:
                cur.execute(
                    "INSERT OR REPLACE INTO person_tombstone VALUES (?, ?)",
                    (old_id, "retired-non-person-result"))
                continue
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
        UPDATE result SET
          championship = (
              SELECT r2.championship FROM result r2
              WHERE r2.stage_id = result.stage_id AND r2.category = result.category
                AND r2.championship IS NOT NULL LIMIT 1),
          championship_eligibility_state = 'provisional',
          championship_eligibility_basis = 'champion_boundary_inference',
          championship_source_scope = 'winner_only'
        WHERE status = 'ok' AND championship IS NULL
          AND person_id IS NOT NULL
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

    n_championship_source_matches = apply_championship_source_entries(cur)

    n_eligibility = apply_championship_eligibility_overrides(cur)
    apply_championship_eligibility_evidence(cur)

    # OOC/AK is independent of finish status but never participates in a
    # championship ranking or medal award.
    cur.execute(
        "UPDATE result SET championship = NULL, national_rank = NULL "
        "WHERE out_of_competition = 1")

    # Every non-nationality-API exclusion source, collected once into a temp
    # table and reused for (1) stripping championship tags below and (2) the
    # eligible-starter count further down - a DNF/MP/DSQ row never got a
    # championship tag to strip in the first place, so it needs checking
    # against this table directly, not just already-tagged rows.
    cur.execute("CREATE TEMP TABLE ineligible_starter (event_id INTEGER, person_id INTEGER)")

    for eid in FOREIGN_HOST_REQUIRE_OFFICIAL_CLUB_EVENTS:
        cur.execute("""
            INSERT INTO ineligible_starter (event_id, person_id)
            SELECT DISTINCT s.event_id, r.person_id
            FROM result r JOIN stage s ON s.id = r.stage_id
            WHERE s.event_id = ? AND r.person_id IS NOT NULL
              AND r.official_club IS NULL
        """, (eid,))

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
                if not cur.execute(
                        """SELECT 1 FROM result r JOIN stage s ON s.id = r.stage_id
                           WHERE s.event_id = ? AND r.person_id = ?
                             AND r.championship_eligibility_basis =
                                 'official_championship_ranking'
                           LIMIT 1""", (int(eid), int(uid))).fetchone():
                    cur.execute(
                        "INSERT INTO ineligible_starter (event_id, person_id) VALUES (?, ?)",
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
        WHERE r.person_id IS NOT NULL AND ((r.club LIKE 'AA %' AND r.club NOT LIKE 'AA %Kärnten%'
               AND r.club NOT LIKE 'AA %Steiermark%')
           OR r.club GLOB '[0-9][0-9][0-9][0-9] - *')
    """)

    # Broader pass: FOREIGN_CLUB_KEYWORDS (see its own docstring for how it
    # was derived and why it stays deliberately conservative). Matched in
    # Python rather than one giant SQL OR chain, since a ~90-keyword list is
    # far more maintainable as a plain list than as SQL string literals.
    cur.execute("""
        SELECT DISTINCT s.event_id, r.person_id, r.club FROM result r
        JOIN stage s ON s.id = r.stage_id
        WHERE r.person_id IS NOT NULL AND r.club IS NOT NULL AND r.club != ''
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
        WHERE r.person_id IS NOT NULL AND r.club IS NOT NULL AND r.club != ''
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
              AND ((result.result_kind = 'pair' AND r2.rank = result.rank)
                   OR (result.result_kind IN ('relay', 'team')
                       AND r2.result_kind = result.result_kind
                       AND COALESCE('n:' || r2.team_number,
                                    't:' || r2.team_name, 'c:' || r2.club, '') =
                           COALESCE('n:' || result.team_number,
                                    't:' || result.team_name, 'c:' || result.club, '')))
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
            HAVING COUNT(DISTINCT CASE
                       WHEN r.result_kind = 'individual' THEN 'p:' || r.person_id
                       WHEN r.result_kind IN ('relay', 'team') THEN
                            COALESCE('n:' || r.team_number,
                                     't:' || r.team_name, 'c:' || r.club)
                       ELSE 'c:' || r.club END) < 3)
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
    compute_national_ranks(cur)

    # Compute source-faithful time gaps only where a time ranking makes them
    # meaningful. Score lists, OOC rows and unranked rows remain without a gap.
    compute_time_behind(cur)

    populate_championship_model(cur)
    populate_quality_model(cur)

    cur.execute("PRAGMA user_version = 10")
    con.commit()
    for table in ("event", "stage", "person", "person_identifier", "person_club_membership", "person_alias",
                  "person_redirect", "person_tombstone", "source_document", "result_list", "result",
                  "championship_source_entry",
                  "audit_issue", "verification_assertion", "championship_rule_set",
                  "championship_instance", "award"):
        print(table, cur.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    print(f"api results: {n_api}, legacy results: {n_legacy}, "
          f"championship rows from title fallback: {n_title_fallback}, "
          f"official championship source matches: {n_championship_source_matches}, "
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
