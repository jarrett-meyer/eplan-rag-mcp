"""Offline tests for ui_windows (dialog list/click) and the two dialog tools.

Win32 is faked by monkeypatching _enum / _post_click / _is_window; the only
real Win32 call is the smoke test against this test process's own windows.
"""

import os
import sys

import pytest

import ui_windows
from api.actions import dialogs

DLG = 0x1234


def _raw(hwnd=DLG, pid=42, title="Project master data", cls="#32770", enabled=True,
         children=((1, "Button", "&Yes"), (2, "Button", "&No"), (3, "Static", "Sync?"))):
    return {"hwnd": hwnd, "pid": pid, "title": title, "class": cls, "owner": 0,
            "enabled": enabled, "children": list(children)}


@pytest.fixture
def fake(monkeypatch):
    state = {"windows": [_raw()], "clicked": []}
    monkeypatch.setattr(ui_windows, "_enum", lambda pids, *a: state["windows"])
    monkeypatch.setattr(ui_windows, "_post_click",
                        lambda h: state["clicked"].append(h) or True)
    monkeypatch.setattr(ui_windows, "_is_window", lambda h: not state["clicked"])
    return state


def test_norm_button_strips_mnemonic():
    assert ui_windows._norm_button("&No") == "No"
    assert ui_windows._norm_button(" Cancel ") == "Cancel"
    assert ui_windows._norm_button(None) == ""


def test_kind_reads_win32_and_winforms_classes():
    assert ui_windows._kind("Button") == "button"
    assert ui_windows._kind("WindowsForms10.BUTTON.app.0.2bf8098_r6_ad1") == "button"
    assert ui_windows._kind("Static") == "text"
    assert ui_windows._kind("RichEdit20W") == "text"
    assert ui_windows._kind("msctls_progress32") == ""


@pytest.mark.parametrize("is_main,enabled,progress,cls,main_disabled,expected", [
    (False, True, False, "#32770", False, True),           # message box
    (False, True, False, "WindowsForms10.Window", True, True),  # WinForms modal
    (False, True, False, "WindowsForms10.Window", False, False),  # tool window
    (False, True, True, "#32770", True, False),            # progress box
    (False, False, False, "#32770", True, False),          # disabled (nested modal parent)
    (True, False, False, "Afx:0001", True, False),         # main window itself
])
def test_is_blocking(is_main, enabled, progress, cls, main_disabled, expected):
    assert ui_windows._is_blocking(is_main, enabled, progress, cls, main_disabled) is expected


def test_list_windows_shapes_and_hides_main(fake):
    fake["windows"] = [_raw(hwnd=1, cls="Afx:400000:8", enabled=False, children=()),
                       _raw(cls="WindowsForms10.Window.8.app", children=(
                           (5, "WindowsForms10.BUTTON.app", "&OK"),
                           (6, "msctls_progress32", "")))]
    shown = ui_windows.list_windows({42})
    assert [w["hwnd"] for w in shown] == [DLG]
    assert shown[0]["buttons"] == ["OK"] and shown[0]["has_progress"]
    assert shown[0]["blocking"] is False                    # progress box is not a prompt
    both = ui_windows.list_windows({42}, include_main=True)
    assert both[0]["is_main"] and not both[0]["blocking"]


def test_list_windows_flags_message_box(fake):
    (w,) = ui_windows.list_windows({42})
    assert w["blocking"] and w["buttons"] == ["Yes", "No"] and w["text"] == ["Sync?"]


@pytest.mark.parametrize("main_enabled, modal", [(True, False), (False, True)])
def test_list_windows_marks_modal_only_behind_a_disabled_main(fake, main_enabled, modal):
    fake["windows"] = [_raw(hwnd=1, cls="Afx:400000:8", enabled=main_enabled, children=()),
                       _raw()]
    (w,) = ui_windows.list_windows({42})
    assert w["blocking"] and w["modal"] is modal


def test_list_windows_never_raises(monkeypatch):
    def boom(*a):
        raise OSError("no user32")
    monkeypatch.setattr(ui_windows, "_enum", boom)
    assert ui_windows.list_windows({1}) == []


def test_safe_buttons_allowlist():
    assert ui_windows.SAFE_BUTTONS == {"OK", "Cancel", "Close", "No", "Skip"}


def test_click_safe_button(fake):
    res = ui_windows.click_button(DLG, "No", "Project master data", {42}, wait_s=0.5)
    assert res == {"success": True, "hwnd": DLG, "title": "Project master data",
                   "class": "#32770", "button": "No", "closed": True}
    assert fake["clicked"] == [2]


