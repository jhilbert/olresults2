import importlib.util
import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent.parent


def load_module(name, relative_path):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


anne_sync = load_module("anne_sync_incremental", "ingest/anne_sync.py")
run_sync = load_module("run_sync_incremental", "ingest/run_sync.py")
sync_selection = load_module("sync_selection_incremental", "ingest/sync_selection.py")


class AttachmentInventoryTests(unittest.TestCase):
    def test_merge_keeps_existing_indices_and_only_appends_new_urls(self):
        old = [
            {"url": "https://example.test/a.pdf", "fileName": "old.pdf",
             "mimeType": "application/pdf"},
            {"url": "https://example.test/b.htm", "fileName": "b.htm",
             "mimeType": "text/html"},
        ]
        fetched = [
            {"url": "https://example.test/b.htm", "fileName": "renamed.htm",
             "mimeType": "text/html"},
            {"url": "https://example.test/c.pdf", "fileName": "c.pdf",
             "mimeType": "application/pdf"},
        ]

        merged, added = anne_sync.merge_attachment_index(old, fetched)

        self.assertEqual(merged[:2], old)
        self.assertEqual(merged[2], fetched[1])
        self.assertEqual(added, [(2, fetched[1])])

    def test_manifest_selects_only_new_attachment_keys(self):
        jobs = [(100, 0, {}), (100, 1, {}), (200, 0, {})]
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / "delta.json"
            manifest.write_text(json.dumps({
                "attachments": [{"eventId": 100, "index": 1}]
            }))
            self.assertEqual(
                sync_selection.select_jobs(jobs, manifest_path=manifest),
                [jobs[1]],
            )

    def test_automatic_index_refresh_skips_known_historic_events(self):
        old_date = (date.today() - timedelta(days=365)).isoformat()
        recent_date = (date.today() - timedelta(days=2)).isoformat()
        events = [
            {"id": 1, "dateFrom": old_date},
            {"id": 2, "dateFrom": recent_date},
            {"id": 3, "dateFrom": old_date},
            {"id": 4, "dateFrom": recent_date, "hasOfficialResults": True},
            {"id": 5, "dateFrom": recent_date, "hasOfficialResults": True,
             "shortTitle": "ÖSTM Sprint"},
        ]
        known = {
            "1": [{"url": "https://example.test/old.pdf", "fileName": "old.pdf",
                   "mimeType": "application/pdf"}],
            "2": [{"url": "https://example.test/recent.pdf", "fileName": "recent.pdf",
                   "mimeType": "application/pdf"}],
        }

        def api_response(url):
            event_id = int(url.split("/event/")[1].split("/")[0])
            return [{
                "type": "results",
                "url": f"https://example.test/{event_id}-new.pdf",
                "fileName": f"{event_id}-new.pdf",
                "mimeType": "application/pdf",
            }]

        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(anne_sync, "RAW", Path(tmp)), \
                patch.object(anne_sync, "get", side_effect=api_response) as get, \
                patch.object(anne_sync, "has_unusable_structured_results", return_value=False), \
                patch.object(anne_sync, "MANUAL_ATTACHMENT_OVERRIDES", {}), \
                patch.object(anne_sync, "MANUAL_PDF_OVERRIDES", {}), \
                patch.object(anne_sync, "MANUAL_HTML_OVERRIDES", {}):
            additions = anne_sync.sync_attachments(
                events,
                known,
                refresh_cutoff=(date.today() - timedelta(days=30)).isoformat(),
            )

        self.assertEqual(get.call_count, 3)
        self.assertEqual(
            {(entry["eventId"], entry["index"]) for entry in additions},
            {(2, 1), (3, 0), (5, 0)},
        )
        self.assertEqual(known["1"][0]["url"], "https://example.test/old.pdf")
        self.assertNotIn("4", known)

    def test_mislabeled_championship_ranking_is_kept_but_splits_are_not(self):
        ranking = {
            "type": "other", "fileName": "ÖM Nacht Meisterschaftswertung.pdf",
            "url": "https://example.test/ranking.pdf",
        }
        splits = {
            "type": "other", "fileName": "ÖSTM Ergebnis SPLIT.pdf",
            "url": "https://example.test/split.pdf",
        }

        self.assertTrue(anne_sync.is_championship_ranking_attachment(ranking))
        self.assertFalse(anne_sync.is_championship_ranking_attachment(splits))

    def test_mislabeled_result_filename_is_kept_but_result_splits_are_not(self):
        result = {
            "type": "other", "fileName": "oestm-oem-ski-lang-2019-erg.pdf",
            "url": "https://example.test/result.pdf",
        }
        splits = {
            "type": "splittimes", "fileName": "Ergebnisse-Zwischenzeiten.pdf",
            "url": "https://example.test/splits.pdf",
        }

        self.assertTrue(anne_sync.is_result_named_attachment(result))
        self.assertFalse(anne_sync.is_result_named_attachment(splits))


class SyncCommandTests(unittest.TestCase):
    def test_nightly_parser_uses_delta_manifest(self):
        command = ["python", "ingest/parse_sportsoftware_pdf.py"]
        result = run_sync.command_for(command, Path("/tmp/delta.json"))
        self.assertEqual(result[-2:], ["--attachment-manifest", "/tmp/delta.json"])

    def test_historic_refresh_is_scoped_to_one_event(self):
        command = ["python", "ingest/parse_sportsoftware_text.py"]
        result = run_sync.command_for(
            command, Path("/tmp/delta.json"), event_id=4474, refresh_source=True)
        self.assertEqual(result[-3:], ["--event-id", "4474", "--force-download"])


if __name__ == "__main__":
    unittest.main()
