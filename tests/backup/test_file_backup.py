"""Synthetic file snapshots only: no real Docker daemon, env files or databases."""
from contextlib import closing
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import stat
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "file_backup.py"
SPEC = importlib.util.spec_from_file_location("file_backup", SCRIPT)
backup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backup)


def database(path, wal=False):
    connection = sqlite3.connect(path)
    if wal:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.execute("CREATE TABLE events (value TEXT)")
    connection.execute("INSERT INTO events VALUES ('synthetic')")
    connection.execute("PRAGMA user_version=42")
    connection.commit()
    return connection


class FakeDocker:
    def __init__(self, fixture):
        self.fixture = fixture
        self.calls = []
        self.containers = []
        self.after_stop = None
        self.after_cp = None
        self.cp_source = None

    def container(self, name, service="api", running=True, mounts=None, project="example"):
        value = {"Id": name, "State": {"Running": running},
                 "Config": {"Labels": {"com.docker.compose.project": project,
                                        "com.docker.compose.service": service}},
                 "Mounts": mounts if mounts is not None else [
                     {"Type": "bind", "Source": str(self.fixture.data),
                      "Destination": "/data", "RW": True}]}
        self.containers.append(value)
        return value

    def __call__(self, args):
        self.calls.append(list(args))
        if args[0] == "compose":
            return json.dumps(self.fixture.config)
        if args[0] == "ps":
            return "\n".join(item["Id"] for item in self.containers)
        if args[0] == "inspect":
            identifiers = args[3:] if args[1:3] == ["--type", "container"] else args[1:]
            return json.dumps([item for item in self.containers if item["Id"] in identifiers])
        if args[0] in {"stop", "start"}:
            item = next(item for item in self.containers if item["Id"] == args[1])
            item["State"]["Running"] = args[0] == "start"
            if args[0] == "stop" and self.after_stop:
                self.after_stop()
            return item["Id"]
        if args[0] == "cp":
            shutil.copytree(self.cp_source, args[2], symlinks=True, dirs_exist_ok=True)
            if self.after_cp:
                self.after_cp()
            return ""
        raise AssertionError("Unexpected Docker command: " + args[0])


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.project = self.root / "project"
        self.project.mkdir()
        self.data = self.root / "data"
        self.data.mkdir()
        with closing(database(self.data / "app.sqlite3")):
            pass
        (self.data / "state.json").write_text('{"value":"synthetic"}')
        (self.project / ".env").write_text("PASSWORD=synthetic-secret\n")
        (self.project / "compose.yaml").write_text("services: {}\n")
        (self.project / "compose.local.yaml").write_text("services: {}\n")
        (self.project / "compose.webserver.yaml").write_text("services: {}\n")
        self.profile = {"version": 1, "sources": [
            {"name": "data", "service": "api", "target": "/data", "sqlite": ["app.sqlite3"]}],
            "stopOrder": ["web", "api"]}
        self.config = {"name": "example", "services": {
            "web": {}, "api": {"volumes": [
                {"type": "bind", "source": str(self.data), "target": "/data"}]}}}
        self.docker = FakeDocker(self)
        self.mock = patch.object(backup, "run_docker", self.docker)
        self.mock.start()
        backup._interrupted = None
        self.save_profile()

    def tearDown(self):
        self.mock.stop()
        backup._interrupted = None
        self.temporary.cleanup()

    def save_profile(self):
        (self.project / "file-backup.json").write_text(json.dumps(self.profile))

    def plan(self):
        self.save_profile()
        return backup.resolve(self.project, "local", ".env", None)

    def create(self, stop=False):
        return backup.create_backup(self.plan(), self.root / "backups", stop)

    def assert_unpublished(self):
        output = self.root / "backups"
        self.assertFalse(output.exists() and list(output.iterdir()))

    def test_snapshot_verify_restore_and_private_configuration(self):
        original = backup.content_inventory(self.data)
        snapshot = self.create()
        manifest = backup.verify_backup(snapshot)
        self.assertEqual(original, backup.content_inventory(self.data))
        self.assertEqual(42, manifest["sqlite"][0]["user_version"])
        self.assertNotIn("synthetic-secret", (snapshot / "manifest.json").read_text())
        self.assertIn("synthetic-secret", (snapshot / "configuration/env").read_text())
        self.assertEqual(str(self.data), manifest["sources"][0]["hostPath"])
        record = next(item for item in manifest["files"] if item["path"].endswith("state.json"))
        self.assertEqual(os.getuid(), record["uid"])
        self.assertEqual(os.getgid(), record["gid"])
        restored = backup.restore_backup(snapshot, self.root / "restored")
        self.assertEqual(backup.content_inventory(snapshot), backup.content_inventory(restored))
        for info in backup.tree(restored).values():
            self.assertEqual(0o700 if stat.S_ISDIR(info.st_mode) else 0o600, stat.S_IMODE(info.st_mode))
        with closing(sqlite3.connect(restored / "sources/data/app.sqlite3")) as connection:
            self.assertEqual([("synthetic",)], connection.execute("SELECT value FROM events").fetchall())

    def test_wal_normalization_never_opens_original(self):
        (self.data / "app.sqlite3").unlink()
        connection = database(self.data / "app.sqlite3", wal=True)
        try:
            original = backup.content_inventory(self.data)
            self.assertIn("app.sqlite3-wal", original)
            real_connect = sqlite3.connect
            opened = []
            def guarded_connect(path, *args, **kwargs):
                opened.append(Path(path))
                self.assertFalse(backup.within(Path(path), self.data))
                return real_connect(path, *args, **kwargs)
            with patch.object(backup.sqlite3, "connect", guarded_connect):
                snapshot = self.create()
            self.assertTrue(opened)
            self.assertEqual(original, backup.content_inventory(self.data))
            self.assertFalse((snapshot / "sources/data/app.sqlite3-wal").exists())
            with closing(real_connect(snapshot / "sources/data/app.sqlite3")) as copied:
                self.assertEqual(1, copied.execute("SELECT count(*) FROM events").fetchone()[0])
        finally:
            connection.close()

    def test_running_writer_without_flag_is_rejected(self):
        self.docker.container("api-1")
        with self.assertRaises(backup.BackupError):
            self.create()
        self.assertFalse(any(call[0] in {"stop", "start"} for call in self.docker.calls))
        self.assert_unpublished()

    def test_stops_in_order_restarts_only_previously_running(self):
        self.docker.container("api-1")
        self.docker.container("api-2", running=False)
        self.docker.container("web-1", service="web", mounts=[])
        self.create(stop=True)
        self.assertEqual([["stop", "web-1"], ["stop", "api-1"], ["start", "api-1"], ["start", "web-1"]],
                         [call for call in self.docker.calls if call[0] in {"stop", "start"}])
        self.assertFalse(self.docker.containers[1]["State"]["Running"])

    def test_foreign_writer_is_never_stopped(self):
        self.docker.container("foreign", project="elsewhere")
        with self.assertRaises(backup.BackupError):
            self.create(stop=True)
        self.assertFalse(any(call[0] in {"stop", "start"} for call in self.docker.calls))

    def test_read_only_foreign_container_does_not_block(self):
        container = self.docker.container("reader", project="elsewhere")
        container["Mounts"][0]["RW"] = False
        backup.verify_backup(self.create())

    def test_matching_parent_bind_writer_blocks(self):
        container = self.docker.container("foreign", project="elsewhere")
        container["Mounts"][0]["Source"] = str(self.root)
        with self.assertRaises(backup.BackupError):
            self.create(stop=True)

    def test_old_volume_container_cannot_backup_new_bind(self):
        self.docker.container("api-old", running=False, mounts=[
            {"Type": "volume", "Name": "old", "Destination": "/data", "RW": True}])
        plan = self.plan()
        with self.assertRaises(backup.BackupError):
            backup.assert_runtime_mounts(plan)
        with self.assertRaises(backup.BackupError):
            backup.create_backup(plan, self.root / "backups", True)
        self.assertFalse(any(call[0] == "stop" for call in self.docker.calls))

    def test_changed_bind_container_is_rejected(self):
        item = self.docker.container("api-old", running=False)
        item["Mounts"][0]["Source"] = str(self.root / "old-data")
        with self.assertRaises(backup.BackupError):
            self.create()

    def test_copy_failure_restarts_services_and_discards_stage(self):
        self.docker.container("api-1")
        with patch.object(backup, "copy_tree", side_effect=backup.BackupError("synthetic")):
            with self.assertRaises(backup.BackupError):
                self.create(stop=True)
        self.assertTrue(self.docker.containers[0]["State"]["Running"])
        self.assert_unpublished()

    def test_interrupt_after_stop_restarts_previously_running(self):
        self.docker.container("api-1")
        self.docker.after_stop = lambda: setattr(backup, "_interrupted", signal.SIGTERM)
        with self.assertRaises(backup.BackupError):
            self.create(stop=True)
        self.assertTrue(self.docker.containers[0]["State"]["Running"])
        self.assert_unpublished()

    def test_new_writer_after_stop_is_rejected_and_services_restarted(self):
        self.docker.container("api-1")
        self.docker.after_stop = lambda: self.docker.container("intruder", project="external")
        with self.assertRaises(backup.BackupError):
            self.create(stop=True)
        self.assertTrue(self.docker.containers[0]["State"]["Running"])
        self.assert_unpublished()

    def test_source_mutation_is_rejected(self):
        original = backup.copy_tree
        def mutate(source, target, entries):
            original(source, target, entries)
            (self.data / "state.json").write_text("changed")
        with patch.object(backup, "copy_tree", mutate):
            with self.assertRaises(backup.BackupError):
                self.create()
        self.assert_unpublished()

    def test_links_and_special_files_are_rejected(self):
        for kind in ("symlink", "fifo"):
            with self.subTest(kind=kind):
                path = self.data / "forbidden"
                if kind == "symlink":
                    path.symlink_to(self.data / "state.json")
                else:
                    os.mkfifo(path)
                try:
                    with self.assertRaises(backup.BackupError):
                        self.create()
                finally:
                    path.unlink()
                self.assert_unpublished()

    def test_missing_expected_database_fails(self):
        (self.data / "app.sqlite3").unlink()
        with self.assertRaises(backup.BackupError):
            self.create()
        self.assert_unpublished()

    def test_output_inside_source_fails_without_mutation(self):
        with self.assertRaises(backup.BackupError):
            backup.create_backup(self.plan(), self.data / "backups")
        self.assertFalse((self.data / "backups").exists())

    def test_uncovered_persistent_readonly_mount_fails(self):
        self.config["services"]["web"]["volumes"] = [
            {"type": "bind", "source": str(self.root / "downloads"),
             "target": "/downloads", "read_only": True}]
        with self.assertRaises(backup.BackupError):
            self.plan()

    def test_named_volume_cannot_be_silently_backed_up(self):
        self.config["services"]["api"]["volumes"][0].update(type="volume", source="database")
        self.config["volumes"] = {"database": {"name": "preserved_database"}}
        plan = self.plan()
        self.assertEqual("preserved_database", plan["sources"][0]["volumeName"])
        with self.assertRaises(backup.BackupError):
            backup.create_backup(plan, self.root / "backups")

    def test_configuration_only_has_no_container_operations(self):
        self.profile.update(sources=[], stopOrder=[])
        snapshot = self.create(stop=True)
        self.assertEqual([], backup.verify_backup(snapshot)["sources"])
        self.assertTrue(all(call[0] == "compose" for call in self.docker.calls))

    def test_verify_and_restore_do_not_need_docker_or_configuration(self):
        snapshot = self.create()
        shutil.rmtree(self.project)
        with patch.object(backup, "run_docker", side_effect=AssertionError("Docker not needed")):
            backup.verify_backup(snapshot)
            backup.restore_backup(snapshot, self.root / "restored")

    def test_tampered_snapshot_rejected(self):
        for case in ("content", "extra", "incomplete"):
            with self.subTest(case=case):
                snapshot = self.create()
                if case == "content":
                    (snapshot / "sources/data/state.json").write_text("tampered")
                elif case == "extra":
                    (snapshot / "unexpected").write_text("extra")
                else:
                    (snapshot / "COMPLETE").unlink()
                with self.assertRaises(backup.BackupError):
                    backup.verify_backup(snapshot)

    def test_manifest_traversal_rejected(self):
        snapshot = self.create()
        manifest = json.loads((snapshot / "manifest.json").read_text())
        manifest["files"][0]["path"] = "../outside"
        raw = json.dumps(manifest).encode()
        (snapshot / "manifest.json").write_bytes(raw)
        (snapshot / "COMPLETE").write_text(hashlib.sha256(raw).hexdigest())
        with self.assertRaises(backup.BackupError):
            backup.verify_backup(snapshot)

    def test_existing_restore_target_never_overwritten(self):
        snapshot = self.create()
        target = self.root / "existing"
        target.mkdir()
        (target / "keep").write_text("preserved")
        with self.assertRaises(backup.BackupError):
            backup.restore_backup(snapshot, target)
        self.assertEqual("preserved", (target / "keep").read_text())

    def setup_volume(self):
        self.docker.cp_source = self.data
        self.docker.container("old", running=False, mounts=[
            {"Type": "volume", "Name": "legacy-data", "Source": "/docker/volumes/legacy-data/_data",
             "Destination": "/data", "RW": True}])

    def test_migration_exports_original_wal_bytes_and_validates_copy(self):
        (self.data / "app.sqlite3").unlink()
        connection = database(self.data / "app.sqlite3", wal=True)
        try:
            self.setup_volume()
            before = backup.content_inventory(self.data)
            target = backup.migrate_volume("old", "/data", self.root / "migrated")
            self.assertEqual(before, backup.content_inventory(self.data))
            self.assertEqual(before, backup.content_inventory(target))
            self.assertEqual(2, sum(call[0] == "cp" for call in self.docker.calls))
            self.assertFalse(any(call[0] in {"compose", "start", "stop", "rm"} for call in self.docker.calls))
        finally:
            connection.close()

    def test_migration_rejects_other_running_volume_user_even_readonly(self):
        self.setup_volume()
        self.docker.container("other", project="other", mounts=[
            {"Type": "volume", "Name": "legacy-data", "Destination": "/data", "RW": False}])
        with self.assertRaises(backup.BackupError):
            backup.migrate_volume("old", "/data", self.root / "migrated")
        self.assertFalse(any(call[0] == "cp" for call in self.docker.calls))

    def test_migration_requires_exact_stopped_named_volume(self):
        self.docker.container("old", running=False)
        with self.assertRaises(backup.BackupError):
            backup.migrate_volume("old", "/data", self.root / "migrated")
        self.docker.containers.clear()
        self.setup_volume()
        self.docker.containers[0]["State"]["Running"] = True
        with self.assertRaises(backup.BackupError):
            backup.migrate_volume("old", "/data", self.root / "migrated")

    def test_migration_rejects_changed_volume_and_keeps_target_absent(self):
        self.setup_volume()
        self.docker.after_cp = lambda: (self.data / "state.json").write_text("changed")
        with self.assertRaises(backup.BackupError):
            backup.migrate_volume("old", "/data", self.root / "migrated")
        self.assertFalse((self.root / "migrated").exists())
        self.assertFalse(list(self.root.glob(".file-migrate-*")))

    def test_migration_rejects_symlinks(self):
        self.setup_volume()
        (self.data / "link").symlink_to(self.data / "state.json")
        with self.assertRaises(backup.BackupError):
            backup.migrate_volume("old", "/data", self.root / "migrated")
        self.assertFalse((self.root / "migrated").exists())

    def test_invalid_profile_types_fail_cleanly(self):
        for change in ({"stopOrder": [{}]}, {"sources": [{"name": "data", "service": "api", "target": "/data", "sqlite": [{}]}]}):
            with self.subTest(change=change):
                original = copy.deepcopy(self.profile)
                self.profile.update(change)
                with self.assertRaises(backup.BackupError):
                    self.plan()
                self.profile = original


if __name__ == "__main__":
    unittest.main()
