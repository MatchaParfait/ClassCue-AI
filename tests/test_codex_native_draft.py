from pathlib import Path
from types import SimpleNamespace

import pytest

from classcue.codex_native_draft import TargetUnavailable, WindowsComposer, transfer


class Target:
    def __init__(self):
        self.draft = "用户已有草稿"
        self.images = {"user-image.png"}
        self.actions = []
        self.clipboard = None
        self.changed = False

    def navigate(self, uri, title):
        self.actions.append(("navigate", uri))

    def check(self):
        if self.changed:
            raise TargetUnavailable("讨论已变化")

    def text(self):
        raise AssertionError("Cached accessibility text must not control a paste")

    def focus_end(self):
        self.check()
        self.actions.append(("focus_end",))

    def has_image(self, name):
        raise AssertionError("Cached attachments must not control a paste")

    def paste(self):
        self.check()
        self.actions.append(("paste",))
        if isinstance(self.clipboard, str):
            self.draft += self.clipboard
        else:
            self.images.update(Path(p).name for p in self.clipboard)

class Clipboard:
    def __init__(self, target):
        self.target = target
        self.restored = False

    def __enter__(self):
        return self

    def copy(self, value):
        self.target.clipboard = value

    def check(self):
        pass

    def __exit__(self, *_):
        self.restored = True


def run(target, **kwargs):
    clip = Clipboard(target)
    result = transfer(target, clip, uri="codex://threads/synthetic", title="合成讨论",
                      text="合成原题", images=[Path("question-1.png"), Path("question-2.png")], **kwargs)
    assert clip.restored
    return result


def test_native_paste_preserves_draft_and_existing_images_and_never_submits():
    target = Target()
    assert run(target) == {"status": "pasted", "image_count": 2}
    assert target.draft == "用户已有草稿\n\n合成原题"
    assert target.images == {"user-image.png", "question-1.png", "question-2.png"}
    assert [a[0] for a in target.actions] == ["navigate", "focus_end", "paste", "focus_end", "paste"]


def test_changed_target_before_input_blocks_without_pasting():
    target = Target()
    target.changed = True
    assert run(target)["status"] == "blocked"
    assert not any(a[0] == "paste" for a in target.actions)


def test_cached_old_content_never_skips_a_first_paste():
    target = Target()
    target.draft = "合成原题"
    target.images.update({"question-1.png", "question-2.png"})
    assert run(target)["status"] == "pasted"
    assert [a[0] for a in target.actions].count("paste") == 2


def test_target_change_after_text_never_pastes_images_into_another_chat():
    target = Target()
    original = target.paste

    def paste():
        original()
        target.changed = True

    target.paste = paste
    assert run(target)["status"] == "unknown"
    assert target.images == {"user-image.png"}


def test_composer_rerender_between_text_and_images_uses_current_editor():
    target = WindowsComposer.__new__(WindowsComposer)
    target.window = 42
    target.input_tick = 100
    target.last_input = lambda: 100
    target.official_window = lambda: 42
    focused = [None]
    editor = SimpleNamespace(SetFocus=lambda: focused.__setitem__(0, editor))

    class RemovedEditor:
        def SetFocus(self):
            raise AssertionError("The previous composer was removed after text paste")

    target.composer = RemovedEditor()
    target.automation = SimpleNamespace(
        ElementFromHandle=lambda window: window,
        GetFocusedElement=lambda: focused[0],
        CompareElements=lambda a, b: a is b,
    )
    target.composers = lambda _: [editor]
    actions = []
    target.keys = lambda keys: actions.append(keys)
    target.focus_end()
    assert focused[0] is editor
    assert actions == [[0x11, 0x23]]


def test_paste_focus_is_refreshed_after_waiting_for_previous_clipboard():
    target = Target()
    target.focused = False
    original_focus = target.focus_end
    original_paste = target.paste

    def focus():
        original_focus()
        target.focused = True

    def paste():
        assert target.focused, "The editor rerendered while the previous paste was settling"
        original_paste()

    class DelayedClipboard(Clipboard):
        def copy(self, value):
            super().copy(value)
            target.focused = False

    target.focus_end, target.paste = focus, paste
    result = transfer(target, DelayedClipboard(target), uri="codex://threads/synthetic",
                      title="合成讨论", text="合成原题", images=[Path("question-1.png")])
    assert result["status"] == "pasted"
    assert "question-1.png" in target.images


@pytest.mark.parametrize("tick,delta,allowed", [(1000, 0, True), (1000, 16, True),
                                               (0xFFFFFFF8, 16, True), (1000, 100, False)])
def test_injected_keys_accept_windows_timer_rounding_and_reject_later_input(tick, delta, allowed):
    target = WindowsComposer.__new__(WindowsComposer)
    observed = [tick - 500]
    target.input_tick = observed[0]
    target.check = lambda: None
    target.last_input = lambda: observed[0]

    def send(count, events, size):
        assert count == 4 and size in {28, 40}
        assert [event.data.ki.wVk for event in events] == [0x11, 0x23, 0x23, 0x11]
        observed[0] = (tick + delta) & 0xFFFFFFFF
        return count

    target.user = SimpleNamespace(GetAsyncKeyState=lambda _: 0, SendInput=send)
    target.kernel = SimpleNamespace(GetTickCount=lambda: tick)
    if allowed:
        target.keys([0x11, 0x23])
        assert target.input_tick == observed[0]
    else:
        with pytest.raises(TargetUnavailable, match="检测到其他输入"):
            target.keys([0x11, 0x23])


def test_cached_document_title_does_not_block_the_unchanged_composer():
    target = WindowsComposer.__new__(WindowsComposer)
    target.window = 42
    target.input_tick = 100
    target.last_input = lambda: 100
    target.official_window = lambda: 42
    target.document = SimpleNamespace(CurrentName="缓存标题")
    target.title = "合成讨论"
    target.composer = SimpleNamespace(CurrentIsEnabled=True)
    target.visible = lambda _: True
    target.check()
    target.official_window = lambda: 43
    with pytest.raises(TargetUnavailable, match="当前窗口"):
        target.check()
