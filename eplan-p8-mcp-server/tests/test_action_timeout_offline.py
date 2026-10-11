"""Every EPLAN call is bounded and fails with a type that says what to do next.

ExecuteAction used to run with no deadline, so a modal dialog or a wedged
EPLAN hung the tool and every call after it. These tests pin the bound, the
typed failures (EplanActionTimeout / EplanBlockedByDialog / EplanBusy /
EplanNotRunning), and that the script layers pass those through instead of
calling them plumbing faults.

Offline: the client is a stub whose ExecuteAction waits on an Event, and
_blocking_dialogs is monkeypatched. No EPLAN, no CLR, no windows.
"""
import sys
import threading
import time
import types

import pytest

import eplan_connection
from eplan_connection import EPLANConnectionManager, RESULT_FIELD_NAMES
from api.actions import scripted

DIALOG = {"hwnd": 4242, "pid": 99, "title": "Project master data", "class": "#32770",
          "buttons": ["Yes", "No"], "blocking": True}


class _Client:
    """ExecuteAction blocks until `release` is set (or raises `fail`)."""
    SynchronousMode = False

    def __init__(self):
        self.release = threading.Event()
        self.fail = None
        self.calls = []

    def ExecuteAction(self, action):
        self.calls.append(action)
        if self.fail:
            raise self.fail
        self.release.wait(10)

    def Disconnect(self):
        self.release.wait(10)

    def Dispose(self):
        pass


