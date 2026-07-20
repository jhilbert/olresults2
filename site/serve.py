#!/usr/bin/env python3
"""Local dev server; chdir first so it works regardless of launch cwd.

Sends `Cache-Control: no-store` on everything so the browser always picks up
the latest app.js / style.css / results.db without a hard refresh - the
GitHub Pages deploy does its own `?v=` cache-busting (see the workflow), so
this header only affects local development.
"""
import json
import mimetypes
import os
import errno
import sqlite3
import sys
import urllib.parse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SITE = Path(__file__).resolve().parent
ROOT = SITE.parent
DB = SITE / "data" / "results.db"
DECISIONS = ROOT / "data" / "review" / "verification.json"
CHAMPIONSHIP_CATALOG = ROOT / "data" / "review" / "championship_catalog.json"
os.chdir(SITE)


class NoCacheHandler(SimpleHTTPRequestHandler):
    def _json(self, value, status=200):
        body = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/review":
            value = json.loads(DECISIONS.read_text()) if DECISIONS.exists() else {"assertions": []}
            return self._json(value)
        if parsed.path == "/api/championship":
            value = (json.loads(CHAMPIONSHIP_CATALOG.read_text())
                     if CHAMPIONSHIP_CATALOG.exists() else {"instances": []})
            return self._json(value)
        if parsed.path == "/review-source":
            list_id = urllib.parse.parse_qs(parsed.query).get("id", [""])[0]
            with sqlite3.connect(DB) as con:
                row = con.execute(
                    """SELECT sd.snapshot_path FROM result_list rl
                       JOIN source_document sd ON sd.id = rl.source_document_id
                       WHERE rl.id = ?""", (list_id,)).fetchone()
            if not row or not row[0]:
                return self.send_error(404, "Kein lokaler Quell-Snapshot")
            path = (ROOT / row[0]).resolve()
            raw_root = (ROOT / "data" / "raw" / "anne").resolve()
            if raw_root not in path.parents or not path.is_file():
                return self.send_error(404, "Quell-Snapshot nicht gefunden")
            content = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return
        return super().do_GET()

    def do_POST(self):
        endpoint = urllib.parse.urlparse(self.path).path
        if endpoint not in {"/api/review", "/api/championship"}:
            return self.send_error(404)
        try:
            size = int(self.headers.get("Content-Length", "0"))
            assertion = json.loads(self.rfile.read(size))
            if endpoint == "/api/championship":
                if assertion.get("state") not in {"candidate", "confirmed", "rejected"}:
                    raise ValueError("Ungültiger Meisterschaftsstatus")
                instance_id = assertion.get("id")
                with sqlite3.connect(DB) as con:
                    row = con.execute(
                        "SELECT input_fingerprint FROM championship_instance WHERE id = ?",
                        (instance_id,)).fetchone()
                if not row or row[0] != assertion.get("input_fingerprint"):
                    raise ValueError("Meisterschaftsinstanz oder Fingerprint ist nicht aktuell")
                payload = (json.loads(CHAMPIONSHIP_CATALOG.read_text())
                           if CHAMPIONSHIP_CATALOG.exists() else {"instances": []})
                instances = payload.setdefault("instances", [])
                instances[:] = [item for item in instances if item.get("id") != instance_id]
                instances.append(assertion)
                instances.sort(key=lambda item: item.get("id", ""))
                CHAMPIONSHIP_CATALOG.parent.mkdir(parents=True, exist_ok=True)
                temp = CHAMPIONSHIP_CATALOG.with_suffix(".json.tmp")
                temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
                temp.replace(CHAMPIONSHIP_CATALOG)
                return self._json({"ok": True, "instance": assertion})
            if assertion.get("dimension") not in {
                    "completeness", "parsing", "identity", "ranking", "rules"}:
                raise ValueError("Ungültige Prüfdimension")
            if assertion.get("state") not in {"confirmed", "flagged", "not_applicable"}:
                raise ValueError("Ungültiger Prüfstatus")
            list_id = assertion.get("scope_key")
            with sqlite3.connect(DB) as con:
                row = con.execute(
                    "SELECT input_fingerprint FROM result_list WHERE id = ?", (list_id,)).fetchone()
            if not row or row[0] != assertion.get("input_fingerprint"):
                raise ValueError("Liste oder Fingerprint ist nicht aktuell")
            payload = json.loads(DECISIONS.read_text()) if DECISIONS.exists() else {"assertions": []}
            assertions = payload.setdefault("assertions", [])
            key = (assertion.get("scope_type", "result_list"), list_id,
                   assertion["dimension"])
            assertions[:] = [a for a in assertions if (
                a.get("scope_type", "result_list"), a.get("scope_key"), a.get("dimension")) != key]
            assertions.append(assertion)
            assertions.sort(key=lambda a: (a.get("scope_type", ""), a.get("scope_key", ""),
                                           a.get("dimension", "")))
            DECISIONS.parent.mkdir(parents=True, exist_ok=True)
            temp = DECISIONS.with_suffix(".json.tmp")
            temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
            temp.replace(DECISIONS)
            return self._json({"ok": True, "assertion": assertion})
        except (ValueError, json.JSONDecodeError) as exc:
            return self._json({"ok": False, "error": str(exc)}, 400)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store, max-age=0")
        super().end_headers()


if __name__ == "__main__":
    requested_port = int(sys.argv[1]) if len(sys.argv) > 1 else 8643
    ports = [requested_port] if len(sys.argv) > 1 else range(requested_port, requested_port + 10)
    server = None
    for port in ports:
        try:
            # results.db is large and browsers may keep its download open for a
            # while.  A single-threaded HTTPServer would then block every other
            # asset and make the whole preview appear unavailable.
            server = ThreadingHTTPServer(("127.0.0.1", port), NoCacheHandler)
            break
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE:
                raise
    if server is None:
        raise SystemExit(
            f"Kein freier Port zwischen {requested_port} und {requested_port + 9} gefunden.")
    if server.server_port != requested_port:
        print(f"Port {requested_port} ist belegt; verwende stattdessen {server.server_port}.",
              flush=True)
    print(f"OLResults: http://127.0.0.1:{server.server_port}/", flush=True)
    print(f"Prüfung:  http://127.0.0.1:{server.server_port}/review.html", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer beendet.")
    finally:
        server.server_close()
