"""
E3D / 3D layout space actions.

These reach the EPLAN DataModel.E3D object model through runtime reflection
because the script engine only compiles Base/ApplicationFramework (direct
references to DataModel/HEServices fail with CS0234). The techniques used here
were proven interactively (see the eplan-development skill reference
`e3d-installation-spaces.md`):

- create_installation_space: headless InstallationSpace.Create
- insert_3d_macro: headless Insert3D.WindowMacro (fileName overload)
"""

import uuid

from ._base import cs_escape
from ._base import _get_connected_manager, _build_action
from .scripted import _execute_script


def create_installation_space(space_name: str) -> dict:
    """
    Create a 3D installation (layout) space in the current project, headless.

    Runs InstallationSpace.Create(project, name) inside a LockingStep via
    runtime reflection, so no EPLAN dialog is shown. If a space with the same
    VisibleName already exists it is left untouched.

    Args:
        space_name: VisibleName for the new space.

    Returns:
        dict with created flag, createdName and existing space list.
    """
    if not space_name:
        return {"success": False, "error": "space_name is required"}
    if '"' in space_name or "\r" in space_name or "\n" in space_name:
        return {"success": False, "error": "space_name must be a single line without double quotes"}

    body = r"""
using System;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Collections.Generic;
using Eplan.EplApi.ApplicationFramework;
using Eplan.EplApi.Base;
using Eplan.EplApi.Scripting;

public class CreateSpace_%EXECID%
{
    private static List<string> Chain(Exception ex)
    {
        var c = new List<string>();
        Exception cur = ex;
        while (cur != null) { c.Add(cur.GetType().Name + ": " + cur.Message); cur = cur.InnerException; }
        return c;
    }

    [Start]
    public void Run()
    {
        var results = new Dictionary<string, object>();
        try
        {
            var dm = Assembly.Load("Eplan.EplApi.DataModelu");
            var he = Assembly.Load("Eplan.EplApi.HEServicesu");

            var lsType = dm.GetType("Eplan.EplApi.DataModel.LockingStep");
            object lockStep = Activator.CreateInstance(lsType);
            try
            {
                var ssType = he.GetType("Eplan.EplApi.HEServices.SelectionSet");
                object ss = Activator.CreateInstance(ssType);
                var getCur = ssType.GetMethod("GetCurrentProject", new Type[] { typeof(bool) });
                object project = getCur.Invoke(ss, new object[] { false });
                if (project == null)
                {
                    results["success"] = false;
                    results["error"] = "no current project via SelectionSet (inside LockingStep)";
                }
                else
                {
                    results["project"] = (string)project.GetType().GetProperty("ProjectFullName").GetValue(project, null);

                    var existing = new List<string>();
                    var isProp = project.GetType().GetProperty("InstallationSpaces");
                    if (isProp != null)
                    {
                        var spaces = (System.Collections.IEnumerable)isProp.GetValue(project, null);
                        foreach (var s in spaces)
                        {
                            existing.Add((string)s.GetType().GetProperty("VisibleName").GetValue(s, null));
                        }
                    }
                    results["existing"] = existing;
                    results["existsAlready"] = existing.Contains("%NAME%");

                    if (existing.Contains("%NAME%"))
                    {
                        results["created"] = false;
                        results["note"] = "already exists";
                    }
                    else
                    {
                        var isType = dm.GetType("Eplan.EplApi.DataModel.E3D.InstallationSpace");
                        var staticCreate = isType.GetMethods(BindingFlags.Public | BindingFlags.Static)
                            .FirstOrDefault(m => m.Name == "Create" && m.GetParameters().Length == 3);
                        if (staticCreate != null)
                        {
                            try
                            {
                                object space = staticCreate.Invoke(null, new object[] { project, "%NAME%", null });
                                results["created"] = true;
                                results["createdName"] = (string)space.GetType().GetProperty("VisibleName").GetValue(space, null);
                                results["via"] = "InstallationSpace.Create static";
                            }
                            catch (Exception ex)
                            {
                                results["created"] = false;
                                results["createError"] = string.Join(" <- ", Chain(ex));
                            }
                        }
                        else
                        {
                            results["created"] = false;
                            results["createError"] = "no static Create(3) found";
                        }
                    }
                    results["success"] = true;
                }
            }
            catch (Exception ex)
            {
                results["success"] = false;
                results["error"] = string.Join(" <- ", Chain(ex));
                results["errorType"] = ex.GetType().FullName;
            }
            finally
            {
                try { lsType.GetMethod("Dispose").Invoke(lockStep, null); } catch { }
            }
        }
        catch (Exception ex)
        {
            results["success"] = false;
            results["error"] = string.Join(" <- ", Chain(ex));
            results["errorType"] = ex.GetType().FullName;
        }
        string json = Newtonsoft.Json.JsonConvert.SerializeObject(results, Newtonsoft.Json.Formatting.Indented);
        File.WriteAllText(@"{{RESULT_PATH}}", json);
    }
}
"""
    body = body.replace("%EXECID%", uuid.uuid4().hex[:8])
    body = body.replace("%NAME%", cs_escape(space_name))

    return _execute_script(body)


