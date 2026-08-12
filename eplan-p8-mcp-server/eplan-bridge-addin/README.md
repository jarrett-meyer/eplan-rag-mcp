# EPLAN Bridge Add-in

An in-process EPLAN add-in that gives the MCP server **live access to the
DataModel of the currently-open project** — the thing that deadlocks when
attempted through `RegisterScript`/`ExecuteScript`.

## Why it exists

Accessing a live `Project` (via `ProjectManager`, `SelectionSet.GetCurrentProject`,
`DMObjectsFinder`, …) from a script dispatched over the Remote API hangs forever:
that script cannot obtain the GED document lock / main-thread cooperation the open
project requires. See the discussion in `mcp_server/api/v2/actions/bridge.py`.

An add-in runs **in-process on EPLAN's main thread**, where live DataModel read
(and, in future, write via `LockingStep`) is legal. Python invokes the add-in's
actions through the **direct** Remote API dispatch path (not the QuietMode script
wrapper), passing an `/OUT:<path>` parameter; each action writes its JSON result
there and Python polls for it.

## Layout

| File | Purpose |
|------|---------|
| `EplanBridge.csproj` | net8.0 project, references EPLAN 2027 `*Netu.dll` (Private=false) |
| `BridgeAddIn.cs` | `IEplAddIn` lifecycle + `IEplAddInShadowCopy` (avoids DLL file-lock) |
| `BridgeResult.cs` | shared helper: read `/OUT`, always write a success/error JSON result |
| `Actions/BridgePing.cs` | no-op liveness action (`BridgePing`) |
| `Actions/BridgeQuery.cs` | `BridgeQueryFunctions`, `BridgeQueryPages` — live DataModel queries |
| `Actions/BridgeEdit.cs` | `BridgeSetFunctionText` — live DataModel **write** (LockingStep) |
| `build.ps1` | builds `bin/EplanBridge.dll` (committed to the repo) |

## Building

```powershell
./build.ps1                       # against EPLAN 2027.0.1
./build.ps1 -EplanBin "C:\Program Files\EPLAN\Platform\2027.0.3\Bin"
```

Requires the .NET SDK (targets `net8.0`, matching EPLAN 2027's coreclr runtime).
EPLAN 2026 and older are .NET Framework and use the `*u.dll` (non-`Net`)
assemblies — a `net48` build config would be needed for those (not yet added).

### The DLL-lock gotcha

EPLAN **file-locks a loaded add-in DLL** until the process restarts. Implementing
`IEplAddInShadowCopy` does *not* change this on its own (shadow-copy has to be
enabled in EPLAN's add-in configuration). Practical consequences:

- Rebuilding `bin/EplanBridge.dll` while EPLAN has it loaded fails with
  `file is locked by Eplan`. **Fully restart EPLAN, then rebuild.**
- For fast dev iteration without restarting, build to a *new* filename each time
  (`-p:AssemblyName=EplanBridgeDevN -p:OutputPath=<tmp>`) and register that copy;
  each loaded copy stays locked until restart, but you're not overwriting it.
- In normal operation this is a non-issue — you don't rebuild during a session.

## Loading at runtime

`bridge.py` auto-loads the add-in on first use via:

```
EplApiModuleAction /register:"<path>\bin\EplanBridge.dll"
```

Once registered, its actions (`BridgePing`, `BridgeQueryFunctions`,
`BridgeQueryPages`, `BridgeSetFunctionText`) are callable by name. `OnRegister` sets
load-on-start, so a persistent install also survives EPLAN restarts.
Alternatively register it once via the GUI: **Utilities → API → Add-ins**.

## Exposed MCP tools

Auto-registered by the server as:

- `eplan_v2_bridge_ping`
- `eplan_v2_bridge_query_functions`
- `eplan_v2_bridge_query_pages`
- `eplan_v2_bridge_set_function_text` — live edit (write); confirm before use
