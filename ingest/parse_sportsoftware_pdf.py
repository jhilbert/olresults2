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
    classify_championship_text, detect_list_type, find_trailing_club, guess_doc_date,
    is_junk_name, load_clubs, looks_like_person, split_by_kat,
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
    status = "ok" if seconds is not None else (
        parse_status(flow.get("statusText") or "") or "unknown")
    is_pair = len(flow["names"]) > 1
    out = []
    for nm in flow["names"]:
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
        jg = flow.get("jg")
        if jg and jg.isdigit():
            y = int(jg)
            res["yearOfBirth"] = y + (2000 if y <= 26 else 1900) if y < 100 else y
        if is_pair:
            res["resultKind"] = "pair"
            res["note"] = "Partner: " + ", ".join(o for o in flow["names"] if o != nm)
        out.append(res)
    return out

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw" / "anne"
FILES = RAW / "files"
OUT = ROOT / "data" / "normalized"

HEADERS = {"User-Agent": "olresults-sync/0.1 (+https://github.com/josefhilbert/olresults)"}

CONTINUATION_RE = re.compile(r"^\(Forts\.?\)$", re.I)
RANK_LEAK_RE = re.compile(r"^(\d{1,3})\s+(\S.*)$")
# split-times ("Zwischenzeiten") reports: a different, per-control layout that
# puts the club on its own line and interleaves dozens of split times into each
# row. They duplicate the plain results list, so we skip them rather than
# mis-parse them. Detected by the header word or a run of "N(controlcode)" tokens.
SPLITS_RE = re.compile(r"Zwischenzeiten|\d+\(\d+\)\s+\d+\(\d+\)")
# SportSoftware repeats the event title + full date as a running page header on
# every page; it leaks in as a bogus result row ("AC Mitteldistanz"). A real
# result row never carries a full dd.mm.yyyy date, so skip any line that does.
DATE_HEADER_RE = re.compile(r"\d{1,2}\.\d{1,2}\.\d{4}")
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
FLOW_CAT_RE = re.compile(r"^(?P<name>.+?)\s*\((?P<starters>\d+)(?:\s*/\s*\d+)?\)?\s*(?P<rest>.*)$")
FLOW_CAT_PLAIN_RE = re.compile(r"^Kategorie\s+(?P<name>.+)$", re.I)
FLOW_TIME_RE = re.compile(r"^\+?\d{1,3}:\d{2}(?::\d{2})?$")
RANK_PREFIX_RE = re.compile(r"^\d+\.?$")
RELAY_HEADER_RE = re.compile(r"^Pl\s+Stnr\s+Staffel\b", re.M)


def group_lines(words):
    """Cluster words sharing (approximately) the same vertical position."""
    lines = {}
    for w in words:
        key = round(w["top"] / LINE_TOLERANCE)
        lines.setdefault(key, []).append(w)
    for key in sorted(lines):
        yield sorted(lines[key], key=lambda w: w["x0"])


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


