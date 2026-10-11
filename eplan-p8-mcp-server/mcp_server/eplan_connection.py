"""
EPLAN Connection Manager
Connection to EPLAN via Remoting API (pythonnet/CLR)

Requirements:
- EPLAN installed
- pip install pythonnet
"""

import sys
import os
import logging
import re
import json
import threading
import time
import uuid
from collections import Counter
from typing import Optional, List
from cs_json import prepare_script

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("EPLAN")


def cs_escape(value) -> str:
    """Escape a value for safe embedding inside a C# regular string literal ("...").

    Handles backslash, double-quote, newlines/tabs and other control
    characters. This is the single defense against C# injection and against
    uncompilable scripts when action parameters, part numbers, setting
    paths, or supplier-supplied values contain quotes/backslashes/newlines.
    Returns the inner content only (no surrounding quotes).
    """
    if value is None:
        return ""
    out = []
    for ch in str(value):
        codepoint = ord(ch)
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif codepoint < 0x20 or codepoint in (0x85, 0x2028, 0x2029):
            # Control chars, plus NEL/LS/PS which C# treats as line
            # terminators even inside a regular string literal.
            out.append("\\u%04x" % codepoint)
        else:
            out.append(ch)
    return "".join(out)

# Root folder of EPLAN installations. Override with the EPLAN_PLATFORM_ROOT
# environment variable for non-standard install locations.
PLATFORM_ROOT = os.environ.get("EPLAN_PLATFORM_ROOT", r"C:\Program Files\EPLAN\Platform")


def _version_key(name: str):
    return tuple(int(p) for p in name.split(".") if p.isdigit())


def detect_installed_versions() -> list:
    """Detect EPLAN installations under PLATFORM_ROOT.

    Returns a list of dicts sorted newest-first, one per major version:
    [{"version": "2027", "full_version": "2027.0.3", "bin": "...", "runtime": "coreclr"}]

    runtime: "coreclr" for .NET 8 builds (EPLAN 2027+, detected via the
    .NET 8-only Grpc.Net.Client.dll), "netfx" for 2026 and older.
    """
    installs = {}
    try:
        for name in os.listdir(PLATFORM_ROOT):
            bin_dir = os.path.join(PLATFORM_ROOT, name, "Bin")
            if not os.path.exists(os.path.join(bin_dir, "Eplan.EplApi.RemoteClientu.dll")):
                continue
            major = "2.9" if re.match(r"^2\.9(?:\.|$)", name) else name.split(".")[0]
            runtime = "coreclr" if os.path.exists(os.path.join(bin_dir, "Grpc.Net.Client.dll")) else "netfx"
            entry = {"version": major, "full_version": name, "bin": bin_dir, "runtime": runtime}
            if major not in installs or _version_key(name) > _version_key(installs[major]["full_version"]):
                installs[major] = entry
    except FileNotFoundError:
        pass
    return sorted(installs.values(), key=lambda e: _version_key(e["full_version"]), reverse=True)


def _select_dotnet_runtime(runtime: str) -> None:
    """Select the pythonnet runtime. Must run before the first `import clr`
    and can only happen once per process — switching afterwards requires
    restarting the MCP server."""
    try:
        from pythonnet import load as _pnet_load
        if runtime == "coreclr":
            _pnet_load("coreclr")
            logger.info("pythonnet: coreclr (.NET 8) runtime selected (EPLAN 2027+)")
        else:
            logger.info("pythonnet: using default runtime (EPLAN 2026 or older)")
    except Exception as _pnet_err:
        logger.warning(f"pythonnet: runtime selection failed ({_pnet_err})")


EPLAN_EXE_NAME = "EPLAN.exe"

# Severity ranking for the message slice, most important first.
#
# Deliberately a hand-written list rather than MessageLevel's numeric order,
# which is NOT a severity order: Trace=0, Message=1, Warning=2, Assert=3,
# Error=4, FatalError=5, so a naive sort puts Assert - documented as "the
# lowest level of an error, which will not appear in GUI" - above Warning.
#
# Trace and Assert are omitted entirely: the SysMessagesCollection docs state
# neither is ever added to the collection, so listing them would imply a
# guarantee that does not exist. Anything unranked sorts last.
MESSAGE_LEVEL_RANK = ("FatalError", "Error", "Warning", "Message")

# Entries kept in eplanMessages after ranking. Half of all captures were
# already hitting the old cap of 20 (measured over 1,463 logged actions on
# 2026-09-03: 104 of the 206 entries carrying messages had exactly 20, and
# nothing exceeded it), so the cap stays where it is - the fix is that the most
# severe entries survive it, and that truncation is now reported, not that more
# text comes back.
MESSAGE_CAP = 20

# Upper bound on what the generated C# collects before Python ranks it. Guards
# against a pathological action pushing a huge payload through the result file,
# while leaving ranking enough to choose from.
MESSAGE_SCAN_CAP = 500

# How long to wait for a generated script's result file before declaring that
# the action did not run. EPLAN's ExecuteScript is synchronous on this path, so
# a slow action still writes its result before returning - meaning this
# expiring points at the script never having run (usually a compile error),
# not at a slow action. A module constant so tests can shorten it without
# patching the clock: eplan_connection.time is the shared time module, and
# monkeypatching its sleep/time affects pytest itself.
SCRIPT_RESULT_TIMEOUT_S = 30.0

# Upper bound on one ExecuteAction call. Unlike the wait above, this one does
# bound a hang: a modal dialog or a wedged EPLAN otherwise blocks the call, and
# the server with it, forever. Generous because full-project exports are slow.
ACTION_TIMEOUT_S = float(os.environ.get("EPLAN_MCP_ACTION_TIMEOUT", 600))

# How often a running action is checked for a blocking dialog. Two hits in a
# row end the wait, so a dialog that flashes up and closes does not.
DIALOG_CHECK_S = 2.0


def _dialog_failfast() -> bool:
    """EPLAN_MCP_DIALOG_FAILFAST=0 turns the dialog abort off (the deadline
    stays). For harnesses that block on a modal on purpose while their own
    watcher clicks it through. Read per call so it can change at runtime."""
    return os.environ.get("EPLAN_MCP_DIALOG_FAILFAST", "1").strip().lower() not in ("0", "false", "no", "off")

# A window that opened during a call and is still open this long, while the
# call has not finished, is presumed to be what EPLAN waits on - this catches
# prompts the class heuristic misses (WinForms pickers). Read per call.
NEW_WINDOW_GRACE_S = 20.0


def _new_window_grace() -> float:
    try:
        return float(os.environ.get("EPLAN_MCP_NEW_WINDOW_GRACE", NEW_WINDOW_GRACE_S))
    except ValueError:
        return NEW_WINDOW_GRACE_S

# Button order tried when suggesting how to dismiss a dialog: least committal
# first. Same five as ui_windows.SAFE_BUTTONS, so a suggestion is never refused.
_SAFEST_BUTTONS = ("Cancel", "No", "Close", "OK", "Skip")

# Known prompts whose safe answer is not the generic first choice (see
# eplan_click_dialog_button): Cancel would abort the open outright.
_KNOWN_SAFE_BUTTON = {"Project master data": "No"}

# The action-result contract, declared rather than implied.
#
# Written because not having it cost real money three times in one session: a
# field was added and silently never reached the trace; a docstring pointed the
# model at a field that did not exist; and a guard meant to catch the second
# could not be written because there was no vocabulary to check names against.
#
# Each entry is (name, when_present, meaning). "when_present" is part of the
# contract, not a note: a field that appears unconditionally is a tax on every
# happy-path response, so anything diagnostic must be able to say when it stays
# quiet. Keep this in sync when adding a field - the tests below fail if a
# producer emits something undeclared.
RESULT_FIELDS = (
    # --- always present on a completed action
    ("success", "always", "Whether EPLAN reported the action as succeeding. NOT proof of effect: several actions return true having done nothing, and at least one returned false after a completed overwrite."),
    ("action", "on failures raised by this server", "The action string as sent."),
    ("parameters", "when the action returned any", "Values read back out of the ActionCallingContext."),
    ("executor", "always on the scripted path", 'Which executor ran it: "action" (ActionManager.FindAction + Action.Execute), "cli-fallback" (the action name did not resolve), "cli-legacy" (EPLAN_MCP_LEGACY_CLI=1), or "none" (it never ran).'),

    # --- diagnostics
    ("error", "when a cause is known", "Human-readable cause. Sourced either from the thrown exception chain or from ActionCallingContext.GetException()."),
    ("errorType", "with error, when the cause was an exception", "The .NET type name, e.g. Eplan.EplApi.Base.BaseException."),
    ("errorFrom", "with error, on the scripted path", '"context" if read from GetException() after execution, "throw" if the exception propagated. Provenance matters: errorType appeared in 0 of 1,463 logged actions before the context read landed, so the two channels have to stay distinguishable in the trace.'),
    ("message", "on failures raised by this server", "Server-side explanation, kept verbatim for callers matching on the older strings."),
    ("failedScriptPath", "when a generated script failed", "Where the generated C# was preserved, since the cleanup deletes the original and it is the only evidence of a compile error."),

    # --- EPLAN's own messages
    ("eplanMessages", "when EPLAN emitted any", "EPLAN's own text, severity-ranked then capped at MESSAGE_CAP. A list of strings."),
    ("eplanMessagesTotal", "with eplanMessages", "How many EPLAN produced, from the collection's own Count plus anything only the per-call context supplied."),
    ("eplanMessagesTruncated", "only when entries were dropped", "True when the cap discarded lower-severity entries."),
    ("eplanMessagesLevels", "only when something outranks Message", "Per-entry severity, parallel to eplanMessages."),
    ("eplanMessagesUnbounded", "only when the slice could not be bounded", "True when no end bookmark could be taken, so entries from outside this action may be included and the total is an upper bound."),
    ("eplanMessagesFromContextOnly", "only when the context knew more", "How many messages came from ActionCallingContext.SysMessages but not the bookmark slice. Its presence is the evidence that would justify switching channels."),

    # --- typed EPLAN failures (EplanBlockedByDialog / EplanActionTimeout / EplanBusy / EplanNotRunning)
    ("next_step", "on typed failures", "One concrete tool call or instruction that moves the caller forward."),
    ("timed_out", "on EplanBlockedByDialog and EplanActionTimeout", "True: the call was abandoned while EPLAN was still running it, so its result is lost."),
    ("dialogs", "only when a blocking window was seen", "The blocking windows, as listed by eplan_list_dialogs."),
    ("in_flight_action", "on a timed-out ping while an action is stuck", "The action EPLAN is still busy with."),
    ("eplan_state", "on EplanActionTimeout, EplanBusy and a timed-out ping", "'not_responding' when Windows flags EPLAN's main window Not Responding (frozen, or a heavy computation that may still finish), 'responding', or 'unknown'."),

    # --- completed actions EPLAN hung during
    ("not_responding_s", "only when Windows saw EPLAN Not Responding during the action", "Seconds, rounded, EPLAN's main window was flagged Not Responding while the call ran; sampled every DIALOG_CHECK_S, so short hangs are missed."),
    ("hang_note", "with not_responding_s", "Plain-language reading of not_responding_s. Its own key, so a tool's 'note' cannot overwrite it."),
)

