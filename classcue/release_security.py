"""Shared publisher/client validation. Remote content is data, never commands."""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re
import stat
import tomllib
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


MAX_METADATA = 512 * 1024
MAX_PACKAGE = 256 * 1024 * 1024
MAX_UNPACKED = 1024 * 1024 * 1024
MARKER = ".release-install.json"
FEED_TAG = "update-feed"


def version(value):
    if not isinstance(value, str) or not re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", value):
        raise ValueError("版本号必须为稳定版 major.minor.patch")
    return tuple(map(int, value.split(".")))


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def runtime_hash(path):
    lock = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    for package in lock.get("package", []):
        if package.get("source") == {"virtual": "."}:
            package.pop("version", None)
    return hashlib.sha256(canonical(lock)).hexdigest()


def verify(envelope, public_key):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    try:
        key = base64.b64decode(public_key, validate=True)
        signature = base64.b64decode(envelope["signature"], validate=True)
        payload = envelope["payload"]
        Ed25519PublicKey.from_public_bytes(key).verify(signature, canonical(payload))
    except Exception as error:
        raise ValueError("发行签名验证失败，未接受远程内容") from error
    if not isinstance(payload, dict) or payload.get("schema") != 1:
        raise ValueError("不支持的发行元数据格式")
    return payload


def sign(payload, private_key):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(private_key, validate=True))
    return {"payload": payload, "signature": base64.b64encode(key.sign(canonical(payload))).decode("ascii")}


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("时间必须为 ISO 格式字符串")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("时间必须包含时区")
    result = parsed.timestamp()
    if not math.isfinite(result):
        raise ValueError("时间无效")
    return result


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def config(root):
    value = json.loads((Path(root) / "release_config.json").read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("repository"), str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value["repository"]):
        raise ValueError("无效的发行仓库")
    if not isinstance(value.get("public_key"), str):
        raise ValueError("发行公钥无效")
    if value["public_key"] and len(base64.b64decode(value["public_key"], validate=True)) != 32:
        raise ValueError("发行公钥无效")
    if type(value.get("data_schema")) is not int or value["data_schema"] < 1:
        raise ValueError("数据兼容版本无效")
    return value


def asset_url(repository, tag, name):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", tag) or not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        raise ValueError("无效的发行附件名称")
    return f"https://github.com/{repository}/releases/download/{tag}/{name}"


def https_url(value, *, hosts=None):
    if not isinstance(value, str) or value.strip() != value or any(ord(char) < 32 for char in value):
        raise ValueError("无效的 HTTPS 地址")
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.port not in (None, 443) or (hosts and parsed.hostname not in hosts)):
        raise ValueError("仅允许可信 HTTPS 地址")
    return value


DOWNLOAD_HOSTS = {"github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"}


class ReleaseRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        https_url(newurl, hosts=DOWNLOAD_HOSTS)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_download(url, *, headers=None):
    https_url(url, hosts=DOWNLOAD_HOSTS)
    response = build_opener(ReleaseRedirects()).open(
        Request(url, headers={"User-Agent": "ClassroomVideoStudy-Updater", "Accept": "application/octet-stream", **(headers or {})}), timeout=15,
    )
    https_url(response.url, hosts=DOWNLOAD_HOSTS)
    return response


def read_remote_json(url):
    with open_download(url) as response:
        data = response.read(MAX_METADATA + 1)
    if len(data) > MAX_METADATA:
        raise ValueError("发行元数据过大")
    return json.loads(data)


def managed_name(name):
    """Validate update paths, including legacy inventories; not a source selector."""
    if not isinstance(name, str) or "\\" in name or len(name) > 240:
        raise ValueError("无效的包内路径")
    parts = PurePosixPath(name).parts
    if not parts or "/".join(parts) != name or name.startswith("/"):
        raise ValueError("无效的包内路径")
    forbidden = {".git", ".runtime", ".venv", ".env", "data", "models", "projects", "node_modules", "__pycache__"}
    for part in parts:
        if (part in {".", ".."} or part.casefold() in forbidden or part[-1:] in {".", " "}
                or any(char in part for char in '<>:"|?*\0') or any(ord(char) < 32 for char in part)
                or re.fullmatch(r"(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\..*)?", part, re.I)):
            raise ValueError("包内路径指向保留文件或用户数据")
    if name == MARKER:
        return name
    root_files = {"pyproject.toml", "uv.lock", "requirements.txt", "release_config.json", "LICENSE",
                  "README.md", "CONTRIBUTING.md", ".env.example"}
    if len(parts) == 1 and (name in root_files or name.endswith((".py", ".cmd"))):
        return name
    if parts[0] in {"classcue", "api", "scripts", "assets", "prompts", "pages", "docs"}:
        return name
    if len(parts) > 2 and parts[:2] == ("frontend", "dist"):
        return name
    raise ValueError("包内文件不属于程序分发范围")


