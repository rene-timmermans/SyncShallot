from pathlib import Path
import os
import stat
import tempfile
import threading
import unittest
from unittest.mock import patch

from syncshallot import engine as e


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.left = self.base / "nas"
        self.right = self.base / "usb"
        self.left.mkdir()
        self.right.mkdir()
        self.recovery = self.base / "history"

    def tearDown(self):
        self.temp.cleanup()

    def put(self, root, name, content=b"hello"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def plan(self, **kwargs):
        return e.compare(self.left, self.right, **kwargs)

    def run_plan(self, plan, **kwargs):
        return e.execute(plan, self.recovery, **kwargs)

    def test_media_upgrade_copy_then_isolate_old_preserves_source(self):
        self.put(self.left, "Films/Movie-4K.mkv", b"new high resolution")
        self.put(self.right, "Films/Movie-720p.mkv", b"old low resolution")
        before = e.scan(self.left, True)
        plan = self.plan()
        self.assertEqual({r.path for r in plan.rows if e.is_extra(plan, r)}, {"Films/Movie-720p.mkv"})
        for row in plan.rows:
            if e.is_extra(plan, row):
                row.action = "isolate"
        result = self.run_plan(plan)
        self.assertEqual((self.right / "Films/Movie-4K.mkv").read_bytes(), b"new high resolution")
        self.assertFalse((self.right / "Films/Movie-720p.mkv").exists())
        archived = Path(result["recovery"]) / "isolated/right/Films/Movie-720p.mkv"
        self.assertEqual(archived.read_bytes(), b"old low resolution")
        self.assertEqual(e.scan(self.left, True), before)
        self.assertTrue(all(r.status == "same" for r in self.plan().rows))

    def test_compare_makes_no_changes_and_leaves_extras_by_default(self):
        self.put(self.left, "new")
        self.put(self.right, "old")
        before = e.scan(self.left, True), e.scan(self.right, True)
        plan = self.plan()
        self.assertEqual((e.scan(self.left, True), e.scan(self.right, True)), before)
        self.run_plan(plan)
        self.assertTrue((self.right / "old").exists())

    def test_thorough_detects_equal_size_time_content_difference(self):
        a = self.put(self.left, "film", b"aaaa")
        b = self.put(self.right, "film", b"bbbb")
        os.utime(b, ns=(a.stat().st_mtime_ns, a.stat().st_mtime_ns))
        self.assertEqual(self.plan(thorough=False).rows[0].status, "same")
        self.assertEqual(self.plan(thorough=True).rows[0].status, "different")

    def test_overwrite_is_backed_up_and_journalled(self):
        self.put(self.left, "movie", b"new")
        self.put(self.right, "movie", b"old")
        result = self.run_plan(self.plan())
        path = Path(result["recovery"])
        self.assertEqual((self.right / "movie").read_bytes(), b"new")
        self.assertEqual((path / "overwritten/right/movie").read_bytes(), b"old")
        self.assertIn('"event": "finished"', (path / "journal.jsonl").read_text())

    def test_unwritable_recovery_is_reported_before_recheck_or_file_actions(self):
        self.put(self.left, "new")
        self.put(self.right, "old")
        plan = self.plan()
        before = e.scan(self.left, True), e.scan(self.right, True)
        denied = PermissionError(13, 'Permission denied', str(self.recovery))
        with patch.object(e.tempfile, 'TemporaryFile', side_effect=denied), patch.object(e, 'revalidate') as recheck:
            with self.assertRaises(e.RecoveryFolderError) as raised:
                self.run_plan(plan)
            recheck.assert_not_called()
        self.assertEqual(raised.exception.path, str(self.recovery))
        self.assertIn('Permission denied', raised.exception.detail)
        self.assertEqual((e.scan(self.left, True), e.scan(self.right, True)), before)
        self.assertFalse((self.recovery / plan.run_id).exists())

    def test_inaccessible_recovery_parent_reports_configured_full_path(self):
        self.put(self.left, 'new')
        plan = self.plan()
        location = self.base / 'disconnected-drive' / 'Geïsoleerd'
        original = Path.resolve
        def resolve(path, *args, **kwargs):
            if path == location:
                raise PermissionError(13, 'Permission denied', str(location.parent))
            return original(path, *args, **kwargs)
        with patch.object(Path, 'resolve', resolve):
            with self.assertRaises(e.RecoveryFolderError) as raised:
                e.execute(plan, location)
        self.assertEqual(raised.exception.path, str(location))
        self.assertFalse((self.right / 'new').exists())

    def test_recovery_path_that_is_a_file_has_specific_folder_error(self):
        self.put(self.left, 'new')
        self.recovery.write_bytes(b'existing file')
        with self.assertRaises(e.RecoveryFolderError):
            self.run_plan(self.plan())
        self.assertEqual(self.recovery.read_bytes(), b'existing file')
        self.assertFalse((self.right / 'new').exists())

    def test_recovery_probe_is_removed_and_actual_copy_still_works(self):
        self.put(self.left, 'new')
        result = self.run_plan(self.plan())
        self.assertEqual((self.right / 'new').read_bytes(), b'hello')
        self.assertEqual([p.name for p in self.recovery.iterdir()], [Path(result['recovery']).name])

    def test_source_access_error_is_not_mislabelled_as_recovery_error(self):
        self.put(self.left, 'new')
        plan = self.plan()
        with patch.object(e, '_root_identity', side_effect=PermissionError(13, 'Source denied', str(self.left))):
            with self.assertRaises(PermissionError):
                self.run_plan(plan)
        self.assertFalse(self.recovery.exists())

    def test_read_only_source_and_empty_directories(self):
        path = self.put(self.left, "folder/file")
        (self.left / "empty").mkdir()
        path.chmod(0o444)
        self.left.chmod(0o555)
        try:
            self.run_plan(self.plan())
            self.assertEqual((self.right / "folder/file").read_bytes(), b"hello")
            self.assertTrue((self.right / "empty").is_dir())
        finally:
            self.left.chmod(0o755)

    def test_reverse_needs_explicit_unlock(self):
        self.put(self.right, "file")
        plan = self.plan(mode="right_to_left")
        with self.assertRaisesRegex(e.SafetyError, "unlocked"):
            self.run_plan(plan)
        self.assertFalse((self.left / "file").exists())
        self.run_plan(plan, allow_left_changes=True)
        self.assertTrue((self.left / "file").exists())

    def test_two_way_adds_both_and_leaves_conflict_until_chosen(self):
        self.put(self.left, "left")
        self.put(self.right, "right")
        self.put(self.left, "conflict", b"aaaa")
        self.put(self.right, "conflict", b"bbbb")
        plan = self.plan(mode="two_way")
        conflict = next(r for r in plan.rows if r.path == "conflict")
        self.assertEqual(conflict.action, "skip")
        conflict.action = "copy_left"
        self.run_plan(plan, allow_left_changes=True)
        self.assertEqual((self.left / "conflict").read_bytes(), b"bbbb")
        self.assertTrue((self.right / "left").exists())
        self.assertTrue((self.left / "right").exists())

    def test_source_write_action_is_rejected_even_if_plan_tampered(self):
        self.put(self.left, "file", b"left")
        self.put(self.right, "file", b"right")
        plan = self.plan()
        plan.rows[0].action = "copy_left"
        with self.assertRaisesRegex(e.SafetyError, "protected"):
            self.run_plan(plan)

    def test_deleted_source_is_not_interpreted_as_cleanup(self):
        path = self.put(self.left, "file")
        self.put(self.right, "old")
        plan = self.plan()
        next(r for r in plan.rows if r.path == "old").action = "delete"
        path.unlink()
        with self.assertRaises(e.SafetyError):
            self.run_plan(plan, allow_permanent_delete=True)
        self.assertTrue((self.right / "old").exists())

    def test_empty_source_blocks_cleanup(self):
        self.put(self.right, "old")
        plan = self.plan()
        plan.rows[0].action = "isolate"
        with self.assertRaisesRegex(e.SafetyError, "Source contains no"):
            self.run_plan(plan)
        self.assertTrue((self.right / "old").exists())

    def test_delete_needs_confirmation_and_makes_no_recovery_copy(self):
        self.put(self.left, "keep")
        self.put(self.right, "old")
        plan = self.plan()
        next(r for r in plan.rows if r.path == "old").action = "delete"
        with self.assertRaisesRegex(e.SafetyError, "confirmation"):
            self.run_plan(plan)
        result = self.run_plan(plan, allow_permanent_delete=True)
        self.assertFalse((self.right / "old").exists())
        self.assertFalse((Path(result["recovery"]) / "isolated").exists())

    def test_source_cannot_be_deleted_as_extra(self):
        self.put(self.left, "file")
        plan = self.plan()
        plan.rows[0].action = "delete"
        with self.assertRaisesRegex(e.SafetyError, "destination-only"):
            self.run_plan(plan, allow_permanent_delete=True)

    def test_source_and_target_nested_overlap_rejected(self):
        child = self.left / "backup"
        child.mkdir()
        for a, b in ((self.left, child), (child, self.left), (self.left, self.left)):
            with self.assertRaises(e.SafetyError):
                e.compare(a, b)

    def test_recovery_cannot_be_inside_either_root(self):
        self.put(self.left, "file")
        for path in (self.left / "history", self.right / "history", self.base):
            with self.assertRaises(e.SafetyError):
                e.execute(self.plan(), path)
        self.assertFalse((self.right / "file").exists())

    def test_recovery_symlink_rejected(self):
        real = self.base / "real-history"
        real.mkdir()
        link = self.base / "linked-history"
        link.symlink_to(real, target_is_directory=True)
        self.put(self.left, "file")
        with self.assertRaisesRegex(e.SafetyError, "symbolic"):
            e.execute(self.plan(), link)

    def test_symlink_source_and_target_are_never_followed(self):
        external = self.base / "outside"
        external.mkdir()
        self.put(external, "secret")
        (self.left / "link").symlink_to(external, target_is_directory=True)
        self.put(self.left, "real")
        (self.right / "real").symlink_to(external / "secret")
        plan = self.plan()
        self.assertTrue(all(r.status == "unsupported" for r in plan.rows))
        self.assertEqual((external / "secret").read_bytes(), b"hello")

    def test_target_directory_swapped_for_symlink_blocks_all_writes(self):
        self.put(self.left, "folder/file", b"new")
        self.put(self.right, "folder/file", b"old")
        plan = self.plan()
        (self.right / "folder").rename(self.base / "original")
        external = self.base / "outside"
        external.mkdir()
        (self.right / "folder").symlink_to(external, target_is_directory=True)
        with self.assertRaises(e.SafetyError):
            self.run_plan(plan)
        self.assertFalse((external / "file").exists())

    def test_parent_fd_never_follows_links_or_traversal(self):
        (self.right / "link").symlink_to(self.left, target_is_directory=True)
        for rel in ("../escape", "/absolute", "link/file"):
            with self.assertRaises((e.SafetyError, OSError)):
                with e.parent_fd(self.right, rel):
                    self.fail("unsafe traversal allowed")

    def test_changed_file_after_scan_rejected(self):
        path = self.put(self.left, "file")
        plan = self.plan()
        path.write_bytes(b"changed")
        with self.assertRaises(e.SafetyError):
            self.run_plan(plan)
        self.assertFalse((self.right / "file").exists())

    def test_replaced_root_rejected(self):
        self.put(self.left, "file")
        plan = self.plan()
        self.right.rename(self.base / "old-usb")
        self.right.mkdir()
        with self.assertRaises(e.SafetyError):
            self.run_plan(plan)
        self.assertEqual(list(self.right.iterdir()), [])

    def test_cancel_before_execution_preserves_both(self):
        self.put(self.left, "file")
        plan = self.plan()
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(e.SafetyError):
            self.run_plan(plan, cancel=cancel)
        self.assertFalse((self.right / "file").exists())

    def test_scan_error_propagates_instead_of_inventing_extras(self):
        self.put(self.left, "file")
        with patch.object(e, "scan", side_effect=OSError("network unavailable")):
            with self.assertRaises(OSError):
                self.plan()

    def test_excluded_and_hidden_files(self):
        self.put(self.left, ".hidden")
        self.put(self.left, "folder/movie.tmp")
        self.put(self.left, ".git/config")
        plan = self.plan(excludes=("*.tmp", ".git"))
        self.assertIn(".hidden", {r.path for r in plan.rows})
        self.run_plan(plan)
        self.assertTrue((self.right / ".hidden").exists())
        self.assertFalse((self.right / "folder/movie.tmp").exists())
        self.assertFalse((self.right / ".git").exists())

    def test_file_directory_conflict_blocks_descendants(self):
        self.put(self.left, "conflict/file")
        self.put(self.right, "conflict")
        plan = self.plan()
        self.assertEqual({r.status for r in plan.rows}, {"type_conflict", "blocked"})
        self.assertTrue(all(r.action == "skip" for r in plan.rows))

    def test_missing_parent_action_is_rejected_before_writes(self):
        self.put(self.left, "folder/file")
        plan = self.plan()
        next(r for r in plan.rows if r.path == "folder").action = "skip"
        with self.assertRaisesRegex(e.SafetyError, "parent directory"):
            self.run_plan(plan)
        self.assertEqual(list(self.right.iterdir()), [])

    def test_no_space_stops_before_copy(self):
        self.put(self.left, "file")
        with patch.object(e.shutil, "disk_usage", return_value=type("Usage", (), {"free": 0})()):
            with self.assertRaisesRegex(e.SafetyError, "Insufficient"):
                self.run_plan(self.plan())
        self.assertFalse((self.right / "file").exists())

    def test_midcopy_failure_leaves_old_target_and_cleans_temp(self):
        self.put(self.left, "file", b"new")
        self.put(self.right, "file", b"old")
        plan = self.plan()
        with patch.object(e, "hash_fd", side_effect=OSError("readback failed")):
            with self.assertRaisesRegex(e.SafetyError, "readback failed"):
                self.run_plan(plan)
        self.assertEqual((self.right / "file").read_bytes(), b"old")
        self.assertFalse(list(self.right.glob(".syncshallot-*.tmp")))

    def test_isolate_nested_extra_directories(self):
        self.put(self.left, "keep")
        self.put(self.right, "old/sub/file")
        plan = self.plan()
        for row in plan.rows:
            if e.is_extra(plan, row):
                row.action = "isolate"
        result = self.run_plan(plan)
        self.assertFalse((self.right / "old").exists())
        self.assertEqual((Path(result["recovery"]) / "isolated/right/old/sub/file").read_bytes(), b"hello")

    def test_special_file_skipped(self):
        os.mkfifo(self.left / "pipe")
        self.assertEqual(self.plan().rows[0].status, "unsupported")

    def test_unselected_or_excluded_children_block_directory_removal_before_writes(self):
        self.put(self.left, "keep")
        self.put(self.right, "old/excluded.tmp")
        plan = self.plan(excludes=("*.tmp",))
        next(r for r in plan.rows if r.path == "old").action = "isolate"
        with self.assertRaisesRegex(e.SafetyError, "unselected, excluded"):
            self.run_plan(plan)
        self.assertFalse((self.right / "keep").exists())
        self.assertTrue((self.right / "old/excluded.tmp").exists())

    def test_empty_extra_directory_is_removed_without_archiving_empty_tree(self):
        self.put(self.left, "keep")
        (self.right / "empty").mkdir()
        plan = self.plan()
        next(r for r in plan.rows if r.path == "empty").action = "isolate"
        result = self.run_plan(plan)
        self.assertFalse((Path(result["recovery"]) / "isolated/right/empty").exists())
        self.assertFalse((self.right / "empty").exists())

    def test_isolation_keeps_only_parent_directories_of_actual_files(self):
        self.put(self.left, "keep")
        self.put(self.right, "Diversen/Muziek/sub/track.mp3", b"extra music")
        (self.right / "Diversen/Bond van Doorstarters/empty/deeper").mkdir(parents=True)
        (self.right / "Diversen/Muziek/empty sibling").mkdir()
        plan = self.plan()
        for row in plan.rows:
            if e.is_extra(plan, row):
                row.action = "isolate"
        result = self.run_plan(plan)
        isolated = Path(result["recovery"]) / "isolated/right"
        self.assertEqual((isolated / "Diversen/Muziek/sub/track.mp3").read_bytes(), b"extra music")
        self.assertEqual({str(p.relative_to(isolated)) for p in isolated.rglob('*') if p.is_dir()},
                         {"Diversen", "Diversen/Muziek", "Diversen/Muziek/sub"})
        self.assertFalse((self.right / "Diversen").exists())
        self.assertEqual((self.left / "keep").read_bytes(), b"hello")

    def test_atomic_install_never_clobbers_unexpected_file(self):
        self.put(self.right, "staged", b"new")
        self.put(self.right, "unexpected", b"old")
        with e.root_fd(self.right) as fd:
            with self.assertRaises(FileExistsError):
                e.install_new(fd, "staged", "unexpected")
        self.assertEqual((self.right / "unexpected").read_bytes(), b"old")

    def test_unicode_and_newlines_in_names_remain_literal(self):
        self.put(self.left, "Sjalot ♥/film\n4k.mkv")
        self.run_plan(self.plan())
        self.assertTrue((self.right / "Sjalot ♥/film\n4k.mkv").is_file())

    def test_progress_covers_copy_backup_verify_isolation_and_real_completion(self):
        self.put(self.left, "updated", b"new master")
        self.put(self.right, "updated", b"old master")
        self.put(self.right, "extra", b"isolate me")
        plan = self.plan()
        next(r for r in plan.rows if r.path == "extra").action = "isolate"
        updates = []
        self.run_plan(plan, progress=lambda *args: updates.append(args))
        self.assertEqual((updates[0][0], updates[0][1]), (0, 2))
        self.assertEqual((updates[-1][0], updates[-1][1]), (2, 2))
        self.assertEqual([u[0] for u in updates], sorted(u[0] for u in updates))
        details = [u[2] for u in updates if isinstance(u[2], e.FileProgress)]
        self.assertEqual({d.phase for d in details}, {"check", "copy", "backup", "verify", "isolate"})
        for phase in ("copy", "backup", "verify", "isolate"):
            self.assertTrue(any(d.phase == phase and d.processed == d.total and d.total > 0 for d in details))

    def test_progress_does_not_claim_completion_if_copy_fails(self):
        self.put(self.left, "file")
        updates = []
        with patch.object(e, "install_new", side_effect=OSError("cannot install")):
            with self.assertRaises(e.SafetyError):
                self.run_plan(self.plan(), progress=lambda *args: updates.append(args))
        self.assertTrue(updates)
        self.assertTrue(all(completed < total for completed, total, _ in updates))
        self.assertFalse((self.right / "file").exists())


if __name__ == "__main__":
    unittest.main()
