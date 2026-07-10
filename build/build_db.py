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
    source TEXT NOT NULL             -- anne-api|sportsoftware-html|...
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
               "result_kind", "note", "source")


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
                          source="anne-api")
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
                      note=" · ".join(note_bits), source="anne-api")
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
                              result_kind=kind, note=note, source=doc["source"])
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
        try:
            rows = json.loads(path.read_text())
        except Exception:
            return False
        if not rows or any(r.get("teamMembers") for r in rows):
            return True  # empty, or a relay/team (handled via teamMembers)
        return any(re.search(r"[A-Za-zÀ-ÿ]{2,}",
                              f"{r.get('firstName') or ''} {r.get('lastName') or ''}")
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
    print(f"api results: {n_api}, legacy results: {n_legacy}")
    cur.execute("VACUUM")
    con.close()
    gz_path = DB_PATH.with_suffix(".db.gz")
    gz_path.write_bytes(gzip.compress(DB_PATH.read_bytes(), 9))
    print(f"wrote {DB_PATH} ({DB_PATH.stat().st_size / 1e6:.1f} MB, "
          f"gz {gz_path.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
