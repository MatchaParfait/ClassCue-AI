"""Versioned, data-only USB messages; independent of the Android implementation."""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import secrets
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from classcue import study_share
from classcue.local_credentials import _crypt_protect, _crypt_unprotect

BLOCK_BYTES = 32 * 1024 * 1024
MAX_CONTROL_BYTES = 8 * 1024 * 1024
ROOT_NAME = "ClassCue-Link"
SHA_PATTERN = re.compile(r"^[0-9a-f]{64}$")
ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical(value: Any) -> bytes:
    if value is None:
        return b"null"
    if value is True:
        return b"true"
    if value is False:
        return b"false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False).replace("\u2028", "\\u2028").replace("\u2029", "\\u2029").encode("utf-8")
    if isinstance(value, (int, float, Decimal)):
        number = Decimal(str(value))
        if not number.is_finite():
            raise ValueError("数值无效")
        text = format(number, "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return ("0" if number == 0 else text).encode("ascii")
    if isinstance(value, list):
        return b"[" + b",".join(canonical(item) for item in value) + b"]"
    if isinstance(value, dict):
        if any(not key.isascii() for key in value):
            raise ValueError("协议字段名无效")
        return b"{" + b",".join(canonical(key) + b":" + canonical(value[key]) for key in sorted(value)) + b"}"
    raise ValueError("不支持的数据类型")


def semantic_digest(content: bytes) -> str:
    study_share.decode_package(content)
    root = json.loads(content, parse_float=Decimal)
    subtitles = []
    for source in sorted(root["subtitles"], key=lambda s: (s["start_ms"], s["id"])):
        subtitle = {key: source[key] for key in ("id", "start_ms", "end_ms", "text")}
        subtitle["words"] = [{**word, "probability": word.get("probability")} for word in source.get("words", [])]
        subtitles.append(subtitle)
    runs = []
    for source in root["analysis_runs"]:
        mode = source["analysis_mode"]
        markers = []
        for original in root["markers"]:
            if original["analysis_mode"] != mode:
                continue
            marker = {key: value for key, value in original.items() if key != "analysis_mode"}
            marker.setdefault("importance", "standard")
            marker.setdefault("user_start_ms", None)
            markers.append(marker)
        markers.sort(key=lambda marker: hashlib.sha256(canonical(marker)).hexdigest())
        runs.append({"mode": "custom" if mode.startswith("custom_") else mode, "name": source["name"], "markers": markers})
    runs.sort(key=lambda run: hashlib.sha256(canonical(run)).hexdigest())
    return hashlib.sha256(canonical({"subtitles": subtitles, "runs": runs})).hexdigest()


def public_key(private: ec.EllipticCurvePrivateKey) -> str:
    return base64.b64encode(private.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)).decode("ascii")


def peer_id(key: str) -> str:
    return hashlib.sha256(base64.b64decode(key, validate=True)).hexdigest()


def pairing_code(left: str, right: str, nonce: str) -> str:
    digest = hashlib.sha256(("\n".join(sorted((left, right))) + "\n" + nonce).encode("utf-8")).digest()
    return f"{int.from_bytes(digest[:4], 'big') % 1000000:06d}"


def load_identity(directory: Path) -> ec.EllipticCurvePrivateKey:
    path = directory / "identity.dpapi"
    if path.is_file():
        return serialization.load_der_private_key(_crypt_unprotect(path.read_bytes()), password=None)
    directory.mkdir(parents=True, exist_ok=True)
    key = ec.generate_private_key(ec.SECP256R1())
    data = key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    temporary = path.with_suffix(".partial")
    temporary.write_bytes(_crypt_protect(data))
    temporary.replace(path)
    return key


