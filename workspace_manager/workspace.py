"""Session-root file workspace.

    workspace/<user_id>/<session_id>/
        .git/                         Git metadata for the session snapshot
        workspace/                    canonical agent working tree
        messages/<message_id>/assets/ uploaded files for one message
        messages/<message_id>/artifact/ downloadable results for one message

Every path component is validated, so a user id, session id, message id or file name can never point outside this tree.
"""
from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import shutil
from pathlib import Path

WORKSPACE_ROOT = Path(os.getenv("WORKSPACE_ROOT", "") or Path(__file__).resolve().parents[1] / "workspace")
SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
MAX_FILE_BYTES = int(os.getenv("WORKSPACE_MAX_FILE_BYTES", "") or 10 * 1024 * 1024)
MAX_WORKSPACE_BYTES = int(os.getenv("WORKSPACE_MAX_BYTES", "") or 100 * 1024 * 1024)
MAX_FILES = 500
_BAD_CHARS = re.compile(r"[^\w.\- ()+,@]+", re.UNICODE)

# The browser must never execute agent-written files from our origin, so these are served as plain text unless downloaded.
_MIME_OVERRIDES = {".md": "text/markdown", ".csv": "text/csv", ".json": "application/json", ".yml": "text/yaml", ".yaml": "text/yaml",
                   ".ts": "text/plain", ".tsx": "text/plain", ".jsx": "text/plain", ".py": "text/x-python", ".svg": "image/svg+xml"}


class WorkspaceError(Exception):
    """Raised with a message that is safe to show to the model and the user."""


def guess_mime(name: str) -> str:
    suffix = Path(name).suffix.lower()
    return _MIME_OVERRIDES.get(suffix) or mimetypes.guess_type(name)[0] or "application/octet-stream"


def safe_filename(name: str) -> str:
    """Last path component only, no control or odd characters, never hidden, never empty."""
    name = (name or "").replace("\\", "/").split("/")[-1].strip()
    name = _BAD_CHARS.sub("_", name).strip(" .")
    name = name[:120] or "file"
    return name if not name.startswith(".") else "_" + name.lstrip(".")


