"""
Bridge actions - in-process EPLAN add-in for LIVE DataModel access.

Why this exists
---------------
Accessing the DataModel of the currently-open project (ProjectManager,
SelectionSet.GetCurrentProject, DMObjectsFinder, ...) from a script dispatched
via RegisterScript/ExecuteScript *deadlocks forever*: that script runs in a
context that cannot obtain the GED document lock / main-thread cooperation the
live project requires.

The bridge sidesteps this by moving the DataModel work into a compiled EPLAN
add-in (../../../eplan-bridge-addin) that runs IN-PROCESS on EPLAN's main
thread, where live read (and, later, write) is legal. Python invokes the
add-in's actions through the *direct* Remote API dispatch path -- NOT the V2
QuietMode script wrapper, which is the very mechanism that deadlocks.

Each add-in action takes an /OUT:<path> parameter and writes its JSON result
there (always, even on error). Python dispatches the action and polls for that
file -- the same handshake used by scripted.py, but without RegisterScript.
"""

import os
import json
import time
import uuid

# _base sets up sys.path so `eplan_connection` is importable and exposes
# TARGET_VERSION. We deliberately use the *raw* manager (not the V2 wrapper),
# because the wrapper forces quiet_mode=True (the deadlocking script path).
from ._base import TARGET_VERSION
from eplan_connection import get_manager

# Locate mcp_server root (dir containing eplan_connection.py), then the sibling
# add-in build output: <server>/eplan-bridge-addin/bin/EplanBridge.dll
_MCP_ROOT = os.path.dirname(os.path.abspath(__file__))
while not os.path.exists(os.path.join(_MCP_ROOT, "eplan_connection.py")):
    _parent = os.path.dirname(_MCP_ROOT)
    if _parent == _MCP_ROOT:
        break
    _MCP_ROOT = _parent

_SERVER_ROOT = os.path.dirname(_MCP_ROOT)  # the eplan-p8-mcp-server folder
ADDON_DLL = os.path.join(_SERVER_ROOT, "eplan-bridge-addin", "bin", "EplanBridge.dll")
RESULTS_DIR = os.path.join(_MCP_ROOT, "scripts", "results")

# Set once the add-in has been registered into the current EPLAN process.
_ADDON_LOADED = False


def _get_raw_manager():
    """Return the raw connection manager (direct dispatch), or an error dict."""
    manager = get_manager(TARGET_VERSION)
    if not manager.connected:
        return None, {
            "success": False,
            "message": "Not connected to EPLAN. Call eplan_connect() first.",
        }
    return manager, None


def _load_addon(manager) -> dict:
    """Register the bridge add-in DLL into the running EPLAN process.

    Uses EplApiModuleAction /register (the documented add-in loader). Idempotent:
    re-registering an already-loaded module is harmless. Shadow-copy in the
    add-in means the DLL on disk is not locked while loaded.
    """
    global _ADDON_LOADED
    if not os.path.exists(ADDON_DLL):
        return {
            "success": False,
            "message": f"Bridge add-in not built: {ADDON_DLL} missing. "
                       f"Build it with eplan-bridge-addin/build.ps1.",
        }
    res = manager.execute_action(
        f'EplApiModuleAction /register:"{ADDON_DLL}"', quiet_mode=False
    )
    if not res.get("success"):
        return {"success": False, "message": f"Failed to load add-in: {res.get('message')}"}
    _ADDON_LOADED = True
    return {"success": True}


