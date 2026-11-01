#!/usr/bin/env python3
"""Discover public ANNE invitation/result attachments for championship candidates.

Discovery is not verification: a link, event title or missing response never
proves a championship category or a winner. Existing observations are retained
on a failed fetch. Run explicitly; this is not part of the nightly sync.
"""
import argparse
import json
import re
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "site/data/results.db"
OUT = ROOT / "data/review/championship_sources.json"
BASE = "https://anne-api.oefol.at/v1/event/{}/attachments"
TITLE_CANDIDATE = re.compile(r"(?:österreichisch.*meister|österr.*meister|\bö(?:st)?m\b|wiener.*meister|wr\.?\s*(?:/\s*nö\s*)?(?:ms|meister))", re.I)


def candidates(con, start, end):
    rows = con.execute("""SELECT e.id,e.date_from,e.title,e.sport_type,
       GROUP_CONCAT(DISTINCT CASE WHEN ci.jurisdiction IN ('AUT','WIEN')
                                  THEN ci.jurisdiction END)
       FROM event e LEFT JOIN stage s ON s.event_id=e.id
       LEFT JOIN championship_instance ci ON ci.stage_id=s.id
       WHERE substr(e.date_from,1,4) BETWEEN ? AND ?
       GROUP BY e.id ORDER BY e.date_from DESC,e.id""", (str(start), str(end))).fetchall()
    return [{"event_id": row[0], "date": row[1], "title": row[2], "sport_type": row[3],
             "jurisdictions": (row[4] or "").split(",") if row[4] else [],
             "candidate_basis": "existing-championship" if row[4] else "event-title-only"}
            for row in rows if row[4] or TITLE_CANDIDATE.search(row[2] or "")]


def relevant_attachments(payload):
    if not isinstance(payload, list):
        raise ValueError("ANNE attachment response is not a list")
    result = []
    for item in payload:
        if not isinstance(item, dict) or item.get("type") not in ("invitation", "results"):
            continue
        url = item.get("url")
        if not isinstance(url, str) or not re.match(r"^https?://", url):
            continue
        result.append({key: item.get(key) for key in
                       ("id", "type", "label", "fileName", "mimeType", "url", "updatedAt")})
    return sorted(result, key=lambda item: (str(item.get("type")), str(item.get("id"))))


def fetch(event_id, timeout=20):
    # System curl uses the platform trust store on macOS, where framework
    # Python can lack configured CA certificates. Never disable TLS checking.
    try:
        response = subprocess.run(["curl", "-fsSL", "--http1.1", "--max-time", str(timeout),
                                   "--user-agent", "OLResults2-source-audit/1.0",
                                   BASE.format(event_id)], capture_output=True, check=True,
                                  timeout=timeout + 2)
    except subprocess.CalledProcessError as exc:
        raise OSError(exc.stderr.decode("utf-8", "replace").strip() or str(exc)) from exc
    except subprocess.TimeoutExpired as exc:
        raise OSError("ANNE attachment request timed out") from exc
    return relevant_attachments(json.loads(response.stdout))


def refresh(items, existing, fetcher=fetch, delay=0.15):
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    events = dict(existing.get("events", {}))
    for index, item in enumerate(items, 1):
        eid = str(item["event_id"])
        previous = events.get(eid, {})
        try:
            attachments = fetcher(item["event_id"])
            events[eid] = {**item, "fetch_status": "ok", "checked_at": stamp,
                           "attachments": attachments}
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            events[eid] = {**previous, **item, "fetch_status": "error", "checked_at": stamp,
                           "error": str(exc), "attachments": previous.get("attachments", [])}
        if index % 20 == 0 or index == len(items):
            print(f"attachment metadata {index}/{len(items)}", flush=True)
        if delay and index < len(items):
            time.sleep(delay)
    return {"schema_version": 1, "source": BASE,
            "scope": "Public attachment metadata only; no document or championship verification.",
            "events": events}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-year", type=int, default=2007)
    parser.add_argument("--to-year", type=int, default=2026)
    parser.add_argument("--db", type=Path, default=DB)
    parser.add_argument("--output", type=Path, default=OUT)
    parser.add_argument("--delay", type=float, default=0.15)
    parser.add_argument("--event-id", type=int, action="append", default=[], help="limit discovery to specified candidate IDs")
    args = parser.parse_args()
    previous = json.loads(args.output.read_text()) if args.output.exists() else {}
    with sqlite3.connect(f"file:{args.db}?mode=ro", uri=True) as con:
        items = candidates(con, args.from_year, args.to_year)
    if args.event_id:
        items = [item for item in items if item["event_id"] in args.event_id]
    data = refresh(items, previous, delay=max(0, args.delay))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.output.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    tmp.replace(args.output)
    print(f"wrote {len(data['events'])} candidates to {args.output}")


if __name__ == "__main__":
    main()