def parse_pdf(path, allow_inline_splits=False):
    import pdfplumber

    categories = []
    current = None
    headers = None
    head_text = ""
    pending_rank = pending_championship = None  # from a champion-announcement
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
            has_inline_splits = bool(SPLITS_RE.search(head_text))
            if has_inline_splits and not allow_inline_splits:
                return [], head_text
            for page in pdf.pages:
                words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
                for line in group_lines(words):
                    if not line:
                        continue
                    text = " ".join(w["text"] for w in line)

                    if line[0]["text"] in ("Pl", "Platz") and len(line) >= 3:
                        headers = [(w["text"], w["x0"]) for w in line]
                        if current is None:
                            # some fun-run/app-based races have no age/gender
                            # classes at all: one flat ranking, no "(N)"
                            # category marker ever appears
                            current = {"name": "Ergebnis", "declaredStarters": None,
                                       "results": []}
                            categories.append(current)
                        continue
                    if CONTINUATION_RE.match(text):
                        continue
                    if DATE_HEADER_RE.search(text):
                        continue  # repeated page-header/title line

                    m = CAT_LINE_RE.match(text)
                    if m:
                        name = m.group("name").strip()
                        if current and current["name"] == name:
                            continue  # continuation of the same category
                        current = {"name": name,
                                   "declaredStarters": int(m.group("starters")),
                                   "results": []}
                        current.update(parse_course_info(m.group("rest")))
                        categories.append(current)
                        pending_rank = pending_championship = None
                        continue

                    if current is None or headers is None:
                        continue

                    annot_rank, annot_championship = parse_champion_annotation(text)
                    if annot_rank is not None:
                        pending_rank, pending_championship = annot_rank, annot_championship
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
                    if not rank_text.isdigit():
                        # a narrow, right-aligned rank column can sit closer
                        # to the next header's x0 than its own, leaking the
                        # digit into the name field instead
                        leaked = RANK_LEAK_RE.match(name)
                        if leaked:
                            rank_text, name = leaked.group(1), leaked.group(2)
                    if is_junk_name(name):
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
                    time_text = (rec.get("Zeit") or rec.get("Gesamt") or "").strip()
                    if not rank_text.isdigit() and not time_text:
                        continue

                    result = {
                        "name": name,
                        "club": (rec.get("Verein") or rec.get("Verein/Schule") or "").strip(),
                        "timeText": time_text,
                    }
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
                    if seconds is not None:
                        result["timeS"] = seconds
                        result["status"] = "ok"
                    else:
                        result["status"] = parse_status(time_text) or "unknown"
                    yob = rec.get("Jg", "").strip()
                    if yob.isdigit():
                        y = int(yob)
                        result["yearOfBirth"] = y + (2000 if y <= 26 else 1900) if y < 100 else y
                    if rec.get("Pkt"):
                        result["scoreText"] = rec["Pkt"].strip()
                    current["results"].append(result)

    categories = [c for c in categories if c["results"]]
    categories = split_by_kat(categories)
    for c in categories:
        if c["declaredStarters"] is None:
            c["declaredStarters"] = len(c["results"])
    return categories, head_text


def parse_flow_category_line(text):
    """Recognize a category header in the numbered-list layout. Returns
    (name, declaredStarters_or_None) or None. Guards against a numbered data
    row ('1. Erik Simkovics ... 1 (Posten 60)') being mistaken for one: a
    genuine category never starts with a rank prefix."""
    if RANK_PREFIX_RE.match(text.split(" ", 1)[0]):
        return None
    m = FLOW_CAT_RE.match(text)
    if m and re.search(r"\(\d", text):
        return m.group("name").strip(), int(m.group("starters"))
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
    forced_status = None
    if toks[0] == "AK":  # "außer Konkurrenz" - non-competitive entry
        forced_status = "nc"
        toks = toks[1:]
    if not toks:
        return None
    rank = None
    if RANK_PREFIX_RE.match(toks[0]):
        rank = int(toks[0].rstrip("."))
        toks = toks[1:]
    if not toks:
        return None

    time_idx = next((i for i, t in enumerate(toks) if FLOW_TIME_RE.match(t)), None)
    if time_idx is not None:
        body, time_text, status_text = toks[:time_idx], toks[time_idx].lstrip("+"), None
    else:
        joined = " ".join(toks)
        m = STATUS_TAIL_RE.search(joined)
        if not m:
            return None
        status_text, time_text = m.group(0).strip(), None
        body = joined[: m.start()].split()

    if not body:
        return None
    club, name_toks = find_trailing_club(body, clubs)
    if club is None:
        club, name_toks = (body[-1], body[:-1]) if len(body) > 1 else ("", body)
    name = " ".join(name_toks).strip()
    if is_junk_name(name) or not looks_like_person(name):
        return None

    result = {"name": name, "club": club or "", "timeText": time_text or status_text or ""}
    if rank is not None:
        result["rank"] = rank
    seconds = parse_time(time_text) if time_text else None
    if seconds is not None:
        result["timeS"] = seconds
    result["status"] = forced_status or ("ok" if seconds is not None else (parse_status(status_text or "") or "unknown"))
    return result


