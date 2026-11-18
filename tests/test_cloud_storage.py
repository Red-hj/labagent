from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from labagent.cloud_storage import CloudPersistence, StorageConflict, StorageError, SupabaseStorage, validate_manifest
from labagent.engine import diagnose
from labagent.personal import Personal
from test_engine import ROOT, case
from test_workspace_archive import workspace


class Remote:
    url = "https://test.supabase.co"
    bucket = "labagent-workspace"

    def __init__(self):
        self.row = None
        self.objects = {}
        self.lost_response = False
        self.fail_upload = False
        self.during_upload = None

    def check(self):
        pass

    def head(self):
        return deepcopy(self.row)

    def upload(self, revision, number, data):
        if self.fail_upload and number == 1:
            raise StorageError("upload failed")
        self.objects[revision, number] = data
        if self.during_upload:
            callback, self.during_upload = self.during_upload, None
            callback()

    def download(self, revision, number):
        return self.objects[revision, number]

    def publish(self, row, expected):
        if (self.row["revision"] if self.row else None) != expected:
            raise StorageConflict("concurrent writer")
        self.row = deepcopy(row)
        if self.lost_response:
            raise StorageError("response lost after commit")

    def remove(self, manifest):
        for number in range(manifest["parts"]):
            self.objects.pop((manifest["revision"], number), None)


class CloudStorageTests(unittest.TestCase):
    def setUp(self):
        base = ROOT / "runtime/tests"
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.workspace = workspace(self.folder / "source")
        self.remote = Remote()
        self.cloud = CloudPersistence(self.workspace, self.remote)
        self.cloud.bootstrap()
        payload = case()
        self.saved = self.workspace.store.save(payload, diagnose(payload))

    def fresh(self, name="restored"):
        return CloudPersistence(workspace(self.folder / name), self.remote)

    def test_restart_restores_accounts_memory_and_history_without_login_tokens(self):
        personal = Personal(self.workspace.store)
        user, token = personal.authenticate({"username": "student", "password": "test-password-123"}, register=True)
        personal.save_memory(user["id"], {"kind": "preference", "title": "习惯", "content": "优先给出检查命令"})
        self.cloud.sync()
        self.assertEqual(self.cloud.status()["state"], "saved")
        restored = self.fresh()
        restored.bootstrap()
        self.assertEqual(restored.workspace.store.history(self.saved["session_id"]), self.workspace.store.history(self.saved["session_id"]))
        target_personal = Personal(restored.workspace.store)
        self.assertIsNone(target_personal.user(token))
        authenticated, _ = target_personal.authenticate({"username": "student", "password": "test-password-123"})
        self.assertEqual(target_personal.list_memories(authenticated["id"])[0]["content"], "优先给出检查命令")

    def test_deleted_history_stays_deleted_after_restoring_latest_snapshot(self):
        self.cloud.sync()
        self.workspace.store.delete_session(self.saved["session_id"])
        self.assertEqual(self.cloud.status()["state"], "pending")
        self.cloud.sync()
        restored = self.fresh()
        restored.bootstrap()
        self.assertIsNone(restored.workspace.store.history(self.saved["session_id"]))

    def test_corrupt_or_missing_chunk_never_restores_partial_database(self):
        self.cloud.sync()
        self.remote.objects[self.cloud.revision, 0] = b"bad"
        target = self.fresh()
        with self.assertRaisesRegex(StorageError, "分片不完整"):
            target.bootstrap()
        self.assertEqual(target.workspace.summary()["sessions"], 0)
        self.assertFalse(target.marker.exists())

    def test_existing_local_workspace_is_never_silently_overwritten(self):
        self.cloud.sync()
        other = self.fresh()
        payload = case("03_verified_demo")
        saved = other.workspace.store.save(payload, diagnose(payload))
        with self.assertRaises(StorageConflict):
            other.bootstrap()
        self.assertIsNotNone(other.workspace.store.history(saved["session_id"]))
        self.assertIsNone(other.workspace.store.history(self.saved["session_id"]))

    def test_second_instance_cannot_overwrite_a_newer_cloud_version(self):
        self.cloud.sync()
        second = self.fresh()
        second.bootstrap()
        self.cloud.sync()
        head = self.remote.head()
        with self.assertRaises(StorageConflict):
            second.sync()
        self.assertEqual(self.remote.head(), head)
        self.assertEqual(second.status()["state"], "conflict")

    def test_compare_and_swap_catches_writer_that_publishes_during_upload(self):
        self.cloud.sync()
        second = self.fresh()
        second.bootstrap()
        self.remote.during_upload = second.sync
        with self.assertRaises(StorageConflict):
            self.cloud.sync()
        self.assertEqual(self.remote.row["revision"], second.revision)
        retained = {self.remote.row["revision"], *[p["revision"] for p in self.remote.row["previous"]]}
        self.assertEqual({revision for revision, _ in self.remote.objects}, retained)

    def test_lost_commit_response_is_confirmed_and_not_deleted(self):
        self.remote.lost_response = True
        self.cloud.sync()
        self.assertEqual(self.cloud.status()["state"], "saved")
        restored = self.fresh()
        restored.bootstrap()
        self.assertEqual(restored.workspace.summary()["sessions"], 1)

    def test_failed_upload_retains_previous_version_and_removes_partial_objects(self):
        with patch("labagent.cloud_storage.PART_BYTES", 256):
            self.cloud.sync()
            previous = self.remote.head()
            self.remote.fail_upload = True
            with self.assertRaises(StorageError):
                self.cloud.sync()
            self.assertEqual(previous, self.remote.head())
            self.assertEqual({revision for revision, _ in self.remote.objects}, {self.cloud.revision})
            self.assertEqual(self.cloud.status()["state"], "error")

    def test_change_during_upload_remains_pending_and_retention_is_two_versions(self):
        def change():
            self.workspace.store.delete_session(self.saved["session_id"])
        self.remote.during_upload = change
        self.cloud.sync()
        self.assertEqual(self.cloud.status()["state"], "pending")
        self.cloud.sync()
        self.cloud.sync()
        self.assertEqual(self.cloud.status()["state"], "saved")
        self.assertEqual(len({revision for revision, _ in self.remote.objects}), 2)

    def test_busy_snapshot_remains_pending_and_can_retry(self):
        with self.workspace.gate.activity():
            with self.assertRaises(ValueError):
                self.cloud.sync()
        self.assertEqual(self.cloud.status()["state"], "pending")
        self.cloud.sync()
        self.assertEqual(self.cloud.status()["state"], "saved")


