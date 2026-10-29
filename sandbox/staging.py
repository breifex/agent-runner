"""Moving a message's files into a throw-away directory for one run, and bringing the results back safely.

The sandbox only ever sees a COPY (stage/work). Nothing it writes can reach the real workspace until validate_tree() has checked the
copy: no links, devices or sockets, no odd names, bounded size and count. Only then is it synced back."""
from __future__ import annotations

import io
import os
import shutil
import stat
import tarfile
import tempfile
from pathlib import Path

from sandbox.policy import LIMITS
from workspace_manager.workspace import MessageWorkspace

SYSTEM_DIRS = (".sbx", ".skills")   # created by us, never copied back
STAGE_ROOT = Path(os.getenv("SANDBOX_TMP", "") or tempfile.gettempdir()) / "agent-sandbox"


class StageError(Exception):
    """Output that must not be copied back. The message explains why (shown to admins, summarised to the model)."""


def new_stage() -> Path:
    STAGE_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix="run-", dir=STAGE_ROOT))
    os.chmod(stage, 0o700)
    return stage


def _open_up(path: Path) -> None:
    """When the server runs as root the container user is unprivileged, so the work tree must be writable by it."""
    if os.getuid() == 0:
        for p in [path, *path.rglob("*")]:
            if p.is_symlink():
                continue
            os.chmod(p, 0o777 if p.is_dir() else 0o666)


def stage_in(ws: MessageWorkspace, stage: Path, extra_files: dict[str, str], bare: bool = False) -> Path:
    """Copy workspace/ and assets/ into stage/work, add our own files under .sbx/ and .skills/."""
    work = stage / "work"
    work.mkdir()
    if not bare:  # a bare stage holds only what the caller adds (used to convert one file for preview)
        _copy_tree(ws.workspace, work)
        if ws.assets.is_dir():
            _copy_tree(ws.assets, work / "assets")
    for rel, content in extra_files.items():
        target = work / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _open_up(work)
    return work


def _copy_tree(src: Path, dst: Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    if not src.is_dir():
        return
    for p in src.rglob("*"):
        if p.is_symlink() or not (p.is_file() or p.is_dir()):
            continue  # never carry links or special files into a sandbox
        target = dst / p.relative_to(src)
        if p.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, target, follow_symlinks=False)


def tree_size(work: Path) -> tuple[int, int]:
    total = count = 0
    for dirpath, _dirs, files in os.walk(work):
        for name in files:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                pass
            count += 1
    return total, count


def validate_tree(work: Path) -> list[Path]:
    """Return the files (relative paths) that may be copied back, or raise StageError."""
    results: list[Path] = []
    total = 0
    for dirpath, dirs, files in os.walk(work):
        rel_dir = Path(dirpath).relative_to(work)
        dirs[:] = [d for d in dirs if not d.startswith(".")]  # scratch and tool profiles (.config, .sbx, ...) are never copied back, so they are not inspected
        if rel_dir.parts and rel_dir.parts[0] == "assets":
            dirs[:] = []
            continue
        if len(rel_dir.parts) > 8:
            raise StageError("The output has folders nested too deeply.")
        for name in list(dirs) + files:
            mode = os.lstat(os.path.join(dirpath, name)).st_mode
            if stat.S_ISLNK(mode):
                raise StageError("The output contains a symbolic link.")
            if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                raise StageError("The output contains a special file (device, socket or pipe).")
            if any(ord(c) < 32 or c in "\\:*?\"<>|`" for c in name) or len(name) > 120:
                raise StageError("The output contains a file with an unsafe name.")
        for name in files:
            rel = rel_dir / name
            if not rel_dir.parts and (name.startswith(".") or name in SYSTEM_DIRS):
                continue  # hidden top-level files are scratch, not results
            if any(part.startswith(".") for part in rel.parts):
                continue
            size = os.lstat(os.path.join(dirpath, name)).st_size
            if size > LIMITS.max_file_bytes:
                raise StageError(f"'{rel.as_posix()}' is larger than {LIMITS.max_file_bytes // (1024 * 1024)} MB.")
            total += size
            results.append(rel)
            if len(results) > LIMITS.max_files:
                raise StageError("The run created too many files.")
    if total > LIMITS.max_work_bytes:
        raise StageError("The run's output is too large.")
    return results


def sync_back(work: Path, ws: MessageWorkspace, files: list[Path]) -> list[str]:
    """Make ws.workspace match the validated sandbox output. Returns the relative names that were added, changed or removed."""
    changed: list[str] = []
    keep = {f.as_posix() for f in files}
    existing = {p.relative_to(ws.workspace).as_posix(): p for p in ws.workspace.rglob("*") if p.is_file() and not p.is_symlink()} if ws.workspace.is_dir() else {}
    for rel, path in existing.items():
        if rel not in keep:
            path.unlink(missing_ok=True)
            changed.append(rel)
    for rel in files:
        src, dst = work / rel, ws.workspace / rel
        if dst.is_symlink():
            dst.unlink()
        if not dst.exists() or dst.stat().st_size != src.stat().st_size or dst.read_bytes() != src.read_bytes():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst, follow_symlinks=False)
            changed.append(rel.as_posix())
    for d in sorted((p for p in ws.workspace.rglob("*") if p.is_dir()), reverse=True):  # tidy folders that became empty
        try:
            d.rmdir()
        except OSError:
            pass
    return changed


# ---- tar transport, used by the remote runner
def tar_dir(work: Path) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for p in sorted(work.rglob("*")):
            if p.is_symlink() or not (p.is_file() or p.is_dir()):
                continue
            tar.add(p, arcname=p.relative_to(work).as_posix(), recursive=False)
    return buf.getvalue()


def untar_to(data: bytes, dest: Path, max_bytes: int) -> None:
    """Extract a tar we received. Rejects absolute paths, '..', links and special files, and bounds the total size."""
    total = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member in tar:
            name = Path(member.name)
            if name.is_absolute() or ".." in name.parts or not (member.isfile() or member.isdir()):
                raise StageError("The transferred archive contains an unsafe entry.")
            total += member.size
            if total > max_bytes:
                raise StageError("The transferred archive is too large.")
            tar.extract(member, dest, filter="data")
