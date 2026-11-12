"""The local review server exposes only intended, contained source snapshots."""
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("olresults_serve", ROOT / "site/serve.py")
serve = importlib.util.module_from_spec(spec)
spec.loader.exec_module(serve)


class ReviewSourcePaths(unittest.TestCase):
    def test_raw_and_archived_snapshots_only(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "project"
            raw = root / "data/raw/anne/files/source.pdf"
            archived = root / "data/review/archived_sources/2021/source.pdf"
            other = root / "data/review/private.txt"
            outside = base / "outside.txt"
            for path in (raw, archived, other, outside):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("example")
            escaped = raw.parent / "escaped.pdf"
            escaped.symlink_to(outside)
            with patch.object(serve, "ROOT", root):
                self.assertEqual(serve.review_source_path("data/raw/anne/files/source.pdf"), raw.resolve())
                self.assertEqual(serve.review_source_path("data/review/archived_sources/2021/source.pdf"), archived.resolve())
                self.assertIsNone(serve.review_source_path("data/review/private.txt"))
                self.assertIsNone(serve.review_source_path("data/raw/anne/files/escaped.pdf"))
                self.assertIsNone(serve.review_source_path("../outside.txt"))
                self.assertIsNone(serve.review_source_path("data/raw/anne/files/missing.pdf"))


if __name__ == "__main__":
    unittest.main()