def _sha(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def session_dir(user_id: int, session_id: str) -> Path:
    if not isinstance(user_id, int) or user_id < 0 or not SAFE_ID.fullmatch(session_id or ""):
        raise WorkspaceError("Invalid session")
    return WORKSPACE_ROOT / str(user_id) / session_id


class MessageWorkspace:
    def __init__(self, user_id: int, session_id: str, message_id: str) -> None:
        if not SAFE_ID.fullmatch(message_id or ""):
            raise WorkspaceError("Invalid message")
        self.user_id, self.session_id, self.message_id = user_id, session_id, message_id
        self.session_root = session_dir(user_id, session_id)
        self.message_root = self.session_root / "messages" / message_id
        # Keep the historical `root` diagnostic path stable; persisted message
        # data itself lives under messages/<message_id> in the session Git root.
        self.root = self.session_root / message_id
        self.assets = self.message_root / "assets"
        self.workspace = self.session_root / "workspace"
        self.artifact = self.message_root / "artifact"

    def ensure(self) -> "MessageWorkspace":
        for d in (self.assets, self.workspace, self.artifact):
            d.mkdir(parents=True, exist_ok=True)
        return self

    # ------------------------------------------------------------------ uploads
    def save_asset(self, name: str, data: bytes) -> str:
        """Store an uploaded file under its own (sanitised) name; duplicates get a numeric suffix. Returns the stored name."""
        self.assets.mkdir(parents=True, exist_ok=True)
        base = safe_filename(name)
        stem, suffix = os.path.splitext(base)
        candidate, n = base, 1
        while (self.assets / candidate).exists():
            n += 1
            candidate = f"{stem}-{n}{suffix}"
        (self.assets / candidate).write_bytes(data)
        return candidate

    def asset_names(self) -> list[str]:
        return sorted(p.name for p in self.assets.iterdir() if p.is_file()) if self.assets.is_dir() else []

    # ---------------------------------------------------------------- snapshots
    def snapshot(self) -> dict[str, str]:
        """{relative path: content hash} of everything in the workspace folder, to tell what the agent changed."""
        out: dict[str, str] = {}
        if self.workspace.is_dir():
            for p in self.workspace.rglob("*"):
                if p.is_file() and not p.is_symlink():
                    out[p.relative_to(self.workspace).as_posix()] = _sha(p)
        return out

    def size(self) -> int:
        total = 0
        for d in (self.workspace,):
            if d.is_dir():
                total += sum(p.stat().st_size for p in d.rglob("*") if p.is_file())
        return total

    # ------------------------------------------------------------------ publish
    def publish(self, baseline: dict[str, str]) -> list[dict]:
        """Copy files the agent created or changed from workspace/ into artifact/ and describe them.
        `baseline` is the snapshot taken after staging inputs, so an untouched copy of an upload is not reported as an update."""
        self.artifact.mkdir(parents=True, exist_ok=True)
        results: list[dict] = []
        for rel, digest in sorted(self.snapshot().items()):
            before = baseline.get(rel)
            if before == digest:
                continue
            src = self.workspace / rel
            dst = self.artifact / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            results.append({"name": rel, "size": dst.stat().st_size, "mimeType": guess_mime(rel), "action": "created" if before is None else "updated"})
            if len(results) >= MAX_FILES:
                break
        return results

    def list_artifacts(self) -> list[dict]:
        if not self.artifact.is_dir():
            return []
        return [{"name": p.relative_to(self.artifact).as_posix(), "size": p.stat().st_size, "mimeType": guess_mime(p.name)}
                for p in sorted(self.artifact.rglob("*")) if p.is_file() and not any(part.startswith(".") for part in p.relative_to(self.artifact).parts)]

    def artifact_path(self, name: str) -> Path:
        return _inside(self.artifact, name)


def _inside(base: Path, name: str) -> Path:
    rel = Path(str(name or "").replace("\\", "/").lstrip("/"))
    if not rel.parts or ".." in rel.parts:
        raise WorkspaceError("Invalid file name")
    path = (base / rel).resolve()
    try:
        path.relative_to(base.resolve())
    except ValueError as error:
        raise WorkspaceError("Invalid file name") from error
    return path


# --------------------------------------------------------------- whole session
def session_files(user_id: int, session_id: str) -> list[dict]:
    """Every file the user uploaded or an agent produced in this chat, newest message first."""
    base = session_dir(user_id, session_id)
    if not base.is_dir():
        return []
    items: list[dict] = []
    messages = base / "messages"
    if not messages.is_dir():
        return []
    for message_dir in sorted((d for d in messages.iterdir() if d.is_dir()), key=lambda d: d.stat().st_mtime, reverse=True):
        for area in ("assets", "artifact"):
            folder = message_dir / area
            if folder.is_dir():
                for p in sorted(folder.rglob("*")):
                    if p.is_file() and not any(part.startswith(".") for part in p.relative_to(folder).parts):
                        items.append({"messageId": message_dir.name, "area": area, "name": p.relative_to(folder).as_posix(), "size": p.stat().st_size})
    return items


def copy_into_workspace(ws: MessageWorkspace, message_id: str, area: str, name: str) -> str:
    """Copy one file from this chat (an upload or an earlier result) into the current message's workspace so it can be edited."""
    if area not in ("assets", "artifact") or not SAFE_ID.fullmatch(message_id or ""):
        raise WorkspaceError("Unknown file")
    source = _inside(session_dir(ws.user_id, ws.session_id) / "messages" / message_id / area, name)
    if not source.is_file():
        raise WorkspaceError(f"'{name}' was not found.")
    if source.stat().st_size > MAX_FILE_BYTES:
        raise WorkspaceError("That file is too large to edit here.")
    rel = Path(name).name if area == "assets" else Path(name).as_posix()
    target = _inside(ws.workspace, rel)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return target.relative_to(ws.workspace).as_posix()


def remove_session(user_id: int, session_id: str) -> None:
    try:
        shutil.rmtree(session_dir(user_id, session_id), ignore_errors=True)
    except WorkspaceError:
        pass