def local_path(root, relative):
    root = Path(root).absolute()
    target = root / relative
    target.relative_to(root)
    if ".." in Path(relative).parts:
        raise ValueError("路径不能离开应用目录")
    for path in [root, *root.parents]:
        if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
            raise ValueError("更新路径不能经过符号链接或目录联接")
    current = root
    for part in target.relative_to(root).parts:
        current = current / part
        if current.is_symlink() or getattr(current, "is_junction", lambda: False)():
            raise ValueError("更新路径不能经过符号链接或目录联接")
    return target


def safe_target(root, name):
    managed_name(name)
    return local_path(root, Path(*PurePosixPath(name).parts))


def validate_inventory(payload):
    version(payload.get("version"))
    files = payload.get("files")
    if not isinstance(files, dict) or not 1 <= len(files) <= 5000 or MARKER in files:
        raise ValueError("发行文件清单无效")
    normalized = set()
    for name, checksum in files.items():
        managed_name(name)
        key = name.casefold()
        if key in normalized or not isinstance(checksum, str) or not re.fullmatch(r"[a-f0-9]{64}", checksum):
            raise ValueError("文件名冲突或校验值无效")
        normalized.add(key)
    for name in normalized:
        if any(str(parent) in normalized for parent in PurePosixPath(name).parents if str(parent) != "."):
            raise ValueError("包内文件与目录冲突")
    required = {"pyproject.toml", "uv.lock", "release_config.json", "scripts/desktop_launcher.vbs", "frontend/dist/index.html"}
    if not required.issubset(files):
        raise ValueError("发行包缺少必要程序文件")
    if type(payload.get("data_schema")) is not int or payload["data_schema"] < 1:
        raise ValueError("数据兼容版本无效")
    return files


def validate_feed(payload, repository, *, now, minimum_revision=0):
    if payload.get("repository") != repository or type(payload.get("revision")) is not int:
        raise ValueError("公告源身份无效")
    if payload["revision"] < minimum_revision:
        raise ValueError("公告源版本倒退，保留已验证内容")
    issued, expires = timestamp(payload["issued_at"]), timestamp(payload["expires_at"])
    if issued > now + 300 or expires <= now or not 0 < expires - issued <= 32 * 86400:
        raise ValueError("公告源已过期或发布时间无效")
    release = payload.get("release")
    if release is not None:
        version(release.get("version"))
        expected = asset_url(repository, "v" + release["version"], release["asset"])
        if release.get("url") != expected or not re.fullmatch(r"[a-f0-9]{64}", release.get("sha256", "")):
            raise ValueError("更新包地址或哈希无效")
        if type(release.get("size")) is not int or not 0 < release["size"] <= MAX_PACKAGE:
            raise ValueError("更新包大小无效")
        if not isinstance(release.get("notes"), str) or len(release["notes"]) > 30000:
            raise ValueError("更新日志无效")
        if not re.fullmatch(r"[a-f0-9]{64}", release.get("runtime_hash", "")) or type(release.get("data_schema")) is not int or release["data_schema"] < 1:
            raise ValueError("兼容信息无效")
    announcements = payload.get("announcements", [])
    if not isinstance(announcements, list) or len(announcements) > 100:
        raise ValueError("公告数量无效")
    seen = set()
    for item in announcements:
        if not isinstance(item, dict) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", item.get("id", "")) or item["id"] in seen:
            raise ValueError("公告标识无效或重复")
        seen.add(item["id"])
        for field, limit in [("title", 120), ("body", 12000)]:
            if not isinstance(item.get(field), str) or not 0 < len(item[field]) <= limit:
                raise ValueError("公告内容无效")
        if item.get("level", "info") not in {"info", "important"}:
            raise ValueError("公告级别无效")
        timestamp(item["published_at"])
        if item.get("expires_at"):
            if timestamp(item["expires_at"]) <= timestamp(item["published_at"]):
                raise ValueError("公告有效期无效")
        for field in ("min_version", "max_version"):
            if item.get(field):
                version(item[field])
        if item.get("url"):
            https_url(item["url"])
    return payload


def validate_zip_entry(info):
    managed_name(info.filename)
    mode = info.external_attr >> 16
    if stat.S_ISLNK(mode) or info.flag_bits & 1:
        raise ValueError("更新包不允许链接或加密文件")
