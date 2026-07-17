import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("run_sync", ROOT / "ingest" / "run_sync.py")
run_sync = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(run_sync)


class SyncRunnerTests(unittest.TestCase):
    def test_stops_on_first_failed_adapter(self):
        commands = [["python", "first.py"], ["python", "second.py"], ["python", "third.py"]]
        with patch.object(run_sync, "COMMANDS", commands), patch.object(
                run_sync.subprocess, "run") as execute:
            execute.side_effect = [
                type("Result", (), {"returncode": 0})(),
                type("Result", (), {"returncode": 7})(),
            ]
            self.assertEqual(run_sync.main(), 7)
            self.assertEqual(execute.call_count, 2)


if __name__ == "__main__":
    unittest.main()
