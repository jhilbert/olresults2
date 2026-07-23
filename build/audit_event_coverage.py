#!/usr/bin/env python3
"""Audit calendar -> attachment -> normalized source -> published DB coverage.

This complements the row-level quality report: a parser cannot emit an audit
issue for an event or attachment that never reached the database at all.
"""
import argparse
import json
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ingest"))
from sportsoftware_common import (  # noqa: E402
    is_championship_ranking_attachment, is_result_named_attachment,
)


def load_events(path):
    # Last copy wins when a moving paginated endpoint returned an ID twice.
    return {int(event["id"]): event for event in json.loads(path.read_text())}


def normalized_rows(path):
    try:
        document = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return 0
    return sum(
        len(category.get("results") or [])
        for category in document.get("categories") or []
    )


def is_past_public_competition(event):
    return (
        event.get("eventType") == "competition"
        and event.get("visibility") == "public"
        and event.get("status") != "cancelled"
        and (event.get("dateFrom") or "9999")[:10] <= date.today().isoformat()
    )


def collect(calendar_path, attachments_path, normalized_dir, raw_files_dir,
            database_path, external_calendar_path=None):
    events = load_events(calendar_path)
    attachments = json.loads(attachments_path.read_text())
    external = load_events(external_calendar_path) if external_calendar_path else {}

    normalized = defaultdict(dict)
    for path in normalized_dir.glob("*.json"):
        match = re.fullmatch(r"(\d+)-(?:club)?(\d+)", path.stem)
        if not match:
            continue
        event_id, index = map(int, match.groups())
        normalized[event_id][index] = normalized_rows(path)

    con = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    try:
        db_events = {row[0] for row in con.execute("SELECT id FROM event")}
        db_result_counts = dict(con.execute(
            "SELECT s.event_id, COUNT(r.id) FROM stage s "
            "LEFT JOIN result r ON r.stage_id=s.id GROUP BY s.event_id"))
        published_paths = {
            row[0] for row in con.execute(
                "SELECT normalized_path FROM source_document "
                "WHERE normalized_path IS NOT NULL")
        }
    finally:
        con.close()

    def event_item(event_id, **extra):
        event = events.get(event_id) or external.get(event_id) or {}
        return {
            "event_id": event_id,
            "date": (event.get("dateFrom") or "")[:10] or None,
            "title": event.get("shortTitle"),
            "url": event.get("url"),
            **extra,
        }

    def display_path(path):
        try:
            return str(path.relative_to(ROOT))
        except ValueError:
            return str(path)

    calendar_missing = [
        event_item(event_id)
        for event_id in sorted(set(external) - set(events))
        if is_past_public_competition(external[event_id])
    ]
    database_missing = [
        event_item(event_id)
        for event_id, event in sorted(events.items())
        if is_past_public_competition(event)
        and "bewertung" not in (event.get("shortTitle") or "").lower()
        and event_id not in db_events
    ]

    parser_gaps = []
    ambiguous_parser_gaps = []
    for event_id, event in sorted(events.items()):
        if not is_past_public_competition(event):
            continue
        for index, attachment in enumerate(attachments.get(str(event_id)) or []):
            cached = sorted(raw_files_dir.glob(f"{event_id}-{index}.*"))
            if not cached or normalized[event_id].get(index, 0) > 0:
                continue
            item = event_item(
                event_id,
                attachment_index=index,
                file_name=attachment.get("fileName"),
                source_url=attachment.get("url"),
                cached_path=display_path(cached[0]),
            )
            if (is_result_named_attachment(attachment)
                    or is_championship_ranking_attachment(attachment)):
                parser_gaps.append(item)
            else:
                ambiguous_parser_gaps.append(item)

    unpublished = []
    for event_id, sources in sorted(normalized.items()):
        for index, rows in sorted(sources.items()):
            relative = f"data/normalized/{event_id}-{index}.json"
            if rows and relative not in published_paths:
                unpublished.append(event_item(
                    event_id, attachment_index=index,
                    normalized_path=relative, parsed_rows=rows,
                ))

    parser_gap_events = []
    by_event = defaultdict(list)
    for item in parser_gaps:
        by_event[item["event_id"]].append(item)
    for event_id, items in by_event.items():
        event = events.get(event_id) or {}
        if (db_result_counts.get(event_id, 0)
                or sum(normalized[event_id].values())
                or "bewertung" in (event.get("shortTitle") or "").lower()):
            continue
        parser_gap_events.append(event_item(
            event_id,
            cached_result_sources=len(items),
            examples=[item.get("file_name") for item in items[:3]],
        ))
    parser_gap_events.sort(key=lambda item: (item.get("date") or "", item["event_id"]),
                           reverse=True)

    return {
        "summary": {
            "calendar_events": len(events),
            "external_events_not_in_snapshot": len(calendar_missing),
            "snapshot_events_not_in_database": len(database_missing),
            "cached_sources_without_parsed_rows": len(parser_gaps),
            "ambiguous_cached_sources_without_parsed_rows": len(ambiguous_parser_gaps),
            "events_with_result_source_but_no_results": len(parser_gap_events),
            "parsed_sources_not_published": len(unpublished),
        },
        "external_events_not_in_snapshot": calendar_missing,
        "snapshot_events_not_in_database": database_missing,
        "cached_sources_without_parsed_rows": parser_gaps,
        "ambiguous_cached_sources_without_parsed_rows": ambiguous_parser_gaps,
        "events_with_result_source_but_no_results": parser_gap_events,
        "parsed_sources_not_published": unpublished,
    }


def render_text(report, limit):
    summary = report["summary"]
    lines = [
        "Event coverage audit",
        ", ".join(f"{key}={value}" for key, value in summary.items()),
    ]
    for key in (
        "external_events_not_in_snapshot",
        "snapshot_events_not_in_database",
        "events_with_result_source_but_no_results",
        "cached_sources_without_parsed_rows",
        "parsed_sources_not_published",
    ):
        items = report[key]
        lines.append(f"\n{key} ({len(items)})")
        for item in items[:limit]:
            detail = item.get("file_name") or item.get("normalized_path") or (
                ", ".join(filter(None, item.get("examples") or [])))
            lines.append(
                f"  {item.get('date') or '?'} · {item['event_id']} · "
                f"{item.get('title') or '?'}" + (f" · {detail}" if detail else "")
            )
        if len(items) > limit:
            lines.append(f"  … {len(items) - limit} more")
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--calendar", type=Path,
                        default=ROOT / "data/raw/anne/events.json")
    parser.add_argument("--external-calendar", type=Path)
    parser.add_argument("--attachments", type=Path,
                        default=ROOT / "data/raw/anne/attachments.json")
    parser.add_argument("--normalized", type=Path,
                        default=ROOT / "data/normalized")
    parser.add_argument("--raw-files", type=Path,
                        default=ROOT / "data/raw/anne/files")
    parser.add_argument("--db", type=Path,
                        default=ROOT / "site/data/results.db")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--limit", type=int, default=30)
    args = parser.parse_args(argv)
    report = collect(args.calendar, args.attachments, args.normalized,
                     args.raw_files, args.db, args.external_calendar)
    rendered = (json.dumps(report, ensure_ascii=False, indent=2) + "\n"
                if args.format == "json" else render_text(report, args.limit))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered)
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
