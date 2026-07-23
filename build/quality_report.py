#!/usr/bin/env python3
"""Produce an actionable parser/data-quality report from the built database.

The report deliberately groups findings by event and source document.  A
reviewer should keep one original source open and clear all affected classes,
not jump between unrelated single categories.
"""
import argparse
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "site" / "data" / "results.db"
DEFAULT_CATALOG = ROOT / "docs" / "rules" / "audit-catalog.json"


def load_catalog(path=DEFAULT_CATALOG):
    return json.loads(Path(path).read_text())


def _campaign_matches(row, campaign):
    if campaign == "national":
        return bool(row["is_national"])
    if campaign == "regional":
        return bool(row["is_regional"])
    return True


def collect(con, catalog, campaign="all", include_domains=None):
    con.row_factory = sqlite3.Row
    list_rows = con.execute(
        """SELECT rl.id, rl.category, rl.declared_starters, rl.parsed_entries,
                  rl.parsed_rows, rl.ranking_basis,
                  e.id AS event_id, e.title AS event_title, e.date_from,
                  s.title AS stage_title, sd.id AS source_document_id,
                  sd.source_type, sd.file_name, sd.source_url,
                  EXISTS(SELECT 1 FROM result r
                          WHERE r.result_list_id=rl.id
                            AND r.championship IS NOT NULL) AS is_national,
                  EXISTS(SELECT 1 FROM regional_category_mapping m
                          WHERE m.result_list_id=rl.id
                            AND m.state!='rejected') AS is_regional
             FROM result_list rl
             JOIN stage s ON s.id=rl.stage_id
             JOIN event e ON e.id=s.event_id
             JOIN source_document sd ON sd.id=rl.source_document_id
            WHERE rl.parsed_rows > 0""").fetchall()
    by_id = {row["id"]: dict(row) for row in list_rows
             if _campaign_matches(row, campaign)}
    issues = con.execute(
        """SELECT result_list_id, severity, code, COUNT(*) AS issue_count
             FROM audit_issue
            GROUP BY result_list_id, severity, code""").fetchall()

    severity_counts = Counter()
    code_counts = Counter()
    domain_counts = Counter()
    for issue in issues:
        row = by_id.get(issue["result_list_id"])
        meta = catalog.get(issue["code"], {
            "rule": "UNMAPPED", "domain": "unmapped", "owner": "review",
            "action": "Auditcode im Regelkatalog ergänzen.",
        })
        if include_domains and meta["domain"] not in include_domains:
            continue
        item = {
            "severity": issue["severity"], "code": issue["code"],
            "count": issue["issue_count"], **meta,
        }
        if row is None:
            continue
        row.setdefault("issues", []).append(item)
        severity_counts[item["severity"]] += item["count"]
        code_counts[item["code"]] += item["count"]
        domain_counts[item["domain"]] += item["count"]

    actionable = []
    weights = {"blocker": 10000, "warning": 100, "info": 1}
    owner_weights = {"parser": 50, "rules": 30, "review": 5, "source": 2}
    for row in by_id.values():
        if not row.get("issues"):
            continue
        row["score"] = sum(
            item["count"] * (weights.get(item["severity"], 1)
                             + owner_weights.get(item["owner"], 0))
            for item in row["issues"])
        actionable.append(row)
    actionable.sort(key=lambda row: (
        0 if row["is_national"] else 1 if row["is_regional"] else 2,
        -row["score"], str(row["date_from"] or ""), row["event_id"],
        str(row["category"] or "")))

    grouped = defaultdict(list)
    for row in actionable:
        grouped[(row["event_id"], row["source_document_id"])].append(row)
    sources = []
    for (_event_id, _source_id), rows in grouped.items():
        first = rows[0]
        sources.append({
            "event_id": first["event_id"], "event_title": first["event_title"],
            "date": first["date_from"], "stage_title": first["stage_title"],
            "source_document_id": first["source_document_id"],
            "source_type": first["source_type"], "file_name": first["file_name"],
            "source_url": first["source_url"],
            "is_national": bool(first["is_national"]),
            "is_regional": bool(first["is_regional"]),
            "score": sum(row["score"] for row in rows), "lists": rows,
        })
    sources.sort(key=lambda source: (
        0 if source["is_national"] else 1 if source["is_regional"] else 2,
        -source["score"], str(source["date"] or ""), source["event_id"]))
    return {
        "campaign": campaign,
        "summary": {
            "affected_lists": len(actionable), "affected_sources": len(sources),
            "by_severity": dict(sorted(severity_counts.items())),
            "by_domain": dict(sorted(domain_counts.items())),
            "by_code": dict(sorted(code_counts.items())),
        },
        "sources": sources,
    }


def render_text(report, limit=30):
    summary = report["summary"]
    lines = [
        f"Quality report ({report['campaign']}): "
        f"{summary['affected_sources']} Quellen / "
        f"{summary['affected_lists']} Klassen mit Befund",
        "Severity: " + ", ".join(
            f"{key}={value}" for key, value in summary["by_severity"].items())
        if summary["by_severity"] else "Severity: keine Befunde",
    ]
    for source in report["sources"][:limit]:
        campaign = "ÖM/ÖSTM" if source["is_national"] else (
            "Landes-MS" if source["is_regional"] else "sonstiges")
        label = source["file_name"] or source["source_type"] or source["source_document_id"]
        lines.append(
            f"\n{source['date'] or '?'} · Event {source['event_id']} · {campaign} · "
            f"{source['event_title']} · {label}")
        for row in source["lists"]:
            issue_text = ", ".join(
                f"{item['code']}×{item['count']} [{item['rule']}]"
                for item in row["issues"])
            lines.append(
                f"  - {row['category']}: {row['declared_starters']} Quelle / "
                f"{row['parsed_entries']} Einheiten / {row['parsed_rows']} Zeilen · "
                f"{issue_text}")
        if source["source_url"]:
            lines.append(f"    {source['source_url']}")
    hidden = len(report["sources"]) - min(limit, len(report["sources"]))
    if hidden:
        lines.append(f"\n… {hidden} weitere Quellen; JSON-Report für vollständige Liste verwenden.")
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--campaign", choices=("all", "national", "regional"),
                        default="all")
    parser.add_argument("--domain", action="append", dest="domains")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--fail-on-blockers", action="store_true")
    args = parser.parse_args(argv)
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    try:
        report = collect(con, load_catalog(args.catalog), args.campaign,
                         set(args.domains or ()))
    finally:
        con.close()
    rendered = (json.dumps(report, ensure_ascii=False, indent=2) + "\n"
                if args.format == "json" else render_text(report, args.limit))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered)
    else:
        print(rendered, end="")
    blockers = report["summary"]["by_severity"].get("blocker", 0)
    if args.fail_on_blockers and blockers:
        print(f"quality gate: FAILED: {blockers} parser/data blockers", file=sys.stderr)
        return 1
    if args.fail_on_blockers:
        print("quality gate: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