def _dispatch(action_name: str, params: dict = None, timeout: float = 30.0) -> dict:
    """Invoke a bridge add-in action and return its JSON result.

    Dispatches via DIRECT Remote API (quiet_mode=False) so the action runs
    in-process on EPLAN's main thread, then polls for the /OUT result file.
    On a missing result (e.g. EPLAN restarted and the add-in is no longer
    loaded), the add-in is reloaded once and the call retried.
    """
    manager, error = _get_raw_manager()
    if error:
        return error

    os.makedirs(RESULTS_DIR, exist_ok=True)

    def _attempt() -> dict:
        exec_id = uuid.uuid4().hex[:8]
        result_path = os.path.join(RESULTS_DIR, f"bridge_{action_name}_{exec_id}.json")

        action = f'{action_name} /OUT:"{result_path}"'
        for key, val in (params or {}).items():
            if val is None or val == "":
                continue
            sval = str(val)
            action += f' /{key}:"{sval}"' if " " in sval else f' /{key}:{sval}'

        try:
            disp = manager.execute_action(action, quiet_mode=False)
            if not disp.get("success"):
                return {"success": False, "message": f"Dispatch failed: {disp.get('message')}"}

            start = time.time()
            while not os.path.exists(result_path):
                if time.time() - start > timeout:
                    return {"_timeout": True}
                time.sleep(0.05)

            time.sleep(0.02)  # let the OS flush the file
            with open(result_path, "r", encoding="utf-8") as f:
                return json.load(f)
        finally:
            try:
                if os.path.exists(result_path):
                    os.remove(result_path)
            except OSError:
                pass

    # Ensure the add-in is loaded (first call this process), then dispatch.
    global _ADDON_LOADED
    if not _ADDON_LOADED:
        load = _load_addon(manager)
        if not load.get("success"):
            return load

    result = _attempt()
    if result.get("_timeout"):
        # Add-in likely not loaded (EPLAN restarted). Reload once and retry.
        _ADDON_LOADED = False
        load = _load_addon(manager)
        if not load.get("success"):
            return load
        result = _attempt()
        if result.get("_timeout"):
            return {
                "success": False,
                "message": f"Timeout waiting for bridge action '{action_name}' result "
                           f"(add-in loaded but produced no output).",
            }
    return result


# =============================================================================
# TOOLS
# =============================================================================


def bridge_ping() -> dict:
    """
    Liveness check for the in-process bridge add-in.

    Loads the add-in if needed and calls a no-op action that writes a result
    file. Confirms the add-in is registered and the direct-dispatch handshake
    works, independent of any DataModel access. Returns {"pong": true} on success.
    """
    return _dispatch("BridgePing")


def bridge_query_functions(contains: str = None, limit: int = 100) -> dict:
    """
    Query the currently-open project's functions (devices) LIVE via DMObjectsFinder.

    Runs in-process on EPLAN's main thread, so it succeeds where the script-based
    search_* actions deadlock. Returns matched count and up to `limit` function
    identifying names.

    Args:
        contains: Case-insensitive substring to filter function names (optional).
        limit: Maximum number of function entries to return (default 100).
    """
    return _dispatch("BridgeQueryFunctions", {"CONTAINS": contains, "LIMIT": limit})


def bridge_query_pages(contains: str = None, limit: int = 100) -> dict:
    """
    Query the currently-open project's pages LIVE via DMObjectsFinder.

    Runs in-process on EPLAN's main thread. Returns matched count and up to
    `limit` pages, each with its name and page type.

    Args:
        contains: Case-insensitive substring to filter page names (optional).
        limit: Maximum number of page entries to return (default 100).
    """
    return _dispatch("BridgeQueryPages", {"CONTAINS": contains, "LIMIT": limit})


def bridge_set_function_text(name: str, text: str, limit: int = 1) -> dict:
    """
    LIVE EDIT: set the function text (FUNC_TEXT) of functions matching `name`.

    FUNC_TEXT is the "Function text" shown on the schematic, so the change is
    visible in EPLAN. Writes directly to the open project's DataModel in-process,
    under a LockingStep write lock (the change is undoable in EPLAN). This is a
    DESTRUCTIVE action -- confirm with the user before calling. Returns the
    previous text of each modified function so the edit can be reversed.

    Args:
        name: Exact function identifying name to match (e.g. "+TEST-TEST").
        text: New function text to set (pass "" to clear).
        limit: Max number of matching functions to modify (default 1, a safety
               cap so a wrong `name` cannot mass-edit the project).

    Note: FUNC_TEXT is a multi-language property; the returned `previous` value
    is EPLAN's internal MultiLangString encoding (an opaque snapshot), not clean
    per-language display text.
    """
    if not name:
        return {"success": False, "message": "name is required"}
    return _dispatch(
        "BridgeSetFunctionText",
        {"NAME": name, "TEXT": text, "LIMIT": limit},
    )