# Fields the generated C# emits for the Python side to consume. They are
# internal plumbing and must never survive into a response - a leak of exactly
# this kind reached every no-message action before it was caught by running the
# template against live EPLAN.
INTERNAL_RESULT_FIELDS = (
    "eplanMessagesRaw",
    "eplanContextMessagesRaw",
    "eplanMessagesScanned",
    "eplanMessagesTrueTotal",
    "eplanMessagesBounded",
)

RESULT_FIELD_NAMES = tuple(name for name, _, _ in RESULT_FIELDS)

# Result keys copied into each actions.jsonl entry, on top of the always-present
# ts / action / duration_s / success.
#
# Derived from RESULT_FIELDS rather than hand-maintained, which is the whole
# point: this tuple is a FILTER, and a field missing from it never reaches the
# trace and so cannot be measured in a later audit. Deriving it means adding a
# field to the contract cannot forget the trace. "action" and "success" are
# excluded because _log_action writes them itself; "parameters" because the
# trace records intent, not the full echo.
#
# The typed-failure fields stay out too: errorType and error already carry the
# cause into the trace, and dialogs can be large.
_NOT_LOGGED = ("success", "action", "parameters",
               "next_step", "timed_out", "dialogs", "in_flight_action", "hang_note")
LOGGED_RESULT_KEYS = tuple(n for n in RESULT_FIELD_NAMES if n not in _NOT_LOGGED)


def eplan_pids() -> list:
    """PIDs of running EPLAN.exe processes (empty list on any error)."""
    import subprocess
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {EPLAN_EXE_NAME}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=15,
        ).stdout
        pids = []
        for line in out.splitlines():
            parts = [p.strip('"') for p in line.strip().split('","')]
            if len(parts) >= 2 and parts[0].lower() == EPLAN_EXE_NAME.lower():
                pids.append(int(parts[1]))
        return pids
    except Exception:
        return []


def pid_alive(pid):
    """True if `pid` is a running EPLAN.exe, False if it is not (exited, or the
    PID now belongs to another program), None if that cannot be told. Checked
    by process handle, not tasklist, whose failure would read as 'not running'."""
    try:
        from process_control import WindowsProcess
        WindowsProcess(pid).close()
        return True
    except RuntimeError:            # not EPLAN.exe, or already exited
        return False
    except OSError as e:            # 87: no such process; access denied etc.: unknown
        return False if getattr(e, "winerror", None) == 87 else None
    except Exception:
        return None


def _blocking_dialogs(pids, windows=None) -> list:
    """Blocking windows owned by `pids` (or among `windows`, an _eplan_windows
    result already in hand); [] when none, or when ui_windows is missing or
    fails. Never raises. A module function so tests can replace it."""
    try:
        import ui_windows
        if windows is None:
            windows = ui_windows.list_windows(pids, blocking_only=True)
        return [w for w in windows if w["blocking"]]
    except Exception:
        return []


def _eplan_windows(pids, full=False) -> list:
    """Every non-main window owned by `pids`. Cheap by default: only windows
    that can be blocking have their controls read; full reads them all.
    [] on any failure. Never raises. A module function so tests can replace it."""
    try:
        import ui_windows
        return ui_windows.list_windows(pids, blocking_only=not full)
    except Exception:
        return []


def _eplan_hwnds(pids) -> set:
    """hwnds of every visible top-level window of `pids`, no control read.
    set() on any failure. Never raises. A module function so tests can replace it."""
    try:
        import ui_windows
        return {h for h, _ in ui_windows._top(ui_windows._user32(), pids)}
    except Exception:
        return set()


def _responding(pids):
    """ui_windows.responding(pids); None when it cannot be told. Never raises.
    A module function so tests can replace it."""
    try:
        import ui_windows
        return ui_windows.responding(pids)
    except Exception:
        return None


def eplan_state(pids) -> str:
    """'responding', 'not_responding' (Windows' Not Responding flag) or 'unknown'."""
    r = _responding(pids) if pids else None
    return "unknown" if r is None else ("responding" if r else "not_responding")


# Said when EPLAN is silent and no dialog explains it. Not Responding is not a
# failure: heavy computations set it too, and usually clear it again.
_STATE_WORDS = {
    "not_responding": " EPLAN is frozen or computing heavily (Windows reports Not "
                      "Responding); a long operation may still finish.",
    "responding": " EPLAN is responsive but still working.",
}

# Every blocking dialog a call ran into, and every click, one JSON line each,
# beside actions.jsonl. Read back by dialog_history to learn which prompts are
# expected and how they keep getting answered.
DIALOG_LOG = "dialogs.jsonl"
_history_cache = {"path": None, "offset": 0, "rows": {}, "titles": {}}
_history_lock = threading.Lock()


def dialog_history(log_dir=None) -> dict:
    """{title: summary} from dialogs.jsonl; {} when there is none. Never raises.

    learned_button is the button whose clicks closed the dialog most often,
    once it has at least 2 such clicks and 70% of them. The file is append-only,
    so only the bytes added since the last read are parsed.
    """
    try:
        path = os.path.join(log_dir or EPLANConnectionManager._log_dir(), DIALOG_LOG)
        size = os.path.getsize(path)
        with _history_lock:
            c = _history_cache
            if c["path"] != path or size < c["offset"]:     # other file, or truncated
                c.update(path=path, offset=0, rows={}, titles={})
            if size == c["offset"]:
                return c["titles"]
            with open(path, "rb") as f:
                f.seek(c["offset"])
                chunk = f.read(size - c["offset"])
            # A half-written last line waits for the next read.
            end = chunk.rfind(b"\n") + 1
            c["offset"] += end
            for line in chunk[:end].decode("utf-8", "replace").splitlines():
                try:
                    e = json.loads(line)
                    title = e["title"]
                except (ValueError, KeyError, TypeError):
                    continue
                r = c["rows"].setdefault(title, {"times_seen": 0, "last_seen": None,
                                                 "actions": Counter(), "clicks": Counter()})
                if e.get("event") == "seen":
                    r["times_seen"] += 1
                    r["last_seen"] = e.get("ts")
                    m = re.match(r'^([^\s/]+)', e.get("action") or "")
                    if m:
                        r["actions"][m.group(1)] += 1
                elif e.get("event") == "clicked" and e.get("success") and e.get("closed"):
                    # Only clicks that closed the dialog teach anything.
                    r["clicks"][e.get("button")] += 1
            titles = {}
            for title, r in c["rows"].items():
                button, n = (r["clicks"].most_common(1) or [(None, 0)])[0]
                learned = n >= 2 and n >= 0.7 * sum(r["clicks"].values())
                titles[title] = {"title": title, "times_seen": r["times_seen"],
                                 "last_seen": r["last_seen"],
                                 "triggered_by": dict(r["actions"].most_common(3)),
                                 "clicks": dict(r["clicks"]),
                                 "learned_button": button if learned else None}
            c["titles"] = titles
            return titles
    except Exception:
        return {}


