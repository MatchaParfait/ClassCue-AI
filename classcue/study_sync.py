"""Windows-owned learning edit documents and transactional three-way merging."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import re
from uuid import uuid4

from classcue.device_protocol import canonical

FIELDS = set(("id anchor kind origin mode scheme_name scheme_revision start_ms end_ms title note "
              "source_quote trigger_phrase confidence user_start_ms importance removed").split())
UUID_PATTERN = re.compile(r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}")
HASH_PATTERN = re.compile(r"[a-f0-9]{64}")
MODE_PATTERN = re.compile(r"(high_recall|medium_recall|low_recall|strict_exam_intent|question_extraction|custom_[a-zA-Z0-9_-]{1,80})")
MAX_DOCUMENT_BYTES = 4 * 1024**2
NAMES = {"high_recall": "高召回", "medium_recall": "中召回", "low_recall": "低召回",
         "strict_exam_intent": "期末考点", "question_extraction": "视频考题"}


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def anchor(record):
    return digest({**{key: record[key] for key in ("kind", "start_ms", "end_ms", "trigger_phrase")},
                   "scheme": f"custom:{record['scheme_name']}:{record['scheme_revision']}"
                   if record["mode"].startswith("custom_") else record["mode"]})


def _text(value, maximum, minimum=0):
    if not isinstance(value, str) or not minimum <= len(value) <= maximum or minimum and not value.strip():
        raise ValueError("同步文字字段无效")
    return value


def _integer(value, maximum, minimum=0):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("同步整数字段无效")
    return value


def validate(document):
    if not isinstance(document, dict) or set(document) != {
            "format", "version", "video_sha256", "duration_ms", "transcript_digest", "records"}:
        raise ValueError("同步文档字段无效")
    if document["format"] != "classcue-study-sync" or _integer(document["version"], 1, 1) != 1:
        raise ValueError("不支持的学习同步版本")
    for key in ("video_sha256", "transcript_digest"):
        if not HASH_PATTERN.fullmatch(_text(document[key], 64)):
            raise ValueError("同步视频或字幕摘要无效")
    duration = _integer(document["duration_ms"], 3600000, 1)
    rows = document["records"]
    if not isinstance(rows, list) or len(rows) > 20000:
        raise ValueError("同步条目数量无效")
    ids, schemes = set(), {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != FIELDS:
            raise ValueError("同步条目字段无效")
        identity = _text(row["id"], 36)
        if not UUID_PATTERN.fullmatch(identity) or identity in ids:
            raise ValueError("同步条目标识无效或重复")
        ids.add(identity)
        if row["kind"] not in ("point", "question") or row["origin"] not in ("analysis", "manual"):
            raise ValueError("同步条目来源无效")
        mode = _text(row["mode"], 100)
        if not MODE_PATTERN.fullmatch(mode) or (row["kind"] == "question") != (mode == "question_extraction"):
            raise ValueError("同步条目模式无效")
        definition = (_text(row["scheme_name"], 200, 1), _integer(row["scheme_revision"], 2**31 - 1))
        if mode in schemes and schemes[mode] != definition:
            raise ValueError("同步方案定义不一致")
        schemes[mode] = definition
        if _integer(row["end_ms"], duration + 1) <= _integer(row["start_ms"], duration + 1):
            raise ValueError("同步时间范围无效")
        if row["user_start_ms"] is not None:
            _integer(row["user_start_ms"], duration)
        for key, maximum, minimum in (("title", 2000, 1), ("note", 100000, 0),
                                     ("source_quote", 100000, 0), ("trigger_phrase", 2000, 0)):
            _text(row[key], maximum, minimum)
        confidence = row["confidence"]
        if type(confidence) not in (int, float) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
            raise ValueError("同步置信度无效")
        if row["importance"] not in ("standard", "important", "critical") or type(row["removed"]) is not bool:
            raise ValueError("同步复习状态无效")
        if row["anchor"] != anchor(row):
            raise ValueError("同步条目原始身份无效")
    if len(canonical(document)) > MAX_DOCUMENT_BYTES:
        raise ValueError("此视频学习修改超过同步大小限制")
    return document


def validate_request(request):
    if not isinstance(request, dict) or set(request) != {"bootstrap", "base_digest", "document"}:
        raise ValueError("同步请求字段无效")
    if type(request["bootstrap"]) is not bool:
        raise ValueError("同步关联字段无效")
    if request["bootstrap"]:
        if request["base_digest"] is not None:
            raise ValueError("首次关联不能携带旧基线")
    elif not HASH_PATTERN.fullmatch(_text(request["base_digest"], 64)):
        raise ValueError("同步基线无效")
    return validate(request["document"])


def records(document):
    return {row["id"]: row for row in document["records"]}


def _normalize_builtin_schemes(document):
    result = deepcopy(document)
    for row in result["records"]:
        if row["mode"] in NAMES:
            row.update(scheme_name=NAMES[row["mode"]], scheme_revision=0)
    return result


def merge(base, local, remote, choices=None):
    choices = choices or {}
    for document in (base, local, remote):
        if document is not None:
            validate(document)
    base = _normalize_builtin_schemes(base) if base is not None else None
    local, remote = map(_normalize_builtin_schemes, (local, remote))
    for key in ("video_sha256", "duration_ms", "transcript_digest"):
        if local[key] != remote[key] or base is not None and base[key] != local[key]:
            raise ValueError("同步视频、时间轴或字幕已改变，请核对原资料")
    before = records(base) if base is not None else {}
    ours, theirs = records(local), records(remote)
    if not before.keys() <= ours.keys() or not before.keys() <= theirs.keys():
        raise ValueError("同步记录缺失，不能将缺失推断为删除")
    conflicts, result = [], []
    for identity in sorted(ours.keys() | theirs.keys()):
        left, right, old = ours.get(identity), theirs.get(identity), before.get(identity)
        if left is None or right is None:
            result.append(deepcopy(left or right))
            continue
        if left["anchor"] != right["anchor"] or old is not None and old["anchor"] != left["anchor"]:
            raise ValueError("同步条目的原始身份改变")
        choice = choices.get(identity)
        if choice not in (None, "local", "remote"):
            raise ValueError("同步冲突选项无效")
        changed = FIELDS - {"id", "anchor", "removed"}
        delete_edit = old is not None and (
            left["removed"] != old["removed"] and any(right[k] != old[k] for k in changed)
            or right["removed"] != old["removed"] and any(left[k] != old[k] for k in changed))
        unresolved = []
        output = deepcopy(left)
        if delete_edit and left != right:
            unresolved = ["removed"]
            output = deepcopy(right if choice == "remote" else left)
        else:
            for field in FIELDS - {"id", "anchor"}:
                if left[field] == right[field] or old is not None and right[field] == old[field]:
                    output[field] = deepcopy(left[field])
                elif old is not None and left[field] == old[field]:
                    output[field] = deepcopy(right[field])
                else:
                    unresolved.append(field)
                    output[field] = deepcopy(right[field] if choice == "remote" else left[field])
        if unresolved and choice is None:
            conflicts.append({"id": identity, "title": left["title"], "fields": sorted(unresolved)})
        result.append(output)
    return validate({**local, "records": result}), conflicts


def initialize(connection):
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(video_markers)")}
    for column, default in (("sync_id", ""), ("sync_origin", "")):
        if column not in columns:
            connection.execute(f"ALTER TABLE video_markers ADD COLUMN {column} TEXT NOT NULL DEFAULT '{default}'")
    connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS markers_sync_identity ON video_markers(video_id,sync_id) WHERE sync_id!=''")
    for statement in (
        """CREATE TABLE IF NOT EXISTS study_sync_bases(
            peer_id TEXT NOT NULL, video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
            document TEXT NOT NULL, PRIMARY KEY(peer_id,video_id))""",
        """CREATE TABLE IF NOT EXISTS study_sync_receipts(
            id TEXT PRIMARY KEY, peer_id TEXT NOT NULL,
            video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
            request_digest TEXT NOT NULL, document TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS study_sync_schemes(
            video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
            local_mode TEXT NOT NULL, wire_mode TEXT NOT NULL, name TEXT NOT NULL, revision INTEGER NOT NULL,
            PRIMARY KEY(video_id,local_mode), UNIQUE(video_id,wire_mode))""",
        """CREATE TABLE IF NOT EXISTS study_sync_tasks(
            id TEXT PRIMARY KEY, body TEXT NOT NULL)""",
    ):
        connection.execute(statement)


def require_unlinked(connection, video_id):
    linked = connection.execute("SELECT 1 FROM study_sync_bases WHERE video_id=? LIMIT 1", (video_id,)).fetchone()
    pending = connection.execute("""SELECT 1 FROM study_sync_tasks WHERE json_extract(body,'$.video_id')=?
        AND json_extract(body,'$.state') NOT IN ('completed','rejected') LIMIT 1""", (video_id,)).fetchone()
    if linked or pending:
        raise ValueError("此视频已关联学习修改同步，不能整包覆盖或重置字幕；请使用学习修改同步")


class StudySyncStore:
    def __init__(self, library):
        self.library = library

    def linked(self, peer, video_id):
        with self.library.connect() as connection:
            return self._base(connection, peer, video_id) is not None

    @staticmethod
    def _base(connection, peer, video_id):
        row = connection.execute("SELECT document FROM study_sync_bases WHERE peer_id=? AND video_id=?",
                                 (peer, video_id)).fetchone()
        return json.loads(row[0]) if row else None

    def export(self, video_id, connection=None):
        if connection is None:
            with self.library.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                return self.export(video_id, connection)
        video = connection.execute("SELECT * FROM videos WHERE id=?", (video_id,)).fetchone()
        if video is None:
            raise ValueError("同步视频不存在")
        subtitles = [dict(row) for row in connection.execute(
            "SELECT segment_id AS id,start_ms,end_ms,text FROM video_transcript_segments WHERE video_id=? ORDER BY start_ms,segment_id",
            (video_id,))]
        if not subtitles:
            raise ValueError("请先处理或传输此视频的字幕")
        scheme_rows = {row["analysis_mode"]: row for row in connection.execute(
            """SELECT r.*,s.name FROM video_analysis_runs r LEFT JOIN recall_schemes s ON s.id=r.analysis_mode
            WHERE r.video_id=?""", (video_id,))}
        saved_schemes = {row["local_mode"]: row for row in connection.execute(
            "SELECT * FROM study_sync_schemes WHERE video_id=?", (video_id,))}
        output = []
        for row in connection.execute("SELECT * FROM video_markers WHERE video_id=? ORDER BY id", (video_id,)).fetchall():
            mode = row["analysis_mode"]
            scheme = saved_schemes.get(mode)
            if scheme is None:
                run = scheme_rows.get(mode)
                name = (run["name"] if run else None) or NAMES.get(mode, "手动考点" if mode == "custom_manual_points" else "自定义方案")
                revision = run["scheme_revision"] if run else (1 if mode.startswith("custom_") else 0)
                connection.execute("INSERT INTO study_sync_schemes VALUES(?,?,?,?,?)",
                                   (video_id, mode, mode, name, revision))
                scheme = {"wire_mode": mode, "name": name, "revision": revision}
                saved_schemes[mode] = scheme
            identity = row["sync_id"] or str(uuid4())
            origin = row["sync_origin"] or ("manual" if row["trigger_phrase"].startswith("MANUAL-") else "analysis")
            connection.execute("UPDATE video_markers SET sync_id=?,sync_origin=? WHERE id=?", (identity, origin, row["id"]))
            record = {key: row[key] for key in ("start_ms", "end_ms", "title", "note", "source_quote",
                                               "trigger_phrase", "confidence", "user_start_ms", "importance")}
            record.update(id=identity, kind="question" if mode == "question_extraction" else "point", origin=origin,
                          mode=scheme["wire_mode"], scheme_name=NAMES.get(mode, scheme["name"]),
                          scheme_revision=0 if mode in NAMES else scheme["revision"],
                          removed=row["review_status"] == "excluded")
            record["anchor"] = anchor(record)
            output.append(record)
        return validate({"format": "classcue-study-sync", "version": 1, "video_sha256": video["content_hash"],
                         "duration_ms": video["duration_ms"], "transcript_digest": digest(subtitles),
                         "records": sorted(output, key=lambda row: row["id"])})

    def request(self, peer, video_id):
        with self.library.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            base = self._base(connection, peer, video_id)
            return {"bootstrap": base is None, "base_digest": digest(base) if base is not None else None,
                    "document": self.export(video_id, connection)}

    @staticmethod
    def _video(connection, document):
        video = connection.execute("SELECT * FROM videos WHERE content_hash=?", (document["video_sha256"],)).fetchone()
        if video is None:
            raise ValueError("请先传输此视频与字幕，再同步学习修改")
        return video

    def plan(self, peer, request, receipt=None, choices=None, connection=None):
        if connection is None:
            with self.library.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                return self.plan(peer, request, receipt, choices, connection)
        remote = validate_request(request)
        video = self._video(connection, remote)
        local = self.export(video["id"], connection)
        if receipt is not None:
            base, remote = request["document"], validate(receipt)
        else:
            base = self._base(connection, peer, video["id"])
            if base is None or request["bootstrap"]:
                raise ValueError("请先从电脑端建立此视频的同步关联")
            if digest(base) != request["base_digest"]:
                raise ValueError("上次同步尚未确认或基线不一致，请重试原同步任务")
        merged, conflicts = merge(base, local, remote, choices)
        return {"local": local, "document": merged, "conflicts": conflicts, "video_id": video["id"]}

    def replay(self, peer, identity, request, connection):
        row = connection.execute("SELECT * FROM study_sync_receipts WHERE id=?", (identity,)).fetchone()
        if row is None:
            return None
        if row["peer_id"] != peer or row["request_digest"] != digest(request):
            raise ValueError("同步任务编号被不同内容复用")
        return json.loads(row["document"])

    def accept(self, peer, identity, request, expected_local, choices, receipt=None):
        with self.library.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            saved = self.replay(peer, identity, request, connection)
            if saved is not None:
                if receipt is not None and saved != receipt:
                    raise ValueError("同步回执内容已改变")
                return saved
            plan = self.plan(peer, request, receipt, choices, connection)
            if digest(plan["local"]) != expected_local:
                raise ValueError("预览后电脑资料已改变，请刷新同步预览")
            if plan["conflicts"]:
                raise ValueError("请先处理全部同步冲突")
            video_id = plan["video_id"]
            self._apply(connection, video_id, plan["document"])
            if self.export(video_id, connection) != plan["document"]:
                raise ValueError("同步结果校验失败，未保存修改")
            baseline = receipt if receipt is not None else plan["document"]
            content = json.dumps(baseline, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            connection.execute("INSERT OR REPLACE INTO study_sync_bases VALUES(?,?,?)", (peer, video_id, content))
            connection.execute("INSERT INTO study_sync_receipts VALUES(?,?,?,?,?)",
                               (identity, peer, video_id, digest(request), content))
            return baseline

    def _apply(self, connection, video_id, document):
        existing = {row["sync_id"]: row for row in connection.execute("SELECT * FROM video_markers WHERE video_id=?", (video_id,))}
        schemes = {row["wire_mode"]: row for row in connection.execute("SELECT * FROM study_sync_schemes WHERE video_id=?", (video_id,))}
        for record in document["records"]:
            mode = record["mode"]
            scheme = schemes.get(mode)
            if scheme is None:
                local_mode = f"custom_{uuid4().hex}" if mode.startswith("custom_") else mode
                connection.execute("INSERT INTO study_sync_schemes VALUES(?,?,?,?,?)",
                                   (video_id, local_mode, mode, record["scheme_name"], record["scheme_revision"]))
                if local_mode.startswith("custom_"):
                    config = json.dumps({"name": record["scheme_name"], "base_mode": "medium_recall", "rules": [], "notes": ""})
                    connection.execute("INSERT INTO recall_schemes(id,name,config_json,revision,archived) VALUES(?,?,?,?,1)",
                                       (local_mode, record["scheme_name"], config, max(1, record["scheme_revision"])))
                scheme = {"local_mode": local_mode}
                schemes[mode] = scheme
            mode = scheme["local_mode"]
            connection.execute("UPDATE study_sync_schemes SET name=?,revision=? WHERE video_id=? AND local_mode=?",
                               (record["scheme_name"], record["scheme_revision"], video_id, mode))
            if record["origin"] == "analysis":
                connection.execute("""INSERT OR IGNORE INTO video_analysis_runs(video_id,analysis_mode,status,scheme_revision)
                                   VALUES(?,?,'completed',?)""", (video_id, mode, record["scheme_revision"]))
            old = existing.get(record["id"])
            status = "excluded" if record["removed"] else ("mastered" if old and old["review_status"] == "mastered" else "active")
            values = {key: record[key] for key in ("start_ms", "end_ms", "title", "note", "source_quote",
                                                  "trigger_phrase", "confidence", "user_start_ms", "importance")}
            values.update(analysis_mode=mode, review_status=status, sync_id=record["id"], sync_origin=record["origin"])
            if old:
                connection.execute(f"UPDATE video_markers SET {','.join(key+'=?' for key in values)} WHERE id=?",
                                   (*values.values(), old["id"]))
            else:
                connection.execute(f"INSERT INTO video_markers(video_id,{','.join(values)}) VALUES(?{',?' * len(values)})",
                                   (video_id, *values.values()))

    def tasks(self):
        with self.library.connect() as connection:
            return [json.loads(row[0]) for row in connection.execute("SELECT body FROM study_sync_tasks ORDER BY rowid DESC")]

    def task(self, identity):
        with self.library.connect() as connection:
            row = connection.execute("SELECT body FROM study_sync_tasks WHERE id=?", (identity,)).fetchone()
            return json.loads(row[0]) if row else None

    def save(self, task):
        with self.library.connect() as connection:
            connection.execute("INSERT OR REPLACE INTO study_sync_tasks VALUES(?,?)",
                               (task["id"], json.dumps(task, ensure_ascii=False, allow_nan=False)))