@pytest.fixture
def mgr(tmp_path, monkeypatch):
    monkeypatch.setenv("EPLAN_MCP_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(eplan_connection, "ACTION_TIMEOUT_S", 0.6)
    monkeypatch.setattr(eplan_connection, "DIALOG_CHECK_S", 0.1)
    monkeypatch.setattr(eplan_connection, "_blocking_dialogs", lambda pids, windows=None: [])
    monkeypatch.setattr(eplan_connection, "eplan_pids", lambda: [99])
    monkeypatch.setattr(eplan_connection, "pid_alive", lambda pid: pid == 99)
    m = EPLANConnectionManager()
    m.connected = True
    m.pid = 99
    m.client = _Client()
    yield m
    m.client and m.client.release.set()


def test_blocked_call_times_out_with_a_typed_failure(mgr):
    started = time.monotonic()
    result = mgr.execute_action("XPrjActionProjectOpen /Project:x")
    assert time.monotonic() - started < 1.5
    assert result["errorType"] == "EplanActionTimeout"
    assert result["timed_out"] is True and result["success"] is False
    assert result["message"] == result["error"]
    assert "EPLAN_MCP_ACTION_TIMEOUT" in result["next_step"]
    assert mgr.connected is True


def test_dialog_seen_twice_ends_the_wait_early(mgr, monkeypatch):
    monkeypatch.setattr(eplan_connection, "ACTION_TIMEOUT_S", 5)
    monkeypatch.setattr(eplan_connection, "_blocking_dialogs", lambda pids, windows=None: [DIALOG])
    started = time.monotonic()
    result = mgr.execute_action("XPrjActionProjectOpen /Project:x")
    assert time.monotonic() - started < 1.5
    assert result["errorType"] == "EplanBlockedByDialog"
    assert result["dialogs"] == [DIALOG]
    assert "Project master data" in result["error"]
    assert "eplan_click_dialog_button(hwnd=4242, button='No'" in result["next_step"]


def test_a_dialog_that_flashes_once_does_not_abort(mgr, monkeypatch):
    seen = iter([[DIALOG]])
    monkeypatch.setattr(eplan_connection, "_blocking_dialogs", lambda pids, windows=None: next(seen, []))
    result = mgr.execute_action("someAction")
    assert result["errorType"] == "EplanActionTimeout"


def test_failfast_off_waits_out_a_dialog_until_the_deadline(mgr, monkeypatch):
    # Harnesses that block on a modal on purpose while their own watcher clicks it.
    monkeypatch.setenv("EPLAN_MCP_DIALOG_FAILFAST", "0")
    monkeypatch.setattr(eplan_connection, "_blocking_dialogs", lambda pids, windows=None: [DIALOG])
    threading.Timer(0.4, mgr.client.release.set).start()
    assert mgr.execute_action("gatedAction")["success"] is True


def test_per_call_timeout_extends_but_never_shortens(mgr):
    threading.Timer(0.9, mgr.client.release.set).start()
    assert mgr.execute_action("longAction", timeout=3)["success"] is True
    mgr.client = _Client()
    started = time.monotonic()
    assert mgr.execute_action("x", timeout=0.1)["errorType"] == "EplanActionTimeout"
    assert time.monotonic() - started >= 0.55


def test_second_call_is_busy_then_succeeds_after_release(mgr):
    mgr.execute_action("slowAction")
    started = time.monotonic()
    busy = mgr.execute_action("nextAction")
    assert time.monotonic() - started < 0.5
    assert busy["errorType"] == "EplanBusy"
    assert "slowAction" in busy["error"]
    assert mgr.client.calls == ["slowAction"]   # never sent

    mgr.client.release.set()
    deadline = time.monotonic() + 2
    while mgr._inflight and time.monotonic() < deadline:
        time.sleep(0.02)
    assert mgr.execute_action("nextAction")["success"] is True


def test_call_leaked_past_a_reconnect_keeps_the_new_marker(mgr):
    old = mgr.client
    mgr.execute_action("slowAction")              # times out, worker leaks
    mgr.client, mgr._inflight = _Client(), "newAction"
    old.release.set()
    time.sleep(0.2)
    assert mgr._inflight == "newAction"


def test_exception_with_eplan_gone_is_not_running(mgr, monkeypatch):
    mgr.client.fail = RuntimeError("channel closed")
    monkeypatch.setattr(eplan_connection, "pid_alive", lambda pid: False)
    result = mgr.execute_action("someAction")
    assert result["errorType"] == "EplanNotRunning"
    assert result["next_step"] == "eplan_app_launch"
    assert mgr.connected is False


def test_exception_with_eplan_alive_keeps_the_old_handler(mgr):
    mgr.client.fail = RuntimeError("boom")
    result = mgr.execute_action("someAction")
    assert "errorType" not in result
    assert "boom" in result["message"]
    assert mgr._inflight is None


def test_generated_script_passes_the_typed_error_through(mgr, tmp_path):
    script = tmp_path / "exec_action_x.cs"
    script.write_text("// stub", encoding="utf-8")
    result = mgr._run_generated_script("realAction /A:1", str(script),
                                       str(tmp_path / "never.json"), time.time())
    assert result["errorType"] == "EplanActionTimeout"
    assert result["action"] == "realAction /A:1"


def test_execute_script_passes_the_typed_error_through(mgr, monkeypatch):
    monkeypatch.setattr(scripted, "_get_connected_manager", lambda: (mgr, None))
    result = scripted._execute_script("// stub {{RESULT_PATH}}", timeout=0.1)
    assert result["errorType"] == "EplanActionTimeout"


def test_hanging_disconnect_is_bounded_and_clears_the_client(mgr, monkeypatch):
    real = eplan_connection.call_with_timeout
    monkeypatch.setattr(eplan_connection, "call_with_timeout",
                        lambda f, t, *a, **k: real(f, 0.2, *a, **k))
    client = mgr.client
    started = time.monotonic()
    result = mgr.disconnect()
    client.release.set()
    assert time.monotonic() - started < 1.5
    assert result["success"] is False and "eplan_connect" in result["message"]
    assert (mgr.client, mgr.connected, mgr.pid, mgr._inflight) == (None, False, None, None)


def test_hanging_connect_ping_is_bounded(monkeypatch):
    release = threading.Event()
    disposed = []

    class _Remote:
        def Connect(self, host, port, timeout):
            pass

        def Ping(self):
            release.wait(10)
            return True

        def Dispose(self):
            disposed.append(True)

    monkeypatch.setitem(sys.modules, "Eplan.EplApi.RemoteClient",
                        types.SimpleNamespace(EplanRemoteClient=_Remote))
    monkeypatch.setitem(sys.modules, "System", types.SimpleNamespace(
        TimeSpan=types.SimpleNamespace(FromSeconds=lambda s: s)))
    m = EPLANConnectionManager()
    m._clr_initialized = True
    m.TIMEOUT_SECONDS = 0.2
    started = time.monotonic()
    try:
        result = m.connect(port="49152")
    finally:
        release.set()
    assert time.monotonic() - started < 1.5
    assert result["success"] is False and "did not answer" in result["message"]
    assert m.connected is False and disposed


def test_ping_timeout_reports_dialog_and_in_flight_action(mgr, monkeypatch):
    monkeypatch.setattr(eplan_connection, "_blocking_dialogs", lambda pids, windows=None: [DIALOG])
    mgr.client.Ping = lambda: mgr.client.release.wait(10)
    mgr._inflight = "slowAction"
    result = mgr.ping(timeout_seconds=0.1)
    assert result["timed_out"] is True
    assert result["process_alive"] is True
    assert result["dialogs"] == [DIALOG]
    assert result["in_flight_action"] == "slowAction"
    assert "eplan_click_dialog_button" in result["next_step"]


def test_call_with_timeout_abort_check_returns_its_value():
    release = threading.Event()
    try:
        value, timed_out, error = eplan_connection.call_with_timeout(
            release.wait, 5, abort_check=lambda: "stop", check_every=0.05)
    finally:
        release.set()
    assert (value, timed_out, error) == ("stop", True, None)


def test_typed_failure_fields_are_declared():
    for name in ("next_step", "timed_out", "dialogs", "in_flight_action"):
        assert name in RESULT_FIELD_NAMES


def test_busy_with_a_dialog_names_the_click(mgr, monkeypatch):
    mgr._inflight, mgr._inflight_since = "slowAction", time.monotonic()
    monkeypatch.setattr(eplan_connection, "_blocking_dialogs", lambda pids, windows=None: [DIALOG])
    busy = mgr.execute_action("nextAction")
    assert busy["errorType"] == "EplanBusy" and busy["dialogs"] == [DIALOG]
    assert "eplan_click_dialog_button(hwnd=4242, button='No'" in busy["next_step"]


def test_busy_past_the_timeout_offers_a_way_out(mgr):
    mgr._inflight, mgr._inflight_since = "slowAction", time.monotonic() - 10
    busy = mgr.execute_action("nextAction")
    assert "eplan_app_restart(force=True)" in busy["next_step"]
    assert "confirming with the user" in busy["next_step"]


def test_tasklist_failure_does_not_read_as_not_running(mgr, monkeypatch):
    mgr.client.fail = RuntimeError("boom")
    monkeypatch.setattr(eplan_connection, "eplan_pids", lambda: [])   # tasklist failed
    monkeypatch.setattr(eplan_connection, "pid_alive", lambda pid: None)
    result = mgr.execute_action("someAction")
    assert result.get("errorType") != "EplanNotRunning" and mgr.connected is True


def test_stale_pid_is_not_scanned_for_dialogs(mgr, monkeypatch):
    monkeypatch.setattr(eplan_connection, "pid_alive", lambda pid: False)
    monkeypatch.setattr(eplan_connection, "eplan_pids", lambda: [7])
    assert mgr._watch_pids() == {7}


@pytest.mark.parametrize("dialog, expect, refuse", [
    ({"title": "Project master data", "buttons": ["Yes", "No", "Cancel"]}, "button='No'", None),
    ({"title": "Save?", "buttons": ["Yes", "No", "Cancel"]}, "button='Cancel'", None),
    ({"title": "Upgrade", "buttons": ["Yes", "Upgrade"]}, "ask which button", "button='"),
    ({"title": "WPF", "buttons": []}, "answer it in EPLAN", "button='"),
])
def test_dialog_next_step_only_names_safe_buttons(dialog, expect, refuse):
    step = EPLANConnectionManager._dialog_next_step(dialog)
    assert expect in step
    assert refuse is None or refuse not in step


def test_busy_names_the_logical_action_not_the_temp_script(mgr, tmp_path):
    script = tmp_path / "exec_action_x.cs"
    script.write_text("// stub", encoding="utf-8")
    mgr._run_generated_script("ProjectOpen /Project:x", str(script),
                              str(tmp_path / "never.json"), time.time())
    busy = mgr.execute_action("nextAction")
    assert "'ProjectOpen /Project:x'" in busy["error"] and "ExecuteScript" not in busy["error"]


@pytest.mark.skipif(sys.platform != "win32", reason="Win32 only")
def test_pid_alive_by_handle():
    import os
    assert eplan_connection.pid_alive(os.getpid()) is False   # python.exe, not EPLAN.exe


def test_wrapped_tools_run_off_the_event_loop():
    # A sync tool body on the loop would block eplan_list_dialogs while an
    # EPLAN call waits out its timeout.
    import asyncio
    import server
    loop_thread = []
    tool = server._json_wrapper(lambda: {"thread": threading.get_ident()})
    assert asyncio.iscoroutinefunction(tool)

    async def run():
        loop_thread.append(threading.get_ident())
        return await tool()
    out = asyncio.run(run())
    assert str(loop_thread[0]) not in out
