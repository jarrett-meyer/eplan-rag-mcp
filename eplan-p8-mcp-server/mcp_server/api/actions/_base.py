"""
Base utilities for EPLAN actions.
Shared functions used by all action modules.

Every action executes inside a C# script under QuietMode
(QuietModes.ShowNoDialogs) so no EPLAN dialog can block unattended runs.
"""

from typing import Optional
import sys
import os

# Add parent directory to path for imports
mcp_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if mcp_root not in sys.path:
    sys.path.insert(0, mcp_root)

# Also insert the current folder's parent so 'from .project import ...' style works if needed
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from eplan_connection import get_manager, cs_escape  # noqa: F401  (cs_escape re-exported)


class QuietManagerWrapper:
    """Wrapper that forces execute_action to run with quiet_mode=True (via script)."""
    def __init__(self, manager):
        self.manager = manager

    def __getattr__(self, name):
        if name == "execute_action":
            # Direct everything to execute_action with quiet_mode=True
            return lambda action, *args, **kwargs: self.manager.execute_action(action, quiet_mode=True)
        return getattr(self.manager, name)


def _get_connected_manager():
    """Get the connection manager, ensuring it's connected."""
    manager = get_manager()
    if not manager.connected:
        return None, {"success": False, "message": "Not connected to EPLAN. Call eplan_connect() first."}
    return QuietManagerWrapper(manager), None


def _build_action(action_name: str, **params) -> str:
    """
    Build an action string with parameters.

    Values are quoted when they contain whitespace. A value that already looks
    like a well-formed quoted token is left alone so callers can pre-quote.

    A double quote anywhere else in a value is REJECTED rather than passed
    through. EPLAN re-parses this command string with
    /([a-zA-Z0-9_]+):("([^"]*)"|([^\\s]*)), so a stray quote lets a value close
    its own token and inject further /PARAM pairs - e.g. a project_name of
    '"x" /EXPORTFILE:C:/evil.pdf' would smuggle in an EXPORTFILE parameter.
    That silently defeats the registry allowlist in catalog.action_run(), which
    is documented as the validated alternative to execute_raw_action. Since no
    EPLAN parameter value legitimately contains a double quote, refusing is
    both safe and lossless.

    Raises:
        ValueError: if a string value contains an unbalanced double quote.
            Callers reach this through the MCP tool wrapper, which turns it
            into {"success": False, "error": ...} rather than a traceback.
    """
    parts = [action_name]
    for key, value in params.items():
        if value is not None and value != "":
            if isinstance(value, bool):
                value = "1" if value else "0"
            if isinstance(value, str):
                quoted = (len(value) >= 2 and value.startswith('"')
                          and value.endswith('"') and '"' not in value[1:-1])
                if not quoted:
                    if '"' in value:
                        raise ValueError(
                            f'Refusing to build action {action_name!r}: value for '
                            f'/{key} contains a double quote, which would let it '
                            f'inject additional parameters. Value: {value!r}'
                        )
                    if any(c.isspace() for c in value):
                        value = f'"{value}"'
            parts.append(f"/{key}:{value}")
    return " ".join(parts)


def _execute_with_quiet_mode(action: str) -> dict:
    """Execute an action with QuietMode enabled to suppress dialogs."""
    manager, error = _get_connected_manager()
    if error:
        return error
    return manager.execute_action(action, quiet_mode=True)
