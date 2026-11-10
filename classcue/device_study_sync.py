"""Signed USB exchange for Windows learning edits, with durable acknowledgements."""

from __future__ import annotations

import time
from uuid import uuid4

from classcue import device_protocol as wire
from classcue.study_sync import StudySyncStore, digest, records, validate, validate_request, UUID_PATTERN

TERMINAL = {"completed", "rejected"}


class StudySyncLink:
    def __init__(self, manager):
        self.manager = manager

    def store(self):
        return StudySyncStore(self.manager.get_store())

    @staticmethod
    def public(task):
        return {key: value for key, value in task.items()
                if key not in {"request", "receipt", "public_key", "remote_sequences", "local_digest", "published_at"}}

    def start(self, device_id, video_ids=None):
        manager = self.manager
        with manager.lock, manager.operation_lock:
            device = manager._device(device_id)
            if not device.get("paired") or not device.get("foreground"):
                raise ValueError("请连接已配对的平板，并打开平板连接页面")
            if "study-sync-v1" not in device.get("capabilities", []):
                raise ValueError("此平板版本尚不支持学习修改同步，请更新平板应用")
            self.tick([device])
            store = self.store()
            rows = manager.get_store().list_videos()
            if video_ids:
                rows = [row for row in rows if row["id"] in video_ids]
            tasks = store.tasks()
            created = []
            for video in rows:
                if not video_ids and not store.linked(device["peer_id"], video["id"]):
                    continue
                if any(task["peer_id"] == device["peer_id"] and task["video_id"] == video["id"]
                       and task["state"] not in TERMINAL for task in tasks):
                    continue
                if reason := manager.block_reason(video["id"]):
                    raise ValueError(reason)
                request = store.request(device["peer_id"], video["id"])
                identity = str(uuid4())
                # Check the complete wrapper limit before saving an unsendable request.
                wire.envelope(manager.identity, "sync_request", identity, device["peer_id"], request)
                task = {"id": identity, "peer_id": device["peer_id"], "peer_name": device["name"],
                        "device_id": device["id"], "public_key": device["public_key"], "video_id": video["id"],
                        "name": video["display_name"] or video["filename"], "direction": "outgoing",
                        "state": "waiting_peer", "request": request, "request_digest": digest(request),
                        "created_at": int(time.time() * 1000), "error": ""}
                store.save(task)
                created.append(self.public(task))
            if not created:
                raise ValueError("没有可发起的同步，请先选择已传到平板的视频建立关联，或处理当前任务")
            return created

    def _publish(self, task, filename, kind, data):
        manager = self.manager
        task["sequence"] = task.get("sequence", 0) + 1
        content = wire.envelope(manager.identity, kind, task["id"], task["peer_id"], data, task["sequence"])
        manager.transport.write(task["device_id"], f"sync/{task['id']}/{filename}.json", content)

    def _read(self, task, filename, kind):
        content = self.manager._read(task["device_id"], f"sync/{task['id']}/{filename}.json")
        if content is None:
            return None
        body, _ = wire.open_envelope(content, key=task["public_key"], recipient=self.manager.peer_id,
                                     message_id=task["id"], message_type=kind)
        sequences = task.setdefault("remote_sequences", {})
        if body["sequence"] < sequences.get(filename, -1):
            raise ValueError("过期的同步消息，请重试原任务")
        sequences[filename] = body["sequence"]
        return body["data"]

    def _preview(self, store, task):
        plan = store.plan(task["peer_id"], task["request"], task.get("receipt"))
        ours, merged = records(plan["local"]), records(plan["document"])
        remote = records(task.get("receipt") or task["request"]["document"])
        added = sum(identity not in ours for identity in merged)
        removed = sum(identity in ours and row["removed"] and not ours[identity]["removed"] for identity, row in merged.items())
        edited = sum(identity in ours and row != ours[identity] and not (row["removed"] and not ours[identity]["removed"])
                     for identity, row in merged.items())
        conflicts = []
        for conflict in plan["conflicts"]:
            fields = conflict["fields"]
            if fields == ["removed"]:
                fields = ["removed", "title", "note", "user_start_ms", "importance"]
            identity = conflict["id"]
            conflicts.append({**conflict, "fields": fields,
                              "local": {field: ours[identity][field] for field in fields},
                              "remote": {field: remote[identity][field] for field in fields}})
        task.update(local_digest=digest(plan["local"]), state="awaiting_confirmation", error="",
                    preview={"added": added, "removed": removed, "edited": edited, "conflicts": conflicts,
                             "bootstrap": task["request"]["bootstrap"]})
        store.save(task)

    def refresh(self, identity):
        with self.manager.lock, self.manager.operation_lock:
            store, task = self._task(identity)
            if task["state"] != "awaiting_confirmation":
                raise ValueError("此任务无需刷新预览")
            self._preview(store, task)
            return self.public(task)

    def _task(self, identity):
        store = self.store()
        task = store.task(identity)
        if task is None:
            raise ValueError("同步任务不存在")
        return store, task

    def accept(self, identity, choices):
        with self.manager.lock, self.manager.operation_lock:
            store, task = self._task(identity)
            if task["state"] != "awaiting_confirmation":
                raise ValueError("此同步状态已改变")
            if reason := self.manager.block_reason(task["video_id"]):
                raise ValueError(reason)
            result = store.accept(task["peer_id"], identity, task["request"], task["local_digest"],
                                  choices, task.get("receipt"))
            incoming = task["direction"] == "incoming"
            task.update(receipt=result, state="awaiting_ack" if incoming else "completed", published_at=0, error="")
            store.save(task)
            # Transport failures after committing are retryable, never undo a library transaction.
            return self.public(task)

    def reject(self, identity):
        with self.manager.lock:
            store, task = self._task(identity)
            if task["direction"] != "incoming" or task["state"] != "awaiting_confirmation":
                raise ValueError("已保存的同步不能拒绝")
            task.update(state="rejected", error="用户未接收此次学习修改", published_at=0)
            store.save(task)
            return self.public(task)

    def _discover(self, store, device):
        try:
            entries = self.manager.transport.list(device["id"], "sync")
        except OSError:
            return
        known = {task["id"] for task in store.tasks()}
        for entry in entries:
            identity = entry["name"]
            if not entry.get("folder") or not UUID_PATTERN.fullmatch(identity) or identity in known:
                continue
            content = self.manager._read(device["id"], f"sync/{identity}/request.json")
            if content is None:
                continue
            try:
                message, _ = wire.open_envelope(content, key=device["public_key"], recipient=self.manager.peer_id,
                                                message_id=identity, message_type="sync_request")
            except Exception:
                continue
            try:
                request_digest = digest(message["data"])
            except (ValueError, TypeError):
                continue
            task = {"id": identity, "peer_id": device["peer_id"], "peer_name": device["name"],
                    "device_id": device["id"], "public_key": device["public_key"], "video_id": None,
                    "name": "学习修改", "direction": "incoming", "state": "awaiting_confirmation",
                    "request": message["data"], "request_digest": request_digest,
                    "created_at": int(time.time() * 1000), "error": ""}
            try:
                document = validate_request(message["data"])
                video = self.manager.get_store().get_video_by_hash(document["video_sha256"])
                if video is None:
                    raise ValueError("请先传输此视频与字幕，再同步学习修改")
                task.update(video_id=video["id"], name=video["display_name"] or video["filename"])
                pending = next((old for old in store.tasks() if old["peer_id"] == device["peer_id"]
                                and old["video_id"] == video["id"] and old["state"] not in TERMINAL), None)
                if pending:
                    # Both devices may press sync before seeing each other. The lower UUID wins,
                    # and its full snapshot still merges every edit from the superseded request.
                    if pending["direction"] != "outgoing" or pending["state"] != "waiting_peer" or pending["id"] < identity:
                        continue
                    pending.update(state="rejected", error="已合并到对端同时发起的同步", published_at=0)
                    store.save(pending)
                if reason := self.manager.block_reason(video["id"]):
                    raise ValueError(reason)
                with store.library.connect() as connection:
                    replay = store.replay(device["peer_id"], identity, message["data"], connection)
                if replay is not None:
                    task.update(receipt=replay, state="awaiting_ack")
                    store.save(task)
                else:
                    self._preview(store, task)
            except (ValueError, TypeError, KeyError) as error:
                task.update(state="rejected", error=str(error)[:500])
                store.save(task)

    def tick(self, devices):
        store = self.store()
        for device in devices:
            if device.get("paired") and device.get("foreground"):
                with self.manager.operation_lock:
                    self._discover(store, device)
        for task in store.tasks():
            device = next((row for row in devices if row.get("peer_id") == task["peer_id"]
                           and row.get("paired") and row.get("foreground")), None)
            if device is None:
                continue
            task["device_id"] = device["id"]
            try:
                incoming, state = task["direction"] == "incoming", task["state"]
                if incoming and state != "rejected":
                    request = self._read(task, "request", "sync_request")
                    if request is None:
                        continue
                    if digest(request) != task["request_digest"]:
                        raise ValueError("同步请求在确认后被改变，未应用不同内容")
                if incoming and state == "awaiting_ack":
                    ack = self._read(task, "ack", "sync_ack")
                    if ack is not None:
                        if (set(ack) != {"request_digest", "document_digest"} or ack["request_digest"] != task["request_digest"]
                                or ack["document_digest"] != digest(task["receipt"])):
                            raise ValueError("同步确认回执不匹配")
                        task.update(state="completed", error="")
                        store.save(task)
                        continue
                if not incoming and state == "waiting_peer":
                    rejection = self._read(task, "rejected", "sync_rejected")
                    if rejection is not None:
                        if set(rejection) != {"request_digest", "message"} or rejection["request_digest"] != task["request_digest"]:
                            raise ValueError("同步拒绝回执不匹配")
                        task.update(state="rejected", error=str(rejection["message"])[:500])
                        store.save(task)
                        continue
                    receipt = self._read(task, "receipt", "sync_receipt")
                    if receipt is not None:
                        if set(receipt) != {"request_digest", "document"} or receipt["request_digest"] != task["request_digest"]:
                            raise ValueError("同步回执不匹配")
                        task["receipt"] = validate(receipt["document"])
                        with self.manager.operation_lock:
                            self._preview(store, task)
                            if not task["preview"]["conflicts"]:
                                self.accept(task["id"], {})
                        continue
                # Recover a library commit that completed before the task journal update.
                if state == "awaiting_confirmation":
                    with store.library.connect() as connection:
                        replay = store.replay(task["peer_id"], task["id"], task["request"], connection)
                    if replay is not None:
                        task.update(receipt=replay, state="awaiting_ack" if incoming else "completed", published_at=0)
                        state = task["state"]
                if time.time() - task.get("published_at", 0) < 10:
                    continue
                if incoming and state == "awaiting_ack":
                    self._publish(task, "receipt", "sync_receipt", {"request_digest": task["request_digest"], "document": task["receipt"]})
                elif incoming and state == "rejected":
                    self._publish(task, "rejected", "sync_rejected", {"request_digest": task["request_digest"], "message": task["error"]})
                elif not incoming and state == "completed":
                    self._publish(task, "ack", "sync_ack", {"request_digest": task["request_digest"], "document_digest": digest(task["receipt"])})
                elif not incoming and state == "waiting_peer":
                    self._publish(task, "request", "sync_request", task["request"])
                else:
                    continue
                task.update(published_at=time.time(), error="" if state != "rejected" else task["error"])
            except Exception as error:
                task["error"] = str(error)[:500]
            store.save(task)
