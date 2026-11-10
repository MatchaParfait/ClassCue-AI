"""Foreground USB course exchange with a durable journal and receiver receipts."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
from pathlib import Path
import shutil
from threading import Event, RLock, Thread
import time
from uuid import UUID, uuid4

from classcue import device_protocol as protocol, study_share
from classcue.device_study import current_digest, current_state_digest, import_study, restore_study, save_transfer_commit, transfer_commit
from classcue.device_transfer_store import TransferStore
from classcue.device_study_sync import StudySyncLink
from classcue.device_wpd import WpdTransport
from classcue.video_library import hash_video_source, register_managed_video


class TransferManager:
    def __init__(self, get_store, directory: Path, video_directory, audio_directory,
                 *, block_reason=lambda _: None, operation_lock=None, transport=None, identity=None):
        self.get_store = get_store
        self.journal = TransferStore(directory)
        self.transport = transport or WpdTransport()
        self.identity = identity or protocol.load_identity(directory)
        self.public_key = protocol.public_key(self.identity)
        self.peer_id = protocol.peer_id(self.public_key)
        self.video_directory = video_directory
        self.audio_directory = audio_directory
        self.block_reason = block_reason
        self.operation_lock = operation_lock or RLock()
        self.lock = RLock()
        self.stop = Event()
        self.devices_cache = []
        self.discovery_error = ""
        self.thread = None
        self.sync = StudySyncLink(self)

    def start(self):
        if self.thread is None:
            self.thread = Thread(target=self._loop, daemon=True, name="classcue-device-transfers")
            self.thread.start()

    def close(self):
        self.stop.set()
        self.transport.close()
        if self.thread is not None:
            self.thread.join(timeout=4)

    def _loop(self):
        while not self.stop.wait(1):
            try:
                self.tick()
            except Exception:
                self.discovery_error = "设备连接暂不可用，请检查平板前台状态和数据线"

    def _read(self, device, path, maximum=protocol.MAX_CONTROL_BYTES):
        try:
            return self.transport.read(device, path, maximum)
        except OSError:
            return None

    def _message(self, device, task, filename, kind, *, public=None):
        content = self._read(device, f"tasks/{task['id']}/{filename}")
        if content is None:
            return None
        value, _ = protocol.open_envelope(content, key=public or task["public_key"],
                                           recipient=self.peer_id, message_id=task["id"], message_type=kind)
        previous = task.setdefault("remote_sequences", {}).get(filename, -1)
        if value["sequence"] < previous:
            return None
        task["remote_sequences"][filename] = value["sequence"]
        return value

    def _publish(self, task, filename, kind, data):
        task["sequence"] = task.get("sequence", 0) + 1
        content = protocol.envelope(self.identity, kind, task["id"], task["peer_id"], data, task["sequence"])
        self.transport.write(task["device_id"], f"tasks/{task['id']}/{filename}", content)

    def scan(self):
        devices = []
        for device in self.transport.devices():
            info = {**device, "root_available": False, "paired": False, "foreground": False}
            try:
                content = self.transport.read(device["id"], "identity.json", protocol.MAX_CONTROL_BYTES)
                identity = json.loads(content)
                public = identity["public_key"]
                if identity["peer_id"] != protocol.peer_id(public):
                    raise ValueError("设备身份无效")
                info.update(root_available=True, peer_id=identity["peer_id"], public_key=public,
                            name=str(identity.get("name", device["name"]))[:200],
                            capabilities=identity.get("capabilities", []),
                            foreground=identity.get("foreground") is True)
                peer = self.journal.peer(info["peer_id"])
                info["paired"] = peer is not None and peer["public_key"] == public
                info["message"] = "" if info["foreground"] else "请在平板打开“连接电脑”"
            except Exception:
                info["message"] = "请在平板“连接电脑”中授权接收目录"
            devices.append(info)
        self.devices_cache = devices
        self.discovery_error = ""
        return devices

    def _device(self, identity):
        device = next((device for device in self.devices_cache if device["id"] == identity), None)
        if device is None:
            self.scan()
            device = next((device for device in self.devices_cache if device["id"] == identity), None)
        if device is None or not device.get("root_available"):
            raise ValueError("设备尚未准备好，请在平板打开连接页面并授权目录")
        return device

    def request_pairing(self, device_id):
        with self.lock:
            device = self._device(device_id)
            for pending in self.journal.pairs():
                if pending["peer_id"] == device["peer_id"] and not pending.get("trusted") and not pending.get("rejected"):
                    return self._public_pair(pending)
            identity = str(uuid4())
            nonce = protocol.token()
            pair = {"id": identity, "device_id": device_id, "peer_id": device["peer_id"],
                    "name": device["name"], "public_key": device["public_key"], "nonce": nonce,
                    "code": protocol.pairing_code(self.public_key, device["public_key"], nonce),
                    "confirmed": False, "accepted": False}
            self.journal.save_pair(pair)
            self.transport.write(device_id, f"pairing/{identity}.request.json", protocol.envelope(
                self.identity, "pair_request", identity, device["peer_id"], {"name": "ClassCue Windows", "nonce": nonce}))
            return self._public_pair(pair)

    @staticmethod
    def _public_pair(pair):
        return {key: pair[key] for key in ("id", "name", "code", "confirmed", "accepted")}

    def confirm_pairing(self, identity):
        with self.lock:
            pair = self.journal.pair(identity)
            if pair is None:
                raise ValueError("配对请求不存在")
            pair["confirmed"] = True
            self.journal.save_pair(pair)
            self._pair_responses()
            return self._public_pair(self.journal.pair(identity))

    def _pair_responses(self):
        for pair in self.journal.pairs():
            if pair.get("trusted") or pair.get("rejected"):
                continue
            content = self._read(pair["device_id"], f"pairing/{pair['id']}.response.json")
            if content is None:
                continue
            try:
                response, _ = protocol.open_envelope(content, key=pair["public_key"], recipient=self.peer_id,
                                                    message_id=pair["id"], message_type="pair_response")
            except Exception:
                continue
            if (response["data"].get("request_id") != pair["id"]
                    or response["data"].get("nonce") != pair["nonce"]):
                continue
            pair["accepted"] = response["data"].get("accepted") is True
            pair["rejected"] = response["data"].get("accepted") is False
            if pair["accepted"] and pair["confirmed"]:
                self.journal.trust(pair["peer_id"], pair["name"], pair["public_key"])
                pair["trusted"] = True
            self.journal.save_pair(pair)

    def status(self):
        with self.lock:
            library_path = str(Path(self.get_store().path).resolve())
            return {
                "devices": [{key: value for key, value in device.items() if key != "public_key"} for device in self.devices_cache],
                "tasks": [self.public_task(task) for task in self.journal.tasks()
                          if task.get("library_path") == library_path],
                "pairings": [self._public_pair(pair) for pair in self.journal.pairs()
                             if not pair.get("trusted") and not pair.get("rejected")],
                "snapshots": self.journal.snapshots(library_path), "error": self.discovery_error,
                "sync_tasks": [self.sync.public(task) for task in self.sync.store().tasks()],
            }

    @staticmethod
    def public_task(task):
        offer = task["offer"]
        return {"id": task["id"], "direction": task["direction"], "title": offer["title"],
                "state": task["state"], "peer_name": task.get("peer_name", ""), "total_bytes": offer["total_bytes"],
                "transferred_bytes": task.get("transferred_bytes", 0),
                "items": [{key: value for key, value in item.items() if key != "local_digest"}
                          for item in task.get("preview", [])],
                "video_count": len(offer["items"]),
                "results": [{**result, "name": next((item["name"] for item in offer["items"] if item["id"] == result["id"]), "")}
                            for result in task.get("results", [])],
                "error": task.get("error", ""), "updated": task.get("updated")}

    def create_send(self, device_id, *, video_ids=None, folder_id=None, retry_task=None):
        with self.lock:
            device = self._device(device_id)
            if not device["paired"]:
                raise ValueError("请先完成两端配对")
            store = self.get_store()
            folders = []
            rows = []
            title = "选中视频"
            if folder_id is not None:
                folder = store.get_folder(folder_id)
                if folder is None:
                    raise ValueError("课程文件夹不存在")
                title = folder["name"]

                def collect(folder):
                    if folder["id"] != folder_id:
                        folders.append({"id": str(folder["id"]), "name": folder["name"],
                                        "parent_id": None if folder["parent_id"] == folder_id else str(folder["parent_id"])})
                    rows.extend(store.list_folder_videos(folder["id"]))
                    for child in store.list_folders(folder["id"]):
                        collect(child)
                collect(folder)
            else:
                for identity in video_ids or []:
                    row = store.get_video(identity)
                    if row is None:
                        raise ValueError("选中的视频不存在")
                    rows.append(row)
                if len(rows) == 1:
                    title = rows[0]["display_name"] or rows[0]["filename"]
            retry_items = {}
            if retry_task is not None:
                folders = retry_task["offer"]["folders"]
                title = retry_task["offer"]["title"]
                retry_items = {item["id"]: item for item in retry_task["offer"]["items"]}
            unique = {row["content_hash"]: row for row in rows}
            if not unique:
                raise ValueError("选中的课程没有视频")
            identity = str(uuid4())
            task_dir = self.journal.directory / "tasks" / identity
            task_dir.mkdir(parents=True, exist_ok=True)
            items = []
            paths = {}
            for row in unique.values():
                if reason := self.block_reason(row["id"]):
                    raise ValueError(reason)
                path = Path(row["video_path"])
                before = path.stat()
                blocks = []
                full = hashlib.sha256()
                with path.open("rb") as stream:
                    while piece := stream.read(protocol.BLOCK_BYTES):
                        full.update(piece)
                        blocks.append({"index": len(blocks), "size": len(piece), "sha256": hashlib.sha256(piece).hexdigest()})
                if full.hexdigest() != row["content_hash"] or path.stat().st_mtime_ns != before.st_mtime_ns:
                    raise ValueError("源视频内容已变化，未发送")
                study = None
                if row["status"] == "review_ready":
                    content = study_share.export_package(store, row["id"], transfer=True)
                    package = study_share.decode_package(content)
                    (task_dir / f"{row['content_hash']}.studyshare").write_bytes(content)
                    study = {"size": len(content), "sha256": hashlib.sha256(content).hexdigest(),
                             "semantic_digest": protocol.semantic_digest(content), "subtitle_count": len(package.subtitles),
                             "marker_count": len(package.markers),
                             "runs": [{"mode": run.analysis_mode, "name": run.name,
                                       "count": sum(marker.analysis_mode == run.analysis_mode for marker in package.markers)}
                                      for run in package.analysis_runs]}
                items.append({"id": row["content_hash"], "name": row["display_name"] or row["filename"],
                              "size": before.st_size, "duration_ms": row["duration_ms"],
                              "mime": mimetypes.guess_type(path.name)[0] or "video/mp4",
                              "folder_id": retry_items[row["content_hash"]]["folder_id"] if retry_task is not None
                                           else str(row["library_folder_id"]) if folder_id is not None and row["library_folder_id"] != folder_id else None,
                              "blocks": blocks, "study": study})
                paths[row["content_hash"]] = {"path": str(path), "mtime_ns": before.st_mtime_ns}
            offer = {"title": title, "course_id": retry_task["offer"]["course_id"] if retry_task is not None
                                                else f"folder-{folder_id}" if folder_id is not None else identity,
                     "folders": folders, "items": items, "total_bytes": sum(item["size"] for item in items)}
            protocol.validate_offer(offer)
            task = {"id": identity, "device_id": device_id, "peer_id": device["peer_id"], "peer_name": device["name"],
                    "public_key": device["public_key"], "direction": "send", "state": "awaiting_confirmation",
                    "offer": offer, "paths": paths, "results": [], "sequence": 0,
                    "library_path": str(Path(store.path).resolve())}
            self.journal.save(task)
            self._publish(task, "offer.json", "offer", offer)
            self.journal.save(task)
            return self.public_task(task)

    def retry_failed(self, identity):
        with self.lock:
            old = self.journal.task(identity)
            if old["direction"] != "send" or old["state"] not in {"completed", "failed"}:
                raise ValueError("当前任务没有可重试的失败项")
            self._require_task_library(old)
            results = {result["id"]: result for result in old.get("results", [])}
            failed = [item for item in old["offer"]["items"]
                      if results.get(item["id"], {}).get("state") == "failed"
                      or old["state"] == "failed" and item["id"] not in results]
            if not failed:
                raise ValueError("当前任务没有失败项")
            rows = [self.get_store().get_video_by_hash(item["id"]) for item in failed]
            if any(row is None for row in rows):
                raise ValueError("失败项的源视频已移除，请重新选择课程")
            self.scan()
            device = next((row for row in self.devices_cache if row.get("peer_id") == old["peer_id"] and row.get("paired")), None)
            if device is None:
                raise ValueError("请先连接已配对的平板")
            return self.create_send(device["id"], video_ids=[row["id"] for row in rows], retry_task=old)

    def _require_task_library(self, task, store=None):
        if task.get("library_path") != str(Path((store or self.get_store()).path).resolve()):
            raise ValueError("传输任务属于另一个视频库，请切回原视频库")

    def _preview(self, offer):
        result = []
        store = self.get_store()
        for item in offer["items"]:
            existing = store.get_video_by_hash(item["id"])
            digest = None
            state_digest = None
            blocked = ""
            if existing:
                if not Path(existing["video_path"]).is_file():
                    blocked = "已有视频文件不可访问，请先重新关联"
                elif self.block_reason(existing["id"]):
                    blocked = self.block_reason(existing["id"])
                else:
                    try:
                        digest = current_digest(store, existing["id"])
                        state_digest = current_state_digest(store, existing["id"])
                    except Exception:
                        blocked = "已有学习资料无法校验，请先检查视频"
            incoming = item.get("study")
            conflict = bool(incoming and digest is not None and digest != incoming["semantic_digest"])
            same = bool(existing and (incoming is None or digest == incoming["semantic_digest"]))
            result.append({"id": item["id"], "name": item["name"], "existing_video_id": existing["id"] if existing else None,
                           "need_video": existing is None, "conflict": conflict, "same": same, "blocked": blocked,
                           "subtitle_count": incoming["subtitle_count"] if incoming else 0,
                           "marker_count": incoming["marker_count"] if incoming else 0,
                           "local_digest": state_digest})
        return result

    def accept(self, identity, actions, target_folder=None):
        with self.lock:
            task = self.journal.task(identity)
            self._require_task_library(task)
            if task["direction"] != "receive" or task["state"] != "awaiting_confirmation":
                raise ValueError("接收任务状态已改变")
            if target_folder is not None and self.get_store().get_folder(target_folder) is None:
                raise ValueError("目标分组不存在")
            lookup = {entry["id"]: entry.get("action") for entry in actions}
            decisions = []
            preview = self._preview(task["offer"])
            previous = {item["id"]: item for item in task["preview"]}
            if any(lookup.get(item["id"], "skip" if item["conflict"] or item["blocked"] else "receive") != "skip"
                   and any(item[key] != previous[item["id"]][key]
                           for key in ("local_digest", "existing_video_id", "need_video")) for item in preview):
                task.update(preview=preview, error="预览后本机资料已改变，请核对更新后的预览再确认")
                self.journal.save(task)
                raise ValueError(task["error"])
            required = 0
            for item in preview:
                action = lookup.get(item["id"], "skip" if item["conflict"] or item["blocked"] else "receive")
                if action not in {"receive", "overwrite", "skip"}:
                    raise ValueError("接收操作无效")
                if action != "skip" and (item["blocked"] or item["conflict"] and action != "overwrite"):
                    raise ValueError(item["blocked"] or "冲突资料需要明确选择覆盖或跳过")
                if action != "skip" and item["need_video"]:
                    required += next(row["size"] for row in task["offer"]["items"] if row["id"] == item["id"])
                decisions.append({"id": item["id"], "action": action, "need_video": item["need_video"] and action != "skip"})
            video_dir = Path(self.video_directory())
            video_dir.mkdir(parents=True, exist_ok=True)
            if shutil.disk_usage(self.journal.directory).free < required + 64 * 1024**2 or shutil.disk_usage(video_dir).free < required + 64 * 1024**2:
                raise ValueError("接收端空间不足")
            task.update(decision={"accepted": True, "items": decisions, "target_folder_id": target_folder},
                        preview=preview, state="receiving", acks=[], completed=[], errors=[], pending_publish=True)
            self.journal.save(task)
            self._publish(task, "decision.json", "decision", task["decision"])
            task["pending_publish"] = False
            self.journal.save(task)
            return self.public_task(task)

    def control(self, identity, action):
        with self.lock:
            task = self.journal.task(identity)
            self._require_task_library(task)
            if action == "pause":
                if task["state"] not in {"sending", "receiving", "verifying", "importing"}:
                    raise ValueError("当前任务不能暂停")
                task["state"] = "paused"
                task["manual_pause"] = True
                task["pending_status"] = task["direction"] == "receive"
            elif action == "resume":
                if task["state"] in {"completed", "cancelled"}:
                    raise ValueError("已结束的任务不能继续")
                task["state"] = "receiving" if task["direction"] == "receive" else "sending"
                task["manual_pause"] = False
                task["error"] = ""
                if task["direction"] == "receive" and task.get("full_hash_failed"):
                    failed_id = task.pop("full_hash_failed")
                    (self.journal.directory / "tasks" / task["id"] / f"{failed_id}.mp4").unlink(missing_ok=True)
                    task["acks"] = [entry for entry in task.get("acks", []) if entry["id"] != failed_id]
                    task["pending_status"] = True
            elif action == "cancel":
                if task["state"] in {"completed", "cancelled"}:
                    raise ValueError("任务已结束")
                task["state"] = "cancelled"
                task["pending_cancel"] = True
                self._cancel_local(task)
            else:
                raise ValueError("未知传输操作")
            self.journal.save(task)
            return self.public_task(task)

    def _cancel_local(self, task):
        directory = self.journal.directory / "tasks" / task["id"]
        for item in task["offer"]["items"]:
            (directory / f"{item['id']}.mp4").unlink(missing_ok=True)
        if task["direction"] == "receive":
            task["receipt_state"] = "cancelled"
            task["pending_receipt"] = True
        task["state"] = "cancelled"
        task["pending_cleanup"] = True
        self.journal.save(task)

    def restore(self, identity):
        with self.lock, self.operation_lock:
            snapshot = self.journal.snapshot_value(identity)
            if reason := self.block_reason(snapshot["video_id"]):
                raise ValueError(reason)
            return restore_study(self.get_store(), self.journal, identity)

    def tick(self):
        with self.lock:
            self.scan()
            self._pair_responses()
            self.sync.tick(self.devices_cache)
            for device in self.devices_cache:
                if device.get("paired") and device.get("foreground"):
                    self._scan_incoming(device)
            for task in self.journal.tasks():
                try:
                    self._require_task_library(task)
                except ValueError:
                    continue
                device = next((row for row in self.devices_cache if row.get("peer_id") == task["peer_id"] and row.get("paired")), None)
                if device is None:
                    continue
                task["device_id"] = device["id"]
                if task.get("pending_cancel"):
                    try:
                        self._publish(task, "cancel.json", "cancel", {})
                        task["pending_cancel"] = False
                        self.journal.save(task)
                    except (OSError, TimeoutError):
                        continue
                try:
                    if task.get("pending_status"):
                        self._status(task)
                        task["pending_status"] = False
                        self.journal.save(task)
                    if task["state"] in {"completed", "cancelled"}:
                        if task.get("pending_receipt"):
                            self._publish_receipt(task)
                        if task.get("pending_cleanup"):
                            self._cleanup_shared(task)
                        continue
                except (OSError, TimeoutError):
                    continue
                if task["state"] in {"cancelled", "failed"} or task.get("manual_pause"):
                    continue
                if not device.get("foreground"):
                    task["error"] = "等待平板回到连接页面"
                    self.journal.save(task)
                    continue
                try:
                    if self._message(device["id"], task, "cancel.json", "cancel"):
                        self._cancel_local(task)
                        if task.get("pending_receipt"):
                            self._publish_receipt(task)
                        continue
                    if task["direction"] == "send":
                        self._send(task)
                    elif task["state"] != "awaiting_confirmation":
                        self._receive(task)
                except (OSError, TimeoutError):
                    task["error"] = "连接暂时中断，等待重新连接"
                except Exception as error:
                    task["error"] = str(error)[:500]
                    task["state"] = "failed"
                self.journal.save(task)

    def _scan_incoming(self, device):
        try:
            directories = self.transport.list(device["id"], "tasks")
        except OSError:
            return
        known = {task["id"] for task in self.journal.tasks()}
        for entry in directories[:1000]:
            if not entry.get("folder"):
                continue
            try:
                identity = str(UUID(entry["name"]))
            except ValueError:
                continue
            if identity in known:
                continue
            content = self._read(device["id"], f"tasks/{identity}/offer.json")
            if content is None:
                continue
            try:
                value, _ = protocol.open_envelope(content, key=device["public_key"])
            except Exception:
                continue
            if value["sender"] == self.peer_id:
                continue
            if value["recipient"] != self.peer_id or value["id"] != identity or value["type"] != "offer":
                continue
            try:
                offer = protocol.validate_offer(value["data"])
            except (ValueError, TypeError, KeyError):
                continue
            task = {"id": identity, "device_id": device["id"], "peer_id": device["peer_id"], "peer_name": device["name"],
                    "public_key": device["public_key"], "direction": "receive", "state": "awaiting_confirmation",
                    "offer": offer, "preview": self._preview(offer), "results": [], "sequence": 0,
                    "library_path": str(Path(self.get_store().path).resolve())}
            self.journal.save(task)

    def _send(self, task):
        device = task["device_id"]
        receipt = self._message(device, task, "receipt.json", "receipt")
        if receipt:
            data = receipt["data"]
            expected = {item["id"] for item in task["offer"]["items"]}
            results = data.get("items", [])
            if not isinstance(results, list) or data.get("state") not in {"completed", "partial", "cancelled"}:
                raise ValueError("接收入库回执状态无效")
            actual = {item["id"] for item in results}
            if (len(actual) != len(results) or not actual <= expected
                    or data["state"] != "cancelled" and actual != expected
                    or any(item.get("state") not in {"imported", "reused", "skipped", "failed"} for item in results)):
                raise ValueError("接收入库回执不完整")
            task["results"] = results
            task["receipt_state"] = data["state"]
            task["state"] = "cancelled" if data["state"] == "cancelled" else "completed"
            task["pending_cleanup"] = True
            finished = {result["id"] for result in results if result["state"] in {"imported", "reused"}}
            choices = {item["id"]: item for item in task.get("decision", {}).get("items", [])}
            task["transferred_bytes"] = sum(item["size"] for item in task["offer"]["items"]
                                             if item["id"] in finished and choices.get(item["id"], {}).get("need_video",
                                                 any(result["id"] == item["id"] and result["state"] == "imported" for result in results)))
            task["error"] = ""
            return
        decision = self._message(device, task, "decision.json", "decision")
        if not decision:
            return
        if not decision["data"].get("accepted"):
            task["state"] = "cancelled"
            return
        task["decision"] = decision["data"]
        choices = {item["id"]: item for item in decision["data"]["items"]}
        status = self._message(device, task, "status.json", "status")
        remote = status["data"] if status else {}
        if remote.get("state") == "paused":
            task["error"] = "接收端已暂停"
            return
        acknowledgements = {(entry["id"], entry["index"]) for entry in remote.get("acks", [])}
        task["state"] = "sending"
        task["transferred_bytes"] = 0
        for item in task["offer"]["items"]:
            choice = choices.get(item["id"])
            if choice is None:
                raise ValueError("接收决定缺少课程条目")
            if choice["action"] == "skip":
                continue
            if item["id"] in remote.get("completed", []):
                task["transferred_bytes"] += item["size"]
                continue
            if item["study"] is not None and time.time() - task.get("study_sent", {}).get(item["id"], 0) > 10:
                content = (self.journal.directory / "tasks" / task["id"] / f"{item['id']}.studyshare").read_bytes()
                self.transport.delete(device, f"tasks/{task['id']}/studies/{item['id']}.ready.json")
                self.transport.write(device, f"tasks/{task['id']}/studies/{item['id']}.studyshare", content)
                self._publish(task, f"studies/{item['id']}.ready.json", "study",
                              {"id": item["id"], "size": len(content), "sha256": item["study"]["sha256"]})
                task.setdefault("study_sent", {})[item["id"]] = time.time()
            if not choice.get("need_video"):
                continue
            source = task["paths"][item["id"]]
            path = Path(source["path"])
            if path.stat().st_mtime_ns != source["mtime_ns"] or path.stat().st_size != item["size"]:
                raise ValueError("源视频在传输期间发生变化")
            for block in item["blocks"]:
                stem = f"tasks/{task['id']}/chunks/{item['id']}.{block['index']:06d}"
                if (item["id"], block["index"]) in acknowledgements:
                    task["transferred_bytes"] += block["size"]
                    if task.get("last_block") == stem:
                        self.transport.delete(device, stem + ".bin")
                        self.transport.delete(device, stem + ".ready.json")
                        task.pop("last_block", None)
                    continue
                if task.get("last_block") == stem and self._read(device, stem + ".ready.json") is not None:
                    return
                with path.open("rb") as stream:
                    stream.seek(block["index"] * protocol.BLOCK_BYTES)
                    content = stream.read(block["size"])
                if hashlib.sha256(content).hexdigest() != block["sha256"]:
                    raise ValueError("源视频数据块已改变")
                self.transport.delete(device, stem + ".ready.json")
                self.transport.write(device, stem + ".bin", content)
                ready = {"id": item["id"], **block}
                self._publish(task, f"chunks/{item['id']}.{block['index']:06d}.ready.json", "block", ready)
                task["last_block"] = stem
                return

    def _status(self, task, state=None):
        self._publish(task, "status.json", "status",
                      {"state": state or task["state"], "acks": task.get("acks", []),
                       "completed": task.get("completed", []), "errors": task.get("errors", []),
                       "updated_at": protocol.now()})

    def _target_folder(self, task, source_id, store=None):
        store = store or self.get_store()
        folders = {folder["id"]: folder for folder in task["offer"]["folders"]}
        if source_id is None and not task["offer"]["course_id"].startswith("folder-"):
            return task["decision"]["target_folder_id"]
        source_key = source_id if source_id is not None else "$root"
        course_key = hashlib.sha256(str(Path(store.path).resolve()).encode()).hexdigest() + ":" + task["offer"]["course_id"]
        existing = self.journal.folder(task["peer_id"], course_key, source_key)
        if existing is not None and store.get_folder(existing) is not None:
            return existing
        if source_id is None:
            parent = task["decision"]["target_folder_id"]
            name = task["offer"]["title"]
        else:
            folder = folders[source_id]
            parent = self._target_folder(task, folder["parent_id"], store)
            name = folder["name"]
        candidate = name
        index = 1
        while store.find_folder_by_name(candidate, parent):
            candidate = f"{name[:180]} ({index})"
            index += 1
        identity = store.create_folder(candidate, parent)
        self.journal.save_folder(task["peer_id"], course_key, source_key, identity)
        return identity

    def _receive(self, task):
        active_store = self.get_store()
        self._require_task_library(task, active_store)
        if task.get("pending_publish"):
            self._publish(task, "decision.json", "decision", task["decision"])
            task["pending_publish"] = False
        directory = self.journal.directory / "tasks" / task["id"]
        directory.mkdir(parents=True, exist_ok=True)
        choices = {entry["id"]: entry for entry in task["decision"]["items"]}
        preview = {entry["id"]: entry for entry in task["preview"]}
        existing_results = {entry["id"]: entry for entry in task.get("results", [])}
        for item in task["offer"]["items"]:
            identity = item["id"]
            choice = choices[identity]
            if identity in existing_results:
                continue
            committed = transfer_commit(active_store, task["id"], identity)
            if committed is not None:
                task["results"].append(committed)
                task.setdefault("completed", []).append(identity)
                self.journal.save(task)
                (directory / f"{identity}.mp4").unlink(missing_ok=True)
                self._status(task)
                return
            if choice["action"] == "skip":
                task["results"].append({"id": identity, "state": "skipped", "video_id": None,
                                        "subtitle_count": 0, "marker_count": 0, "message": "已跳过"})
                self.journal.save(task)
                continue
            content = None
            if item["study"] is not None:
                ready = self._message(task["device_id"], task, f"studies/{identity}.ready.json", "study")
                if ready is None:
                    self._status(task)
                    return
                if ready["data"] != {"id": identity, "size": item["study"]["size"], "sha256": item["study"]["sha256"]}:
                    self._item_failed(task, identity, "学习资料提交标记不匹配")
                    return
                content = self._read(task["device_id"], f"tasks/{task['id']}/studies/{identity}.studyshare", study_share.MAX_SHARE_BYTES)
                if content is None or len(content) != item["study"]["size"] or hashlib.sha256(content).hexdigest() != item["study"]["sha256"]:
                    self.transport.delete(task["device_id"], f"tasks/{task['id']}/studies/{identity}.ready.json")
                    self.transport.delete(task["device_id"], f"tasks/{task['id']}/studies/{identity}.studyshare")
                    self._status(task)
                    return
                try:
                    if protocol.semantic_digest(content) != item["study"]["semantic_digest"]:
                        raise ValueError("学习资料内容摘要不匹配")
                except ValueError as error:
                    self._item_failed(task, identity, str(error))
                    return
            stage = directory / f"{identity}.mp4"
            verified_source = None
            if choice["need_video"]:
                acknowledged = {(entry["id"], entry["index"]) for entry in task["acks"]}
                for block in item["blocks"]:
                    if (identity, block["index"]) in acknowledged:
                        if not stage.is_file() or stage.stat().st_size < block["index"] * protocol.BLOCK_BYTES + block["size"]:
                            task["acks"] = [entry for entry in task["acks"] if entry["id"] != identity]
                            self.journal.save(task)
                            self._status(task)
                            return
                        continue
                    stem = f"tasks/{task['id']}/chunks/{identity}.{block['index']:06d}"
                    message = self._message(task["device_id"], task,
                                            f"chunks/{identity}.{block['index']:06d}.ready.json", "block")
                    if message is None:
                        self._status(task)
                        return
                    if message["data"] != {"id": identity, **block}:
                        raise ValueError("数据块清单不匹配")
                    chunk = self._read(task["device_id"], stem + ".bin", protocol.BLOCK_BYTES)
                    if chunk is None:
                        return
                    if len(chunk) != block["size"] or hashlib.sha256(chunk).hexdigest() != block["sha256"]:
                        self.transport.delete(task["device_id"], stem + ".ready.json")
                        self.transport.delete(task["device_id"], stem + ".bin")
                        self._status(task)
                        return
                    if shutil.disk_usage(directory).free < len(chunk) + 32 * 1024**2:
                        raise ValueError("接收端存储空间不足")
                    with stage.open("r+b" if stage.exists() else "w+b") as output:
                        output.seek(block["index"] * protocol.BLOCK_BYTES)
                        output.write(chunk)
                        output.flush()
                        os.fsync(output.fileno())
                    task["acks"].append({"id": identity, "index": block["index"]})
                    task["transferred_bytes"] = task.get("transferred_bytes", 0) + block["size"]
                    self.journal.save(task)
                    self._status(task)
                    return
                verified_source = hash_video_source(self.get_store(), stage)
                if verified_source[1].st_size != item["size"] or verified_source[0] != identity:
                    task["full_hash_failed"] = identity
                    raise ValueError("完整视频校验失败，请重试任务")
            created_video = None
            try:
                with self.operation_lock:
                    store = self.get_store()
                    self._require_task_library(task, store)
                    existing = store.get_video_by_hash(identity)
                    if existing:
                        video_id = existing["id"]
                        if reason := self.block_reason(video_id):
                            raise ValueError(reason)
                    else:
                        video_id, managed_path, registered_existing, _ = register_managed_video(
                            store, stage, Path(self.audio_directory()), Path(self.video_directory()),
                            delete_source_after_import=False, source_folder="", _verified_source=verified_source)
                        if not registered_existing:
                            created_video = (store, video_id, managed_path)
                            store.update_video_display_name(video_id, item["name"])
                            store.set_video_folder(video_id, self._target_folder(task, item.get("folder_id"), store))
                        else:
                            existing = store.get_video(video_id)
                    result = {"id": identity, "state": "reused" if existing else "imported", "video_id": video_id,
                              "subtitle_count": item["study"]["subtitle_count"] if item["study"] else 0,
                              "marker_count": item["study"]["marker_count"] if item["study"] else 0,
                              "message": "已入库", "snapshot_id": None}
                    if content is not None:
                        import_study(store, self.journal, video_id, content,
                                     overwrite=choice["action"] == "overwrite",
                                     expected_digest=preview[identity]["local_digest"],
                                     commit_task=task["id"], commit_result=result)
                    else:
                        with store.connect() as connection:
                            save_transfer_commit(connection, task["id"], identity, result)
                    task["results"].append(result)
                    task["completed"].append(identity)
                    self.journal.save(task)
                    stage.unlink(missing_ok=True)
                    self._status(task)
                    return
            except Exception as error:
                if created_video is not None:
                    self._discard_uncommitted_video(*created_video, identity, task["id"])
                if isinstance(error, ValueError):
                    self._item_failed(task, identity, str(error))
                    stage.unlink(missing_ok=True)
                    return
                raise
        # Preserve empty groups only after all accepted items are processed.
        if any(result["state"] == "imported" for result in task["results"]):
            with self.operation_lock:
                store = self.get_store()
                self._require_task_library(task, store)
                for folder in task["offer"]["folders"]:
                    self._target_folder(task, folder["id"], store)
        task["state"] = "completed"
        task["receipt_state"] = "partial" if any(result["state"] == "failed" for result in task["results"]) else "completed"
        task["pending_receipt"] = True
        self.journal.save(task)
        self._publish_receipt(task)

    def _publish_receipt(self, task):
        self._publish(task, "receipt.json", "receipt",
                      {"state": task.get("receipt_state", "completed"), "items": task["results"], "completed_at": protocol.now()})
        task["pending_receipt"] = False
        task["pending_cleanup"] = True
        self.journal.save(task)

    def _cleanup_shared(self, task):
        allowed = {
            "chunks": {f"{item['id']}.{block['index']:06d}{suffix}"
                       for item in task["offer"]["items"] for block in item["blocks"]
                       for suffix in (".bin", ".ready.json")},
            "studies": {f"{item['id']}{suffix}" for item in task["offer"]["items"]
                        for suffix in (".studyshare", ".ready.json")},
        }
        for folder, names in allowed.items():
            path = f"tasks/{task['id']}/{folder}"
            try:
                children = self.transport.list(task["device_id"], path)
            except FileNotFoundError:
                continue
            for child in children:
                if not child.get("folder") and child["name"] in names:
                    self.transport.delete(task["device_id"], f"{path}/{child['name']}")
        for item in task["offer"]["items"]:
            (self.journal.directory / "tasks" / task["id"] / f"{item['id']}.studyshare").unlink(missing_ok=True)
        task["pending_cleanup"] = False
        self.journal.save(task)

    def _item_failed(self, task, identity, message):
        task["results"].append({"id": identity, "state": "failed", "video_id": None,
                                "subtitle_count": 0, "marker_count": 0, "message": message[:500]})
        task.setdefault("errors", []).append({"id": identity, "message": message[:500]})
        task.setdefault("completed", []).append(identity)
        self.journal.save(task)
        self._status(task)

    def _discard_uncommitted_video(self, store, video_id, managed_path, identity, task_id):
        with self.operation_lock:
            row = store.get_video(video_id)
            if (row is None or row["content_hash"] != identity or Path(row["video_path"]) != Path(managed_path)
                    or self.block_reason(video_id) or current_digest(store, video_id) is not None):
                return
            if transfer_commit(store, task_id, identity) is not None:
                return
            with store.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                if current_digest(store, video_id) is not None:
                    return
                connection.execute("DELETE FROM videos WHERE id=? AND content_hash=? AND video_path=?",
                                   (video_id, identity, str(managed_path)))
            Path(managed_path).unlink(missing_ok=True)
