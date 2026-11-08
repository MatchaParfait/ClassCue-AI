"""Append unsent native Codex images through its ordinary Windows paste handler."""

from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from ctypes import wintypes


class TargetUnavailable(Exception):
    pass


def insert_native_draft(*, uri, title, text, images):
    """Bound UIA failures in a separate process; no terminal or model interaction."""
    payload = {"uri": uri, "title": title, "text": text, "images": [str(p) for p in images]}
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if len(encoded) > 64 * 1024 * 1024:
        return {"status": "blocked", "error": "原题文字过大，已停止附图。"}
    try:
        process = subprocess.Popen(
            [sys.executable, "-m", "classcue.codex_native_draft"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError:
        return {"status": "blocked", "error": "无法启动原生附图，请重试。"}
    try:
        output, _ = process.communicate(encoded, timeout=32)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        return {"status": "unknown", "error": "附图响应超时，请先检查 Codex 输入框。"}
    try:
        result = json.loads(output)
        if result["status"] not in {"pasted", "blocked", "unknown"}:
            raise ValueError()
        return result
    except (ValueError, KeyError):
        return {"status": "unknown", "error": "未收到附图回执，请先检查 Codex 输入框。"}


def transfer(target, clipboard, *, uri, title, text, images):
    """Append once; ClassCue's persisted receipt handles repeated opens.

    Chromium's accessibility content can belong to an old chat, so never use it
    to skip a paste or claim that the current draft's attachments were verified.
    """
    mutated = False
    stage = "打开讨论"
    try:
        target.navigate(uri, title)
        stage = "保存剪贴板"
        with clipboard as clip:
            if text:
                stage = "准备题目文字"
                clip.copy("\n\n" + text)
                target.focus_end()
                target.check()
                clip.check()
                mutated = True
                stage = "粘贴题目文字"
                target.paste()
            if images:
                stage = "准备题图"
                # Keep the previous paste's clipboard alive before resolving
                # the editor again: its delayed render can replace the element.
                clip.copy(images)
                target.focus_end()
                target.check()
                clip.check()
                mutated = True
                stage = "粘贴题图"
                target.paste()
            stage = "完成粘贴"
            target.check()
        return {"status": "pasted", "image_count": len(images)}
    except Exception as error:
        message = str(error) if isinstance(error, TargetUnavailable) else f"{stage}未完成，请检查 Codex 输入框。"
        return {"status": "unknown" if mutated else "blocked", "error": message,
                "stage": stage, "error_type": type(error).__name__}


class WindowsComposer:
    """Select the official foreground app after navigating its bound thread URI."""

    def __init__(self):
        if os.name != "nt":
            raise TargetUnavailable("原生附图需要 Windows 桌面。")
        import comtypes.client
        self.com = comtypes
        self.uia = comtypes.client.GetModule("UIAutomationCore.dll")
        self.automation = comtypes.client.CreateObject(self.uia.CUIAutomation8, interface=self.uia.IUIAutomation2)
        self.automation.ConnectionTimeout = 1000
        self.automation.TransactionTimeout = 1000
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.user.GetForegroundWindow.restype = wintypes.HWND
        self.user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.GetPackageFamilyName.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.UINT), wintypes.LPWSTR]
        self.user.GetLastInputInfo.argtypes = [ctypes.c_void_p]
        self.user.GetAsyncKeyState.argtypes = [ctypes.c_int]
        self.user.GetAsyncKeyState.restype = ctypes.c_short
        self.window = None
        self.document = None
        self.composer = None

    def last_input(self):
        class LastInput(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]
        info = LastInput(ctypes.sizeof(LastInput), 0)
        if not self.user.GetLastInputInfo(ctypes.byref(info)):
            raise TargetUnavailable("无法确认键盘状态，已停止附图。")
        return info.dwTime

    def official_window(self):
        window = self.user.GetForegroundWindow()
        pid = wintypes.DWORD()
        self.user.GetWindowThreadProcessId(window, ctypes.byref(pid))
        process = self.kernel.OpenProcess(0x1000, False, pid.value)
        if not process:
            return None
        try:
            length = wintypes.UINT(256)
            name = ctypes.create_unicode_buffer(length.value)
            result = self.kernel.GetPackageFamilyName(process, ctypes.byref(length), name)
            if result != 0 or name.value != "OpenAI.Codex_2p2nqsd0c76g0":
                return None
            return window
        finally:
            self.kernel.CloseHandle(process)

    def elements(self, root, control_type):
        condition = self.automation.CreatePropertyCondition(30003, control_type)
        found = root.FindAll(4, condition)
        if found.Length > 1500:
            raise TargetUnavailable("Codex 页面尚未就绪，请重试。")
        return [found.GetElement(i) for i in range(found.Length)]

    @staticmethod
    def visible(element):
        rectangle = element.CurrentBoundingRectangle
        return not element.CurrentIsOffscreen and rectangle.right > rectangle.left and rectangle.bottom > rectangle.top

    def composers(self, root):
        return [e for e in self.elements(root, 50004)
                if self.visible(e) and e.CurrentIsEnabled
                and (e.CurrentAutomationId == "prompt-textarea"
                     or e.CurrentClassName in {"tiptap ProseMirror", "ProseMirror"}
                     or e.CurrentName in {"和 Codex 一起工作", "Work with Codex",
                         "输入后续信息", "Ask Codex", "Ask anything", "Message Codex"})]

    def navigate(self, uri, title):
        if not re.fullmatch(r"codex://threads/[a-zA-Z0-9_-]{1,100}", uri):
            raise TargetUnavailable("讨论地址无效，已停止附图。")
        self.title = title
        self.input_tick = self.last_input()
        os.startfile(uri)
        started = time.monotonic()
        deadline = started + 9
        stable = 0
        previous_window = None
        while time.monotonic() < deadline:
            if self.last_input() != self.input_tick:
                raise TargetUnavailable("检测到其他操作，已暂停附图；请重新点击讨论。")
            window = self.official_window()
            if window:
                root = self.automation.ElementFromHandle(window)
                documents = [e for e in self.elements(root, 50030)
                             if self.visible(e) and e.CurrentName == title]
                if len(documents) == 1:
                    document = documents[0]
                    edits = self.composers(document)
                    if len(edits) == 1:
                        stable = stable + 1 if previous_window == window else 1
                        previous_window = window
                        if stable >= 3 and time.monotonic() - started >= .8:
                            self.window, self.document, self.composer = window, document, edits[0]
                            self.check()
                            return
                else:
                    stable = 0
            time.sleep(.15)
        raise TargetUnavailable("未能确认目标 Codex 输入框，原讨论已保留，请重试。")

    def check(self):
        if self.last_input() != self.input_tick:
            raise TargetUnavailable("检测到其他键鼠操作，已停止附图，请检查输入框。")
        # Chromium can leave the document title cached after the composer rerenders.
        # Navigation checks the title; later input is guarded by the same foreground
        # window, unchanged user input and the exact focused composer element.
        if self.official_window() != self.window:
            raise TargetUnavailable("Codex 当前窗口已变化，已停止附图。")

    def keys(self, keys):
        self.check()
        if any(self.user.GetAsyncKeyState(key) & 0x8000 for key in [0x10, 0x11, 0x12, 0x5B, 0x5C]):
            raise TargetUnavailable("请松开修饰键后重新点击讨论。")

        class Keyboard(ctypes.Structure):
            _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

        class Mouse(ctypes.Structure):
            _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

        class InputData(ctypes.Union):
            _fields_ = [("ki", Keyboard), ("mi", Mouse)]

        class Input(ctypes.Structure):
            _fields_ = [("type", wintypes.DWORD), ("data", InputData)]

        tick = self.kernel.GetTickCount() & 0xFFFFFFFF
        events = (Input * (2 * len(keys)))()
        for index, (key, up) in enumerate([(k, False) for k in keys] + [(k, True) for k in reversed(keys)]):
            events[index].type = 1
            events[index].data.ki = Keyboard(key, 0, 2 if up else 0, tick, 0)
        self.user.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(Input), ctypes.c_int]
        count = self.user.SendInput(len(events), events, ctypes.sizeof(Input))
        if count != len(events):
            raise TargetUnavailable("键盘附图操作未完整完成，请检查输入框。")
        deadline = time.monotonic() + .2
        while self.last_input() == self.input_tick and time.monotonic() < deadline:
            time.sleep(.005)
        observed = self.last_input()
        # Windows can quantize the injected event time by one timer interval.
        # Keep a small bound instead of treating our own +16 ms event as user input.
        if (observed - tick) & 0xFFFFFFFF > 32:
            raise TargetUnavailable("检测到其他输入，已停止附图。")
        self.input_tick = observed

    def focus_end(self):
        self.check()
        # A text paste can rerender the composer and invalidate its COM element.
        # Resolve the unique current editor inside the same guarded window again.
        root = self.automation.ElementFromHandle(self.window)
        edits = self.composers(root)
        if len(edits) != 1:
            raise TargetUnavailable("未能确认唯一的 Codex 输入框，已停止附图。")
        self.composer = edits[0]
        self.composer.SetFocus()
        focused = self.automation.GetFocusedElement()
        if not self.automation.CompareElements(focused, self.composer):
            raise TargetUnavailable("未能聚焦 Codex 输入框，已停止附图。")
        self.keys([0x11, 0x23])  # Ctrl+End only; never clear, select all, or submit.

    def paste(self):
        self.check()
        if (not self.visible(self.composer) or not self.composer.CurrentIsEnabled
                or not self.automation.CompareElements(self.automation.GetFocusedElement(), self.composer)):
            raise TargetUnavailable("输入焦点已变化，已停止附图。")
        try:
            self.keys([0x11, 0x56])
        except TargetUnavailable:
            # A delivered paste key can still be queued in the target app.
            # Do not immediately restore an unrelated clipboard underneath it.
            time.sleep(.4)
            raise

def worker_main():
    from classcue.windows_clipboard import PreservedClipboard
    try:
        payload = json.loads(sys.stdin.buffer.read(64 * 1024 * 1024 + 1))
        result = transfer(WindowsComposer(), PreservedClipboard(), **payload)
    except Exception:
        result = {"status": "blocked", "error": "原生附图暂不可用，请确认 Codex 已打开后重试。"}
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=True).encode())


if __name__ == "__main__":
    worker_main()