class StorageTransportTests(unittest.TestCase):
    def test_secret_key_only_goes_to_server_apikey_header_and_redirects_are_refused(self):
        remote = SupabaseStorage("https://test.supabase.co", "sb_secret_" + "x" * 30)
        response = io.BytesIO(b'{"public":false}')
        with patch.object(remote.opener, "open", return_value=response) as opened:
            remote.check()
        request = opened.call_args.args[0]
        self.assertEqual(request.get_header("Apikey"), remote.key)
        self.assertIsNone(request.get_header("Authorization"))
        from labagent.cloud_storage import NoRedirect
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, "", {}, "https://elsewhere.invalid"))

    def test_public_bucket_and_publishable_or_incomplete_keys_are_rejected(self):
        for key in ("sb_publishable_abc", "placeholder", "sb_secret_abc••••"):
            with self.assertRaises(StorageError):
                SupabaseStorage("https://test.supabase.co", key)
        remote = SupabaseStorage("https://test.supabase.co", "sb_secret_" + "x" * 30)
        with patch.object(remote, "request", return_value={"public": True}):
            with self.assertRaisesRegex(StorageError, "私有"):
                remote.check()

    def test_manifest_rejects_external_paths_and_unbounded_downloads(self):
        for value in ({}, {"format": "labagent-cloud-v1", "revision": "../../other"}):
            with self.assertRaises(StorageError):
                validate_manifest(value)

    def test_conditional_update_filters_by_previous_revision(self):
        remote = SupabaseStorage("https://test.supabase.co", "sb_secret_" + "x" * 30)
        with patch.object(remote, "request", return_value=[]) as request:
            with self.assertRaises(StorageConflict):
                remote.publish({"revision": "b" * 32}, "a" * 32)
        self.assertIn("revision=eq." + "a" * 32, request.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
