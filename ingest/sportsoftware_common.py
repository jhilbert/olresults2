"""Shared parsing helpers for Stephan Krämer SportSoftware (OE/OE2003/OE12/
OEScore) result exports, used by both the HTML and PDF adapters.
"""
import json
import re
from functools import lru_cache
from pathlib import Path

CAT_RE = re.compile(r"^(?P<name>.+?)\s+\((?P<starters>\d+)\)\s*$")
# same, but for formats (PDF, fixed-width text) where course info trails the
# category on the same line: "H21-Wien (21) 7.8 km 280 Hm 27 P"
CAT_LINE_RE = re.compile(r"^(?P<name>.+?)\s+\((?P<starters>\d+)\)\s*(?P<rest>.*)$")
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
    "n ang": "dns",
    "ohne wertung": "nc", "außer konkurrenz": "nc", "wertungsfrei": "nc",
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


def detect_list_type(file_name, doc_text):
    """Relay lists parse poorly as tables; cumulative multi-day standings
    shouldn't count as a single race. But a relay/team event often also has an
    individual ('Einzel') result file — that's a normal race, even though the
    event title (and thus the document head) mentions 'Staffel'."""
    head = doc_text[:4000]
    if re.search(r"einzel", file_name, re.I):
        return "race"
    if re.search(r"staffel|relay", file_name, re.I) or re.search(r"Staffel", head):
        return "relay"
    if re.search(r"gesamt", file_name, re.I) or "Gesamtwertung" in head:
        return "overall"
    return "race"


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


def expand_pair_result(result):
    """If a parsed result's name holds a '/'-joined pair of clean two-token
    names (run-in-pairs events), return one result per runner sharing the
    club/time/rank, each with a 'Partner: …' note. Otherwise return [result]
    unchanged. For HTML/text sources where name and club are already column-
    separated — the flowing-PDF path has its own club-anchored handling."""
    name = result.get("name", "")
    if "/" not in name:
        return [result]
    names = split_pair_names(name)
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
    r"ohne wertung|dnf|dns|dsq|mp)\s*$")


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
            "timeText": time_text, "statusText": status_text}


def split_pair_names(name_text):
    """'Kasper Matilda / Hoffmann Marlene' -> two names. 'Hnilica Hannes/Sonja'
    (shared surname, second part is a lone forename) -> ['Hnilica Hannes',
    'Hnilica Sonja']. SportSoftware uses Lastname-Firstname order here, so the
    shared surname is the first token."""
    parts = [p.strip() for p in re.split(r"\s*/\s*", name_text) if p.strip()]
    if len(parts) <= 1:
        return parts
    surname = parts[0].split()[0] if parts[0].split() else ""
    out = [parts[0]]
    for p in parts[1:]:
        out.append(f"{surname} {p}" if (len(p.split()) == 1 and surname) else p)
    return out
