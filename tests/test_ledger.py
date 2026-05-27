import tempfile
import unittest
from pathlib import Path

from codex_memory.ledger import Ledger


class LedgerTransactionTest(unittest.TestCase):
    def test_transaction_rolls_back_on_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(Path(tmp) / "ledger.sqlite3")
            try:
                with self.assertRaises(RuntimeError):
                    with ledger.transaction():
                        ledger.add_event("manual", {"text": "will rollback"})
                        raise RuntimeError("boom")
                count = ledger.conn.execute("SELECT COUNT(*) AS count FROM events").fetchone()["count"]
                self.assertEqual(count, 0)
            finally:
                ledger.close()

    def test_nested_transaction_commits_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(Path(tmp) / "ledger.sqlite3")
            try:
                with ledger.transaction():
                    ledger.add_event("manual", {"text": "outer"})
                    with ledger.transaction():
                        ledger.add_event("manual", {"text": "inner"})
                count = ledger.conn.execute("SELECT COUNT(*) AS count FROM events").fetchone()["count"]
                self.assertEqual(count, 2)
            finally:
                ledger.close()

    def test_schema_migrations_baseline_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(Path(tmp) / "ledger.sqlite3")
            try:
                row = ledger.conn.execute("SELECT version,name FROM schema_migrations WHERE version=1").fetchone()
                self.assertIsNotNone(row)
                self.assertEqual(row["name"], "baseline_ledger_cognitive_runtime")
            finally:
                ledger.close()

    def test_export_prune_and_wipe(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(Path(tmp) / "ledger.sqlite3")
            try:
                event_id = ledger.add_event("manual", {"text": "hello", "_raw_payload_stored": False})
                ledger.mark_event_processed(event_id)
                exported = ledger.export_data()
                self.assertEqual(exported["version"], 1)
                self.assertEqual(len(exported["events"]), 1)
                pruned = ledger.prune_events()
                self.assertEqual(pruned["pruned_events"], 1)
                self.assertEqual(ledger.list_events(), [])
                ledger.add_event("manual", {"text": "again", "_raw_payload_stored": False})
                wiped = ledger.wipe_all()
                self.assertGreaterEqual(wiped["wiped"]["events"], 1)
                self.assertEqual(ledger.stats()["events"]["pending"], 0)
            finally:
                ledger.close()
