"""
Edit and navigation actions.
"""

from ._base import _get_connected_manager, _build_action


def edit_open_page(
    page_name: str,
    project_name: str = None,
    x: float = None,
    y: float = None
) -> dict:
    """
    Open a specific page in the editor.
    Action: edit

    Args:
        page_name: Page name to open
        project_name: Project path (optional)
        x: X coordinate to position cursor
        y: Y coordinate to position cursor
    """
    manager, error = _get_connected_manager()
    if error:
        return error

    action = _build_action(
        "edit",
        PROJECTNAME=project_name,
        PAGENAME=page_name,
        X=x,
        Y=y
    )
    return manager.execute_action(action)


def edit_goto_device(
    device_name: str,
    project_name: str = None
) -> dict:
    """
    Navigate to a device in the project.
    Action: edit
    """
    manager, error = _get_connected_manager()
    if error:
        return error

    action = _build_action(
        "edit",
        PROJECTNAME=project_name,
        DEVICENAME=device_name
    )
    return manager.execute_action(action)


def edit_open_layout_space(
    installation_space: str,
    project_name: str = None
) -> dict:
    """
    Open a layout/installation space.
    Action: edit
    """
    manager, error = _get_connected_manager()
    if error:
        return error

    action = _build_action(
        "edit",
        PROJECTNAME=project_name,
        INSTALLATIONSPACE=installation_space
    )
    return manager.execute_action(action)


def close_pages() -> dict:
    """
    Close all selected pages in the editor.
    Action: XGedClosePage
    """
    manager, error = _get_connected_manager()
    if error:
        return error

    return manager.execute_action("XGedClosePage")


def redraw_ged() -> dict:
    """
    Redraw the graphical editor.
    Action: gedRedraw

    Note: This action only works interactively. Under QuietMode (which this
    server always forces) the GED is not redrawn and the action returns FALSE.
    """
    manager, error = _get_connected_manager()
    if error:
        return error

    return manager.execute_action("gedRedraw")


def get_selected_pages() -> dict:
    """
    Get selected pages.
    Action: selectionset

    The result is returned in the calling context parameter "PAGES"
    (page names separated by ";", e.g. "=AP+ST1/1;=AP+ST1/2").
    """
    manager, error = _get_connected_manager()
    if error:
        return error

    action = _build_action(
        "selectionset",
        TYPE="PAGES"
    )
    return manager.execute_action(action)


def preview_page(
    page_name: str = None,
    project_name: str = None,
    macro_name: str = None,
    open: bool = True
) -> dict:
    """
    Open or close the preview of a project page or macro.
    Action: XSDPreviewAction

    Args:
        page_name: Page name to preview (optional)
        project_name: Project path (optional)
        macro_name: Full path to a window/page macro (optional, with extension)
        open: True to open the preview, False to close it (maps to SHOW=1/0)

    Note: If both page_name and macro_name are empty, all project pages
    are shown in the preview.
    """
    manager, error = _get_connected_manager()
    if error:
        return error

    action = _build_action(
        "XSDPreviewAction",
        PROJECTNAME=project_name,
        PAGENAME=page_name,
        MACRONAME=macro_name,
        SHOW="1" if open else "0"
    )
    return manager.execute_action(action)


def navigate_to_eec(object_id: str) -> dict:
    """
    Navigate to object in EPLAN Engineering Configuration.
    Action: navigateToEEC

    Args:
        object_id: Object ID in the EEC to navigate to (parameter EECOBJECTID).
    """
    manager, error = _get_connected_manager()
    if error:
        return error

    action = _build_action(
        "navigateToEEC",
        EECOBJECTID=object_id
    )
    return manager.execute_action(action)