def record_dialog(event: dict) -> None:
    """Append one event to dialogs.jsonl. Never raises: learning is a bonus."""
    try:
        log_dir = EPLANConnectionManager._log_dir()
        os.makedirs(log_dir, exist_ok=True)
        entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **event}
        with open(os.path.join(log_dir, DIALOG_LOG), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def carry_hang_note(src: dict, dst: dict) -> dict:
    """Copy not_responding_s and hang_note from the inner ExecuteScript result
    onto the result the caller gets: the hang belongs to the wrapped action."""
    for key in ("not_responding_s", "hang_note"):
        if key in src:
            dst[key] = src[key]
    return dst


def eplan_listening_ports(only_pids=None, pids=None) -> list:
    """TCP ports EPLAN.exe processes are LISTENING on (via netstat).

    Fallback discovery: GetActiveEplanServersOnLocalMachine is unreliable
    right after EPLAN (re)starts - it can return empty while the remoting
    port is already up and accepting connections (observed live 2026-08-20:
    fresh EPLAN 2027 listening on 49153, server enumeration empty). The
    remoting port is dynamic (49152 is only the usual first choice), so
    never assume the default - discover.

    Args:
        only_pids: If given, restrict results to ports owned by one of these
            PIDs (any iterable, coerced to a set). Used by app_launch to tell
            the instance it just started apart from one that was already
            running - see Audit #42 item 12.
        pids: Already-computed result of eplan_pids(). If given, skips the
            internal tasklist spawn.
    """
    import subprocess
    pids = set(eplan_pids() if pids is None else pids)
    if not pids:
        return []
    if only_pids is not None:
        pids &= set(only_pids)
        if not pids:
            return []
    ports = []
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                             capture_output=True, text=True, timeout=30).stdout
        for line in out.splitlines():
            parts = line.split()
            # TCP  0.0.0.0:49153  0.0.0.0:0  LISTENING  45472
            if (len(parts) >= 5 and parts[0] == "TCP" and parts[3] == "LISTENING"
                    and parts[4].isdigit() and int(parts[4]) in pids):
                port = parts[1].rsplit(":", 1)[-1]
                if port.isdigit() and port not in ports:
                    ports.append(port)
    except Exception:
        pass
    return ports



def call_with_timeout(func, timeout_seconds, *args, abort_check=None, check_every=2.0, **kwargs):
    """
    Run a blocking call with a deadline. Returns (result, timed_out, error).

    Every EplanRemoteClient call except Connect() is an unbounded synchronous
    call into the CLR. When EPLAN cannot service it - a human mid-edit, a modal
    dialog, a project that will not close - the call never returns, and with it
    goes the tool that made it AND this server's ability to answer anything
    else. Measured 2026-09-11: StopEplan() blocked for 8m44s while a human had
    a project open, and the session's only recovery was restarting the server.

    The worker is a daemon thread and is deliberately NOT joined after the
    timeout: it is stuck in native code and cannot be interrupted. Letting it
    leak is the lesser evil - the alternative is hanging forever, which is the
    bug being fixed. It ends when the process does.

    abort_check, if given, is polled every check_every seconds while the call
    runs; the first truthy value it returns ends the wait early as
    (that_value, True, None). A plain timeout still returns (None, True, None).
    """
    import threading

    box = {}

    def run():
        try:
            box["value"] = func(*args, **kwargs)
        except BaseException as e:      # noqa: BLE001 - reported, not swallowed
            box["error"] = e

    worker = threading.Thread(target=run, daemon=True,
                              name=f"eplan-call-{getattr(func, '__name__', 'anon')}")
    worker.start()
    deadline = time.monotonic() + timeout_seconds
    while True:
        remaining = deadline - time.monotonic()
        worker.join(max(0, min(check_every, remaining) if abort_check else remaining))
        if not worker.is_alive():
            return box.get("value"), False, box.get("error")
        if remaining <= check_every or not abort_check:
            return None, True, None
        hit = abort_check()
        if hit:
            return hit, True, None


