"""Find byte-identical regular files within one tree and explicitly remove extra copies.

Size narrows candidates, SHA-256 groups them, and byte comparisons verify equality.
No mutation takes place while scanning. Every cleanup retains a designated keeper.
"""
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
import json
import os
from pathlib import Path
import shutil
import stat
import time
import uuid

from . import engine as fs


class CleanupError(fs.SafetyError):
    def __init__(self, error, completed, total, recovery):
        self.completed, self.total = completed, total
        self.recovery = str(recovery)
        super().__init__(f"{error}\nCompleted: {completed}/{total}\nRecovery: {self.recovery}")


@dataclass
class Group:
    digest: str
    size: int
    files: tuple[str, ...]
    keeper: str


@dataclass
class Plan:
    root: Path
    root_signature: tuple
    entries: dict
    groups: list[Group]
    excludes: tuple[str, ...] = ()
    skipped_links: int = 0
    skipped_special: int = 0
    skipped_hardlinks: int = 0
    actions: dict[str, str] = field(default_factory=dict)
    run_id: str = field(default_factory=lambda: time.strftime("duplicates-%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8])


@contextmanager
def checked_file(root, relative, entry):
    with fs.parent_fd(root, relative) as (parent, name):
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or fs.signature(info) != entry.sig:
                raise fs.SafetyError(f"File changed or is no longer an independent regular file: {relative}")
            yield fd
            if fs.signature(os.fstat(fd)) != entry.sig:
                raise fs.SafetyError(f"File changed during verification: {relative}")
        finally:
            os.close(fd)


def byte_equal(root, first, second, entries, cancel=None, progress=None):
    a, b = entries[first], entries[second]
    if a.size != b.size:
        return False
    with checked_file(root, first, a) as left, checked_file(root, second, b) as right:
        processed = 0
        if progress:
            progress(fs.FileProgress("duplicate_check", second, 0, b.size))
        while True:
            fs.checkpoint(cancel)
            block_a, block_b = _read_block(left, cancel), _read_block(right, cancel)
            if block_a != block_b:
                return False
            if not block_a:
                return True
            processed += len(block_a)
            if progress:
                progress(fs.FileProgress("duplicate_check", second, processed, b.size))


def _read_block(fd, cancel):
    chunks = []
    remaining = 1024 * 1024
    while remaining:
        fs.checkpoint(cancel)
        chunk = os.read(fd, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b''.join(chunks)


def _root_unchanged(plan, full=False):
    info = fs.signature(plan.root.stat())
    current = info if full else info[:2]
    expected = plan.root_signature if full else plan.root_signature[:2]
    if current != expected:
        raise fs.SafetyError("The selected directory changed or its drive disconnected. Scan again")


def revalidate(plan, cancel=None, progress=None):
    _root_unchanged(plan, full=True)
    if progress:
        progress(fs.FileProgress('check', ''))
    current = fs.scan(plan.root, False, plan.excludes, cancel,
                      (lambda path: progress(fs.FileProgress('check', path))) if progress else None)
    if current.keys() != plan.entries.keys() or any(current[p].sig != plan.entries[p].sig for p in current):
        raise fs.SafetyError("Files changed after the duplicate scan. Scan again")
    _root_unchanged(plan, full=True)


def find(root, excludes=(), cancel=None, progress=None):
    root = Path(root).expanduser().resolve(strict=True)
    if not root.is_dir() or root == Path('/'):
        raise fs.SafetyError("Choose an existing directory other than the filesystem root")
    original = fs.signature(root.stat())
    entries = fs.scan(root, False, excludes, cancel, progress)
    plan = Plan(root, original, entries, [], tuple(excludes))
    sizes = defaultdict(list)
    for path, entry in entries.items():
        fs.checkpoint(cancel)
        if entry.kind == 'link':
            plan.skipped_links += 1
        elif entry.kind == 'special':
            plan.skipped_special += 1
        elif entry.kind == 'file':
            info = fs.inspect(root, path)
            if info is None or fs.signature(info) != entry.sig:
                raise fs.SafetyError(f"File changed during scan: {path}")
            if info.st_nlink != 1:
                plan.skipped_hardlinks += 1
            else:
                sizes[entry.size].append(path)
    candidates = sorted(path for paths in sizes.values() if len(paths) > 1 for path in paths)
    hashes = defaultdict(list)
    for index, path in enumerate(candidates):
        entry = entries[path]
        def report(detail):
            if progress:
                progress(index, len(candidates), detail)
        report(fs.FileProgress('duplicate_hash', path, 0, entry.size))
        with checked_file(root, path, entry) as fd:
            digest = fs.hash_fd(fd, cancel, lambda n: report(fs.FileProgress('duplicate_hash', path, n, entry.size)))
        entries[path] = replace(entry, digest=digest)
        hashes[(entry.size, digest)].append(path)
        if progress:
            progress(index + 1, len(candidates), path)
    # Even a SHA-256 collision must never turn a unique file into a removable duplicate.
    possible = [paths for paths in hashes.values() if len(paths) > 1]
    total = sum(len(paths) - 1 for paths in possible)
    done = 0
    for paths in possible:
        buckets = []
        for path in sorted(paths):
            placed = False
            for bucket in buckets:
                callback = (lambda detail: progress(done, total, detail)) if progress else None
                if byte_equal(root, bucket[0], path, entries, cancel, callback):
                    bucket.append(path)
                    placed = True
                    break
            if not placed:
                buckets.append([path])
            else:
                done += 1
        for bucket in buckets:
            if len(bucket) > 1:
                entry = entries[bucket[0]]
                plan.groups.append(Group(entry.digest, entry.size, tuple(bucket), bucket[0]))
    plan.groups.sort(key=lambda group: group.files[0])
    plan.actions = {path: 'skip' for group in plan.groups for path in group.files}
    revalidate(plan, cancel, (lambda detail: progress(None, None, detail)) if progress else None)
    return plan


def keep(plan, group, path):
    if not any(g is group for g in plan.groups) or path not in group.files:
        raise fs.SafetyError("The kept file must belong to this duplicate group")
    group.keeper = path
    plan.actions[path] = 'skip'


def validate_actions(plan):
    members = {}
    for group in plan.groups:
        if len(group.files) < 2 or group.keeper not in group.files:
            raise fs.SafetyError("Each duplicate group needs a file to keep")
        for path in group.files:
            if path in members or path not in plan.entries or plan.entries[path].kind != 'file':
                raise fs.SafetyError("Invalid duplicate group")
            members[path] = group
        if plan.actions.get(group.keeper, 'skip') != 'skip':
            raise fs.SafetyError("The designated file to keep cannot be isolated or deleted")
    selected = []
    for path, action in plan.actions.items():
        if path not in members or action not in ('skip', 'isolate', 'delete'):
            raise fs.SafetyError("Invalid duplicate action")
        if action != 'skip':
            selected.append((path, action, members[path]))
    return sorted(selected, key=lambda item: item[0])


class Archive:
    def __init__(self, base, plan):
        _root_unchanged(plan)
        base = fs.validate_recovery_storage(base, (plan.root,), (plan.root_signature,), (plan.entries,))
        self.path = base / plan.run_id
        self.log = None
        try:
            self.path.mkdir(mode=0o700)
            self.log = open(self.path / 'journal.jsonl', 'x', encoding='utf-8')
            self.record(event='start', mode='duplicates', root=str(plan.root),
                        keepers=[{'path': g.keeper, 'sha256': g.digest} for g in plan.groups])
        except OSError as error:
            if self.log is not None:
                try:
                    self.log.close()
                except OSError:
                    pass
            raise fs.RecoveryFolderError(base, error) from error

    def record(self, **values):
        self.log.write(json.dumps(values, ensure_ascii=True) + '\n')
        self.log.flush()
        os.fsync(self.log.fileno())


def execute(plan, recovery_base, cancel=None, progress=None, allow_permanent_delete=False):
    selected = validate_actions(plan)
    if not selected:
        raise fs.SafetyError("No duplicate cleanup actions selected")
    if any(action == 'delete' for _, action, _ in selected) and not allow_permanent_delete:
        raise fs.SafetyError("Permanent deletion needs explicit confirmation")
    fs.checkpoint(cancel)
    _root_unchanged(plan)
    fs.validate_recovery_storage(recovery_base, (plan.root,), (plan.root_signature,), (plan.entries,))
    if progress:
        progress(0, len(selected), fs.FileProgress('check', ''))
    revalidate(plan, cancel, (lambda detail: progress(0, len(selected), detail)) if progress else None)
    # Verify every selected pair before starting any file actions.
    for path, _, group in selected:
        callback = (lambda detail: progress(0, len(selected), detail)) if progress else None
        if not byte_equal(plan.root, group.keeper, path, plan.entries, cancel, callback):
            raise fs.SafetyError(f"File contents are no longer identical: {path}")
    archive = Archive(recovery_base, plan)
    completed = 0
    try:
        needed = sum(plan.entries[p].size for p, action, _ in selected if action == 'isolate')
        if shutil.disk_usage(archive.path).free < needed + 1024 * 1024:
            raise fs.SafetyError("Insufficient free space in the recovery folder")
        for path, action, group in selected:
            fs.checkpoint(cancel)
            _root_unchanged(plan)
            callback = (lambda detail: progress(completed, len(selected), detail)) if progress else None
            if not byte_equal(plan.root, group.keeper, path, plan.entries, cancel, callback):
                raise fs.SafetyError(f"Kept file and duplicate differ: {path}")
            archive.record(event='operation_pending', path=path, action=action, kept=group.keeper)
            if action == 'isolate':
                target = archive.path / 'isolated' / path
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                archive.record(event='backup_pending', original=str(plan.root / path), recovery=str(target))
                fs._copy_backup(plan.root, path, plan.entries[path], target, cancel, callback, 'isolate')
                archive.record(event='backup_complete', original=str(plan.root / path), recovery=str(target))
            fs.checkpoint(cancel)
            fs._expect(plan.root, group.keeper, plan.entries[group.keeper])
            fs._expect(plan.root, path, plan.entries[path])
            with fs.parent_fd(plan.root, path) as (fd, name):
                os.unlink(name, dir_fd=fd)
                completed += 1
                os.fsync(fd)
            archive.record(event='operation_complete', path=path, action=action, kept=group.keeper)
            if progress:
                progress(completed, len(selected), path)
        archive.record(event='finished', completed=completed)
        return {'completed': completed, 'recovery': str(archive.path)}
    except Exception as error:
        try:
            archive.record(event='stopped', completed=completed, error=str(error))
        except OSError:
            pass
        raise CleanupError(error, completed, len(selected), archive.path) from error
    finally:
        try:
            archive.log.close()
        except OSError:
            pass
