"""
See and dismiss EPLAN's dialogs without going through EPLAN.

A modal prompt (for example "Project master data") blocks every remoting
call, so eplan_ping and eplan_get_system_messages cannot report it. These
tools read the windows straight from Windows instead, so they work while
EPLAN is blocked and even without a connection. Every dialog a call runs into
and every click is recorded in dialogs.jsonl, so eplan_dialog_history can say
which prompts are expected and how they were answered.
"""

import ui_windows
from eplan_connection import get_manager, eplan_pids, pid_alive, eplan_state, record_dialog
from eplan_connection import dialog_history as _history


def _pids():
    """The connected EPLAN's PID when known, else every local EPLAN.exe."""
    pid = getattr(get_manager(), "pid", None)
    # A stale pid (EPLAN closed outside the MCP, PID since reused) must not
    # aim a click at another program.
    if pid and pid_alive(pid) is not False:
        return {pid}
    return set(eplan_pids())


def list_dialogs(include_main: bool = False) -> dict:
    """
    List EPLAN's open windows and dialogs, read from Windows (never blocks on EPLAN).

    Use this when a call returns errorType EplanBlockedByDialog or EplanBusy,
    or eplan_ping times out: it shows the prompt EPLAN is waiting on. Works
    without a connection (it then looks at every running EPLAN.exe).

    Args:
        include_main: Also list EPLAN's main window (default False).

    Returns:
        windows: one entry per visible window - hwnd, pid, title, class,
            owner, enabled, is_main, buttons (captions), text (the prompt's
            message lines), has_progress, blocking (True when EPLAN is
            waiting on it), and modal (blocking with EPLAN's main window
            disabled behind it; only these refuse a call before it is sent).
        modal_suspected: True when any window is blocking.
        eplan_state: "not_responding" when Windows flags EPLAN Not Responding
            (frozen, or a heavy computation that may still finish),
            "responding", or "unknown".
        Dismiss one with eplan_click_dialog_button(hwnd, button, title).
    """
    pids = _pids()
    windows = ui_windows.list_windows(pids, include_main=include_main)
    return {"success": True, "count": len(windows), "windows": windows,
            "modal_suspected": any(w["blocking"] for w in windows),
            "eplan_state": eplan_state(pids)}


def click_dialog_button(hwnd: int, button: str, title: str,
                        allow_any_button: bool = False) -> dict:
    """
    Click one button on an EPLAN dialog, after checking it is still the same window.

    Take hwnd, title and the button caption from eplan_list_dialogs. The click
    is refused if the window has closed, changed title or is not EPLAN's.
    Only safe buttons are allowed by default: OK, Cancel, Close, No, Skip.
    Safe choices for known prompts:
      - "Project master data" (master data differs from the system's): No,
        which leaves the project's own master data untouched
      - "Eplan Electric P8" error box: OK
    The interrupted EPLAN call's result is lost: check the state afterwards
    (for example eplan_get_current_project) before carrying on.

    Args:
        hwnd: Window handle from eplan_list_dialogs.
        button: Button caption exactly as listed (the '&' marker is ignored).
        title: Window title exactly as listed.
        allow_any_button: Advanced. Allow buttons outside the safe list
            (for example Yes or Save). Only when the user asked for it.

    Returns:
        success, hwnd, title, class, button, closed (the window went away
        within 2 s). On refusal: errorType DialogNotFound, ButtonNotAllowed or
        ButtonNotFound, with error and next_step.
    """
    result = ui_windows.click_button(hwnd, button, title, _pids(), allow_any=allow_any_button)
    record_dialog({"event": "clicked", "title": title, "button": ui_windows._norm_button(button),
                   "success": bool(result.get("success")), "closed": result.get("closed"),
                   "allow_any": bool(allow_any_button)})
    return result


def dialog_history(title_contains: str = None, limit: int = 20) -> dict:
    """
    Which EPLAN dialogs came up before, what raised them, and how they were answered.

    Read from dialogs.jsonl (beside actions.jsonl): every blocking dialog an
    EPLAN call ran into and every eplan_click_dialog_button. Use it to tell an
    expected prompt from one that keeps having to be closed. Read-only; never
    clicks anything.

    Args:
        title_contains: Only titles containing this text (case-insensitive).
        limit: How many titles to return, most seen first (default 20).

    Returns:
        dialogs: one entry per title - title, times_seen, last_seen,
            triggered_by (action -> times, top 3), clicks (button ->
            clicks that closed the dialog), and learned_button: the button
            picked in at least 70% of 2 or more such clicks, else null.
            Suggested next steps mention it but keep the safe default, since
            one title can cover different prompts.
    """
    rows = list(_history().values())
    if title_contains:
        rows = [r for r in rows if title_contains.lower() in r["title"].lower()]
    rows.sort(key=lambda r: (r["times_seen"], r["last_seen"] or ""), reverse=True)
    return {"success": True, "count": len(rows), "dialogs": rows[:max(0, int(limit))]}
