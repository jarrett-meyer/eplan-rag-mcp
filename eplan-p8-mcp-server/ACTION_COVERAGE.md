# Full EPLAN action coverage

Goal: let Claude drive EPLAN the way a human can — every API action *and* every
GUI button — without publishing one MCP tool per action.

Measured on **EPLAN Electric P8 2027.0.1 Premium** (`C:\Program Files\EPLAN\Platform\2027.0.1`).

## The inventory

| Source | Count | What it is |
|---|---:|---|
| Official API docs (`api/2027/API Actions.html`) | 100 | "the list of all official Eplan API actions available for the user" |
| `Cfg/MFTools.xml` → `UsedActions` | 704 | GUI command id → full action command line (with real parameters) |
| `Cfg/MFTools.xml` → `ActionCategory` | 703 | rights-management grouping of GUI actions |
| `Cfg/MFTools.xml` → `Dialogs` | 157 | dialog-opening actions |
| **Union (registry total)** | **1150** | `mcp_server/api/actions/data/action_registry.json` |

Regenerate with `python tools/build_action_registry.py`. It is stdlib-only,
byte-stable (no timestamp), and degrades gracefully with a warning when
`MFTools.xml` is absent, so the repo stays portable to machines without EPLAN.

### What this install actually has

`ActionManager.FindAction(name, silent=true)` probed over all 1148 candidate
names (`tools/data/live_actions_2027.json`):

- **937 resolve** — registered, module loaded, licensed. Includes **all 100
  documented actions**, so nothing in the official API is outside this
  subscription.
- 211 do not, and they are explained rather than mysterious:
  - 141 come only from the `Dialogs` section — those are **dialog ids**, not actions.
  - 46 are **GED interaction names** (values for `XGedStartInteractionAction /Name:`,
    not standalone actions) — extracted to `tools/data/ged_interaction_names.json`.
  - **7 are genuine gaps**: `ExportActionPxcCxe`, `ExportActionPxcMarking`,
    `ExportActionPxcPlanning`, `ExportActionPxcSettings` (Phoenix Contact add-on,
    not installed), `XGedViewExternalDocumentAction`,
    `XSettingsProjCreateAllDescAction`, `XSettingsProjCreateDescAction`.

## How the actions are exposed

**Tier 1 — typed tools (101 actions, 187 `eplan_*` tools).** The 15 documented
actions that had no wrapper were added following the existing convention
(`_build_action`, an `Action: <Name>` docstring line so `tools/validate_actions.py`
keeps working, exported from `api/actions/__init__.py`):

`exportToGraphics`, `InsertModelViewAction`, `XPlaUpdateDetailAction`,
`XCCreateGravingtextAction`, `XPrjConvertBaseProjectsAction`,
`XAMlExportProductionData2RASCenterAction`,
`XAMlExportProductionData2SmartMountingAction`, `ProjectAction`,
`EplApiModuleActionNet`, `RegisterCustomPropertyEditorAction`,
`XGedStartInteractionAction`, `XDLInsertDeviceAction`, `XEGActionInsertSymRef`,
`XPamsDeviceSelectionAction`, `LockUnlockAllObjects`.

**Tier 2 — catalog + validated dispatch (`catalog.py`), 4 tools** for the other
~1049. One tool per GUI action would have tripled the server's tool count and
degraded tool selection for everything else.

- `eplan_action_catalog(search, category, documented_only, wrapped, available_only, limit)`
  — offline registry search; always reports the true match count so a truncated
  list is never mistaken for the total. `available_only=True` restricts results
  to the 937 actions the live probe found registered on this installation, and
  every record carries `live_resolved` + `module_name`.
- `eplan_action_describe(name)` — registry entry + live `FindAction` probe
  (resolved? `ModuleName`?). Degrades to registry-only when disconnected.
- `eplan_action_run(name, params, dry_run, allow_unknown_params)` — validated
  dispatch for *any* action. Rejects unknown action names (with near matches) and
  unknown parameter keys, with an escape hatch because many registry params are
  observed rather than documented. Supersedes `execute_raw_action`.
- `eplan_ribbon_catalog(tab, search)` — the live GUI tree, tab → group → button.
  The full tree serialises to ~147,000 characters (past the tool-result cap), so
  it drills down: no arguments returns the tab index (~2.4 KB), `tab="Insert"`
  returns that tab's buttons (~20 KB), `search="macro"` finds a button by label
  across all tabs.

### The ribbon → action bridge

EPLAN documents `RibbonCommand.ActionCommandLine` as *"available only from a
custom command"*, and live testing confirmed built-in buttons return `""`. But
they do expose their **command id**, and `MFTools.xml/UsedActions` is keyed by
that same id. `build_action_registry.py` emits a `_command_index`
(1019 entries) and `ribbon_catalog()` joins against it.