def insert_3d_macro(space_name: str, macro_path: str, variant: int = 0) -> dict:
    """
    Insert a 3D window macro into an installation space, headless.

    Uses Insert3D.WindowMacro(fileName, variant, parentSpace, identityMatrix,
    MoveKind=0, NumerationMode=0) via runtime reflection. The parent space is
    located by VisibleName inside the current project; nothing is shown.

    Args:
        space_name: VisibleName of the target installation space.
        macro_path: Absolute path to the .ema window macro.
        variant: Macro variant to insert (default 0).

    Returns:
        dict with spaceFound, placedVia and pluralCount.
    """
    if not space_name:
        return {"success": False, "error": "space_name is required"}
    if not macro_path:
        return {"success": False, "error": "macro_path is required"}

    body = r"""
using System;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Collections.Generic;
using Eplan.EplApi.ApplicationFramework;
using Eplan.EplApi.Base;
using Eplan.EplApi.Scripting;

public class InsertMacro_%EXECID%
{
    private static List<string> Chain(Exception ex)
    {
        var c = new List<string>();
        Exception cur = ex;
        while (cur != null) { c.Add(cur.GetType().Name + ": " + cur.Message); cur = cur.InnerException; }
        return c;
    }

    [Start]
    public void Run()
    {
        var results = new Dictionary<string, object>();
        try
        {
            var dm = Assembly.Load("Eplan.EplApi.DataModelu");
            var he = Assembly.Load("Eplan.EplApi.HEServicesu");

            var lsType = dm.GetType("Eplan.EplApi.DataModel.LockingStep");
            object lockStep = Activator.CreateInstance(lsType);
            try
            {
                var ssType = he.GetType("Eplan.EplApi.HEServices.SelectionSet");
                object ss = Activator.CreateInstance(ssType);
                object project = ssType.GetMethod("GetCurrentProject", new Type[] { typeof(bool) })
                    .Invoke(ss, new object[] { false });
                if (project == null) { results["success"] = false; results["error"] = "no project"; return; }
                results["project"] = (string)project.GetType().GetProperty("ProjectFullName").GetValue(project, null);

                Type isType = dm.GetType("Eplan.EplApi.DataModel.E3D.InstallationSpace");
                object space = null;
                var rawSpaces = project.GetType().GetProperty("InstallationSpaces").GetValue(project, null);
                foreach (var s in (System.Collections.IEnumerable)rawSpaces)
                {
                    if ((string)s.GetType().GetProperty("VisibleName").GetValue(s, null) == "%SPACE%")
                    { space = s; break; }
                }
                if (space == null) { results["success"] = false; results["error"] = "space not found: %SPACE%"; return; }
                results["spaceFound"] = true;

                Type insertType = he.GetType("Eplan.EplApi.HEServices.Insert3D");
                object insert = Activator.CreateInstance(insertType);
                var candidates = insertType.GetMethods(BindingFlags.Public | BindingFlags.Instance)
                    .Where(m => m.Name == "WindowMacro" && m.GetParameters().Length == 6)
                    .ToList();
                if (candidates.Count == 0) { results["success"] = false; results["error"] = "no 6-param WindowMacro"; return; }

                MethodInfo wanted = null;
                foreach (var m in candidates)
                {
                    var p0 = m.GetParameters()[0];
                    if (p0.ParameterType == typeof(string)) { wanted = m; break; }
                }
                if (wanted == null) { results["success"] = false; results["error"] = "no fileName overload"; return; }

                var ps = wanted.GetParameters();
                var args = new object[ps.Length];
                args[0] = "%MACRO%";
                foreach (var p in ps)
                {
                    if (p.Name == "oParent") { args[p.Position] = space; }
                    else if (p.Name == "oMatrix")
                    {
                        args[p.Position] = Activator.CreateInstance(p.ParameterType);
                    }
                    else if (p.Name == "nMoveCondition" || p.Name == "nNumerationMode")
                    {
                        args[p.Position] = Enum.ToObject(p.ParameterType, 0);
                    }
                    else if (p.Name == "strFileName") { args[p.Position] = "%MACRO%"; }
                    else if (p.Name == "nVariant") { args[p.Position] = %VARIANT%; }
                    else if (p.Name == "dRotationAngle" || p.Name == "dX" || p.Name == "dY" || p.Name == "dZ")
                    {
                        if (args[p.Position] == null) args[p.Position] = 0.0;
                    }
                }

                results["withArgs"] = string.Join(", ", ps.Select(p => p.Name + ":" + p.ParameterType.Name));
                try
                {
                    object placed = wanted.Invoke(insert, args);
                    results["success"] = true;
                    results["placedVia"] = "Insert3D.WindowMacro(fileName,...)";
                    if (placed is System.Collections.IEnumerable)
                    {
                        int n = 0;
                        foreach (var o in (System.Collections.IEnumerable)placed) n++;
                        results["pluralCount"] = n;
                    }
                }
                catch (Exception ex)
                {
                    results["success"] = false;
                    results["insertError"] = string.Join(" <- ", Chain(ex));
                }
            }
            finally
            {
                try { lsType.GetMethod("Dispose").Invoke(lockStep, null); } catch { }
            }
        }
        catch (Exception ex)
        {
            results["success"] = false;
            results["error"] = string.Join(" <- ", Chain(ex));
            results["errorType"] = ex.GetType().FullName;
        }
        string json = Newtonsoft.Json.JsonConvert.SerializeObject(results, Newtonsoft.Json.Formatting.Indented);
        File.WriteAllText(@"{{RESULT_PATH}}", json);
    }
}
"""
    body = body.replace("%EXECID%", uuid.uuid4().hex[:8])
    body = body.replace("%SPACE%", cs_escape(space_name))
    body = body.replace("%MACRO%", cs_escape(macro_path))
    body = body.replace("%VARIANT%", str(int(variant)))

    return _execute_script(body)