def test_click_refuses_unsafe_button_without_touching_windows(fake):
    res = ui_windows.click_button(DLG, "&Yes", "Project master data", {42})
    assert res["errorType"] == "ButtonNotAllowed" and res["next_step"]
    assert fake["clicked"] == []


def test_click_allow_any_permits_yes(fake):
    res = ui_windows.click_button(DLG, "Yes", "Project master data", {42},
                                  allow_any=True, wait_s=0.1)
    assert res["success"] and fake["clicked"] == [1]


def test_click_refuses_title_mismatch(fake):
    res = ui_windows.click_button(DLG, "No", "Something else", {42})
    assert res["errorType"] == "DialogNotFound" and fake["clicked"] == []


def test_click_refuses_foreign_pid(fake):
    fake["windows"] = [_raw(pid=99)]
    res = ui_windows.click_button(DLG, "No", "Project master data", {42})
    assert res["errorType"] == "DialogNotFound" and fake["clicked"] == []


def test_click_refuses_missing_button(fake):
    res = ui_windows.click_button(DLG, "Cancel", "Project master data", {42})
    assert res["errorType"] == "ButtonNotFound" and "Yes, No" in res["error"]
    assert fake["clicked"] == []


def test_list_dialogs_scopes_to_connected_pid(monkeypatch):
    seen = {}
    monkeypatch.setattr(dialogs, "get_manager", lambda: type("M", (), {"pid": 7})())
    monkeypatch.setattr(dialogs, "pid_alive", lambda pid: True)
    monkeypatch.setattr(dialogs, "eplan_pids", lambda: pytest.fail("must not scan"))
    monkeypatch.setattr(dialogs.ui_windows, "list_windows",
                        lambda pids, include_main: seen.setdefault("pids", pids) and
                        [{"blocking": True}])
    monkeypatch.setattr(dialogs, "eplan_state", lambda pids: "responding")
    res = dialogs.list_dialogs()
    assert seen["pids"] == {7}
    assert res == {"success": True, "count": 1, "windows": [{"blocking": True}],
                   "modal_suspected": True, "eplan_state": "responding"}


def test_click_dialog_button_falls_back_to_all_eplan_pids(monkeypatch):
    seen = {}
    monkeypatch.setattr(dialogs, "get_manager", lambda: object())   # no pid yet
    monkeypatch.setattr(dialogs, "eplan_pids", lambda: [3, 4])
    monkeypatch.setattr(dialogs.ui_windows, "click_button",
                        lambda h, b, t, pids, allow_any:
                        seen.update(pids=pids, any=allow_any) or {"success": True})
    dialogs.click_dialog_button(1, "OK", "x", allow_any_button=True)
    assert seen == {"pids": {3, 4}, "any": True}


@pytest.mark.skipif(sys.platform != "win32", reason="Win32 only")
def test_list_windows_smoke_on_own_process():
    # _enum, not list_windows: list_windows swallows errors, so a broken ctypes
    # signature would pass silently there.
    assert isinstance(ui_windows._enum({os.getpid()}), list)


def test_stale_connected_pid_falls_back_to_eplan_pids(monkeypatch):
    monkeypatch.setattr(dialogs, "get_manager", lambda: type("M", (), {"pid": 7})())
    monkeypatch.setattr(dialogs, "pid_alive", lambda pid: False)   # PID reused
    monkeypatch.setattr(dialogs, "eplan_pids", lambda: [8])
    assert dialogs._pids() == {8}


def test_module_imports_without_winfunctype(monkeypatch):
    import ctypes
    import importlib
    monkeypatch.delattr(ctypes, "WINFUNCTYPE", raising=False)
    try:
        importlib.reload(ui_windows)
    finally:
        monkeypatch.undo()
        importlib.reload(ui_windows)


def test_file_dialog_with_a_progress_control_still_blocks(fake):
    # Live 2026-10-10: EPLAN's "Open project" (#32770) carries msctls_progress32
    # in its Explorer address bar; treating it as a progress box hid it for 600s.
    fake["windows"] = [_raw(cls="#32770", children=(
        (5, "Button", "&Open"), (6, "Button", "Cancel"), (7, "msctls_progress32", "")))]
    shown = ui_windows.list_windows({42})
    assert shown[0]["has_progress"] and shown[0]["blocking"] is True