Result, verified live: **352 of 361 ribbon buttons resolved** (9 unresolved).

```
Home  Clipboard  Paste          -> GfDlgMgrActionIGfWind /function:Paste
Home  Page       New            -> XPageNewDialogShow
Home  3D layout  New            -> XCabCreateInstallationSpace
```

So a button a human clicks can be found by name and then run with
`action_run()` — which is what "use EPLAN like a human" actually requires.

## Testing

**Offline: 351 passed, 2 skipped** (182 pre-existing, 169 new; zero regressions).
The two skips are the only two actions whose doc pages 404
(`LockUnlockAllObjects`, `XAMlExportProductionData2SmartMountingAction`).

The highest-value assertions cross-check every emitted `/KEY` against
`tools/data/official_actions_2027.json` verbatim, and pin parameter **casing**
(`/NOCLOSE`, `/OpenMode`, `/PartNr`, `/Register`, `/ConfigScheme`,
`/Placementmode`, and the all-lowercase `register`/`registerModule` on
`EplApiModuleActionNet`). The wrapper test suite was mutation-checked: seeded
faults produced exactly the expected failures.

**Live (scratch only): 13/14 wrapper calls + ribbon resolution.**
Ran against a disposable clone of an EPLAN sample project in
`%TEMP%\eplan_mcp_scratch`, upgraded to the 2027 scheme. No project or master
data outside that scratch directory was written to.

| Action | Result | Note |
|---|---|---|
| `exportToGraphics` | PASS | 52 PNGs (project) + 2 (per-page). **`TYPE` is `GRAPHICPROJECT`/`GRAPHICPAGE`** — `PROJECT` raises *"This operation is not supported."* |
| `ProjectAction` | PASS | **Only works on a CLOSED project** — it opens the project itself. Against an open one: *"Project is already open."* |
| `XCCreateGravingtextAction` | PASS | |
| `XGedStartInteractionAction` | PASS | Needs an open page; returns immediately, does **not** block |
| `XEGActionInsertSymRef` | PASS | |
| `XPamsDeviceSelectionAction` | PASS | |
| `XDLInsertDeviceAction` | PASS | Opens the configured parts database **read-only** to resolve the part number |
| `action_catalog` / `describe` / `run` / `ribbon_catalog` | PASS | `describe` resolved `exportToGraphics` → module `Eplan.EplApi.CommandLineActionsNet` |
| `XPlaUpdateDetailAction` | FAIL (expected) | Sample project has no Preplanning objects |
| `LockUnlockAllObjects` | FAIL | Deprecated; *"Unable to gain access to the database."* |
| `XAMlExportProductionData2RASCenterAction` | FAIL (expected) | Needs 3D construction spaces |

All four interaction-start actions were run with a UI dismiss watcher armed:
**0 modal dialogs appeared, and every call returned immediately** — safe to call
headless under QuietMode. `interaction.py` documents this measured behaviour
rather than the theoretical "may block" warning. The residual caveat is that a
call returning means the interaction was *started*, not finished: EPLAN is left
in that editor mode.

## Known gaps

- `InsertModelViewAction`, `XAMlExportProductionData2SmartMountingAction` and
  `XPrjConvertBaseProjectsAction` are **not live-verified** — no suitable fixture
  (3D layout space / legacy `.ept`-`.epb` basic projects) on this machine.
- `XAMlExportProductionData2SmartMountingAction` parameters are **inferred** from
  its sibling RAS Center action; its doc page 404s. If a call is silently
  ignored, suspect these names first.
- `EplApiModuleActionNet` and `RegisterCustomPropertyEditorAction` were
  deliberately **not executed** — they persist registration into the session.
- The registry can go stale: `tools/build_action_registry.py` must be re-run
  after adding wrappers, and the determinism test only compares build-vs-build,
  never committed-vs-source.
- `ribbon_catalog`'s `resolved_from_command_index` / `unresolved_commands`
  counters describe the whole ribbon walk, not the filtered view.
- EPLAN appears to lowercase parameter keys internally (an error echoed
  `/projectname:` for a `/PROJECTNAME:` call), so the casing discipline above may
  be stricter than required — documented casing is still what the wrappers send.

## Refreshing after a version or licence change

```
python tools/build_action_registry.py      # re-mine MFTools.xml + docs
python -m pytest tests/ -q                 # 351 expected
python tools/validate_actions.py --out tools/action_validation_report.md
```

`tools/data/live_actions_2027.json` (the FindAction probe) is a point-in-time
snapshot of what this installation had registered; re-probe after upgrading.