def parse_flowing_pdf(path):
    """Fallback for the numbered-list layout (no Pl/Stnr/Verein columns) that
    parse_pdf() can't see at all, since it never finds a "Pl"/"Platz" header
    to anchor on. Works on plain extracted text, not word x-positions -
    there are no columns to align."""
    import pdfplumber

    categories, current = [], None
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                for line in (page.extract_text() or "").split("\n"):
                    line = line.strip()
                    if not line or DATE_HEADER_RE.search(line):
                        continue
                    cat = parse_flow_category_line(line)
                    if cat:
                        name, starters = cat
                        if current and current["name"] == name:
                            continue  # repeated header across pages
                        current = {"name": name, "declaredStarters": starters, "results": []}
                        categories.append(current)
                        continue
                    if current is None:
                        continue
                    row = parse_flow_result_row(line, CLUBS)
                    if row:
                        current["results"].append(row)

    categories = [c for c in categories if c["results"]]
    for c in categories:
        if c["declaredStarters"] is None:
            c["declaredStarters"] = len(c["results"])
    return categories


def parse_relay_pdf(path):
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

    def flush():
        nonlocal pending_team
        if not pending_team or not pending_team["members"]:
            pending_team = None
            return
        names = [m["name"] for m in pending_team["members"]]
        for i, m in enumerate(pending_team["members"]):
            seconds = parse_time_loose(m["timeText"]) if m["timeText"] else None
            status = "ok" if seconds is not None else (parse_status(m["timeText"] or "") or "unknown")
            mates = list(dict.fromkeys(n for n in names if n != m["name"]))
            note_bits = [f"Staffel: {pending_team['name']}", f"Leg {i + 1}/{len(names)}"]
            if mates:
                note_bits.append("Team: " + ", ".join(mates))
            result = {"name": m["name"], "club": pending_team["name"],
                      "timeText": m["timeText"] or "", "resultKind": "relay",
                      "note": " · ".join(note_bits), "status": status}
            if pending_team["rank"] is not None:
                result["rank"] = pending_team["rank"]
            if seconds is not None:
                result["timeS"] = seconds
            current["results"].append(result)
        pending_team = None

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                for line in (page.extract_text() or "").split("\n"):
                    line = line.strip()
                    if not line or CONTINUATION_RE.match(line) or DATE_HEADER_RE.search(line):
                        continue
                    if line in ("Pl Stnr Staffel Zeit", "Name Jg Zeit"):
                        continue
                    m = CAT_LINE_RE.match(line)
                    if m and re.search(r"\(\d", line):
                        flush()
                        name = m.group("name").strip()
                        current = {"name": name, "declaredStarters": int(m.group("starters")),
                                   "results": []}
                        categories.append(current)
                        continue
                    if current is None:
                        continue

                    flow = parse_flow_row(line, {})
                    if not flow or not flow["names"] or not (flow["timeText"] or flow["statusText"]):
                        continue
                    time_text = flow["timeText"] or flow["statusText"] or ""
                    if line[0].isdigit():
                        flush()
                        pending_team = {"name": flow["names"][0], "rank": flow["rank"], "members": []}
                    elif pending_team is not None:
                        pending_team["members"].append({"name": flow["names"][0], "timeText": time_text})
            flush()

    categories = [c for c in categories if c["results"]]
    for c in categories:
        if c["declaredStarters"] is None:
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
    args = ap.parse_args()

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
            fetch(f["url"], pdf_path)
            cats, head_text = parse_pdf(pdf_path, allow_inline_splits=sole_attachment)
            if RELAY_HEADER_RE.search(head_text):
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
                continue
            out_path.write_text(json.dumps({
                "eventId": eid,
                "source": "sportsoftware-pdf",
                "sourceUrl": f["url"],
                "fileName": f["fileName"],
                "listType": detect_list_type(f["fileName"], head_text, sole_attachment),
                "docDate": MANUAL_DOC_DATE_OVERRIDES.get((eid, f["fileName"]))
                           or guess_doc_date(f["fileName"], head_text),
                "categories": cats,
            }, ensure_ascii=False))
            ok += 1
        except Exception as e:
            failed += 1
            print(f"  FAIL {eid}-{n} {f['fileName']}: {e}", file=sys.stderr)
    print(f"parsed: {ok}, empty: {empty}, failed: {failed}")


if __name__ == "__main__":
    main()
