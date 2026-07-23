import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ingest"))
SPEC = importlib.util.spec_from_file_location(
    "parse_sportsoftware_pdf_verified_scans",
    ROOT / "ingest" / "parse_sportsoftware_pdf.py",
)
pdf_parser = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pdf_parser)


class VerifiedScanTranscriptTests(unittest.TestCase):
    def test_reviewed_transcripts_match_exact_cached_sources(self):
        expected = {
            931: (17, 94),
            932: (18, 96),
            1062: (42, 602),
            1722: (9, 81),
            3781: (20, 68),
            3929: (1, 47),
        }
        for event_id, (category_count, row_count) in expected.items():
            with self.subTest(event_id=event_id):
                transcript = pdf_parser.load_verified_scan_transcript(
                    ROOT / "data" / "raw" / "anne" / "files"
                    / f"{event_id}-0.pdf",
                    event_id,
                    0,
                )
                self.assertEqual(len(transcript["categories"]), category_count)
                self.assertEqual(
                    sum(
                        len(category["results"])
                        for category in transcript["categories"]
                    ),
                    row_count,
                )

    def test_changed_source_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            changed_pdf = Path(tmp) / "931-0.pdf"
            changed_pdf.write_bytes(b"%PDF-changed")
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                pdf_parser.load_verified_scan_transcript(
                    changed_pdf, 931, 0)

    def test_known_scan_ties_and_statuses_survive_transcription(self):
        transcript = pdf_parser.load_verified_scan_transcript(
            ROOT / "data" / "raw" / "anne" / "files" / "3929-0.pdf",
            3929,
            0,
        )
        rows = transcript["categories"][0]["results"]
        sona = next(row for row in rows if row["name"] == "Sona Asenbauer")
        mika = next(row for row in rows if row["name"] == "Mika Asenbauer")
        self.assertEqual((sona["rank"], mika["rank"]), (5, 5))

        transcript = pdf_parser.load_verified_scan_transcript(
            ROOT / "data" / "raw" / "anne" / "files" / "1722-0.pdf",
            1722,
            0,
        )
        rows = [
            row
            for category in transcript["categories"]
            for row in category["results"]
        ]
        self.assertEqual(
            next(row for row in rows if row["name"] == "Roland Wölfler")[
                "status"
            ],
            "dnf",
        )

        transcript = pdf_parser.load_verified_scan_transcript(
            ROOT / "data" / "raw" / "anne" / "files" / "1062-0.pdf",
            1062,
            0,
        )
        rows = [
            row
            for category in transcript["categories"]
            for row in category["results"]
        ]
        self.assertEqual(
            next(row for row in rows if row["name"] == "Mack Judith")[
                "status"
            ],
            "mp",
        )
        self.assertEqual(
            next(row for row in rows if row["name"] == "Lechthaler Philipp")[
                "status"
            ],
            "dns",
        )

        transcript = pdf_parser.load_verified_scan_transcript(
            ROOT / "data" / "raw" / "anne" / "files" / "932-0.pdf",
            932,
            0,
        )
        rows = [
            row
            for category in transcript["categories"]
            for row in category["results"]
        ]
        kirchmeir = next(
            row for row in rows if row["name"] == "Elisabeth Kirchmeir"
        )
        self.assertEqual(
            (kirchmeir["rank"], kirchmeir["timeText"], kirchmeir["timeS"]),
            (3, "1:33:25", 5605),
        )
        novak = next(
            row for row in rows if row["name"] == "Elisabeth Novak-Fragner"
        )
        self.assertEqual(
            (novak["rank"], novak["timeText"], novak["timeS"]),
            (6, "1:48:03", 6483),
        )


if __name__ == "__main__":
    unittest.main()
