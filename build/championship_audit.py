#!/usr/bin/env python3
"""Build a conservative, source-linked ÖM/Wien championship review inventory.

This report is not an award calculator and never changes results, review
decisions or eligibility. A discovered document is not a reviewed class;
an imported annual table is not automatically a matched title.
"""
import argparse
import hashlib
import json
import re
import sqlite3
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "site/data/results.db"
DEFAULT_OUT = ROOT / "site/data/championship-audit.json"


def read_json(path, fallback):
    return json.loads(path.read_text()) if path.exists() else fallback


def regional_category_evidence(item, category, source_categories):
    """Accept only explicit source-to-regional mappings, never event titles."""
    for source, target in (item.get("categories") or {}).items():
        targets = list(target) if isinstance(target, dict) else [target]
        if source in source_categories and category in targets:
            return True
    return False


def name_key(value):
    value = unicodedata.normalize("NFKD", (value or "").casefold().replace("ß", "ss"))
    value = "".join(c for c in value if not unicodedata.combining(c))
    return " ".join(sorted(re.findall(r"[^\W_]+", value)))


def race_kind(value):
    value = (value or "").casefold()
    if re.search(r"staffel|mannschaft|relay", value):
        return None
    knockout = bool(re.search(r"knock.?out", value))
    found = {key for key, pattern in (("night", r"nacht"), ("middle", r"mittel"),
             ("long", r"lang"), ("sprint", r"sprint")) if re.search(pattern, value)}
    if knockout:
        found.discard("sprint")
        found.add("knockout")
    return next(iter(found)) if len(found) == 1 else None


def age_signature(category, title=""):
    """Only unambiguous youth or masters ages; open/range classes stay open."""
    text = (category or "").strip()
    if re.search(r"\bab\s*\d+\s*(?:bis|[-–])\s*\d+|\b\d+\s*[-–]\s*\d+", text, re.I):
        return None
    g = "D" if re.match(r"^(?:Damen|D(?=\s|-|\d))", text, re.I) else \
        "H" if re.match(r"^(?:Herren|H(?=\s|-|\d))", text, re.I) else None
    if g is None and title:
        g = "D" if re.search(r"meisterin\b", title, re.I) else \
            "H" if re.search(r"meister\b", title, re.I) else None
    upper = re.search(r"\bbis\s*(12|14|16|18|20)\b", text, re.I)
    compact = re.match(r"^[DH]\s*-?(12|14|16|18|20)(?:-?E)?(?:$|\s|/)", text, re.I)
    age = int(upper.group(1)) if upper else int(compact.group(1)) if compact else None
    if g and age:
        return (g, "upper", age)
    minimum = re.search(r"\bab\s*(35|40|45|50|55|60|65|70|75|80|85|90)\b", text, re.I)
    short_min = re.match(r"^(?:[DH]\s*)?(35|40|45|50|55|60|65|70|75|80|85|90)\s*-(?:$|\s)", text, re.I)
    minimum_age = int(minimum.group(1)) if minimum else int(short_min.group(1)) if short_min else None
    return (g, "minimum", minimum_age) if g and minimum_age else None


def event_race_kind(event_title, stage_title):
    """Prefer a specific stage; never infer an individual race from a team event."""
    if re.search(r"staffel|mannschaft|relay", f"{event_title or ''} {stage_title or ''}", re.I):
        return None
    if re.search(r"nacht|mittel|lang|sprint|knock.?out", stage_title or "", re.I):
        return race_kind(stage_title)
    return race_kind(event_title)


def regional_rule_findings(jurisdiction, date, rows):
    if jurisdiction != "WIEN" or date < "2022-10-10":
        return []
    started = [r for r in rows if r.get("regional_eligibility_state") == "eligible"
               and r.get("status") in ("ok", "dnf", "mp", "dsq")
               and not r.get("out_of_competition")]
    clubs = {r["official_club"] for r in started if r.get("official_club")}
    if not started or len(clubs) >= 2 or not all(r.get("official_club") for r in started):
        return []
    return [{"code": "wien_possible_two_club_rule",
        "message": "Bei den eindeutig wertungsberechtigten Gestarteten ist nur ein Verein erkennbar. Zwei-Vereine-Regel und Ausnahmen anhand der Ausschreibung und historischen Vereine prüfen; keine automatische Aberkennung.",
        "known_started_entries": len({r["regional_entry_id"] for r in started}),
        "known_clubs": sorted(clubs),
        "other_entries_open": any(r.get("regional_eligibility_state") == "provisional" or r.get("status") == "unknown" for r in rows)}]


