from pathlib import Path
import tempfile
import unittest

from wechat_dot_bridge.errors import BridgeError
from wechat_dot_bridge.lifecycle import LEASE_SECONDS
from wechat_dot_bridge.store import Store


class WorkLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = 1800000000
        self.store = Store(Path(self.tmp.name) / "state", clock=lambda: self.now)
        self.store.import_binding(dict(id="fixture-bot", owner="fixture-owner", token="fixture-token",
                                       base_url="https://ilinkai.weixin.qq.com"))
        self.store.set_enabled(True)
        self.store.ingest("fixture-bot", [dict(source_id=str(index), peer="fixture-owner",
            text="fixture", created_at=self.now, context_token="fixture-context") for index in range(3)], "fixture")
        self.ids = [row["message_id"] for row in self.store.list_pending()]
        for mid in self.ids:
            self.store.read_message(mid)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_late_unversioned_heartbeat_cannot_revive_completed_work(self):
        mid = self.ids[0]
        self.store.report_progress(mid, active=False, completed=True)
        completed = self.store.work_status(mid)
        with self.assertRaisesRegex(BridgeError, "processing_already_completed"):
            self.store.report_progress(mid, active=True)
        # Even a legacy late stop must not make terminal work pending again.
        self.store.report_progress(mid, active=False)
        self.assertEqual(self.store.work_status(mid), completed)
        self.assertNotIn(mid, {row["message_id"] for row in self.store.list_pending()})
        self.assertIsNone(self.store.typing_target([mid]))

    def test_explicit_subset_forks_membership_before_implicit_completion(self):
        first, second, third = self.ids
        initial = self.store.report_progress(first, message_ids=self.ids)["work"]
        subset = self.store.report_progress(first, message_ids=self.ids[:2])["work"]
        self.assertNotEqual(subset["processing_id"], initial["processing_id"])
        self.assertGreater(subset["revision"], initial["revision"])
        self.assertEqual(set(self.store.work_members(first)), {first, second})
        self.assertEqual(self.store.work_status(third)["processing_id"], initial["processing_id"])
        self.store.report_progress(first, active=False, completed=True)
        self.assertEqual(self.store.work_status(first)["state"], "completed")
        self.assertEqual(self.store.work_status(second)["state"], "completed")
        self.assertEqual(self.store.work_status(third)["state"], "active")

    def test_explicit_subset_completion_leaves_excluded_message_recoverable(self):
        first, second, third = self.ids
        self.store.report_progress(first, message_ids=self.ids)
        self.store.report_progress(first, active=False, completed=True, message_ids=self.ids[:2])
        self.assertEqual({row["message_id"] for row in self.store.list_pending()}, {third})
        self.assertEqual(set(self.store.work_members(first)), {first, second})

    def test_resuming_expired_group_synchronizes_all_processing_mirrors(self):
        first = self.ids[0]
        original = self.store.report_progress(first, message_ids=self.ids)["work"]
        self.now += LEASE_SECONDS + 1
        resumed = self.store.read_message(first)["processing"]
        self.assertNotEqual(resumed["processing_id"], original["processing_id"])
        mirrors = dict(self.store.db.execute("SELECT message_id,expires FROM processing"))
        for mid in self.ids:
            value = self.store.work_status(mid)
            self.assertEqual(value["processing_id"], resumed["processing_id"])
            self.assertEqual(value["state"], "active")
            self.assertEqual(mirrors[mid], self.now + LEASE_SECONDS)
        from wechat_dot_bridge.operations import diagnostics
        self.assertNotIn("processing_stalled", {issue["code"] for issue in diagnostics(self.store)["issues"]})

    def test_additional_interim_reply_starts_new_primary_generation(self):
        first, second, third = self.ids
        self.store.report_progress(first, message_ids=self.ids)
        self.store.queue_reply(first, "first completion", reply_key="first-final")
        completed = self.store.work_status(first)
        self.store.queue_reply(first, "additional progress", reply_key="next-progress", final=False)
        next_work = self.store.work_status(first)
        self.assertEqual(next_work["state"], "active")
        self.assertNotEqual(next_work["processing_id"], completed["processing_id"])
        self.assertGreater(next_work["revision"], completed["revision"])
        self.assertEqual(self.store.work_members(first), [first])
        self.assertEqual(self.store.work_status(second)["state"], "completed")
        self.assertEqual(self.store.work_status(third)["state"], "completed")

    def test_completed_primary_cannot_attach_new_pending_work_with_heartbeat(self):
        first, second, _ = self.ids
        self.store.report_progress(first, active=False, completed=True)
        with self.assertRaisesRegex(BridgeError, "processing_already_completed"):
            self.store.report_progress(first, active=False, completed=True, message_ids=[first, second])
        self.assertEqual(self.store.work_status(second)["state"], "active")
