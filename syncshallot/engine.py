"""Filesystem-only engine. No writes during comparison; explicit, stale-checked plans.

Linux dir_fd + O_NOFOLLOW traversal prevents writes through symlink ancestors.
This is a single-user tool, not a security boundary against a hostile local process.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import ctypes
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import stat
import shutil
import threading
import time
import uuid


class SafetyError(RuntimeError):
    pass


class Cancelled(SafetyError):
    pass


def checkpoint(cancel):
    if cancel and cancel.is_set():
        raise Cancelled("cancelled")


def signature(st):
    return (st.st_dev, st.st_ino, st.st_mode, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


@dataclass(frozen=True)
class Entry:
    kind: str
    sig: tuple
    digest: str = ""
    link: str = ""

    @property
    def size(self):
        return self.sig[3]


@dataclass(frozen=True)
class FileProgress:
    """Read-only progress information; byte counts are Python ints, including large media."""
    phase: str
    path: str
    processed: int = 0
    total: int = 0


@dataclass
class Row:
    path: str
    status: str
    left: Entry | None
    right: Entry | None
    action: str = "skip"


@dataclass
class Plan:
    left: Path
    right: Path
    mode: str
    thorough: bool
    roots: tuple
    entries: tuple[dict, dict]
    rows: list[Row]
    excludes: tuple[str, ...] = ()
    run_id: str = field(default_factory=lambda: time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8])


@contextmanager
def root_fd(root):
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        yield fd
    finally:
        os.close(fd)


@contextmanager
def parent_fd(root, relative):
    parts = Path(relative).parts
    if not parts or any(p in ("..", "/", "") for p in parts) or Path(relative).is_absolute():
        raise SafetyError("Invalid relative path")
    with root_fd(root) as base:
        current = os.dup(base)
        try:
            for part in parts[:-1]:
                new = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
                os.close(current)
                current = new
            yield current, parts[-1]
        finally:
            os.close(current)


def inspect(root, rel):
    try:
        with parent_fd(root, rel) as (fd, name):
            return os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def hash_fd(fd, cancel=None, progress=None):
    digest = hashlib.sha256()
    processed = 0
    while True:
        checkpoint(cancel)
        block = os.read(fd, 1024 * 1024)
        if not block:
            return digest.hexdigest()
        digest.update(block)
        processed += len(block)
        if progress:
            progress(processed)


def install_new(parent, temp, name):
    """Linux atomic no-clobber rename, including filesystems without hardlinks (USB FAT/exFAT)."""
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, "renameat2", None)
    if rename is None:
        raise SafetyError("Atomic no-replace rename is unavailable on this Linux system")
    rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    rename.restype = ctypes.c_int
    if rename(parent, os.fsencode(temp), parent, os.fsencode(name), 1) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), name)


def scan(root, thorough, excludes=(), cancel=None, progress=None):
    entries = {}
    def walk(fd, prefix=""):
        # scandir(fd) never follows symbolic directory links.
        with os.scandir(fd) as items:
            for item in items:
                checkpoint(cancel)
                rel = f"{prefix}/{item.name}" if prefix else item.name
                if any(fnmatch.fnmatchcase(rel, p) or fnmatch.fnmatchcase(item.name, p) for p in excludes):
                    continue
                before = item.stat(follow_symlinks=False)
                mode = before.st_mode
                kind = "dir" if stat.S_ISDIR(mode) else "file" if stat.S_ISREG(mode) else "link" if stat.S_ISLNK(mode) else "special"
                digest = ""
                link = ""
                if kind == "file" and thorough:
                    if progress:
                        progress(FileProgress("scan", rel, 0, before.st_size))
                    file_fd = os.open(item.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                    try:
                        if signature(os.fstat(file_fd)) != signature(before):
                            raise SafetyError(f"Changed during scan: {rel}")
                        digest = hash_fd(file_fd, cancel, (lambda count: progress(FileProgress("scan", rel, count, before.st_size))) if progress else None)
                        if signature(os.fstat(file_fd)) != signature(before):
                            raise SafetyError(f"Changed during scan: {rel}")
                    finally:
                        os.close(file_fd)
                if kind == "link":
                    link = os.readlink(item.name, dir_fd=fd)
                entries[rel] = Entry(kind, signature(before), digest, link)
                if progress:
                    progress(rel)
                if kind == "dir":
                    child_fd = os.open(item.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    try:
                        if signature(os.fstat(child_fd)) != signature(before):
                            raise SafetyError(f"Changed during scan: {rel}")
                        walk(child_fd, rel)
                    finally:
                        os.close(child_fd)
                after = os.stat(item.name, dir_fd=fd, follow_symlinks=False)
                if signature(after) != signature(before):
                    raise SafetyError(f"Changed during scan: {rel}")
    with root_fd(root) as fd:
        walk(fd)
    return entries


def roots_valid(left, right):
    left, right = Path(left).expanduser().resolve(strict=True), Path(right).expanduser().resolve(strict=True)
    if not left.is_dir() or not right.is_dir():
        raise SafetyError("Both paths must be existing directories")
    if left == right or left in right.parents or right in left.parents:
        raise SafetyError("Choose separate, non-overlapping directories")
    if left == Path("/") or right == Path("/"):
        raise SafetyError("The filesystem root cannot be synchronized")
    if os.path.samefile(left, right):
        raise SafetyError("Both paths refer to the same directory")
    return left, right


def compare(left, right, mode="left_to_right", thorough=True, excludes=(), cancel=None, progress=None):
    if mode not in ("left_to_right", "right_to_left", "two_way"):
        raise SafetyError("Unknown synchronization mode")
    left, right = roots_valid(left, right)
    roots = (signature(left.stat()), signature(right.stat()))
    a = scan(left, thorough, excludes, cancel, progress)
    b = scan(right, thorough, excludes, cancel, progress)
    if roots != (signature(left.stat()), signature(right.stat())):
        raise SafetyError("A root directory changed during scan")
    # Also catch common bind-mount aliases of nested directories.
    ids = [{e.sig[:2] for e in entries.values() if e.kind == "dir"} | {root[:2]}
           for entries, root in zip((a, b), roots)]
    if ids[0] & ids[1]:
        raise SafetyError("The directories overlap through a mount alias")
    rows = []
    blocked = set()
    for rel in sorted(a.keys() | b.keys()):
        x, y = a.get(rel), b.get(rel)
        action = "skip"
        if any(str(p) in blocked for p in Path(rel).parents):
            status = "blocked"
        elif any(e and e.kind in ("special", "link") for e in (x, y)):
            status = "unsupported"
            blocked.add(rel)
        elif x and y and x.kind != y.kind:
            status = "type_conflict"
            blocked.add(rel)
        elif x and y:
            equal = x.kind == "dir" or (x.size == y.size and (x.digest == y.digest if thorough else abs(x.sig[4] - y.sig[4]) <= 2_000_000_000))
            status = "same" if equal else "different"
            if not equal:
                action = "copy_right" if mode == "left_to_right" else "copy_left" if mode == "right_to_left" else "skip"
        else:
            status = "left_only" if x else "right_only"
            if mode == "two_way" or (x and mode == "left_to_right") or (y and mode == "right_to_left"):
                action = "copy_right" if x else "copy_left"
        rows.append(Row(rel, status, x, y, action))
    return Plan(left, right, mode, thorough, roots, (a, b), rows, tuple(excludes))


def is_extra(plan, row):
    return (plan.mode == "left_to_right" and row.status == "right_only") or (plan.mode == "right_to_left" and row.status == "left_only")


def validate_actions(plan):
    seen = set()
    lookup = {r.path: r for r in plan.rows}
    for row in plan.rows:
        if row.path in seen:
            raise SafetyError("Duplicate operation")
        seen.add(row.path)
        if row.action == "skip":
            continue
        if row.status in ("same", "unsupported", "blocked", "type_conflict"):
            raise SafetyError(f"Blocked operation: {row.path}")
        if row.action in ("isolate", "delete"):
            if not is_extra(plan, row):
                raise SafetyError("Only destination-only entries can be removed")
            source_entries = plan.entries[0 if plan.mode == "left_to_right" else 1]
            if not any(e.kind == "file" for e in source_entries.values()):
                raise SafetyError("Source contains no regular files. Destination cleanup is blocked; check the source and its mount")
            target_root, entry = (plan.right, row.right) if plan.mode == "left_to_right" else (plan.left, row.left)
            if entry.kind == "dir":
                with parent_fd(target_root, row.path) as (fd, name):
                    child_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    try:
                        with os.scandir(child_fd) as children:
                            for child in children:
                                child_path = row.path + "/" + child.name
                                if child_path not in lookup or lookup[child_path].action not in ("isolate", "delete"):
                                    raise SafetyError(f"Directory contains unselected, excluded or unsupported items: {row.path}")
                    finally:
                        os.close(child_fd)
        elif row.action in ("copy_left", "copy_right"):
            if (row.action == "copy_left" and plan.mode == "left_to_right") or (row.action == "copy_right" and plan.mode == "right_to_left"):
                raise SafetyError("The leading directory is protected")
            entry = row.right if row.action == "copy_left" else row.left
            if entry is None or entry.kind not in ("file", "dir"):
                raise SafetyError(f"Invalid copy source: {row.path}")
            target_entries = plan.entries[0 if row.action == "copy_left" else 1]
            for parent in Path(row.path).parents:
                key = str(parent)
                if key != "." and key not in target_entries:
                    if key not in lookup or lookup[key].action != row.action:
                        raise SafetyError(f"Enable the parent directory copy first: {key}")
        else:
            raise SafetyError("Unknown action")


def revalidate(plan, cancel=None):
    roots_valid(plan.left, plan.right)
    if plan.roots != (signature(plan.left.stat()), signature(plan.right.stat())):
        raise SafetyError("A root changed or was disconnected. Scan again")
    for root, expected in zip((plan.left, plan.right), plan.entries):
        current = scan(root, False, plan.excludes, cancel)
        if current.keys() != expected.keys() or any(current[p].sig != expected[p].sig for p in current):
            raise SafetyError("Files changed after comparison. Scan again")


def _expect(root, rel, expected):
    current = inspect(root, rel)
    if (signature(current) if current else None) != (expected.sig if expected else None):
        raise SafetyError(f"Changed since scan: {rel}")


def _root_identity(plan):
    for root, old in zip((plan.left, plan.right), plan.roots):
        st = root.stat()
        if (st.st_dev, st.st_ino) != old[:2]:
            raise SafetyError("A directory was replaced or a drive disconnected")


def _copy(root, destination, rel, source, target, archive, cancel, progress=None):
    _expect(root, rel, source)
    _expect(destination, rel, target)
    with parent_fd(root, rel) as (src_parent, src_name), parent_fd(destination, rel) as (dst_parent, dst_name):
        source_fd = os.open(src_name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=src_parent)
        temp = ".syncshallot-" + uuid.uuid4().hex + ".tmp"
        created = False
        try:
            if signature(os.fstat(source_fd)) != source.sig:
                raise SafetyError(f"Changed since scan: {rel}")
            output_fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dst_parent)
            created = True
            digest = hashlib.sha256()
            processed = 0
            if progress:
                progress(FileProgress("copy", rel, 0, source.size))
            try:
                while True:
                    checkpoint(cancel)
                    chunk = os.read(source_fd, 1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                    processed += len(chunk)
                    view = memoryview(chunk)
                    while view:
                        view = view[os.write(output_fd, view):]
                    if progress:
                        progress(FileProgress("copy", rel, processed, source.size))
                os.fchmod(output_fd, stat.S_IMODE(source.sig[2]) & 0o777)
                os.utime(output_fd, ns=(source.sig[4], source.sig[4]))
                os.fsync(output_fd)
            finally:
                os.close(output_fd)
            if signature(os.fstat(source_fd)) != source.sig or (source.digest and digest.hexdigest() != source.digest):
                raise SafetyError(f"Source changed during copy: {rel}")
            # Read back the staged copy before changing an existing destination.
            verify_fd = os.open(temp, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dst_parent)
            try:
                if progress:
                    progress(FileProgress("verify", rel, 0, source.size))
                if hash_fd(verify_fd, cancel, (lambda count: progress(FileProgress("verify", rel, count, source.size))) if progress else None) != digest.hexdigest():
                    raise SafetyError(f"Copy verification failed: {rel}")
            finally:
                os.close(verify_fd)
            checkpoint(cancel)
            _expect(root, rel, source)
            _expect(destination, rel, target)
            if target:
                archive.backup(destination, rel, target, cancel=cancel, progress=progress)
                _expect(destination, rel, target)
                os.replace(temp, dst_name, src_dir_fd=dst_parent, dst_dir_fd=dst_parent)
            else:
                install_new(dst_parent, temp, dst_name)
            created = False
            os.fsync(dst_parent)
        finally:
            os.close(source_fd)
            if created:
                os.unlink(temp, dir_fd=dst_parent)


class Archive:
    """Private, unique run directory outside both selected roots; JSONL recovery log."""
    def __init__(self, base, plan):
        if not str(base).strip():
            raise SafetyError("Choose a recovery folder outside both directories")
        base = Path(base).expanduser().absolute()
        resolved = base.resolve()
        for root in (plan.left, plan.right):
            if resolved == root or root in resolved.parents or resolved in root.parents:
                raise SafetyError("Recovery folder must be outside both selected directories")
        # Refuse symlink ancestors, including the base itself.
        for part in (base, *base.parents):
            if part.is_symlink():
                raise SafetyError("Recovery folder must not use symbolic links")
        base.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Detect a base mounted inside one of the selected directory trees by alias.
        protected = {s[:2] for s in plan.roots}
        protected |= {e.sig[:2] for entries in plan.entries for e in entries.values() if e.kind == "dir"}
        if any((p.stat().st_dev, p.stat().st_ino) in protected for p in (base, *base.parents)):
            raise SafetyError("Recovery folder overlaps a selected directory through a mount")
        self.path = base / plan.run_id
        self.path.mkdir(mode=0o700)
        self.plan = plan
        self.log = open(self.path / "journal.jsonl", "x", encoding="utf-8")
        self.record(event="start", left=str(plan.left), right=str(plan.right), mode=plan.mode)

    def record(self, **values):
        self.log.write(json.dumps(values, ensure_ascii=True) + "\n")
        self.log.flush()
        os.fsync(self.log.fileno())

    def backup(self, root, rel, entry, category="overwritten", cancel=None, progress=None):
        side = "left" if root == self.plan.left else "right"
        destination = self.path / category / side / rel
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.record(event="backup_pending", original=str(root / rel), recovery=str(destination))
        _copy_backup(root, rel, entry, destination, cancel, progress,
                     "isolate" if category == "isolated" else "backup")
        self.record(event="backup_complete", original=str(root / rel), recovery=str(destination))


def _copy_backup(root, rel, entry, destination, cancel=None, progress=None, phase="backup"):
    # A recovery copy is made and verified before the original can be changed.
    _expect(root, rel, entry)
    with parent_fd(root, rel) as (parent, name):
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            if signature(os.fstat(fd)) != entry.sig:
                raise SafetyError(f"Changed before backup: {rel}")
            digest = hashlib.sha256()
            processed = 0
            if progress:
                progress(FileProgress(phase, rel, 0, entry.size))
            with open(destination, "xb") as out:
                while block := os.read(fd, 1024 * 1024):
                    checkpoint(cancel)
                    digest.update(block)
                    out.write(block)
                    processed += len(block)
                    if progress:
                        progress(FileProgress(phase, rel, processed, entry.size))
                out.flush()
                os.fsync(out.fileno())
            if signature(os.fstat(fd)) != entry.sig:
                raise SafetyError(f"Changed during backup: {rel}")
            with open(destination, "rb") as verify:
                if progress:
                    progress(FileProgress("verify", rel, 0, entry.size))
                if hash_fd(verify.fileno(), cancel, (lambda count: progress(FileProgress("verify", rel, count, entry.size))) if progress else None) != digest.hexdigest():
                    raise SafetyError(f"Recovery copy verification failed: {rel}")
            os.chmod(destination, stat.S_IMODE(entry.sig[2]) & 0o777)
            os.utime(destination, ns=(entry.sig[4], entry.sig[4]))
        finally:
            os.close(fd)
    _expect(root, rel, entry)


def execute(plan, recovery_base, cancel=None, progress=None, allow_permanent_delete=False, allow_left_changes=False):
    validate_actions(plan)
    if plan.mode != "left_to_right" and not allow_left_changes:
        raise SafetyError("Left-side protection must be explicitly unlocked")
    if any(r.action == "delete" for r in plan.rows) and not allow_permanent_delete:
        raise SafetyError("Permanent deletion needs explicit confirmation")
    selected = [r for r in plan.rows if r.action != "skip"]
    if not selected:
        raise SafetyError("No actions selected")
    if progress:
        progress(0, len(selected), FileProgress("check", ""))
    revalidate(plan, cancel)
    # Create directories first, files next, removals deepest first.
    copies = [r for r in selected if r.action.startswith("copy_")]
    copies.sort(key=lambda r: (len(Path(r.path).parts), r.path))
    removals = sorted((r for r in selected if r.action in ("isolate", "delete")), key=lambda r: -len(Path(r.path).parts))
    archive = Archive(recovery_base, plan)
    completed = 0
    try:
        # Budget complete staged copies and recovery data on each filesystem.
        required = {}
        locations = {}
        for row in selected:
            if row.action.startswith("copy_"):
                src, dst, target = (row.left, plan.right, row.right) if row.action == "copy_right" else (row.right, plan.left, row.left)
                if src.kind == "file":
                    device = dst.stat().st_dev
                    locations[device] = dst
                    required[device] = required.get(device, 0) + src.size
                if target and target.kind == "file":
                    device = archive.path.stat().st_dev
                    locations[device] = archive.path
                    required[device] = required.get(device, 0) + target.size
            elif row.action == "isolate":
                entry = row.right if plan.mode == "left_to_right" else row.left
                if entry.kind == "file":
                    device = archive.path.stat().st_dev
                    locations[device] = archive.path
                    required[device] = required.get(device, 0) + entry.size
        for device, size in required.items():
            if shutil.disk_usage(locations[device]).free < size + 1024 * 1024:
                raise SafetyError(f"Insufficient free space: {locations[device]} (need at least {size} bytes plus reserve)")
        for row in copies + removals:
            checkpoint(cancel)
            _root_identity(plan)
            archive.record(event="operation_pending", path=row.path, action=row.action)
            if row.action.startswith("copy_"):
                src, dst, entry, target = (plan.left, plan.right, row.left, row.right) if row.action == "copy_right" else (plan.right, plan.left, row.right, row.left)
                if entry.kind == "dir":
                    # Existing directory timestamps change as children are copied.
                    if target is None:
                        _expect(src, row.path, entry)
                        with parent_fd(dst, row.path) as (fd, name):
                            os.mkdir(name, stat.S_IMODE(entry.sig[2]) | 0o700, dir_fd=fd)
                else:
                    _copy(src, dst, row.path, entry, target, archive, cancel,
                          (lambda text: progress(completed, len(selected), text)) if progress else None)
            else:
                dst, entry = (plan.right, row.right) if plan.mode == "left_to_right" else (plan.left, row.left)
                if entry.kind == "dir":
                    current = inspect(dst, row.path)
                    if current is None or (current.st_dev, current.st_ino) != entry.sig[:2] or not stat.S_ISDIR(current.st_mode):
                        raise SafetyError(f"Directory changed: {row.path}")
                    # rmdir refuses to remove unselected, excluded or new children.
                    with parent_fd(dst, row.path) as (fd, name):
                        os.rmdir(name, dir_fd=fd)
                else:
                    _expect(dst, row.path, entry)
                    if row.action == "isolate":
                        archive.backup(dst, row.path, entry, "isolated", cancel,
                                       (lambda detail: progress(completed, len(selected), detail)) if progress else None)
                    _expect(dst, row.path, entry)
                    with parent_fd(dst, row.path) as (fd, name):
                        os.unlink(name, dir_fd=fd)
            completed += 1
            archive.record(event="operation_complete", path=row.path, action=row.action)
            if progress:
                progress(completed, len(selected), row.path)
        archive.record(event="finished", completed=completed)
        return {"completed": completed, "recovery": str(archive.path)}
    except BaseException as error:
        archive.record(event="stopped", completed=completed, error=str(error))
        raise SafetyError(f"{error}\nCompleted: {completed}/{len(selected)}\nRecovery: {archive.path}") from error
    finally:
        archive.log.close()