def envelope(key: ec.EllipticCurvePrivateKey, message_type: str, message_id: str, recipient: str,
             data: dict, sequence: int = 0) -> bytes:
    public = public_key(key)
    body = {"type": message_type, "id": message_id, "sender": peer_id(public), "recipient": recipient,
            "sequence": sequence, "created_at": now(), "data": data}
    payload = json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    signature = key.sign(payload, ec.ECDSA(hashes.SHA256()))
    encoded = json.dumps({"format": "classcue-link", "version": 1,
                      "payload": base64.b64encode(payload).decode("ascii"), "public_key": public,
                      "signature": base64.b64encode(signature).decode("ascii")}, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_CONTROL_BYTES:
        raise ValueError("连接消息超过大小限制，请减少单次发送的课程数量")
    return encoded


def open_envelope(content: bytes, *, key: str | None = None, recipient: str | None = None,
                  message_id: str | None = None, message_type: str | None = None) -> tuple[dict, str]:
    if not content or len(content) > MAX_CONTROL_BYTES:
        raise ValueError("连接消息为空或过大")
    outer = json.loads(content)
    if set(outer) != {"format", "version", "payload", "public_key", "signature"} or outer["format"] != "classcue-link" or type(outer["version"]) is not int or outer["version"] != 1:
        raise ValueError("不支持的连接协议")
    public = outer["public_key"]
    if key is not None and public != key:
        raise ValueError("设备身份已改变，请重新配对")
    verifier = serialization.load_der_public_key(base64.b64decode(public, validate=True))
    if not isinstance(verifier, ec.EllipticCurvePublicKey) or not isinstance(verifier.curve, ec.SECP256R1):
        raise ValueError("设备公钥类型无效")
    payload = base64.b64decode(outer["payload"], validate=True)
    verifier.verify(base64.b64decode(outer["signature"], validate=True), payload, ec.ECDSA(hashes.SHA256()))
    result = json.loads(payload)
    if set(result) != {"type", "id", "sender", "recipient", "sequence", "created_at", "data"} or result["sender"] != peer_id(public):
        raise ValueError("设备消息身份无效")
    if recipient is not None and result["recipient"] != recipient:
        raise ValueError("消息接收设备不匹配")
    if message_id is not None and result["id"] != message_id:
        raise ValueError("传输任务编号不匹配")
    if message_type is not None and result["type"] != message_type:
        raise ValueError("传输消息类型不匹配")
    if type(result["sequence"]) is not int or result["sequence"] < 0 or not isinstance(result["data"], dict):
        raise ValueError("传输消息内容无效")
    return result, public


def token() -> str:
    return secrets.token_hex(24)


def validate_offer(data: dict) -> dict:
    if not isinstance(data, dict) or set(data) != {"title", "course_id", "folders", "items", "total_bytes"}:
        raise ValueError("课程清单字段无效")
    if not isinstance(data.get("title"), str) or not 1 <= len(data["title"]) <= 200:
        raise ValueError("课程名称无效")
    if not isinstance(data.get("course_id"), str) or not ID_PATTERN.fullmatch(data["course_id"]):
        raise ValueError("课程编号无效")
    items = data.get("items")
    folders = data.get("folders")
    if not isinstance(items, list) or not 1 <= len(items) <= 10000 or not isinstance(folders, list) or len(folders) > 10000:
        raise ValueError("课程条目数量无效")
    ids = set()
    folder_map = {}
    for folder in folders:
        if not isinstance(folder, dict) or set(folder) != {"id", "name", "parent_id"}:
            raise ValueError("课程分组字段无效")
        fid = folder.get("id")
        if not isinstance(fid, str) or not ID_PATTERN.fullmatch(fid) or fid in folder_map:
            raise ValueError("课程分组编号无效")
        if not isinstance(folder.get("name"), str) or not 1 <= len(folder["name"].strip()) <= 200:
            raise ValueError("课程分组名称无效")
        folder_map[fid] = folder
    for fid in folder_map:
        seen = set()
        current = fid
        while current is not None:
            if current in seen or current not in folder_map:
                raise ValueError("课程分组层级无效")
            seen.add(current)
            current = folder_map[current].get("parent_id")
    for item in items:
        if not isinstance(item, dict) or set(item) != {"id", "name", "size", "duration_ms", "mime", "folder_id", "blocks", "study"}:
            raise ValueError("视频清单字段无效")
        identity = item.get("id")
        if not isinstance(identity, str) or not SHA_PATTERN.fullmatch(identity) or identity in ids:
            raise ValueError("课程视频编号无效")
        ids.add(identity)
        size = item.get("size")
        duration = item.get("duration_ms")
        if type(size) is not int or not 1 <= size <= 1024**3 or type(duration) is not int or not 1 <= duration <= 3600000:
            raise ValueError("课程视频大小或时长超出支持范围")
        if not isinstance(item.get("name"), str) or not 1 <= len(item["name"]) <= 200:
            raise ValueError("视频名称无效")
        if not isinstance(item.get("mime"), str) or len(item["mime"]) > 128 or not item["mime"].startswith("video/"):
            raise ValueError("视频媒体类型无效")
        if item.get("folder_id") is not None and item["folder_id"] not in folder_map:
            raise ValueError("视频所属分组无效")
        blocks = item.get("blocks", [])
        if not isinstance(blocks, list) or len(blocks) != (size + BLOCK_BYTES - 1) // BLOCK_BYTES:
            raise ValueError("视频数据块数量无效")
        for index, block in enumerate(blocks):
            if (not isinstance(block, dict) or set(block) != {"index", "size", "sha256"}
                    or type(block.get("index")) is not int or type(block.get("size")) is not int
                    or block["index"] != index or block["size"] != min(BLOCK_BYTES, size - index * BLOCK_BYTES)
                    or not SHA_PATTERN.fullmatch(str(block.get("sha256", "")))):
                raise ValueError("视频数据块无效")
        study = item.get("study")
        if study is not None and (not isinstance(study, dict) or type(study.get("size")) is not int or not 1 <= study["size"] <= study_share.MAX_SHARE_BYTES or not all(SHA_PATTERN.fullmatch(str(study.get(key, ""))) for key in ("sha256", "semantic_digest"))):
            raise ValueError("学习资料摘要无效")
        if study is not None:
            if set(study) != {"size", "sha256", "semantic_digest", "subtitle_count", "marker_count", "runs"}:
                raise ValueError("学习资料清单字段无效")
            if any(type(study.get(key)) is not int or not 0 <= study[key] <= 1000000 for key in ("subtitle_count", "marker_count")):
                raise ValueError("学习资料数量无效")
            runs = study.get("runs")
            if not isinstance(runs, list) or len(runs) > 1000:
                raise ValueError("学习方案清单无效")
            modes = set()
            for run in runs:
                if (not isinstance(run, dict) or set(run) != {"mode", "name", "count"}
                        or not isinstance(run["mode"], str) or not ID_PATTERN.fullmatch(run["mode"])
                        or run["mode"] in modes or not isinstance(run["name"], str) or not 1 <= len(run["name"]) <= 200
                        or type(run["count"]) is not int or not 0 <= run["count"] <= 1000000):
                    raise ValueError("学习方案条目无效")
                modes.add(run["mode"])
            if sum(run["count"] for run in runs) != study["marker_count"]:
                raise ValueError("学习方案考点数量不匹配")
    if type(data["total_bytes"]) is not int or data["total_bytes"] != sum(item["size"] for item in items):
        raise ValueError("课程总大小无效")
    return data
