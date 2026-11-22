from dataclasses import replace
from pathlib import Path
import os
import tempfile
import threading
import unittest
from unittest.mock import patch

from syncshallot import duplicates as d, engine as fs


class DuplicateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / 'photos'
        self.root.mkdir()
        self.recovery = self.base / 'recovery'

    def tearDown(self):
        self.temp.cleanup()

    def put(self, path, contents=b'same photo'):
        file = self.root / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(contents)
        return file

    def pair(self):
        self.put('album/keep.jpg')
        self.put('backup/renamed.jpg')
        return d.find(self.root)

    def test_renamed_files_across_subdirectories_are_found_without_changes(self):
        self.put('album/original.jpg')
        copy = self.put('other/a-different-name.png')
        os.utime(copy, (100000, 100000))
        self.put('unique.jpg', b'other data')
        before = fs.scan(self.root, True)
        plan = d.find(self.root)
        self.assertEqual(len(plan.groups), 1)
        self.assertEqual(set(plan.groups[0].files), {'album/original.jpg', 'other/a-different-name.png'})
        self.assertTrue(all(action == 'skip' for action in plan.actions.values()))
        self.assertEqual(fs.scan(self.root, True), before)

    def test_equal_size_different_contents_are_not_duplicates(self):
        self.put('one.jpg', b'abcd')
        self.put('two.jpg', b'efgh')
        self.assertEqual(d.find(self.root).groups, [])

    def test_size_filter_avoids_hashing_unique_sizes(self):
        self.put('a', b'abcd')
        self.put('b', b'abcd')
        self.put('unique', b'longer contents')
        with patch.object(fs, 'hash_fd', wraps=fs.hash_fd) as hashing:
            plan = d.find(self.root)
        self.assertEqual(hashing.call_count, 2)
        self.assertEqual(len(plan.groups), 1)

    def test_hash_collision_is_split_by_actual_content(self):
        self.put('a', b'abcd')
        self.put('b', b'efgh')
        self.put('c', b'abcd')
        with patch.object(fs, 'hash_fd', return_value='forced-collision'):
            plan = d.find(self.root)
        self.assertEqual([set(g.files) for g in plan.groups], [{'a', 'c'}])

    def test_short_reads_do_not_hide_identical_files(self):
        self.put('a', b'1234567890')
        self.put('b', b'1234567890')
        real_read = os.read
        with patch.object(d.os, 'read', side_effect=lambda fd, size: real_read(fd, min(3, size))):
            self.assertEqual(len(d.find(self.root).groups), 1)

    def test_no_default_cleanup_actions(self):
        plan = self.pair()
        with self.assertRaisesRegex(fs.SafetyError, 'No duplicate cleanup'):
            d.execute(plan, self.recovery)
        self.assertFalse(self.recovery.exists())

    def test_isolation_retains_keeper_and_relative_path_with_no_empty_branches(self):
        plan = self.pair()
        duplicate = next(p for p in plan.groups[0].files if p != plan.groups[0].keeper)
        before_keeper = fs.inspect(self.root, plan.groups[0].keeper)
        plan.actions[duplicate] = 'isolate'
        result = d.execute(plan, self.recovery)
        self.assertFalse((self.root / duplicate).exists())
        self.assertEqual((self.root / plan.groups[0].keeper).read_bytes(), b'same photo')
        self.assertEqual(fs.signature(fs.inspect(self.root, plan.groups[0].keeper)), fs.signature(before_keeper))
        self.assertEqual((Path(result['recovery']) / 'isolated' / duplicate).read_bytes(), b'same photo')
        self.assertFalse((Path(result['recovery']) / 'isolated/album').exists())
        self.assertIn('"mode": "duplicates"', (Path(result['recovery']) / 'journal.jsonl').read_text())

    def test_permanent_delete_requires_confirmation(self):
        plan = self.pair()
        duplicate = next(p for p in plan.groups[0].files if p != plan.groups[0].keeper)
        plan.actions[duplicate] = 'delete'
        with self.assertRaisesRegex(fs.SafetyError, 'confirmation'):
            d.execute(plan, self.recovery)
        result = d.execute(plan, self.recovery, allow_permanent_delete=True)
        self.assertFalse((self.root / duplicate).exists())
        self.assertTrue((self.root / plan.groups[0].keeper).exists())
        self.assertFalse((Path(result['recovery']) / 'isolated').exists())

    def test_designated_keeper_cannot_be_removed(self):
        plan = self.pair()
        plan.actions[plan.groups[0].keeper] = 'delete'
        with self.assertRaisesRegex(fs.SafetyError, 'designated file'):
            d.execute(plan, self.recovery, allow_permanent_delete=True)
        self.assertTrue(all((self.root / p).exists() for p in plan.groups[0].files))

    def test_changing_keeper_clears_its_removal_without_arming_previous_keeper(self):
        plan = self.pair()
        group = plan.groups[0]
        old = group.keeper
        other = next(p for p in group.files if p != old)
        plan.actions[other] = 'isolate'
        d.keep(plan, group, other)
        self.assertEqual(group.keeper, other)
        self.assertEqual(plan.actions, {old: 'skip', other: 'skip'})

    def test_missing_or_changed_keeper_blocks_every_action(self):
        for remove in (False, True):
            with self.subTest(remove=remove):
                if not (self.root / 'album/keep.jpg').exists():
                    self.put('album/keep.jpg')
                if not (self.root / 'backup/renamed.jpg').exists():
                    self.put('backup/renamed.jpg')
                plan = d.find(self.root)
                group = plan.groups[0]
                duplicate = next(p for p in group.files if p != group.keeper)
                plan.actions[duplicate] = 'delete'
                if remove:
                    (self.root / group.keeper).unlink()
                else:
                    (self.root / group.keeper).write_bytes(b'changed')
                with self.assertRaises(fs.SafetyError):
                    d.execute(plan, self.recovery, allow_permanent_delete=True)
                self.assertTrue((self.root / duplicate).exists())
                self.put('album/keep.jpg')

    def test_keeper_change_between_operations_preserves_remaining_copy(self):
        for name in ('a', 'b', 'c'):
            self.put(name)
        plan = d.find(self.root)
        plan.actions.update(b='delete', c='delete')
        def progress(completed, total, detail):
            if completed == 1:
                (self.root / 'a').write_bytes(b'changed keeper')
        with self.assertRaises(fs.SafetyError):
            d.execute(plan, self.recovery, progress=progress, allow_permanent_delete=True)
        self.assertFalse((self.root / 'b').exists())
        self.assertEqual((self.root / 'c').read_bytes(), b'same photo')

    def test_modified_or_replaced_duplicate_is_not_deleted(self):
        plan = self.pair()
        duplicate = next(p for p in plan.groups[0].files if p != plan.groups[0].keeper)
        plan.actions[duplicate] = 'delete'
        (self.root / duplicate).unlink()
        (self.root / duplicate).symlink_to(self.root / plan.groups[0].keeper)
        with self.assertRaises(fs.SafetyError):
            d.execute(plan, self.recovery, allow_permanent_delete=True)
        self.assertTrue((self.root / duplicate).is_symlink())

    def test_changed_root_is_rejected(self):
        plan = self.pair()
        plan.actions['backup/renamed.jpg'] = 'isolate'
        self.root.rename(self.base / 'old-root')
        self.root.mkdir()
        with self.assertRaises(fs.SafetyError):
            d.execute(plan, self.recovery)

    def test_symlinks_and_hardlinks_are_reported_and_skipped(self):
        original = self.put('original')
        os.link(original, self.root / 'hardlink')
        (self.root / 'symbolic').symlink_to(original)
        self.put('copy')
        plan = d.find(self.root)
        self.assertEqual(plan.groups, [])
        self.assertEqual(plan.skipped_links, 1)
        self.assertEqual(plan.skipped_hardlinks, 2)

    def test_hidden_files_included_and_exclusions_respected(self):
        self.put('.hidden')
        self.put('copy')
        self.put('excluded/also-copy')
        plan = d.find(self.root, excludes=('excluded',))
        self.assertEqual(set(plan.groups[0].files), {'.hidden', 'copy'})

    def test_empty_files_can_be_deduplicated_but_one_remains(self):
        self.put('a', b'')
        self.put('b', b'')
        plan = d.find(self.root)
        plan.actions['b'] = 'delete'
        d.execute(plan, self.recovery, allow_permanent_delete=True)
        self.assertTrue((self.root / 'a').exists())
        self.assertFalse((self.root / 'b').exists())

    def test_cancellation_preserves_data(self):
        plan = self.pair()
        plan.actions['backup/renamed.jpg'] = 'isolate'
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(fs.Cancelled):
            d.execute(plan, self.recovery, cancel=cancel)
        self.assertTrue(all((self.root / p).exists() for p in plan.groups[0].files))
        self.assertFalse(self.recovery.exists())

    def test_backup_failure_preserves_original_and_keeper(self):
        plan = self.pair()
        plan.actions['backup/renamed.jpg'] = 'isolate'
        with patch.object(fs, '_copy_backup', side_effect=OSError('disk unavailable')):
            with self.assertRaises(fs.SafetyError):
                d.execute(plan, self.recovery)
        self.assertTrue(all((self.root / p).exists() for p in plan.groups[0].files))

    def test_error_after_unlink_reports_actual_completed_count_and_recovery(self):
        import stat
        plan = self.pair()
        plan.actions['backup/renamed.jpg'] = 'isolate'
        real_sync = os.fsync
        def sync(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError('directory sync failed')
            return real_sync(fd)
        with patch.object(d.os, 'fsync', side_effect=sync):
            with self.assertRaises(d.CleanupError) as raised:
                d.execute(plan, self.recovery)
        self.assertEqual(raised.exception.completed, 1)
        self.assertTrue((self.root / plan.groups[0].keeper).exists())
        self.assertEqual((Path(raised.exception.recovery) / 'isolated/backup/renamed.jpg').read_bytes(), b'same photo')

    def test_recovery_must_be_outside_selected_root(self):
        plan = self.pair()
        plan.actions['backup/renamed.jpg'] = 'isolate'
        with self.assertRaises(fs.RecoveryFolderError):
            d.execute(plan, self.root / 'recovery')
        self.assertFalse((self.root / 'recovery').exists())

    def test_forged_nonidentical_member_is_rejected_before_any_cleanup(self):
        plan = self.pair()
        unique = self.put('unique.jpg', b'other data')
        # Rebuild a fresh snapshot to isolate the byte-identity check itself.
        plan = d.find(self.root)
        group = plan.groups[0]
        group.files += ('unique.jpg',)
        plan.actions['unique.jpg'] = 'delete'
        plan.actions['backup/renamed.jpg'] = 'delete'
        with self.assertRaisesRegex(fs.SafetyError, 'no longer identical'):
            d.execute(plan, self.recovery, allow_permanent_delete=True)
        self.assertTrue(unique.exists())
        self.assertTrue((self.root / 'backup/renamed.jpg').exists())