class EPLANConnectionManager:
    """Manages the connection to EPLAN via Remote Client API."""

    DEFAULT_PORT = "49152"
    DEFAULT_HOST = "localhost"
    TIMEOUT_SECONDS = 10

    # Hosts treated as "this workstation". EPLAN remoting has no authentication,
    # so connecting anywhere else is a trust decision, not a configuration
    # detail: whoever runs that EPLAN sees every project this session touches
    # and every action it runs. Kept as a set so get_status() and the action
    # trace can both label the target.
    LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", ""})

    def __init__(self, target_version: str = None):
        # target_version: EPLAN major version like "2026".
        # None = auto-detect (newest installed version).
        self.target_version = str(target_version) if target_version else None
        self.client = None
        self.connected = False
        self.pid = None         # EPLAN.exe behind the connection, when local
        self._inflight = None   # action string of a call still stuck in EPLAN
        self._inflight_since = 0.0
        self._not_responding_s = 0.0   # seen Not Responding during the last call
        self._recorded = set()  # (hwnd, title) of dialogs already in dialogs.jsonl
        # Tools run on worker threads, so two calls can overlap; this keeps the
        # in-flight check-and-set atomic and sends EPLAN one call at a time.
        # Waited on only briefly: a caller behind a long action gets EplanBusy.
        self._exec_lock = threading.Lock()
        self.host = self.DEFAULT_HOST
        self.port = self.DEFAULT_PORT
        self.last_error = ""
        # Lazy: DLLs load on first use so tools like eplan_versions can run
        # without committing this process to one version's .NET runtime.
        self._clr_initialized = False

    def _setup_api(self) -> bool:
        """Load EPLAN DLLs via pythonnet."""
        try:
            installs = detect_installed_versions()
            if not installs:
                self.last_error = f"No EPLAN installation found under {PLATFORM_ROOT}"
                logger.error(self.last_error)
                return False

            if self.target_version:
                chosen = next((i for i in installs if i["version"] == self.target_version), None)
                if chosen is None:
                    available = ", ".join(i["version"] for i in installs)
                    self.last_error = f"EPLAN {self.target_version} not installed (available: {available})"
                    logger.error(self.last_error)
                    return False
            else:
                chosen = installs[0]
                logger.info(f"Auto-detected EPLAN {chosen['full_version']} (newest installed)")

            self.target_version = chosen["version"]
            _select_dotnet_runtime(chosen["runtime"])

            import clr

            eplan_path = chosen["bin"]

            if eplan_path not in sys.path:
                sys.path.append(eplan_path)

            # Add additional dependency paths
            dep_paths = [
                r"C:\Program Files\EPLAN\Common\IdentityClient",
                os.path.join(os.path.dirname(eplan_path), "Bin"),
            ]
            for dp in dep_paths:
                if os.path.exists(dp) and dp not in sys.path:
                    sys.path.append(dp)

            # Load EPLAN DLLs via LoadFrom so .NET probes the EPLAN Bin directory
            # for dependencies (e.g. Grpc.Core), preventing version conflicts with
            # any system-wide or Python-env assembly of the same name.
            import System.Reflection
            import System

            def _resolve_from_eplan(sender, args):
                asm_name = System.Reflection.AssemblyName(args.Name).Name
                candidate = os.path.join(eplan_path, asm_name + ".dll")
                if os.path.exists(candidate):
                    return System.Reflection.Assembly.LoadFrom(candidate)
                return None

            System.AppDomain.CurrentDomain.AssemblyResolve += _resolve_from_eplan

            for dll in ("Eplan.EplApi.Starteru.dll", "Eplan.EplApi.RemoteClientu.dll", "Eplan.EplApi.Remotingu.dll"):
                dll_path = os.path.join(eplan_path, dll)
                if os.path.exists(dll_path):
                    System.Reflection.Assembly.LoadFrom(dll_path)

            clr.AddReference("Eplan.EplApi.Starteru")
            clr.AddReference("Eplan.EplApi.RemoteClientu")
            clr.AddReference("Eplan.EplApi.Remotingu")

            logger.info(f"EPLAN API loaded from: {eplan_path}")
            return True

        except ImportError:
            self.last_error = "pythonnet not installed. Run: pip install pythonnet"
            logger.error(self.last_error)
            return False
        except Exception as e:
            self.last_error = f"Failed to load EPLAN API: {e}"
            logger.error(self.last_error)
            return False

    def get_active_servers(self) -> list:
        """Get active EPLAN servers on the local machine."""
        if not self._clr_initialized:
            self._clr_initialized = self._setup_api()
        if not self._clr_initialized:
            return []

        try:
            from Eplan.EplApi.RemoteClient import EplanRemoteClient, EplanServerData
            from System.Collections.Generic import List as NetList

            temp = EplanRemoteClient()
            # out parameter in pythonnet
            servers = NetList[EplanServerData]()
            _, timed_out, err = call_with_timeout(
                temp.GetActiveEplanServersOnLocalMachine, 10, servers)
            if err is not None:
                raise err
            if timed_out:
                raise TimeoutError("server enumeration did not answer within 10s")

            result = []
            for s in servers:
                result.append({
                    "version": str(s.EplanVersion),
                    "variant": str(s.EplanVariant),
                    "port": str(s.ServerPort)
                })
                logger.info(f"Found: EPLAN {s.EplanVersion} on port {s.ServerPort}")

            temp.Dispose()
            return result

        except Exception as e:
            self.last_error = f"Error getting servers: {e}"
            logger.error(self.last_error)
            return []

    def connect(self, host: str = None, port: str = None) -> dict:
        """Connect to an EPLAN instance."""
        if not self._clr_initialized:
            self._clr_initialized = self._setup_api()
        if not self._clr_initialized:
            return {"success": False, "message": self.last_error}

        host = host or self.DEFAULT_HOST

        try:
            from Eplan.EplApi.RemoteClient import EplanRemoteClient
            import System

            # Auto-detect port(s) if not specified. Server enumeration first;
            # if it comes back empty (it is unreliable right after EPLAN
            # starts), fall back to EPLAN.exe's actual listening ports from
            # netstat, then the historical default as a last resort. Each
            # candidate is tried until one answers a Ping.
            if port:
                candidates = [str(port)]
            else:
                servers = self.get_active_servers()
                if servers:
                    candidates = [servers[-1]["port"]]
                    logger.info(f"Auto-detected port: {candidates[0]}")
                else:
                    candidates = (eplan_listening_ports() if host in ("localhost", "127.0.0.1")
                                  else []) or [self.DEFAULT_PORT]
                    logger.info(f"Server enumeration empty; trying ports: {candidates}")

            timeout = System.TimeSpan.FromSeconds(self.TIMEOUT_SECONDS)
            last_exc = None
            for candidate in candidates:
                # `client` is disposed on EVERY path except the one where it is
                # handed to self.client. Previously Dispose() ran only when
                # Connect succeeded AND Ping returned False, so the ordinary
                # wrong-port case - Connect throwing - dropped a CLR remoting
                # client (and its gRPC channel) undisposed. Across the reconnect
                # retries the docstrings encourage and repeated app_launch
                # cycles, those accumulate for the life of the MCP process.
                client = None
                keep = False
                try:
                    logger.info(f"Connecting to {host}:{candidate}...")
                    client = EplanRemoteClient()
                    client.Connect(host, candidate, timeout)
                    alive, timed_out, err = call_with_timeout(client.Ping, self.TIMEOUT_SECONDS)
                    if err is not None:
                        raise err
                    if timed_out:
                        raise TimeoutError(f"Ping on port {candidate} did not answer "
                                           f"within {self.TIMEOUT_SECONDS}s")
                    if alive:
                        self.client = client
                        self.host = host
                        self.port = candidate
                        self.connected = True
                        self._inflight = None
                        self.pid = None
                        if host in self.LOCAL_HOSTS:
                            try:
                                from process_control import _listener_pid
                                self.pid = _listener_pid(candidate)
                            except Exception:
                                pass
                        keep = True
                        logger.info(f"Connected to EPLAN at {host}:{candidate}")
                        return {
                            "success": True,
                            "message": f"Connected to EPLAN at {host}:{candidate}",
                            "host": host,
                            "port": candidate
                        }
                except Exception as exc:
                    last_exc = exc
                finally:
                    if client is not None and not keep:
                        # Bounded: a client whose Ping hung can hang here too.
                        call_with_timeout(client.Dispose, 10)
            if last_exc is not None:
                raise last_exc
            return {"success": False, "message": "Connected but ping failed"}

        except Exception as e:
            self.last_error = f"Connection failed: {e}"
            logger.error(self.last_error)
            self.connected = False
            return {"success": False, "message": self.last_error}

    def ping(self, timeout_seconds: float = 15.0) -> dict:
        """
        Check if EPLAN is responding.

        Bounded on purpose. TIMEOUT_SECONDS applies to Connect() only - every
        other remoting call is an unbounded synchronous CLR call, so a wedged
        EPLAN turns the health check itself into a hang, and the one tool whose
        job is to answer "is it alive?" must always answer.
        """
        if not self.connected or not self.client:
            return {"alive": False, "message": "Not connected"}

        alive, timed_out, error = call_with_timeout(self.client.Ping, timeout_seconds)
        if timed_out:
            # Not marked disconnected: the client may still be valid and EPLAN
            # merely busy (a modal dialog, a long redraw). Saying so is more
            # useful than guessing either way.
            result = {"alive": False, "timed_out": True,
                      "message": f"EPLAN did not answer Ping within {timeout_seconds}s - "
                                 "it is running but not servicing remoting calls "
                                 "(a modal dialog or a long operation will do this)"}
            pids = self._watch_pids()
            dialogs = _blocking_dialogs(pids) if pids else []
            result["process_alive"] = pid_alive(self.pid) if self.pid else None
            result["eplan_state"] = eplan_state(pids)
            if not dialogs:
                result["message"] += _STATE_WORDS.get(result["eplan_state"], "")
            if result["process_alive"] is False:
                result["next_step"] = "EPLAN is no longer running: call eplan_app_launch."
            elif dialogs:
                result["dialogs"] = dialogs
                result["next_step"] = self._dialog_next_step(dialogs[0])
            elif self._inflight:
                result["next_step"] = self._stuck_next_step()
            else:
                result["next_step"] = ("Wait a few seconds and call eplan_ping again; if it "
                                       "stays silent, check eplan_list_dialogs.")
            if self._inflight:
                result["in_flight_action"] = self._inflight
            return result
        if error is not None:
            self.connected = False
            return {"alive": False, "message": f"Ping failed: {error}"}
        return {
            "alive": bool(alive),
            "message": "EPLAN responding" if alive else "No response"
        }

    def _watch_pids(self):
        """PIDs to scan for dialogs: the connected EPLAN, else every local one."""
        # A stale pid (EPLAN closed outside the MCP, PID since reused) must not
        # aim a dialog scan or click at another program.
        if self.pid and pid_alive(self.pid) is not False:
            return {self.pid}
        if self.host in self.LOCAL_HOSTS:
            return set(eplan_pids())
        return set()

    @staticmethod
    def _dialog_next_step(dialog: dict, sent: bool = True) -> str:
        buttons = dialog.get("buttons") or []
        title = dialog.get("title", "")
        then = ("verify state (e.g. eplan_get_current_project): the result of this "
                "action is lost." if sent else "retry this call.")
        # What this exact prompt was answered with before (dialogs.jsonl).
        past = dialog_history().get(title) or {}
        learned = past.get("learned_button")
        learned = learned if learned in buttons else None
        known = _KNOWN_SAFE_BUTTON.get(title)
        button = known if known in buttons else next(
            (b for b in _SAFEST_BUTTONS if b in buttons), None)
        # History is keyed by title alone and one title ("EPLAN") can cover
        # different prompts, so a learned answer is mentioned, never substituted.
        why = ""
        if learned and learned == button:
            why = f" (answered {learned!r} {past['clicks'][learned]} times before)"
        elif learned:
            why = (f" ({learned!r} answered it {past['clicks'][learned]} times before; "
                   "click it instead only after the user confirms"
                   + ("" if learned in _SAFEST_BUTTONS else ", with allow_any_button=True")
                   + ")")
        if button is None:
            # Never name an unsafe button: the click would be refused, and the
            # override is the user's call, not the model's.
            how = ("ask which button to press; call eplan_click_dialog_button with "
                   "allow_any_button=True only for a button the user names"
                   if buttons else "ask the user to answer it in EPLAN")
            return (f"Show the user dialog {title!r} (eplan_list_dialogs has its text) "
                    f"and {how}{why}. Then {then}")
        return (f"eplan_click_dialog_button(hwnd={dialog.get('hwnd')}, button={button!r}, "
                f"title={title!r}){why}, then {then}")

    @staticmethod
    def _typed_failure(error_type, action, error, next_step, dialogs=None, timed_out=False,
                       eplan_state=None):
        """The shared failure shape for an EPLAN call that did not complete."""
        result = {"success": False, "errorType": error_type, "error": error,
                  "message": error, "next_step": next_step, "action": action}
        if timed_out:
            result["timed_out"] = True
        if dialogs:
            result["dialogs"] = dialogs
        if eplan_state:
            result["eplan_state"] = eplan_state
        return result

    def _note_dialogs(self, dialogs, phase, action) -> None:
        """Record each blocking dialog as "seen" once while it stays open, keyed
        hwnd+title, so retries against the same dialog do not inflate times_seen."""
        recorded = self._recorded
        self._recorded = {(d.get("hwnd"), d.get("title")) for d in dialogs}  # forget closed ones
        for d in dialogs:
            if (d.get("hwnd"), d.get("title")) in recorded:
                continue
            record_dialog({"event": "seen", "phase": phase, "title": d.get("title", ""),
                                 "class": d.get("class", ""), "buttons": d.get("buttons") or [],
                                 "text": [t[:200] for t in (d.get("text") or [])[:3]],
                                 "action": action})

    def _new_windows(self, state, windows, found, now, label):
        """(learned, suspected): windows that opened after the call was sent and
        that the class heuristic did not flag. learned: titles a blocking dialog
        has had and dialogs.jsonl has a learned_button for, sent down the fast
        path. suspected: any other one that disabled its owner or is in the
        foreground (a status form does neither), open past the grace period.
        Either must have a title or a button and not be a progress box; each
        suspect is recorded and returned once."""
        if state["before"] is None:             # pid was unknown before sending
            # ponytail: taken one tick late, so a window that opened in the first
            # DIALOG_CHECK_S is missed unless it disabled its owner; no pid also
            # means every local EPLAN is watched. Both end in the plain timeout.
            state["before"] = {w["hwnd"] for w in windows if not w.get("owner_disabled")}
            return [], []
        flagged = {d.get("hwnd") for d in found}
        history, grace = dialog_history(), _new_window_grace()
        new, learned, due = {}, [], []
        for w in windows:
            h = w["hwnd"]
            if h in state["before"]:
                continue
            # First-seen survives a tick spent flagged or disabled.
            new[h] = first = state["new"].get(h, now)
            if h in flagged or w.get("blocking") or w.get("is_main") or not w.get("enabled"):
                continue
            title = w.get("title")
            past = (history.get(title) or {}) if title else {}
            if past.get("times_seen", 0) >= 1 and past.get("learned_button"):
                learned.append(h)
            elif (now - first >= grace and h not in state["decided"]
                  and (w.get("owner_disabled") or w.get("foreground"))):
                due.append(h)
        state["new"] = new          # a window that closed starts over if it returns
        if not (learned or due):
            return [], []
        # Controls are read only now, once something is worth naming, and once
        # per window: a learned window open for the whole call is not re-read.
        # ponytail: that one read still covers every non-main window; read only
        # the wanted hwnds if it ever shows up in a tick's cost.
        import ui_windows   # importable: _eplan_windows just returned windows
        cache = state["full"]
        if any(h not in cache for h in learned + due):
            fresh = {w["hwnd"]: w for w in _eplan_windows(state["pids"], full=True)}
            cache.update((h, fresh[h]) for h in learned + due if h in fresh)

        def worth(w):
            return w and (w.get("title") or w.get("buttons")) and not ui_windows.is_progress_box(w)
        suspected = []
        for h in due:
            state["decided"].add(h)
            w = cache.get(h)
            if not worth(w):
                continue
            w = {**w, "suspected": True, "appeared_s": round(new[h] - state["start"])}
            # Not "seen": a suspect must not teach the fast path (dialog_history).
            record_dialog({"event": "suspected", "phase": "new_window",
                           "title": w.get("title", ""),
                           "class": w.get("class", ""), "buttons": w.get("buttons") or [],
                           "text": [t[:200] for t in (w.get("text") or [])[:3]],
                           "action": label})
            suspected.append(w)
        return [cache[h] for h in learned if worth(cache.get(h))], suspected

    @staticmethod
    def _with_hang_note(result: dict, hung_s: float) -> dict:
        """Add not_responding_s and hang_note when Windows saw EPLAN hang during the call."""
        s = round(hung_s)
        if s > 0:
            result["not_responding_s"] = s
            result["hang_note"] = (f"EPLAN was not responding for ~{s}s during this action "
                              "(heavy computation or a hang that recovered).")
        return result

    def _stuck_next_step(self) -> str:
        """next_step for a call still in flight with no dialog on screen."""
        if not self._inflight:
            return "Wait a few seconds, then retry: another call to EPLAN is finishing."
        age = time.monotonic() - self._inflight_since
        if age >= ACTION_TIMEOUT_S:
            return (f"EPLAN has been stuck on '{self._inflight}' for {age:.0f}s with no "
                    "dialog found. After confirming with the user, call "
                    "eplan_app_restart(force=True); eplan_connect alone does not unstick EPLAN.")
        return (f"EPLAN is still running '{self._inflight}': wait, then retry. If "
                "eplan_list_dialogs shows nothing and it is still stuck after "
                f"{ACTION_TIMEOUT_S:g}s, ask the user before calling "
                "eplan_app_restart(force=True).")

    def _busy_failure(self, action: str) -> dict:
        """EplanBusy: another call still holds EPLAN, so this one was not sent."""
        running = self._inflight
        pids = self._watch_pids()
        dialogs = _blocking_dialogs(pids) if pids else []
        self._note_dialogs(dialogs, "busy", running or action)
        st = eplan_state(pids)
        what = (f"EPLAN is still running '{running}' from an earlier call" if running
                else "Another call to EPLAN is still in progress")
        return self._typed_failure(
            "EplanBusy", action,
            what + ", so this call was not sent." + ("" if dialogs else _STATE_WORDS.get(st, "")),
            self._dialog_next_step(dialogs[0]) if dialogs else self._stuck_next_step(),
            dialogs=dialogs, eplan_state=st)

    def _bounded_execute(self, action: str, label: str = None, timeout: float = None):
        """(_bounded_execute_locked's result, seconds EPLAN was seen Not Responding).
        The seconds are read under the lock, so an overlapping call cannot swap them."""
        if not self._exec_lock.acquire(timeout=DIALOG_CHECK_S):
            return self._busy_failure(action), 0.0
        try:
            return (self._bounded_execute_locked(action, label or action, timeout),
                    self._not_responding_s)
        finally:
            self._exec_lock.release()

    def _bounded_execute_locked(self, action: str, label: str, timeout: float = None):
        """Run client.ExecuteAction(action) with a deadline.

        Returns None when EPLAN completed the call, a typed failure dict when it
        did not, and re-raises an exception from a live EPLAN for the caller's
        existing handler. A call abandoned on timeout keeps running inside
        EPLAN; self._inflight stays set until it returns, and refuses new calls
        meanwhile, since EPLAN would only queue them behind it.
        """
        self._not_responding_s = 0.0
        failfast = _dialog_failfast()
        if self._inflight:
            return self._busy_failure(action)

        # Pre-flight: a dialog already up would only swallow this call too.
        # Only with a known pid, so it never costs a tasklist spawn.
        pids = self._watch_pids() if self.pid else None
        if pids and failfast:
            # Only a true modal (EPLAN's main window disabled behind it) refuses
            # a call unsent: a modeless #32770 tool window does not block EPLAN.
            def modal():
                return [d for d in _blocking_dialogs(pids) if d.get("modal")]
            found = modal()
            if found:
                # Looked at twice, like the in-call check: the previous call's
                # dialog (back-to-back script plumbing) may still be closing.
                time.sleep(min(0.5, DIALOG_CHECK_S))
                found = modal()
            if found:
                self._note_dialogs(found, "preflight", label)
                d = found[0]
                buttons = ", ".join(d.get("buttons") or []) or "none found"
                return self._typed_failure(
                    "EplanBlockedByDialog", action,
                    f"EPLAN already has dialog '{d.get('title', '')}' open (buttons: "
                    f"{buttons}); this call was not sent.",
                    self._dialog_next_step(d, sent=False), dialogs=found)

        client = self.client
        state = {"pids": pids, "prev": False, "hung_s": 0.0, "tick": time.monotonic(),
                 "last": [], "start": time.monotonic(), "new": {}, "decided": set(),
                 "full": {},
                 # Windows open before the call cannot be what it waits on.
                 "before": _eplan_hwnds(pids) if pids else None}

        def dialog_check():
            # PIDs resolved on the first check only, so a fast action never
            # pays for a tasklist spawn.
            if state["pids"] is None:
                state["pids"] = self._watch_pids()
            # Not Responding is sampled, never acted on: heavy computations
            # set it legitimately. It only ends up in the result.
            now = time.monotonic()
            if state["pids"] and _responding(state["pids"]) is False:
                state["hung_s"] += now - state["tick"]
                self._not_responding_s = state["hung_s"]
            state["tick"] = now
            found = learned = suspected = []
            if state["pids"]:
                windows = _eplan_windows(state["pids"])     # one enumeration per tick
                found = _blocking_dialogs(state["pids"], windows)
                learned, suspected = self._new_windows(state, windows, found, now, label)
            found = found + learned
            # Reported as blocking once: never reported again as a suspect.
            state["decided"].update(d.get("hwnd") for d in found)
            self._note_dialogs(found, "during", label)
            state["last"] = found
            # With failfast off the loop still runs to sample and record;
            # it just never aborts. A suspect already waited out its grace.
            hit = found if (found and state["prev"] and failfast) else None
            hit = hit or (suspected if failfast else None)
            state["prev"] = bool(found)
            return hit

        def run():
            try:
                client.ExecuteAction(action)
            finally:
                # A call leaked past a reconnect must not clear the marker
                # of a call made on the new client.
                if self.client is client:
                    self._inflight = None

        self._inflight, self._inflight_since = label, time.monotonic()
        # A caller's own longer budget (e.g. a script's timeout_seconds) wins
        # over the default; it never shortens it.
        limit = max(ACTION_TIMEOUT_S, timeout or 0)
        dialogs, timed_out, error = call_with_timeout(
            run, limit, abort_check=dialog_check, check_every=DIALOG_CHECK_S)
        # With failfast off a dialog never aborts, but one still up at the
        # deadline is why the call did not finish.
        if timed_out and not dialogs:
            dialogs = state["last"]
        if timed_out and dialogs and dialogs[0].get("suspected"):
            d = dialogs[0]
            buttons = ", ".join(d.get("buttons") or []) or "none found"
            return self._typed_failure(
                "EplanBlockedByDialog", action,
                f"A new EPLAN window '{d.get('title', '')}' (buttons: {buttons}) appeared "
                f"{d['appeared_s']}s into the action and is still open; EPLAN has not "
                "finished, so it is probably waiting on it.",
                self._dialog_next_step(d) + " If this window is expected to stay open "
                "(a tool window), tell the user; set EPLAN_MCP_NEW_WINDOW_GRACE higher "
                "to wait longer.", dialogs=dialogs, timed_out=True)
        if timed_out and dialogs:
            d = dialogs[0]
            buttons = ", ".join(d.get("buttons") or []) or "none found"
            return self._typed_failure(
                "EplanBlockedByDialog", action,
                f"EPLAN is waiting on dialog '{d.get('title', '')}' (buttons: {buttons}); "
                "the action has not finished.",
                self._dialog_next_step(d), dialogs=dialogs, timed_out=True)
        if timed_out:
            st = eplan_state(state["pids"])
            return self._typed_failure(
                "EplanActionTimeout", action,
                f"EPLAN did not finish the action within {limit:g}s and no "
                "blocking dialog is open; it may still be running."
                + _STATE_WORDS.get(st, ""),
                "Call eplan_ping until it answers, then verify state. If this action "
                "legitimately takes longer, set EPLAN_MCP_ACTION_TIMEOUT (seconds) "
                "and restart the MCP server. If EPLAN never answers and "
                "eplan_list_dialogs shows nothing, ask the user before calling "
                "eplan_app_restart(force=True).",
                timed_out=True, eplan_state=st)
        if error is not None:
            if self.pid and self.host in self.LOCAL_HOSTS and pid_alive(self.pid) is False:
                self.connected = False
                return self._typed_failure(
                    "EplanNotRunning", action,
                    f"EPLAN (pid {self.pid}) is no longer running; the call failed "
                    f"with: {error}",
                    "eplan_app_launch")
            raise error
        return None

    @staticmethod
    def _shape_messages(result: dict) -> dict:
        """Turn eplanMessagesRaw into a ranked, capped, honest eplanMessages.

        Replaces a plain "first 20 in tree order" slice. In tree order the 20
        kept were whichever EPLAN happened to log first, which for a chatty
        action means twenty "Started opening database" lines while the actual
        per-project error falls off the end - measured on a real
        upgrade_projects call.

        eplanMessages keeps its existing shape, a list of strings, so this is
        additive rather than a contract break. Added alongside it:
          eplanMessagesTruncated  only when entries were dropped
          eplanMessagesTotal      how many EPLAN actually produced
          eplanMessagesLevels     only when something above Message is present

        The "only when" matters: on the healthy path this adds nothing to the
        payload at all.
        """
        # Drain every internal hint first. The C# emits eplanMessagesBounded
        # and eplanMessagesTrueTotal whenever it took a bookmark, which is on
        # every action - so returning early before popping them leaked two
        # internal fields into the response of every action that produced no
        # messages at all, i.e. most of them. Caught by running the real
        # template against live EPLAN; the offline tests all supplied messages.
        drained = {name: result.pop(name, None)
                   for name in INTERNAL_RESULT_FIELDS}
        raw = drained["eplanMessagesRaw"]
        ctx_raw = drained["eplanContextMessagesRaw"]
        scanned = drained["eplanMessagesScanned"]
        true_total = drained["eplanMessagesTrueTotal"]
        bounded = drained["eplanMessagesBounded"]

        if not isinstance(raw, list):
            raw = []
        if not isinstance(ctx_raw, list):
            ctx_raw = []
        if not raw and not ctx_raw:
            return result

        # Merge the per-call context collection with the bookmark slice,
        # de-duplicating on text. The bookmark slice goes first so its
        # chronology survives; anything only the context knew about is
        # appended and counted, which is how we find out whether that channel
        # is worth switching to. On the two actions measured so far the two
        # sources agreed exactly, so this is expected to add nothing most of
        # the time - eplanMessagesFromContextOnly appearing at all is the
        # signal that the bookmark slice has a gap.
        seen = set()
        merged = []
        for record in raw:
            if not isinstance(record, dict):
                continue
            text = record.get("text")
            if text and text not in seen:
                seen.add(text)
                merged.append(record)
        context_only = 0
        for record in ctx_raw:
            if not isinstance(record, dict):
                continue
            text = record.get("text")
            if text and text not in seen:
                seen.add(text)
                merged.append(record)
                context_only += 1
        raw = merged

        # Prefer the collection's own Count over how many entries we walked.
        # eplanMessagesScanned is bounded by MESSAGE_SCAN_CAP, so using it as
        # the total under-reports whenever the scan cap is what stopped us -
        # which is precisely the case where an accurate total matters.
        total = true_total
        if not isinstance(total, int) or total < 1:
            total = scanned
        if not isinstance(total, int):
            total = len(raw)
        # Count is the bookmark collection's own total and knows nothing about
        # the context collection, so anything only the context supplied has to
        # be added or the total under-reports what we are holding.
        total += context_only

        def rank(record):
            level = (record or {}).get("level") or ""
            try:
                return MESSAGE_LEVEL_RANK.index(level)
            except ValueError:
                return len(MESSAGE_LEVEL_RANK)

        # sorted() is stable, so chronological order is preserved inside each
        # severity bucket - a reader still sees the sequence EPLAN produced.
        ordered = sorted((r for r in raw if isinstance(r, dict)), key=rank)
        kept = ordered[:MESSAGE_CAP]


        texts = [r.get("text", "") for r in kept if r.get("text")]
        if not texts:
            return result

        result["eplanMessages"] = texts
        result["eplanMessagesTotal"] = total
        if total > len(texts):
            result["eplanMessagesTruncated"] = True

        levels = [r.get("level") for r in kept if r.get("level")]
        if any(lvl != "Message" for lvl in levels):
            result["eplanMessagesLevels"] = levels

        # Only surfaced when the slice could NOT be bounded, since that is the
        # case where entries from outside this action may have leaked in and
        # the total is therefore an upper bound rather than a fact.
        if bounded is False:
            result["eplanMessagesUnbounded"] = True
        # Only when the context collection knew something the bookmark slice
        # did not. Absent on the healthy path, and its presence is the
        # evidence that would justify switching channels.
        if context_only:
            result["eplanMessagesFromContextOnly"] = context_only
        return result

    @staticmethod
    def _log_dir() -> str:
        """Directory for actions.jsonl.

        EPLAN_MCP_LOG_DIR overrides the default, matching the EPLAN_MCP_MODE /
        EPLAN_MCP_EXTENSIONS convention. Read per call rather than cached at
        import, so a test fixture can point it at a tmp_path.
        """
        override = os.environ.get("EPLAN_MCP_LOG_DIR")
        if override:
            return override
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")

    def _log_action(self, action: str, result: dict, started: float) -> None:
        """Append one JSON line per executed action to actions.jsonl.

        Persistent trace of what the LLM did in EPLAN (Audit/TODO.md item 2):
        survives the conversation and lets failures be correlated with what
        the user saw on screen. Never raises.

        Note that LOGGED_RESULT_KEYS is a filter, not documentation: a
        diagnostic field added to the result but not to that tuple is absent
        from the trace, and so cannot be measured in a later audit.
        """
        try:
            log_dir = self._log_dir()
            os.makedirs(log_dir, exist_ok=True)
            entry = {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                # WHERE the action ran. Without this the trace for an action
                # against a remote, unauthenticated EPLAN is byte-identical to
                # one against localhost, so a reviewer cannot tell the session
                # ever left this workstation. Only recorded, never used to
                # decide anything.
                "host": self.host,
                "port": self.port,
                "action": action,
                "duration_s": round(time.time() - started, 3),
                "success": result.get("success"),
            }
            for key in LOGGED_RESULT_KEYS:
                if result.get(key):
                    entry[key] = result[key]
            with open(os.path.join(log_dir, "actions.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def execute_action(self, action: str, quiet_mode: bool = False, label: str = None,
                       timeout: float = None) -> dict:
        """
        Execute an EPLAN action.

        Args:
            action: The action string to execute
            quiet_mode: If True, suppresses all EPLAN dialogs during execution using a C# script.
            timeout: Seconds to allow the remote call when longer than
                ACTION_TIMEOUT_S (direct calls only).
        """
        if not self.connected or not self.client:
            return {"success": False, "message": "Not connected"}

        started = time.time()
        try:
            # Parse the action name (first word before any space or '/')
            action_name_match = re.match(r'^([^\s/]+)', action)
            action_name = action_name_match.group(1) if action_name_match else action
            action_name_lower = action_name.lower()

            # RegisterScript, ExecuteScript, and UnregisterScript MUST run directly
            # to avoid infinite recursion. Also run directly if quiet_mode is False.
            if action_name_lower in ("registerscript", "executescript", "unregisterscript") or not quiet_mode:
                logger.info(f"Executing directly: {action}")
                self.client.SynchronousMode = True
                result, hung_s = self._bounded_execute(action, label, timeout)
                if result is None:
                    result = self._with_hang_note(
                        {"success": True, "message": f"Executed directly: {action}", "action": action},
                        hung_s)
                # Script plumbing (register/execute/unregister) is logged only
                # as part of the wrapped action, not as separate entries.
                if action_name_lower not in ("registerscript", "executescript", "unregisterscript"):
                    self._log_action(action, result, started)
                return result

            # Parse parameters
            params = {}
            matches = re.finditer(r'/([a-zA-Z0-9_]+):(?:("([^"]*)"|([^\s]*)))', action)
            for m in matches:
                key = m.group(1)
                val = m.group(3) if m.group(2).startswith('"') else m.group(4)
                params[key] = val

            # Generate directories
            base_dir = os.path.dirname(os.path.abspath(__file__))
            script_dir = os.path.join(base_dir, "scripts", "generated")
            results_dir = os.path.join(base_dir, "scripts", "results")
            os.makedirs(script_dir, exist_ok=True)
            os.makedirs(results_dir, exist_ok=True)

            exec_id = str(uuid.uuid4())[:8]
            script_path = os.path.join(script_dir, f"exec_action_{exec_id}.cs")
            result_path = os.path.join(results_dir, f"exec_result_{exec_id}.json")

            # C# parameters generation. Keys are constrained to
            # [a-zA-Z0-9_]+ by the parse regex above; values are cs_escape'd
            # to prevent injection / uncompilable scripts.
            acc_parameters_code = ""
            check_keys = ["PROJECT", "PROJECTS", "PAGES", "LAYOUTSPACES", "PropertyValue", "value", "Value", "Result", "Output", "Success", "Count", "Error", "Message"]
            for key, val in params.items():
                acc_parameters_code += f'\n                acc.AddParameter("{key}", "{cs_escape(val)}");'
                if key not in check_keys:
                    check_keys.append(key)

            check_keys_code = ", ".join([f'"{k}"' for k in check_keys])
            escaped_result_path = result_path.replace("\\", "\\\\")
            escaped_action_name = cs_escape(action_name)
            raw_scan_cap = MESSAGE_SCAN_CAP

            # Escape hatch: EPLAN_MCP_LEGACY_CLI=1 emits the original
            # CommandLineInterpreter-only template (no FindAction, no message
            # capture) as a known-good fallback in case the enhanced template
            # fails to compile on some EPLAN version.
            if os.environ.get("EPLAN_MCP_LEGACY_CLI") == "1":
                script_content = self._legacy_script_content(
                    exec_id, acc_parameters_code, check_keys_code,
                    escaped_result_path, escaped_action_name,
                )
                with open(script_path, "w", encoding="utf-8") as f:
                    f.write(prepare_script(script_content, self.target_version))
                return self._run_generated_script(action, script_path, result_path, started)

            # C# Script Content.
            # Executor strategy (Audit/TODO.md item 1): resolve the action via
            # ActionManager.FindAction and run Action.Execute, which lets real
            # EPLAN exceptions propagate to our catch block — unlike
            # CommandLineInterpreter.Execute, which swallows them and returns
            # only false. CLI remains as fallback for unresolvable actions.
            # A message-tree bookmark taken before execution captures the
            # warnings/errors EPLAN emitted during the call even when no
            # exception is thrown (covers unreliable success:false results).
            script_content = f"""using System;
using System.IO;
using System.Collections.Generic;
using Eplan.EplApi.ApplicationFramework;
using Eplan.EplApi.Base;
using Eplan.EplApi.Scripting;

public class QuietExecute_{exec_id}
{{
    private static string ExceptionChain(Exception ex)
    {{
        var parts = new List<string>();
        while (ex != null)
        {{
            parts.Add(ex.Message);
            ex = ex.InnerException;
        }}
        return string.Join(" <- ", parts);
    }}

    [Start]
    public void Run()
    {{
        var results = new Dictionary<string, object>();
        int bookmark = 0;
        try
        {{
            using (var marker = new BaseException("MCP bookmark", MessageLevel.Message))
            {{
                bookmark = marker.GetBookmarkID();
            }}
        }}
        catch {{}}
        try
        {{
            using (var qm = new QuietModeStep(QuietModes.ShowNoDialogs))
            {{
                var acc = new ActionCallingContext();
                {acc_parameters_code}

                bool success;
                Eplan.EplApi.ApplicationFramework.Action eplanAction = null;
                try {{ eplanAction = new ActionManager().FindAction("{escaped_action_name}"); }}
                catch {{}}
                if (eplanAction != null)
                {{
                    results["executor"] = "action";
                    success = eplanAction.Execute(acc);
                }}
                else
                {{
                    results["executor"] = "cli-fallback";
                    var cli = new CommandLineInterpreter();
                    success = cli.Execute("{escaped_action_name}", acc);
                }}
                results["success"] = success;

                // The exception behind a silent success:false is already in
                // the context we are holding. The Action docs say exceptions
                // occurring during execution can be retrieved from the
                // ActionCallingContext via GetException(). Measured
                // 2026-09-03 on EPLAN 2027.0.1: after Action.Execute of
                // projectmanagement /TYPE:READPROJECTINFO with PROJECTNAME
                // omitted, this returns EPLAN's own no-file-found text
                // naming the FILENAME parameter, as a BaseException.
                //
                // Read AFTER the single execution and never by re-running the
                // action: success:false does not mean nothing happened
                // (restore_masterdata overwrote a folder and took sibling
                // files with it while reporting false), so harvesting a
                // message by retrying could repeat a side effect.
                try
                {{
                    var ctxEx = acc.GetException();
                    if (ctxEx != null)
                    {{
                        results["error"] = ctxEx.Message;
                        results["errorType"] = ctxEx.GetType().FullName;
                        results["errorFrom"] = "context";
                    }}
                }}
                catch {{}}

                // ActionCallingContext.SysMessages is a per-call collection,
                // scoped by construction rather than by bookmarking the global
                // tree. Measured populated on the Action.Execute path WITHOUT
                // CommandLineInterpreter's bCollectSysMessages flag, which is
                // undocumented. Collected as a second source and merged with
                // the bookmark slice below rather than replacing it: it has
                // been compared on two actions only, so this widens coverage
                // without betting the existing channel on a small sample.
                try
                {{
                    var ctxMsgs = acc.SysMessages;
                    if (ctxMsgs != null)
                    {{
                        var craw = new List<Dictionary<string, string>>();
                        var cit = ctxMsgs.GetSysMsgEnumerator();
                        int cscanned = 0;
                        while (cit.MoveNext() && cscanned < {raw_scan_cap})
                        {{
                            var cm = cit.Current as BaseException;
                            if (cm != null && !string.IsNullOrEmpty(cm.Message))
                            {{
                                var crec = new Dictionary<string, string>();
                                crec["text"] = cm.Message;
                                try {{ crec["level"] = cm.MessageLevel.ToString(); }}
                                catch {{ crec["level"] = ""; }}
                                craw.Add(crec);
                                cscanned++;
                            }}
                        }}
                        if (craw.Count > 0)
                        {{
                            results["eplanContextMessagesRaw"] = craw;
                        }}
                    }}
                }}
                catch {{}}

                var returnParams = new Dictionary<string, string>();
                string[] checkKeys = new string[] {{ {check_keys_code} }};
                foreach (var key in checkKeys)
                {{
                    try
                    {{
                        string val = "";
                        acc.GetParameter(key, ref val);
                        if (!string.IsNullOrEmpty(val))
                        {{
                            returnParams[key] = val;
                        }}
                    }}
                    catch {{}}
                }}
                results["parameters"] = returnParams;
            }}
        }}
        catch (Exception ex)
        {{
            results["success"] = false;
            results["error"] = ExceptionChain(ex);
            results["errorType"] = ex.GetType().FullName;
            // Provenance matters for auditing: errorType was absent from all
            // 1,463 logged actions before this change, so when it starts
            // appearing we need to know which channel produced it.
            results["errorFrom"] = "throw";
        }}

        // Close the slice at the top. With only a start bookmark the
        // collection is open-ended, so anything EPLAN emits after this action
        // - including from a later action in the same session - lands in it.
        int bookmarkEnd = 0;
        try
        {{
            using (var endMarker = new BaseException("MCP bookmark end", MessageLevel.Message))
            {{
                bookmarkEnd = endMarker.GetBookmarkID();
            }}
        }}
        catch {{}}

        // Collect system messages emitted during this action (bounded slice
        // only - never the whole historical tree).
        if (bookmark > 0)
        {{
            try
            {{
                // Collect text AND severity, and count everything seen. The
                // ranking and the cap are applied on the Python side, so the
                // policy is unit-testable without string-matching generated
                // C# - in the one file where a typo mutes all ~180 tools.
                var raw = new List<Dictionary<string, string>>();
                // Three-arg ctor (start, end, level) bounds the slice at both
                // ends; fall back to the open-ended two-arg one only if the
                // end bookmark could not be taken.
                SysMessagesCollection col;
                if (bookmarkEnd > bookmark)
                {{
                    col = new SysMessagesCollection(bookmark, bookmarkEnd, MessageLevel.Message);
                    results["eplanMessagesBounded"] = true;
                }}
                else
                {{
                    col = new SysMessagesCollection(bookmark, MessageLevel.Message);
                    results["eplanMessagesBounded"] = false;
                }}
                // Count is the collection's OWN total, so truncation is
                // reported exactly rather than inferred from how many entries
                // we managed to walk.
                try {{ results["eplanMessagesTrueTotal"] = col.Count; }} catch {{}}
                var it = col.GetSysMsgEnumerator();
                int scanned = 0;
                while (it.MoveNext() && scanned < {raw_scan_cap})
                {{
                    // SysMessagesEnumerator.Current is typed object - it must
                    // be cast before .Message is reachable, or the generated
                    // script fails to compile and every action breaks.
                    var m = it.Current as BaseException;
                    if (m != null && !string.IsNullOrEmpty(m.Message)
                        && m.Message != "MCP bookmark"
                        && m.Message != "MCP bookmark end")
                    {{
                        var rec = new Dictionary<string, string>();
                        rec["text"] = m.Message;
                        // Use MessageLevel. The shorter "Level" property does
                        // not exist and raises CS1061, which breaks every
                        // action - verified live 2026-09-01. ToString() rather
                        // than an enum member, so a renamed member cannot
                        // become a CS0117 at generation time. (Written without
                        // the forbidden spelling on purpose: a test greps the
                        // generated source for it.)
                        try {{ rec["level"] = m.MessageLevel.ToString(); }}
                        catch {{ rec["level"] = ""; }}
                        raw.Add(rec);
                        scanned++;
                    }}
                }}
                if (raw.Count > 0)
                {{
                    results["eplanMessagesRaw"] = raw;
                    results["eplanMessagesScanned"] = raw.Count;
                }}
            }}
            catch {{}}
        }}

        string json = Newtonsoft.Json.JsonConvert.SerializeObject(results);
        File.WriteAllText("{escaped_result_path}", json);
    }}
}}
"""
            with open(script_path, "w", encoding="utf-8") as f:
                f.write(prepare_script(script_content, self.target_version))
            return self._run_generated_script(action, script_path, result_path, started)

        except Exception as e:
            self.last_error = f"Scripted execution failed: {e}"
            logger.error(self.last_error)
            result = {"success": False, "message": self.last_error, "action": action}
            self._log_action(action, result, started)
            return result

    def _legacy_script_content(self, exec_id, acc_parameters_code, check_keys_code,
                               escaped_result_path, escaped_action_name) -> str:
        """Original CommandLineInterpreter-only template (EPLAN_MCP_LEGACY_CLI=1).

        Known-good fallback: no FindAction, no message-tree capture, so it
        cannot be broken by a message-API mismatch on some EPLAN version.
        It swallows EPLAN exceptions (returns only a bool) - the trade-off
        the escape hatch accepts for maximum compatibility.
        """
        return f"""using System;
using System.IO;
using System.Collections.Generic;
using Eplan.EplApi.ApplicationFramework;
using Eplan.EplApi.Scripting;

public class QuietExecute_{exec_id}
{{
    [Start]
    public void Run()
    {{
        var results = new Dictionary<string, object>();
        try
        {{
            using (var qm = new QuietModeStep(QuietModes.ShowNoDialogs))
            {{
                var acc = new ActionCallingContext();
                {acc_parameters_code}

                results["executor"] = "cli-legacy";
                var cli = new CommandLineInterpreter();
                bool success = cli.Execute("{escaped_action_name}", acc);
                results["success"] = success;

                var returnParams = new Dictionary<string, string>();
                string[] checkKeys = new string[] {{ {check_keys_code} }};
                foreach (var key in checkKeys)
                {{
                    try
                    {{
                        string val = "";
                        acc.GetParameter(key, ref val);
                        if (!string.IsNullOrEmpty(val))
                        {{
                            returnParams[key] = val;
                        }}
                    }}
                    catch {{}}
                }}
                results["parameters"] = returnParams;
            }}
        }}
        catch (Exception ex)
        {{
            results["success"] = false;
            results["error"] = ex.Message;
        }}

        string json = Newtonsoft.Json.JsonConvert.SerializeObject(results);
        File.WriteAllText("{escaped_result_path}", json);
    }}
}}
"""

    def _preserve_failed_script(self, script_path: str) -> Optional[str]:
        """Copy a generated script aside before the cleanup deletes it.

        When no result file appears the generated C# is the only evidence of
        why, and the `finally` below removes it - which is what made commit
        21d10d4d6 (a CS1061 that broke every wrapped action) expensive to
        diagnose. Returns the preserved path, or None if it could not be kept;
        never raises, because this runs on an error path.
        """
        try:
            dest_dir = os.path.join(self._log_dir(), "failed_scripts")
            os.makedirs(dest_dir, exist_ok=True)
            dest = os.path.join(dest_dir, os.path.basename(script_path))
            with open(script_path, "r", encoding="utf-8") as src:
                content = src.read()
            with open(dest, "w", encoding="utf-8") as out:
                out.write(content)
            return dest
        except Exception:
            return None

    def _script_failure(self, action, script_path, started, error_type, error, message):
        """Build, log and return a 'the action did not run' result.

        The three no-result paths below all used to return a bare `message`,
        which reads to a caller exactly like a slow action that eventually
        worked. Each now carries errorType plus executor="none", so the model
        can tell "EPLAN ran this and it failed" from "this never reached
        EPLAN", and stop retrying in the second case.
        """
        result = {
            "success": False,
            "executor": "none",
            "errorType": error_type,
            "error": error,
            "message": message,
            "action": action,
        }
        preserved = self._preserve_failed_script(script_path)
        if preserved:
            result["failedScriptPath"] = preserved
        self._log_action(action, result, started)
        return result

    def _run_generated_script(self, action, script_path, result_path, started) -> dict:
        """Execute a generated [Start] script, await the result file, clean up."""
        try:
            logger.info(f"Wrapping action via script: {action} (script={os.path.basename(script_path)})")
            # Execute only - deliberately NOT RegisterScript first. The wrapper
            # script generated above has only a [Start] method; RegisterScript is
            # for persistent [DeclareAction]/[DeclareEventHandler]/[DeclareMenu]
            # hooks, and ExecuteScript compiles and runs [Start] on its own.
            # Registering it only makes EPLAN report "The script does not
            # contain attributes for loading." (in its UI, not in the API
            # result) and costs two extra round-trips per action. Under
            # QuietMode - the whole point of this path - that is an error the
            # caller cannot even see. See scripted.py.
            # label: a later EplanBusy names the real action, not the temp script.
            exec_result = self.execute_action(f'ExecuteScript /ScriptFile:"{script_path}"',
                                              quiet_mode=False, label=action)
            if str(exec_result.get("errorType", "")).startswith("Eplan"):
                # A typed EPLAN failure (dialog, timeout, busy, dead) is not a
                # plumbing fault; pass it through so its next_step survives.
                exec_result["action"] = action
                self._log_action(action, exec_result, started)
                return exec_result
            if not exec_result.get("success"):
                return self._script_failure(
                    action, script_path, started,
                    "McpScriptExecuteFailed",
                    "EPLAN refused to run the generated wrapper script, so the "
                    "action did NOT execute. This is a fault in this MCP "
                    "server's script plumbing, not in the action or its "
                    "parameters - do not retry with different parameters. "
                    "Underlying ExecuteScript result: %s"
                    % exec_result.get("message"),
                    "Failed to execute action via script: %s"
                    % exec_result.get("message"),
                )

            # Wait for result file
            timeout = SCRIPT_RESULT_TIMEOUT_S
            start_time = time.time()
            while not os.path.exists(result_path):
                if time.time() - start_time > timeout:
                    return self._script_failure(
                        action, script_path, started,
                        "McpScriptNoResult",
                        "The action did NOT run: the generated wrapper script "
                        "produced no result file within %gs. This is NOT a "
                        "timeout on a slow action - EPLAN's ExecuteScript is "
                        "synchronous here, so a slow action would still have "
                        "written its result before returning. The usual cause "
                        "is that the generated C# failed to compile, which "
                        "affects every wrapped action equally rather than just "
                        "this one. The script has been preserved at "
                        "failedScriptPath; check eplan_get_system_messages for "
                        "a compiler error." % timeout,
                        "Timeout waiting for scripted action execution result",
                    )
                time.sleep(0.1)

            # Read results, tolerating a partially-written file (the C# writer
            # is not atomic vs our existence probe).
            res_data = None
            for attempt in range(10):
                if attempt:
                    time.sleep(0.05)
                try:
                    with open(result_path, "r", encoding="utf-8") as f:
                        res_data = json.load(f)
                    break
                except (json.JSONDecodeError, ValueError):
                    continue
            if res_data is None:
                return self._script_failure(
                    action, script_path, started,
                    "McpScriptBadResult",
                    "The action may or may not have run: a result file appeared "
                    "but never became valid JSON. Unlike McpScriptNoResult the "
                    "script did reach its write, so treat any side effect of "
                    "this action as possibly applied and verify before "
                    "retrying. The script has been preserved at "
                    "failedScriptPath.",
                    "Could not parse action result file",
                )

            # The hang seen by the inner ExecuteScript call belongs to this action.
            res_data = carry_hang_note(exec_result, self._shape_messages(res_data))
            self._log_action(action, res_data, started)
            return res_data

        except Exception as e:
            self.last_error = f"Scripted execution failed: {e}"
            logger.error(self.last_error)
            result = {"success": False, "message": self.last_error, "action": action}
            self._log_action(action, result, started)
            return result

        finally:
            try:
                if os.path.exists(script_path):
                    # No UnregisterScript - nothing was registered (see above).
                    os.remove(script_path)
            except Exception:
                pass
            try:
                if os.path.exists(result_path):
                    os.remove(result_path)
            except Exception:
                pass

    def disconnect(self) -> dict:
        """Disconnect from EPLAN."""
        try:
            if self.client:
                for name in ("Disconnect", "Dispose"):
                    _, timed_out, err = call_with_timeout(getattr(self.client, name), 10)
                    if err is not None:
                        raise err
                    if timed_out:
                        raise TimeoutError(f"EPLAN did not answer {name} within 10s")
            logger.info("Disconnected")
            return {"success": True, "message": "Disconnected"}
        except Exception as e:
            return {"success": False,
                    "message": f"Disconnect failed: {e}. The client was dropped anyway; "
                               "call eplan_connect to reconnect."}
        finally:
            # Always forget the client: a half-closed one is no use to anyone.
            self.client = None
            self.connected = False
            self.pid = None
            self._inflight = None

    def get_status(self) -> dict:
        """
        Get current connection status.

        "host" is reported because EPLAN remoting is UNAUTHENTICATED and this
        manager is a process-wide singleton: once something retargets it at
        another machine, every later action runs there. Without the host in the
        status (and in the action trace) neither the user nor a post-incident
        reviewer could tell a session had left the workstation.
        """
        return {
            "connected": self.connected,
            "api_loaded": self._clr_initialized,
            "target_version": self.target_version,
            "host": self.host if self.connected else None,
            "is_local": (self.host in self.LOCAL_HOSTS) if self.connected else None,
            "port": self.port if self.connected else None,
            "last_error": self.last_error
        }


# Singleton
_manager: Optional[EPLANConnectionManager] = None


def get_manager(target_version: str = None) -> EPLANConnectionManager:
    """Return the singleton connection manager.

    target_version: EPLAN major version like "2026". None = auto (newest
    installed). Once the DLLs of one version are loaded into this process,
    switching versions requires restarting the MCP server; a mismatching
    request keeps the loaded version and logs a warning.
    """
    global _manager
    if _manager is None:
        _manager = EPLANConnectionManager(target_version)
    elif target_version and str(target_version) != _manager.target_version:
        if _manager._clr_initialized:
            logger.warning(
                f"EPLAN {_manager.target_version} DLLs already loaded; "
                f"restart the MCP server to target {target_version}"
            )
        else:
            _manager = EPLANConnectionManager(target_version)
    return _manager
