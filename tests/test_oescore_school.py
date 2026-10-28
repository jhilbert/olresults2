import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ingest"))
import parse_sportsoftware_pdf as parser


class OEScoreSchoolTests(unittest.TestCase):
    def test_school_teams_score_disqualification_and_guests(self):
        words, chars = [], []

        def line(top, fields):
            for x, text, bold in fields:
                words.append({"x0": x, "top": top, "text": text})
                for i, char in enumerate(text):
                    chars.append({"x0": x + i * 4, "top": top, "text": char,
                                  "fontname": "Helvetica-Bold" if bold else "Helvetica"})

        line(10, [(21, "Damen Unterstufe (2)", False)])
        line(20, [(38, "1", False), (50.2, "MUSTER Anna", False),
                  (210, "BEISPIEL Bea", False), (361.5, "TEST Cara", False),
                  (450, "Schule", False), (573, "60", True),
                  (606, "36:10", False)])
        line(30, [(50.2, "TEST Dan", False), (210, "MUSTER Eva", False),
                  (450, "Schule", False), (554, "Disqu", True)])
        line(40, [(21, "Damen Gäste (1)", False)])
        line(50, [(38, "1", False), (50.2, "Name", False),
                  (210, "Vorname", False), (450, "Verein", False),
                  (567, "126", True), (606, "21:20", False)])

        page = SimpleNamespace(chars=chars, extract_words=lambda: words)
        document = MagicMock()
        document.__enter__.return_value.pages = [page]
        with patch("pdfplumber.open", return_value=document):
            school, guests = parser.parse_oescore_school_pdf("ignored.pdf")

        self.assertEqual(school["declaredStarters"], 2)
        self.assertEqual([r["name"] for r in school["results"][:3]],
                         ["MUSTER Anna", "BEISPIEL Bea", "TEST Cara"])
        self.assertEqual({r["teamNumber"] for r in school["results"][:3]},
                         {school["results"][0]["teamNumber"]})
        self.assertEqual((school["results"][0]["scoreText"],
                          school["results"][0]["timeS"]), ("60", 2170))
        self.assertEqual(school["results"][0]["note"], "Ergebnis: 60 Punkte")
        self.assertEqual([r["status"] for r in school["results"][-2:]], ["dsq", "dsq"])
        self.assertEqual((guests["results"][0]["name"], guests["results"][0]["scoreText"]),
                         ("Name Vorname", "126"))


if __name__ == "__main__":
    unittest.main()
