from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest

from ensemble3d.assets import AssetStore, cube_glb, scale_glb, unpack_fixture
from ensemble3d.demo import crash_and_recover, run_demo
from ensemble3d.studio_api.db import StudioDB
from ensemble3d.studio_api.jobs import JobManager, QueueFull, WorkspaceBusy
from ensemble3d.workspace import Workspace


def eventually(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("Condition did not become true before the deadline")


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.db = StudioDB(Path(self.temporary.name) / "studio.db")
        self.project = self.db.create_project(title="Synthetic", prompt="", style="test", run_dir=".")

    def version(self, **kwargs):
        return self.db.create_model_version(project_id=self.project.id, label="Fixture", source_kind="synthetic", **kwargs)

    def test_concurrent_json_merges_keep_every_independent_update(self):
        # Adapted from the original Studio installer-reliability regression.
        self.db.set_setting_json("preferences", {"base": True, "remove": True})
        barrier = threading.Barrier(16)
        def merge(index):
            barrier.wait(timeout=5)
            self.db.merge_setting_json("preferences", updates={f"worker_{index}": index})
        with ThreadPoolExecutor(max_workers=16) as pool:
            list(pool.map(merge, range(16)))
        value = self.db.merge_setting_json("preferences", remove_keys=["remove"])
        self.assertEqual(value, {"base": True, **{f"worker_{i}": i for i in range(16)}})

    def test_concurrent_versions_keep_one_current_pointer(self):
        barrier = threading.Barrier(8)
        def create(index):
            barrier.wait(timeout=5)
            return self.version(metadata={"worker": index})
        with ThreadPoolExecutor(max_workers=8) as pool:
            ids = list(pool.map(create, range(8)))
        versions = self.db.list_model_versions(self.project.id)
        current = [v["id"] for v in versions if v["is_current"]]
        self.assertEqual(len(versions), 8)
        self.assertEqual(current, [self.db.get_project(self.project.id).current_version_id])
        self.assertIn(current[0], ids)

    def test_concurrent_selection_remains_consistent(self):
        ids = [self.version() for _ in range(4)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda vid: self.db.set_current_version(self.project.id, vid), ids * 8))
        selected = self.db.get_project(self.project.id).current_version_id
        self.assertEqual([v["id"] for v in self.db.list_model_versions(self.project.id) if v["is_current"]], [selected])

    def test_invalid_cross_project_parent_and_selection_do_not_change_current(self):
        original = self.version()
        other = self.db.create_project(title="Other", prompt="", style="test", run_dir=".")
        alien = self.db.create_model_version(project_id=other.id, label="Other", source_kind="synthetic")
        with self.assertRaises(ValueError):
            self.version(parent_version_id=alien)
        with self.assertRaises(KeyError):
            self.db.set_current_version(self.project.id, alien)
        self.assertEqual(self.db.get_project(self.project.id).current_version_id, original)
        self.assertEqual(len(self.db.list_model_versions(self.project.id)), 1)

    def test_job_and_version_commit_together_or_roll_back(self):
        original = self.version()
        self.db.create_job(job_id="atomic", project_id=self.project.id, kind="scale")
        with self.assertRaises(ValueError):
            self.version(parent_version_id=original, job_id="atomic")
        self.assertEqual(len(self.db.list_model_versions(self.project.id)), 1)
        self.assertEqual(self.db.get_job("atomic")["status"], "queued")
        self.db.update_job("atomic", status="running")
        with self.assertRaises(sqlite3.IntegrityError):
            self.version(version_id=original, job_id="atomic")
        self.assertEqual(self.db.get_job("atomic")["status"], "running")
        self.assertEqual(self.db.get_project(self.project.id).current_version_id, original)
        committed = self.version(parent_version_id=original, job_id="atomic")
        self.assertEqual(self.db.get_job("atomic")["result"], {"version_id": committed})
        self.assertEqual(self.db.get_job("atomic")["status"], "done")
        self.assertEqual(self.db.get_project(self.project.id).current_version_id, committed)


class JobTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.db = StudioDB(Path(self.temporary.name) / "studio.db")
        self.project = self.db.create_project(title="Synthetic", prompt="", style="test", run_dir=".")

    def submit(self, manager, run, **kwargs):
        return manager.submit(project_id=self.project.id, kind="test", payload={}, run=run, **kwargs)

    def test_worker_and_waiting_capacity_are_bounded_without_creating_rejected_jobs(self):
        release = threading.Event()
        lock = threading.Lock()
        running = 0
        peak = 0
        def block(_):
            nonlocal running, peak
            with lock:
                running += 1
                peak = max(peak, running)
            release.wait(5)
            with lock:
                running -= 1
        with JobManager(self.db, max_workers=2, max_pending=2) as manager:
            try:
                ids = [self.submit(manager, block) for _ in range(4)]
                eventually(lambda: running == 2)
                with self.assertRaises(QueueFull):
                    self.submit(manager, block)
                self.assertEqual(len(self.db.list_jobs(self.project.id)), 4)
                self.assertEqual(sum(self.db.get_job(jid)["status"] == "queued" for jid in ids), 2)
            finally:
                release.set()
        self.assertEqual(peak, 2)
        self.assertTrue(all(self.db.get_job(jid)["status"] == "done" for jid in ids))

    def test_fractional_boolean_and_out_of_range_capacities_are_rejected(self):
        for options in ({"max_workers": 1.5}, {"max_pending": 0.5}, {"max_workers": True},
                        {"max_pending": False}, {"max_workers": 0}, {"max_pending": -1}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                JobManager(self.db, **options)

    def test_duplicate_job_id_does_not_overwrite_or_leak_capacity(self):
        self.db.create_job(job_id="existing", project_id=self.project.id, kind="old", status="done")
        with JobManager(self.db, max_workers=1, max_pending=0) as manager:
            with self.assertRaises(sqlite3.IntegrityError):
                self.submit(manager, lambda _: None, job_id="existing")
            self.assertEqual(manager.get("existing")["kind"], "old")
            jid = self.submit(manager, lambda _: None)
        self.assertEqual(self.db.get_job(jid)["status"], "done")

    def test_failures_including_system_exit_are_terminal_and_do_not_expose_details(self):
        for exception in (RuntimeError, SystemExit):
            def fail(_):
                raise exception("synthetic-private-detail")
            with JobManager(self.db, max_workers=1, max_pending=0) as manager:
                jid = self.submit(manager, fail)
            row = self.db.get_job(jid)
            self.assertEqual(row["status"], "error")
            self.assertEqual(row["error"], exception.__name__)
            self.assertNotIn("synthetic-private-detail", json.dumps(row))

    def test_second_owner_is_rejected_without_interrupting_live_job(self):
        release = threading.Event()
        with JobManager(self.db, max_workers=1) as manager:
            try:
                jid = self.submit(manager, lambda _: release.wait(5) and None)
                eventually(lambda: manager.get(jid)["status"] == "running")
                with self.assertRaises(WorkspaceBusy):
                    JobManager(self.db)
                self.assertEqual(manager.get(jid)["status"], "running")
            finally:
                release.set()
        with JobManager(self.db) as reopened:
            self.assertEqual(reopened.recovered_jobs, 0)

    def test_shutdown_cancels_waiting_work_and_waits_for_the_running_handler(self):
        release = threading.Event()
        ran_queued = threading.Event()
        manager = JobManager(self.db, max_workers=1, max_pending=1)
        active = self.submit(manager, lambda _: release.wait(5) and None)
        eventually(lambda: manager.get(active)["status"] == "running")
        queued = self.submit(manager, lambda _: ran_queued.set())
        closer = threading.Thread(target=lambda: manager.close(cancel_pending=True))
        closer.start()
        try:
            eventually(lambda: manager.get(queued)["stage"] == "cancelled")
            self.assertTrue(closer.is_alive())
            with self.assertRaises(RuntimeError):
                self.submit(manager, lambda _: None)
            with self.assertRaises(WorkspaceBusy):
                JobManager(self.db)
        finally:
            release.set()
            closer.join(timeout=5)
        self.assertFalse(closer.is_alive())
        self.assertFalse(ran_queued.is_set())
        self.assertEqual(manager.get(active)["status"], "done")


class AssetAndRecoveryTests(unittest.TestCase):
    def test_failed_workspace_construction_does_not_leak_owner_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            blocker = Path(directory) / "assets"
            blocker.write_text("not a directory")
            with self.assertRaises(OSError):
                Workspace(directory)
            blocker.unlink()
            with Workspace(directory) as workspace:
                self.assertEqual(workspace.jobs.recovered_jobs, 0)

    def test_glb_roundtrip_scaling_and_original_preservation(self):
        original = cube_glb(1)
        self.assertEqual(original, cube_glb(1))
        before = hashlib.sha256(original).hexdigest()
        transformed = scale_glb(original, 2)
        _, _, old_vertices = unpack_fixture(original)
        _, _, new_vertices = unpack_fixture(transformed)
        self.assertEqual(new_vertices, [tuple(value * 2 for value in v) for v in old_vertices])
        self.assertEqual(hashlib.sha256(original).hexdigest(), before)
        self.assertNotEqual(original, transformed)

    def test_concurrent_asset_publication_deduplicates_and_rejects_traversal_or_corruption(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AssetStore(directory)
            original = cube_glb()
            with ThreadPoolExecutor(max_workers=8) as pool:
                digests = list(pool.map(store.put, [original] * 16))
            self.assertEqual(len(set(digests)), 1)
            self.assertEqual(len(list(Path(directory).iterdir())), 1)
            self.assertEqual(store.read(digests[0]), original)
            for invalid in ("../outside", "G" * 64, "a" * 63):
                with self.assertRaises(ValueError):
                    store.read(invalid)
            (Path(directory) / (digests[0] + ".glb")).write_bytes(b"corrupt")
            with self.assertRaises(ValueError):
                store.put(original)

    def test_invalid_mesh_sizes_and_unsupported_layouts_fail(self):
        for size in (0, -1, float("nan"), float("inf"), 1001):
            with self.assertRaises(ValueError):
                cube_glb(size)
        for data in (b"", cube_glb()[:-1], b"not a GLB container" * 10):
            with self.assertRaises((ValueError, UnicodeDecodeError)):
                unpack_fixture(data)

    def test_real_process_crash_recovers_jobs_and_preserves_committed_version(self):
        with tempfile.TemporaryDirectory() as directory:
            with Workspace(directory) as workspace:
                project = workspace.create_project()
                completed = workspace.wait(workspace.generate(project.id))
                vid = completed["result"]["version_id"]
                original = workspace.db.get_model_version(vid)
                data = workspace.assets.read(original["metadata"]["sha256"])
            recovered = crash_and_recover(directory, project.id)
            self.assertEqual(len(recovered), 2)
            with Workspace(directory) as workspace:
                self.assertEqual(workspace.jobs.recovered_jobs, 0)  # recovery is idempotent
                self.assertEqual(workspace.db.get_project(project.id).current_version_id, vid)
                self.assertEqual(workspace.assets.read(original["metadata"]["sha256"]), data)
                self.assertEqual(len(workspace.db.list_model_versions(project.id)), 1)
                self.assertEqual(len(list((Path(directory) / "assets").glob("*.glb"))), 2)

    def test_demo_exports_real_assets_and_refuses_nonempty_output(self):
        with tempfile.TemporaryDirectory() as parent:
            output = Path(parent) / "demo"
            report = run_demo(output)
            self.assertTrue(report["original_preserved"])
            self.assertEqual(report["recovered_jobs"], 2)
            self.assertEqual(len(report["versions"]), 2)
            self.assertEqual(sum(job["status"] == "done" for job in report["jobs"]), 2)
            for version in report["versions"]:
                self.assertEqual(hashlib.sha256((output / version["file"]).read_bytes()).hexdigest(), version["sha256"])
            self.assertIn("Persistent job journal", (output / "report.html").read_text())
            with self.assertRaises(ValueError):
                run_demo(output)


if __name__ == "__main__":
    unittest.main()
