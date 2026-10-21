"""Windows-user encrypted local storage for optional API-key memory."""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from pathlib import Path

from classcue.diagnostics import register_secret


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob(data: bytes) -> tuple[_DataBlob, ctypes.Array[ctypes.c_char]]:
    buffer = ctypes.create_string_buffer(data)
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer


def _crypt_protect(data: bytes) -> bytes:
    if os.name != "nt":
        raise RuntimeError("API Key 记忆仅支持 Windows 本机环境")
    source, source_buffer = _blob(data)
    protected = _DataBlob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    if not crypt32.CryptProtectData(ctypes.byref(source), "课堂考点 DeepSeek API Key", None, None, None, 0, ctypes.byref(protected)):
        raise OSError(ctypes.get_last_error(), "无法加密 API Key")
    try:
        return ctypes.string_at(protected.pbData, protected.cbData)
    finally:
        ctypes.WinDLL("kernel32", use_last_error=True).LocalFree(protected.pbData)


def _crypt_unprotect(data: bytes) -> bytes:
    if os.name != "nt":
        raise RuntimeError("API Key 记忆仅支持 Windows 本机环境")
    source, source_buffer = _blob(data)
    unprotected = _DataBlob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    if not crypt32.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 0, ctypes.byref(unprotected)):
        raise OSError(ctypes.get_last_error(), "无法解密已保存的 API Key")
    try:
        return ctypes.string_at(unprotected.pbData, unprotected.cbData)
    finally:
        ctypes.WinDLL("kernel32", use_last_error=True).LocalFree(unprotected.pbData)


def load_api_key(path: str | Path) -> str | None:
    key_path = Path(path)
    if not key_path.is_file():
        return None
    try:
        value = _crypt_unprotect(key_path.read_bytes()).decode("utf-8").strip() or None
        register_secret(value)
        return value
    except (OSError, UnicodeDecodeError):
        return None


def save_api_key(path: str | Path, api_key: str) -> None:
    value = api_key.strip()
    if not value:
        raise ValueError("API Key 不能为空")
    register_secret(value)
    key_path = Path(path)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_bytes(_crypt_protect(value.encode("utf-8")))


def delete_api_key(path: str | Path) -> bool:
    key_path = Path(path)
    if not key_path.is_file():
        return False
    key_path.unlink()
    return True