def rulebook_fingerprint(directory):
    h = hashlib.sha256()
    for path in sorted(directory.glob("*.md")):
        h.update(path.name.encode())
        h.update(b"\0")
        h.update(path.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def oefol_winner_candidates(data):
    """Build a narrow suggestion index, never automatic confirmation.

    The ÖFOL table is annual and usually lacks event IDs/dates. Only clear
    individual youth and masters rows are indexed; team and ambiguous formats stay open.
    """
    out = defaultdict(list)
    for table in data.get("tables", []):
        year, sport, discipline = table.get("year"), None, None
        for row in table.get("rows", []):
            cells = [str(c.get("text") or "").strip() for c in row.get("cells", [])]
            if not cells:
                continue
            first = cells[0]
            if re.match(r"^(?:ski\s*[- ]?o)\s*\d{4}", first, re.I):
                sport, discipline = "skiOrienteering", None
                continue
            if re.match(r"^mtbo\s*\d{4}", first, re.I):
                sport, discipline = "mountainbikeOrienteering", None
                continue
            if re.match(r"^ol\s*\d{4}", first, re.I):
                sport, discipline = "footOrienteering", None
                continue
            if "meister" not in first.casefold():
                kind = race_kind(first)
                if kind:
                    discipline = kind
                elif re.search(r"staffel|mannschaft|relay", first, re.I):
                    discipline = None
                continue
            if len(cells) < 4 or not year or not sport or not discipline:
                continue
            signature = age_signature(cells[1], first)
            if not signature:
                continue
            # 2025 uses separate first/last columns; most older tables use
            # one full-name column and the club in the next cell.
            if len(cells) >= 5 and cells[4]:
                name, club = f"{cells[2]} {cells[3]}".strip(), cells[4]
            else:
                name, club = cells[2], cells[3]
            if not name or not club or re.search(r"[,/]|\bund\b|keine\s+wertung", name, re.I):
                continue
            out[(str(year), sport, discipline, signature, name_key(name), name_key(club))].append({
                "table_id": table.get("table_id"), "source_row": row.get("source_row"),
                "name": name, "club": club, "category": cells[1], "discipline": discipline})
    return out


def collect(con, source_inventory, national_evidence, regional_classes,
            regional_titles, national_rows, wolv_tables, wolv_sources=None, rules_fingerprint="", source_cases=None):
    con.row_factory = sqlite3.Row
    classes = [dict(row) for row in con.execute("""SELECT ci.id,ci.jurisdiction,ci.category,
        ci.championship_type,ci.state,ci.detection_basis,ci.input_fingerprint,
        e.id AS event_id,e.title AS event_title,e.sport_type,e.date_from,
        s.id AS stage_id,s.number AS stage_number,s.title AS stage_title,
        COALESCE(s.date,e.date_from) AS stage_date
        FROM championship_instance ci JOIN stage s ON s.id=ci.stage_id
        JOIN event e ON e.id=s.event_id WHERE ci.jurisdiction IN ('AUT','WIEN')
        ORDER BY e.date_from DESC,e.id,s.number,ci.jurisdiction,ci.category""")]
    by_id = {r["id"]: r for r in classes}
    results = defaultdict(list)
    common = """r.id AS result_id,r.result_list_id,r.category AS source_category,
        r.person_id,r.observed_name,r.official_club,r.rank,r.national_rank,
        r.status,r.out_of_competition,r.identity_state,r.championship_eligibility_state,r.time_s,
        r.result_kind,r.team_number,rl.input_fingerprint AS list_fingerprint,
        sd.source_url AS result_url"""
    sql_national = f"""SELECT ci.id AS instance_id,{common} FROM championship_instance ci
        JOIN result r ON r.stage_id=ci.stage_id AND r.category=ci.category
          AND r.championship=ci.championship_type
        LEFT JOIN result_list rl ON rl.id=r.result_list_id
        LEFT JOIN source_document sd ON sd.id=rl.source_document_id
        WHERE ci.jurisdiction='AUT' AND ci.detection_basis!='official_additional_classification'"""
    sql_additional = f"""SELECT ci.id AS instance_id,{common},cse.observed_rank AS official_rank
        FROM championship_instance ci JOIN championship_source_entry cse
          ON cse.stage_id=ci.stage_id AND cse.category=ci.category
         AND cse.championship_type=ci.championship_type
         AND cse.evidence_kind='official_additional_classification'
        JOIN result r ON r.id=cse.result_id
        LEFT JOIN result_list rl ON rl.id=r.result_list_id
        LEFT JOIN source_document sd ON sd.id=cse.source_document_id
        WHERE ci.jurisdiction='AUT' AND ci.detection_basis='official_additional_classification'"""
    sql_regional = f"""SELECT ci.id AS instance_id,{common},ce.id AS regional_entry_id,ce.regional_rank,
        ce.eligibility_state AS regional_eligibility_state FROM championship_instance ci
        JOIN championship_entry ce ON ce.championship_instance_id=ci.id
        JOIN championship_entry_result cer ON cer.championship_entry_id=ce.id
        JOIN result r ON r.id=cer.result_id
        LEFT JOIN result_list rl ON rl.id=r.result_list_id
        LEFT JOIN source_document sd ON sd.id=rl.source_document_id
        WHERE ci.jurisdiction='WIEN'"""
    for sql in (sql_national, sql_regional, sql_additional):
        for row in con.execute(sql):
            item = dict(row)
            if "official_rank" in item:
                item["national_rank"] = item.pop("official_rank")
            results[row["instance_id"]].append(item)
    issues = defaultdict(Counter)
    for row in con.execute("SELECT result_list_id,severity,code,COUNT(*) AS n FROM audit_issue GROUP BY result_list_id,severity,code"):
        issues[row["result_list_id"]][(row["severity"], row["code"])] += row["n"]
    confirmations = {(r["scope_key"], r["dimension"]): (r["state"], r["input_fingerprint"])
        for r in con.execute("SELECT scope_key,dimension,state,input_fingerprint FROM verification_assertion WHERE scope_type='result_list'")}
    official = {r[0] for r in con.execute("SELECT result_id FROM official_national_title")} if con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='official_national_title'").fetchone() else set()
    official_years = {t.get("year") for t in national_rows.get("tables", []) if t.get("year")}
    oefol_candidates = oefol_winner_candidates(national_rows)
    wolv_years = {d.get("year"): d for d in wolv_tables.get("documents", [])}
    wolv_sources = wolv_sources or {}
    output = []
    for c in classes:
        rows = results[c["id"]]
        date = (c["stage_date"] or "")[:10]
        year = date[:4]
        source_categories = {r["source_category"] for r in rows}
        lists = {r["result_list_id"]: r["list_fingerprint"] for r in rows if r["result_list_id"]}
        issue_counts = Counter()
        for list_id in lists:
            issue_counts.update(issues[list_id])
        summary_issues = [{"severity": sev, "code": code, "count": count}
                          for (sev, code), count in sorted(issue_counts.items())]
        completeness = "human_confirmed" if lists and all(
            confirmations.get((lid, "completeness")) == ("confirmed", fingerprint)
            for lid, fingerprint in lists.items()) else "not_confirmed"
        sources = source_inventory.get("events", {}).get(str(c["event_id"]), {})
        discovered = [a for a in sources.get("attachments", []) if a.get("type") == "invitation"]
        invitation = {"state": "found_unreviewed" if discovered else
                      "not_found" if sources.get("fetch_status") == "ok" else "not_checked",
                      "urls": [a["url"] for a in discovered]}
        reviewed = None
        if c["jurisdiction"] == "AUT":
            ev = national_evidence.get("events", {}).get(str(c["event_id"]), {})
            if ev.get("event_date") == date and ev.get("stage_number") == c["stage_number"]:
                if c["category"] in ev.get("categories", []):
                    reviewed = ev
                elif c["category"] in ev.get("excluded_from_national_championship_categories", []):
                    invitation["state"] = "category_conflict"
                    invitation["reviewed_url"] = ev.get("url")
        else:
            ev = regional_classes.get("events", {}).get(str(c["event_id"]), {})
            region = ev.get("jurisdictions", {}).get("WIEN", {})
            if ev.get("date") == date and ev.get("stage_number") == c["stage_number"] and regional_category_evidence(region, c["category"], source_categories):
                reviewed = region
        if reviewed:
            # Official-results prove the regional classification but are not
            # the invitation. Keep the two sources distinct.
            if reviewed.get("evidence_kind") == "official-results":
                invitation["classification_source"] = {"kind": "official-results", "url": reviewed.get("url")}
            else:
                invitation["state"] = "category_reviewed"
                invitation["reviewed_url"] = reviewed.get("url")
                invitation["sha256"] = reviewed.get("sha256")
        cross = {"state": "not_compared", "source_url": None}
        if c["jurisdiction"] == "AUT":
            if c["detection_basis"] == "official_additional_classification":
                cross.update(state="official_classification",
                             source_url=next((r.get("result_url") for r in rows if r.get("result_url")), None))
            elif any(r["result_id"] in official for r in rows):
                cross.update(state="winner_partially_matched", source_url=national_rows.get("source_url"))
            elif year and int(year) in official_years:
                cross["source_url"] = national_rows.get("source_url")
                winners = [r for r in rows if r.get("national_rank") == 1 and r.get("status") == "ok"
                           and r.get("result_kind") == "individual" and r.get("identity_state") == "resolved"]
                discipline = event_race_kind(c.get("event_title"), c.get("stage_title"))
                signature = age_signature(c.get("category"))
                if len(winners) == 1 and discipline and signature:
                    winner = winners[0]
                    key = (year, c.get("sport_type"), discipline, signature,
                           name_key(winner.get("observed_name")), name_key(winner.get("official_club")))
                    suggestions = oefol_candidates.get(key, [])
                    if len(suggestions) == 1:
                        cross["state"] = "winner_suggested"
                        cross["source_row"] = suggestions[0]
            else:
                cross["state"] = "year_unavailable"
        else:
            ev = regional_titles.get("events", {}).get(str(c["event_id"]), {})
            if ev.get("event_date") == date and ev.get("stage_number") == c["stage_number"]:
                cross["source_url"] = ev.get("url")
                winners = [r for r in rows if r.get("regional_rank") == 1]
                def matches(r, t):
                    return (str(r.get("person_id")) == str(t.get("person_id"))
                        and r.get("source_category") == t.get("result_category")
                        and r.get("official_club") == t.get("club")
                        and r.get("time_s") == t.get("time_s")
                        and (t.get("team_number") is None or r.get("team_number") == t.get("team_number"))
                        and (t.get("result_kind") is None or r.get("result_kind") == t.get("result_kind")))
                if any(matches(r, t) for r in winners for t in ev.get("conflicted_winners", [])):
                    cross["state"] = "conflict"
                elif any(matches(r, t) and t.get("name_resolution") == "anne_result_precedence"
                         for r in winners for t in ev.get("titles", [])):
                    cross["state"] = "winner_name_resolved_by_anne"
                elif winners and all(any(matches(r, t) for t in ev.get("titles", [])) for r in winners):
                    cross["state"] = "winner_matched_only"
                elif any(matches(r, t) for r in winners for t in ev.get("titles", [])):
                    cross["state"] = "winner_partially_matched"
            annual = wolv_years.get(int(year)) if year.isdigit() else None
            if annual and not cross["source_url"]:
                cross["source_url"] = annual.get("source_url")
            if not cross["source_url"]:
                cross["source_url"] = wolv_sources.get("years", {}).get(year)
            if not cross["source_url"] and year.isdigit():
                cross["source_url"] = next((r.get("url") for r in wolv_sources.get("ranges", [])
                    if r.get("from_year", 9999) <= int(year) <= r.get("to_year", 0)), None)
            if not cross["source_url"]:
                cross["state"] = "year_unavailable"
        identity_open = sum(r.get("person_id") is not None and r.get("identity_state") != "resolved" for r in rows)
        identity_applicable = sum(r.get("person_id") is not None for r in rows)
        rule_findings = regional_rule_findings(c["jurisdiction"], date, rows)
        class_data = {**c, "stage_date": date, "source_categories": sorted(x for x in source_categories if x),
            "result": {"row_count": len(rows), "list_count": len(lists),
                       "list_ids": sorted(lists),
                       "source_urls": sorted({r["result_url"] for r in rows if r["result_url"]}),
                       "completeness": completeness, "identity_open": identity_open,
                       "identity_applicable": identity_applicable, "issues": summary_issues},
            "invitation": invitation, "crosscheck": cross, "rule_findings": rule_findings}
        class_data["audit_fingerprint"] = hashlib.sha256(json.dumps({
            "instance": c["input_fingerprint"], "lists": sorted(lists.items()),
            "invitation": invitation, "crosscheck": cross, "rule_findings": rule_findings,
            "rules": rules_fingerprint,
            "national_table": national_rows.get("source_sha256") if c["jurisdiction"] == "AUT" else None,
            "regional_table": (wolv_years.get(int(year), {}).get("sha256")
                               if c["jurisdiction"] == "WIEN" and year.isdigit() else None),
            "result_state": sorted((r.get("result_id"), r.get("person_id"), r.get("official_club"),
                r.get("identity_state"), r.get("status"), r.get("out_of_competition"), r.get("rank"), r.get("national_rank"),
                r.get("regional_rank"), r.get("championship_eligibility_state"),
                r.get("regional_eligibility_state"), r.get("team_number"), r.get("time_s")) for r in rows),
        }, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()
        output.append(class_data)
    grouped = defaultdict(Counter)
    for c in output:
        key = ((c["stage_date"] or c["date_from"] or "")[:4], c["jurisdiction"])
        grouped[key]["classes"] += 1
        grouped[key]["invitation_" + c["invitation"]["state"]] += 1
        grouped[key]["crosscheck_" + c["crosscheck"]["state"]] += 1
        grouped[key]["result_" + c["result"]["completeness"]] += 1
    missing_candidates = [v for v in source_inventory.get("events", {}).values()
                          if not v.get("jurisdictions")]
    return {"schema_version": 1,
            "scope": "Inventory and partial checks only. No global completeness or award verification implied.",
            "annual": [{"year": y, "jurisdiction": j, **dict(v)} for (y, j), v in sorted(grouped.items(), reverse=True)],
            "national_source_years": sorted(official_years), "regional_source_years": sorted(y for y in wolv_years if y),
            "candidate_events_without_instance": missing_candidates,
            "open_source_claims": [{"event_id": int(eid), "date": item.get("event_date"), **claim,
                                     "source_url": item.get("url")}
                for eid, item in regional_titles.get("events", {}).items() for claim in item.get("open_claims", [])]
                + (source_cases or {}).get("open_claims", []),
            "classes": output}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--output", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()
    def load(relative):
        return read_json(ROOT / relative, {})
    with sqlite3.connect(f"file:{args.db}?mode=ro", uri=True) as con:
        report = collect(con, load("data/review/championship_sources.json"),
            load("site/data/national-category-evidence.json"),
            load("data/review/regional_source_classes.json"),
            load("site/data/regional-title-evidence.json"),
            load("data/review/oefol_meistertafel_rows.json"),
            load("data/review/wolv_annual_medal_tables_2024_2026.json"),
            load("data/review/wolv_annual_sources.json"),
            rulebook_fingerprint(ROOT / "docs/rules"),
            load("data/review/championship_source_cases.json"))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.output.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(report, ensure_ascii=False, separators=(",", ":")) + "\n")
    tmp.replace(args.output)
    print(f"championship inventory: {len(report['classes'])} classes, {len(report['annual'])} year/jurisdiction groups; {args.output}")


if __name__ == "__main__":
    main()
