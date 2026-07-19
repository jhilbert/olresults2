import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("sync_local", ROOT / "build" / "sync_local.py")
sync_local = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync_local)


class LocalSyncTests(unittest.TestCase):
    def test_reads_quoted_local_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env.local"
            path.write_text(
                "# comment\nOLRESULTS_GATEWAY_URL=https://gateway.example\n"
                "ANNE_GATEWAY_TOKEN='secret value'\n")
            self.assertEqual(sync_local.read_local_env(path), {
                "OLRESULTS_GATEWAY_URL": "https://gateway.example",
                "ANNE_GATEWAY_TOKEN": "secret value",
            })

    def test_saved_gateway_config_is_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env.local"
            sync_local.save_local_gateway_config("https://gateway.example", "secret", path)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(
                sync_local.read_local_env(path)["ANNE_GATEWAY_TOKEN"], "secret")

    def test_sync_runs_pull_build_validate_in_order(self):
        commands = []

        def record(command, env=None, capture=False):
            commands.append(command[1] if len(command) > 1 else command[0])
            return ""

        with patch.object(sync_local, "run", side_effect=record), \
                patch.object(sync_local, "review_overlay_summary"):
            sync_local.sync_local(os.environ.copy(), update_git=False)

        self.assertEqual(commands, [
            "ingest/eligibility_state.py",
            "ingest/identity_state.py",
            "build/build_db.py",
            "build/validate_db.py",
        ])

    def test_missing_noninteractive_token_is_rejected(self):
        with patch.object(sync_local, "read_local_env", return_value={}), \
                patch.dict(os.environ, {
                    "OLRESULTS_GATEWAY_URL": "",
                    "ANNE_GATEWAY_TOKEN": "",
                }, clear=False):
            with self.assertRaises(sync_local.SyncError):
                sync_local.gateway_environment(prompt=False)


if __name__ == "__main__":
    unittest.main()