def insert_model_view(
    layout_space: str,
    page_name: str,
    dx: float,
    dy: float,
    project_name: str = None,
    structure: str = None,
    view_name: str = None,
    description: str = None,
    angle: int = None,
    selection_scheme: str = None,
    style: int = None,
    item_labeling: str = None,
    viewpoint: int = None,
    root_elements: str = None,
    scale_setting: int = None,
    scale: str = None,
    view_type: int = None,
    object_id: str = None
) -> dict:
    """
    Insert a model view object of a layout space onto a page.
    Action: InsertModelViewAction

    Args:
        layout_space: Name of the layout space the model view is created for (mandatory)
        page_name: Full name of the page the model view is inserted on (mandatory)
        dx: Width of the model view (mandatory)
        dy: Height of the model view (mandatory)
        project_name: Project name with full path (optional; selected project if omitted)
        structure: Structure identifier - mandatory when the layout space name
                   is not unique within the project
        view_name: Name of the model view
        description: Description of the model view (multi-language string format allowed)
        angle: Rotation of the model view content - 1 = 90 deg counter-clockwise,
               2 = 90 deg in the opposite direction
        selection_scheme: Name of the selection scheme; only used together with
                          view_type = 1 (cabinet)
        style: Display style - 0 wire frame, 1 hidden lines, 2 shading,
               3 hidden lines / simplified, 4 shading / simplified
        item_labeling: Name of the scheme applied for labeling items in the view
        viewpoint: Direction objects are seen from - 0 default, 1 bottom, 2 top,
                   3 left, 4 right, 5 front, 6 rear, 7 SE isometric,
                   8 SW isometric, 9 NE isometric, 10 NW isometric
        root_elements: FUNCTION3D_ID_RELATIVE values of the 3D placements to set
                       as root elements, separated by "#"
        scale_setting: Scaling type - 0 automatic, 1 fit, 2 manually defined
        scale: Scale used to display objects in the model view
        view_type: View type - 0 undefined, 1 cabinet, 2 EMI, 3 unfolding, 4 drill view
        object_id: [OUT] Object id of the created model view. This is an output
                   slot of the action; passing it in the command string has no
                   effect, so leave it None.
    """
    manager, error = _get_connected_manager()
    if error:
        return error

    action = _build_action(
        "InsertModelViewAction",
        PROJECTNAME=project_name,
        LAYOUTSPACE=layout_space,
        STRUCTURE=structure,
        PAGENAME=page_name,
        DX=dx,
        DY=dy,
        VIEWNAME=view_name,
        DESCRIPTION=description,
        ANGLE=angle,
        SELECTIONSCHEME=selection_scheme,
        STYLE=style,
        ITEMLABELING=item_labeling,
        VIEWPOINT=viewpoint,
        ROOTELEMENTS=root_elements,
        SCALESETTING=scale_setting,
        SCALE=scale,
        VIEWTYPE=view_type,
        OBJECTID=object_id
    )
    return manager.execute_action(action)