def navigator_select(project: str, pages: list = None, open_page: bool = True,
                     timeout_seconds: float = 60.0) -> dict:
    """
    Focus an OPEN project in the page navigator and select pages there - the
    click a user would make. Selection-driven tools (eplan_set_page_property,
    eplan_close_project, eplan_get_selected_pages, ...) then act on that
    project instead of whatever the user last clicked. eplan_open_project
    cannot do this: it refuses a project that is already open.

    With a page of ANOTHER project open in the editor, the navigator selection
    alone does not move EPLAN's focus; open_page=True (default) then opens the
    first requested page and tries again. Pass False to never change the open
    page (the result then reports verified=False instead).

    Args:
        project: Name, full name or .elk path of an open project.
        pages: Page names to select (as shown in the navigator). Omit to only
            focus the project.
        open_page: Allow opening the first page when needed (see above).
        timeout_seconds: Max seconds to wait for the script (default 60).

    Returns:
        {"success", "project", "project_selected", "unresolved_pages",
         "opened_page", "focused_project", "selected_pages", "verified"}.
        verified is True only when EPLAN's own selection read-back shows the
        project (no pages given) or every requested page.
    """
    from ._base import cs_escape
    from .live import _execute_script, _script
    import uuid

    names = [str(p) for p in (pages or [])]
    want = "new string[] { " + ", ".join('"' + cs_escape(n) + '"' for n in names) + " }"
    body = '''            object target = PickProject(project, "''' + cs_escape(project or "") + '''");
            string targetName = PropText(target, "ProjectFullName");
            results["project"] = targetName;
            string[] want = ''' + want + ''';
            bool allowOpen = ''' + ("true" if open_page else "false") + ''';

            Type editType = FindType("Eplan.EplApi.HEServices.Edit");
            object edit = Activator.CreateInstance(editType);
            Type ssType = FindType("Eplan.EplApi.HEServices.SelectionSet");
            bool projectSelected = false;
            try
            {
                projectSelected = (bool)Call(RequireMethod(editType, "SelectProjectInPagesNavigator",
                    new string[] { "Project" }, false), edit, new object[] { target });
            }
            catch (Exception ex) { results["project_select_err"] = Flatten(ex); }
            results["project_selected"] = projectSelected;

            Type finderType = FindType("Eplan.EplApi.DataModel.DMObjectsFinder");
            object finder = Activator.CreateInstance(finderType, new object[] { target });
            Type pfType = FindType("Eplan.EplApi.DataModel.PagesFilter");
            IEnumerable allPages = (IEnumerable)Call(RequireMethod(finderType, "GetPages",
                new string[] { pfType.Name }, false), finder, new object[] { Activator.CreateInstance(pfType) });
            List<object> hits = new List<object>();
            List<string> unresolved = new List<string>();
            foreach (string w in want)
            {
                object match = null;
                foreach (object pg in allPages)
                {
                    if (string.Equals(PropText(pg, "Name"), w, StringComparison.OrdinalIgnoreCase) ||
                        string.Equals(PropText(pg, "IdentifyingName"), w, StringComparison.OrdinalIgnoreCase))
                    { match = pg; break; }
                }
                if (match == null) unresolved.Add(w); else hits.Add(match);
            }
            results["unresolved_pages"] = unresolved;

            Array arr = Array.CreateInstance(FindType("Eplan.EplApi.DataModel.StorableObject"), hits.Count);
            for (int i = 0; i < hits.Count; i++) arr.SetValue(hits[i], i);
            Action sync = () =>
            {
                try
                {
                    Call(RequireMethod(editType, "SynchronizeObjectsToNavigators",
                        new string[] { "StorableObject[]" }, false), edit, new object[] { arr });
                }
                catch (Exception ex) { results["sync_err"] = Flatten(ex); }
            };
            Func<List<string>> selected = () =>
            {
                List<string> s = new List<string>();
                object ss = Activator.CreateInstance(ssType);
                IEnumerable sp = Call(RequireMethod(ssType, "GetSelectedPages", new string[] { }, false),
                    ss, new object[] { }) as IEnumerable;
                if (sp != null) foreach (object p in sp) s.Add(PropText(p, "Name"));
                return s;
            };
            Func<string> focused = () =>
            {
                object ss = Activator.CreateInstance(ssType);
                object cur = Call(RequireMethod(ssType, "GetCurrentProject", new string[] { "Boolean" }, false),
                    ss, new object[] { false });
                return cur == null ? null : PropText(cur, "ProjectFullName");
            };
            Func<bool> verify = () =>
            {
                if (want.Length == 0) return string.Equals(focused(), targetName, StringComparison.OrdinalIgnoreCase);
                List<string> s = selected();
                foreach (object h in hits)
                {
                    bool found = false;
                    foreach (string n in s) if (string.Equals(n, PropText(h, "Name"), StringComparison.OrdinalIgnoreCase)) found = true;
                    if (!found) return false;
                }
                return true;
            };

            if (hits.Count > 0) sync();
            bool ok = verify();
            results["opened_page"] = null;
            if (!ok && allowOpen && hits.Count > 0)
            {
                try
                {
                    Call(RequireMethod(editType, "OpenPageWithName", new string[] { "String", "String" }, false),
                        edit, new object[] { PropText(target, "ProjectLinkFilePath"), PropText(hits[0], "Name") });
                    results["opened_page"] = PropText(hits[0], "Name");
                }
                catch (Exception ex) { results["open_err"] = Flatten(ex); }
                sync();
                ok = verify();
            }

            results["focused_project"] = focused();
            results["selected_pages"] = selected();
            results["verified"] = ok && unresolved.Count == 0;
            results["success"] = true;
'''
    return _execute_script(
        _script("NavigatorSelect_" + uuid.uuid4().hex[:6], body),
        timeout=timeout_seconds,
    )
