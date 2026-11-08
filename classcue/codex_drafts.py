"""ClassCue-owned draft handoff shared with the local Codex MCP plugin."""

from pathlib import Path
import hashlib
import json
import os
import sqlite3
from uuid import uuid4

from filelock import FileLock

from classcue.sqlite_connection import ClosingConnection


def connect_workspace(home, root):
    """Update only the installer's ClassCue connection file; running plugins reread it."""
    path = Path(home) / "classcue" / "connection.json"
    if not path.exists():
        return False
    if path.is_symlink() or path.parent.is_symlink() or path.stat().st_size > 128 * 1024:
        raise ValueError("插件连接设置不可用")
    with FileLock(str(path.with_suffix(".lock")), timeout=2):
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        if (not isinstance(value, dict) or value.get("format") != "classcue-codex-connection" or value.get("version") != 1
                or not isinstance(value.get("roots"), list) or not value["roots"]
                or len(value["roots"]) > 32
                or any(not isinstance(p, str) or not Path(p).is_absolute() for p in value["roots"])):
            raise ValueError("插件连接设置格式无效")
        selected = str(Path(root).resolve())
        roots = [selected, *[p for p in value["roots"]
                             if os.path.normcase(p) != os.path.normcase(selected)]]
        if len(roots) > 32:
            raise ValueError("插件已登记的工作区过多，请检查连接设置")
        if value["roots"] != roots:
            value["roots"] = roots
            temporary = path.with_name(f"connection.{uuid4().hex}.tmp")
            try:
                with temporary.open("x", encoding="utf-8") as output:
                    json.dump(value, output, ensure_ascii=False)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
    return True


def draft_database(root):
    root = Path(root).resolve()
    directory = root / ".classcue-codex"
    directory.mkdir(exist_ok=True)
    if directory.is_symlink() or directory.resolve().parent != root:
        raise ValueError("Codex 插件目录不可用")
    path = directory / "drafts.sqlite"
    if path.is_symlink() or path.resolve().parent != directory.resolve():
        raise ValueError("Codex 插件数据库不可用")
    return path


def register_draft(root, receipt, *, native=False):
    path = draft_database(root)
    context = Path(receipt["context_file"])
    if not context.resolve().is_relative_to(Path(root).resolve()):
        raise ValueError("原题快照不在插件工作区中")
    digest = hashlib.sha256(context.read_bytes()).hexdigest()
    with sqlite3.connect(path, timeout=2, factory=ClosingConnection) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS drafts (
            request_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL UNIQUE,
            context_file TEXT NOT NULL, title TEXT NOT NULL, context_sha256 TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', attempt TEXT, error TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
        db.execute("""INSERT INTO drafts(request_id,thread_id,context_file,title,context_sha256,status)
            VALUES(?,?,?,?,?,?) ON CONFLICT(request_id) DO NOTHING""",
                   (receipt["request_id"], receipt["thread_id"], str(context), receipt["title"], digest,
                    "blocked" if native else "pending"))
        row = db.execute("SELECT thread_id,context_sha256 FROM drafts WHERE request_id=?",
                         (receipt["request_id"],)).fetchone()
        if row != (receipt["thread_id"], digest):
            raise ValueError("原讨论的插件绑定已变化")
        if native:
            # Old running panel clients auto-claim only pending rows.
            db.execute("UPDATE drafts SET status='blocked' WHERE request_id=? AND status='pending'",
                       (receipt["request_id"],))
    return draft_status(root, receipt["request_id"])


def draft_status(root, request_id):
    path = Path(root) / ".classcue-codex" / "drafts.sqlite"
    if not path.is_file():
        return {"status": "pending"}
    if path.is_symlink() or path.resolve().parent.parent != Path(root).resolve():
        raise ValueError("Codex 插件数据库不可用")
    with sqlite3.connect(path, timeout=2, factory=ClosingConnection) as db:
        row = db.execute("SELECT status FROM drafts WHERE request_id=?", (request_id,)).fetchone()
    return {"status": row[0] if row else "pending"}