def export_production_data_ras_center(
    file_name: str = None,
    project_path: str = None,
    database_id: str = None,
    whole_project: bool = None,
    config_scheme: str = None
) -> dict:
    """
    Export the installation spaces of a project in AutomationML format for the
    Rittal RiPanel Processing Center (RAS Center), which drives the machines
    that create openings and cut mounting rails and wiring ducts.
    Action: XAMlExportProductionData2RASCenterAction

    Args:
        file_name: Full target path + file name of the AutomationML export. If
                   empty, EPLAN shows a dialog - always pass it for unattended runs.
        project_path: Project to export; it must already be open in P8. If the
                      path is invalid the current project is used.
        database_id: Database ID of the project to be exported
        whole_project: Use the whole project as the input objects for the export
                       instead of the current selection
        config_scheme: Configuration scheme (optional). Default: most recently
                       used configuration scheme.
    """
    manager, error = _get_connected_manager()
    if error:
        return error

    action = _build_action(
        "XAMlExportProductionData2RASCenterAction",
        ProjectPath=project_path,
        FileName=file_name,
        DatabaseId=database_id,
        WholeProject=whole_project,
        ConfigScheme=config_scheme
    )
    return manager.execute_action(action)


def export_production_data_smart_mounting(
    file_name: str = None,
    project_path: str = None,
    database_id: str = None,
    whole_project: bool = None,
    config_scheme: str = None
) -> dict:
    """
    Export production data of the installation spaces in AutomationML format for
    Rittal Smart Mounting.
    Action: XAMlExportProductionData2SmartMountingAction

    UNVERIFIED PARAMETERS: the official documentation page for this action 404s,
    so the parameter set below is inferred from its sibling AutomationML export
    action XAMlExportProductionData2RASCenterAction and has NOT been verified
    against the docs. Treat the names and casing as a best guess; if a call is
    silently ignored, the parameter names are the first thing to suspect.

    Args:
        file_name: Full target path + file name of the AutomationML export. If
                   empty, EPLAN is expected to show a dialog.
        project_path: Project to export; it must already be open in P8.
        database_id: Database ID of the project to be exported
        whole_project: Use the whole project as the input objects for the export
        config_scheme: Configuration scheme (optional)
    """
    manager, error = _get_connected_manager()
    if error:
        return error

    action = _build_action(
        "XAMlExportProductionData2SmartMountingAction",
        ProjectPath=project_path,
        FileName=file_name,
        DatabaseId=database_id,
        WholeProject=whole_project,
        ConfigScheme=config_scheme
    )
    return manager.execute_action(action)
