"""Backup to a DVC remote and restore on a new machine, with local stand-ins for S3 and GitHub."""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from brick_runner import backup  # noqa: E402
from brick_runner.ledger import Ledger  # noqa: E402
from brick_runner.store import Store, StoreError  # noqa: E402


@unittest.skipUnless(shutil.which("dvc"), "dvc is not on PATH")
class BackupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="brick-backup-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / "home"
        self.bucket = self.tmp / "bucket"          # stands in for s3://bucket/prefix
        self.git = self.tmp / "evidence.git"       # stands in for a private GitHub repository
        subprocess.run(["git", "init", "-q", "--bare", str(self.git)], check=True)

    def test_s3_remote_is_configured_without_credentials(self) -> None:
        state = backup.configure(self.home, "s3://my-bucket/brick-runner", region="us-east-1")
        self.assertEqual(state["dvc_remote"], "s3://my-bucket/brick-runner")
        config = (self.home / "store/.dvc/config").read_text()
        self.assertIn("url = s3://my-bucket/brick-runner", config)
        self.assertIn("region = us-east-1", config)
        self.assertNotIn("access_key", config)

    def test_round_trip_to_a_new_machine(self) -> None:
        store = Store(self.home / "store")
        [record] = store.add_records([{"kind": "run", "brick": "b", "trace_id": "t", "time": "2026-01-01",
                                       "input": {"symbol": "BTC"}}])
        ledger = Ledger(self.home)
        trial = ledger.write("b", "trial", {"split": "validation", "passed": "True"}, {"accuracy": 0.97},
                             {"passed": True})
        (self.home / "runner.key").write_bytes(b"k" * 32)
        backup.configure(self.home, str(self.bucket), git=str(self.git))
        result = backup.sync(self.home)
        self.assertEqual(result["pushed"], {"dvc": True, "git": True})
        self.assertTrue(any(self.bucket.rglob("*")))

        # A second sync with nothing new commits nothing.
        self.assertFalse(backup.sync(self.home)["committed"])

        # The machine is lost. Restore into the same home path on a new machine.
        shutil.rmtree(self.home)
        restored = backup.restore(self.home, str(self.git))
        self.assertFalse(restored["signing_key"])  # the key comes from the secret manager, never the backup
        self.assertEqual(Store(self.home / "store").record(record)["input"], {"symbol": "BTC"})
        [entry] = Ledger(self.home).find("b", kind="trial")
        self.assertEqual(entry.run_id, trial)
        self.assertEqual(Ledger(self.home).envelope(trial), {"passed": True})

    def test_restore_refuses_a_used_home_or_a_moved_path(self) -> None:
        Store(self.home / "store").add_records([{"kind": "run", "time": "2026-01-01"}])
        backup.configure(self.home, str(self.bucket), git=str(self.git))
        backup.sync(self.home)
        with self.assertRaises(StoreError):
            backup.restore(self.home, str(self.git))
        with self.assertRaises(StoreError):
            backup.restore(self.tmp / "elsewhere", str(self.git))


if __name__ == "__main__":
    unittest.main()
