"""List and click EPLAN's top-level windows from outside EPLAN (Win32 via ctypes).

A modal dialog blocks every remoting call, so nothing here touches the EPLAN
API: windows are found by owning PID and read with bounded window messages.
Nothing here raises; on any error, or off Windows, the result is [] or a
failure dict.
"""

# ponytail: Win32 HWNDs only. Covers #32770 message boxes and WinForms dialogs
# (every control is an HWND); a WPF dialog shows only its title. Add UI
# Automation (out of process) if such a dialog ever has to be clicked.

import ctypes
import time
from ctypes import wintypes

WM_GETTEXT, WM_GETTEXTLENGTH, BM_CLICK = 0x000D, 0x000E, 0x00F5
SMTO_ABORTIFHUNG, GW_OWNER = 0x0002, 4
MAX_TEXT_LINES, MAX_TEXT_CHARS = 20, 500

SAFE_BUTTONS = frozenset({"OK", "Cancel", "Close", "No", "Skip"})

# WINFUNCTYPE exists only on Windows; CFUNCTYPE keeps the import working
# elsewhere, where WinDLL then fails inside the guarded calls.
_WNDENUMPROC = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)(
    wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
_u32 = None


def _user32():
    global _u32
    if _u32 is None:
        api = ctypes.WinDLL("user32", use_last_error=True)
        signatures = {
            "EnumWindows": ([_WNDENUMPROC, wintypes.LPARAM], wintypes.BOOL),
            "EnumChildWindows": ([wintypes.HWND, _WNDENUMPROC, wintypes.LPARAM], wintypes.BOOL),
            "GetWindowThreadProcessId": ([wintypes.HWND, ctypes.POINTER(wintypes.DWORD)],
                                         wintypes.DWORD),
            "IsWindow": ([wintypes.HWND], wintypes.BOOL),
            "IsHungAppWindow": ([wintypes.HWND], wintypes.BOOL),
            "IsWindowVisible": ([wintypes.HWND], wintypes.BOOL),
            "IsWindowEnabled": ([wintypes.HWND], wintypes.BOOL),
            "GetForegroundWindow": ([], wintypes.HWND),
            "GetWindow": ([wintypes.HWND, wintypes.UINT], wintypes.HWND),
            "GetClassNameW": ([wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
            "GetWindowTextW": ([wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
            "SendMessageTimeoutW": ([wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                     wintypes.LPARAM, wintypes.UINT, wintypes.UINT,
                                     ctypes.POINTER(ctypes.c_size_t)], wintypes.LPARAM),
            "PostMessageW": ([wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM],
                             wintypes.BOOL),
        }
        for name, (args, returns) in signatures.items():
            fn = getattr(api, name)
            fn.argtypes, fn.restype = args, returns
        _u32 = api
    return _u32


def _norm_button(text):
    """Button caption without its '&' mnemonic marker: '&No' -> 'No'."""
    return (text or "").replace("&", "").strip()


def _kind(cls):
    """'button', 'text' or '' for a child window class (Win32 or WinForms)."""
    # WinForms wraps the Win32 class: "WindowsForms10.BUTTON.app.0.2bf8098_r6_ad1".
    base = cls.split(".")[1] if cls.startswith("WindowsForms") and "." in cls else cls
    base = base.upper()
    if base == "BUTTON":
        return "button"
    if base in ("STATIC", "EDIT") or base.startswith("RICHEDIT"):
        return "text"
    return ""


_PROGRESS_BUTTONS = frozenset({"Cancel", "Abort", "Stop", "Close", "OK", ""})


def is_progress_box(w):
    """A progress control alone does not make a progress box: the Explorer
    file dialog has one in its address bar (seen live on "Open project").
    Only a window whose buttons are all just stop/acknowledge buttons is one."""
    return w["has_progress"] and set(w["buttons"]) <= _PROGRESS_BUTTONS


def _is_blocking(is_main, enabled, has_progress, cls, main_disabled):
    """Is this window a prompt EPLAN is waiting on (not the main window or a progress box)?"""
    return (not is_main and enabled and not has_progress
            and (cls == "#32770" or main_disabled))


def _class(u, hwnd):
    buf = ctypes.create_unicode_buffer(256)
    u.GetClassNameW(hwnd, buf, len(buf))
    return buf.value


def _title(u, hwnd):
    # GetWindowTextW reads a top-level caption without messaging the owner, so
    # a hung EPLAN cannot hang this call.
    buf = ctypes.create_unicode_buffer(512)
    u.GetWindowTextW(hwnd, buf, len(buf))
    return buf.value


def _child_text(u, hwnd, ms):
    # Control text in another process is only reachable by WM_GETTEXT; the
    # timeout plus SMTO_ABORTIFHUNG keeps a hung dialog from hanging us.
    n = ctypes.c_size_t()
    if not u.SendMessageTimeoutW(hwnd, WM_GETTEXTLENGTH, 0, 0, SMTO_ABORTIFHUNG, ms,
                                 ctypes.byref(n)):
        return ""
    buf = ctypes.create_unicode_buffer(min(n.value, 4096) + 1)
    if not u.SendMessageTimeoutW(hwnd, WM_GETTEXT, len(buf), ctypes.addressof(buf),
                                 SMTO_ABORTIFHUNG, ms, ctypes.byref(n)):
        return ""
    return buf.value


def _top(u, pids):
    """(hwnd, pid) of every visible top-level window owned by one of `pids`."""
    pids = {int(p) for p in pids}
    found = []

    def top(hwnd, _):
        pid = wintypes.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids and u.IsWindowVisible(hwnd):
            found.append((hwnd, pid.value))
        return True

    u.EnumWindows(_WNDENUMPROC(top), 0)
    return found


def responding(pids):
    """False when Windows reports EPLAN's main window Not Responding, True when
    it does not, None when that cannot be told (no main window, off Windows).
    IsHungAppWindow only reads a flag, so a hung EPLAN cannot hang this call."""
    try:
        u = _user32()
        mains = [h for h, _ in _top(u, pids) if _class(u, h).startswith("Afx")]
        return not any(u.IsHungAppWindow(h) for h in mains) if mains else None
    except Exception:
        return None


def _enum(pids, max_controls=200, text_timeout_ms=200, skip_main=False, candidates_only=False):
    """Raw facts about every visible top-level window owned by one of `pids`.
    Reading a control's text costs one cross-process message, up to
    text_timeout_ms each while EPLAN is busy, so on every dialog check:
    skip_main leaves the main (Afx) window's children unread, and
    candidates_only leaves unread every window that cannot be blocking."""
    u = _user32()
    tops = [(h, pid, _class(u, h), bool(u.IsWindowEnabled(h))) for h, pid in _top(u, pids)]
    main_disabled = {pid for _, pid, cls, en in tops if cls.startswith("Afx") and not en}
    out = []
    for hwnd, pid, cls, enabled in tops:
        children = []
        is_main = cls.startswith("Afx")

        def child(h, _):
            children.append(h)
            return len(children) < max_controls

        if not ((skip_main and is_main) or (candidates_only and not _is_blocking(
                is_main, enabled, False, cls, pid in main_disabled))):
            u.EnumChildWindows(hwnd, _WNDENUMPROC(child), 0)
        out.append({
            "hwnd": hwnd, "pid": pid, "title": _title(u, hwnd), "class": cls,
            "owner": u.GetWindow(hwnd, GW_OWNER) or 0, "enabled": enabled,
            "children": [(h, _class(u, h), _child_text(u, h, text_timeout_ms))
                         for h in children if u.IsWindowVisible(h)],
        })
    return out


def list_windows(pids, include_main=False, max_controls=200, text_timeout_ms=200,
                 blocking_only=False):
    """One dict per visible top-level window of `pids` (see the shared contract C1).
    blocking_only: read controls only on windows that can be blocking (the
    dialog check); the others come back with no buttons or text."""
    try:
        raw = _enum(pids, max_controls, text_timeout_ms, not include_main, blocking_only)
    except Exception:
        return []
    windows = []
    for w in raw:
        kids = w["children"]
        text = [t[:MAX_TEXT_CHARS] for _, c, t in kids if _kind(c) == "text" and t.strip()]
        windows.append({
            "hwnd": w["hwnd"], "pid": w["pid"], "title": w["title"], "class": w["class"],
            "owner": w["owner"], "enabled": w["enabled"],
            "is_main": w["class"].startswith("Afx"),
            "buttons": [_norm_button(t) for _, c, t in kids if _kind(c) == "button"],
            "text": text[:MAX_TEXT_LINES],
            "has_progress": any("progress" in c.lower() for _, c, _t in kids),
        })
    main_disabled = {w["pid"] for w in windows if w["is_main"] and not w["enabled"]}
    enabled = {w["hwnd"]: w["enabled"] for w in windows}
    foreground = _foreground()
    for w in windows:
        w["blocking"] = _is_blocking(w["is_main"], w["enabled"], is_progress_box(w),
                                     w["class"], w["pid"] in main_disabled)
        # A true modal disables its owner; a modeless #32770 does not. The owner
        # is not always the main window: live 2026-10-10, "Open project" was
        # owned by a floating BCGPMiniFrame, which it disabled, while the main
        # window stayed enabled.
        w["owner_disabled"] = _owner_disabled(w["owner"], enabled)
        w["foreground"] = bool(foreground) and w["hwnd"] == foreground
        w["modal"] = w["blocking"] and (w["pid"] in main_disabled or w["owner_disabled"])
    return [w for w in windows if include_main or not w["is_main"]]


def _owner_disabled(owner, enabled):
    """Is `owner` a disabled window? Looked up in `enabled` (visible windows),
    else asked directly: a hidden owner frame is not in the map. Both calls
    only read a flag, so a hung EPLAN cannot hang them."""
    if not owner:
        return False
    if owner in enabled:
        return not enabled[owner]
    try:
        u = _user32()
        return bool(u.IsWindow(owner)) and not u.IsWindowEnabled(owner)
    except Exception:
        return False


def _foreground():
    try:
        return _user32().GetForegroundWindow() or 0
    except Exception:
        return 0


def _post_click(button_hwnd):
    # Posted, not sent: the dialog's own modal loop processes it, so we never
    # block on a window that is busy unwinding the EPLAN call it interrupted.
    return bool(_user32().PostMessageW(button_hwnd, BM_CLICK, 0, 0))


def _is_window(hwnd):
    return bool(_user32().IsWindow(hwnd))


def _fail(error_type, error, next_step):
    return {"success": False, "errorType": error_type, "error": error, "next_step": next_step}


def click_button(hwnd, button, title, pids, allow_any=False, wait_s=2.0):
    """Click one button on one EPLAN window, after re-checking it is still that window."""
    button = _norm_button(button)
    if not allow_any and button not in SAFE_BUTTONS:
        return _fail("ButtonNotAllowed",
                     "'%s' is not a safe button (%s), so it was not clicked."
                     % (button, ", ".join(sorted(SAFE_BUTTONS))),
                     "Click a safe button instead, or pass allow_any_button=True if you "
                     "are sure this button is what the user wants.")
    try:
        pids = {int(p) for p in pids}
        match = [w for w in _enum(pids) if w["hwnd"] == hwnd and w["pid"] in pids]
    except Exception as e:
        return _fail("DialogNotFound", "Could not read EPLAN's windows: %s" % e,
                     "Call eplan_list_dialogs to see whether the window is still there.")
    if not match or match[0]["title"] != title:
        return _fail("DialogNotFound",
                     "No visible EPLAN window %s titled '%s' exists any more; it may have "
                     "closed or been replaced." % (hwnd, title),
                     "Call eplan_list_dialogs and use the hwnd and title it returns now.")
    w = match[0]
    buttons = [(h, _norm_button(t)) for h, c, t in w["children"] if _kind(c) == "button"]
    target = next((h for h, t in buttons if t == button), None)
    if target is None:
        return _fail("ButtonNotFound",
                     "Window '%s' has no button '%s' (buttons: %s)."
                     % (title, button, ", ".join(t for _, t in buttons) or "none"),
                     "Use one of the listed buttons, spelled exactly as shown.")
    if not _post_click(target):
        return _fail("ButtonNotFound",
                     "Windows refused the click on '%s' (error %d); the button may "
                     "have just closed." % (button, ctypes.get_last_error()),
                     "Call eplan_list_dialogs to see whether the window is still there.")
    deadline = time.monotonic() + wait_s
    closed = not _is_window(hwnd)
    while not closed and time.monotonic() < deadline:
        time.sleep(0.1)
        closed = not _is_window(hwnd)
    return {"success": True, "hwnd": hwnd, "title": w["title"], "class": w["class"],
            "button": button, "closed": closed}
