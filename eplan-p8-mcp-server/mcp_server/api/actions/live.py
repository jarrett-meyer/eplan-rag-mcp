"""
Live DataModel actions - read and edit the project currently open in EPLAN.

The action-based tools in this package drive EPLAN, but they cannot enumerate
or mutate the object model of the loaded project: `using Eplan.EplApi.DataModel;`
does not compile inside EPLAN's script engine (CS0234 - the engine compiles
against a fixed assembly set). The technique that does work, documented in
claude-skills/eplan-development (references/api-data-access.md and
references/e3d-installation-spaces.md), is runtime reflection over the object
model, with a `LockingStep` held for the duration (without it every project
access throws NoLockingStepException) and `SelectionSet.GetCurrentProject(false)`
to reach the project without ProjectManager.

Types are resolved by scanning AppDomain.CurrentDomain.GetAssemblies() rather
than by Assembly.Load of a hardcoded name, because the assembly that carries
the managed object model was RENAMED between releases:

    EPLAN 2025 (.NET Framework)  Eplan.EplApi.DataModelu / ...HEServicesu
    EPLAN 2027 (.NET 8/coreclr)  Eplan.EplApi.DataModelNetu / ...HEServicesNetu

On 2027 both names are present in the process, but the un-suffixed
`Eplan.EplApi.DataModelu` is the mixed-mode NATIVE twin: Assembly.Load of it
throws BadImageFormatException (0x8007000B, "attempt to load a program with an
incorrect format"), so the documented Assembly.Load("Eplan.EplApi.DataModelu")
recipe fails outright there. EPLAN has the managed assembly loaded already in
either case, so searching the loaded set for the type is both version-proof and
cheaper than a load. Verified on EPLAN 2027 (LockingStep resolved from
Eplan.EplApi.DataModelNetu).

This module applies that technique to the operations the action surface has no
answer for:

    live_query_functions    enumerate the open project's functions (devices)
    live_query_pages        enumerate the open project's pages
    live_set_function_text  set FUNC_TEXT on functions matched by name (write)
    live_set_connection_designations
                            set a function's connection point numbers (write)

Notes on the generated C#, all of them load-bearing (see the pitfalls section
of e3d-installation-spaces.md):
- No `using Eplan.EplApi.DataModel;` / `...HEServices;` - that is the CS0234 trap.
- No index initializers (`new Dictionary<..> { ["k"] = v }`): the script engine
  rejects them with CS1525. Dictionary entries are separate statements.
- Every value interpolated into the script goes through cs_escape.
- Reflective calls are wrapped and InnerException chains flattened, because
  EPLAN wraps the real failure in TargetInvocationException.
- The LockingStep is constructed before the project is touched and disposed in
  a finally block.
"""

import uuid

from ._base import cs_escape
from .scripted import _execute_script

# EPLAN's dialogs join a function's connection point designations with this
# separator ("Y09¶Y10"); the API stores one designation per index, so it is
# only ever a display convention here - never write it into a single index.
PILCROW = "\u00b6"


_HEADER = '''using System;
using System.IO;
using System.Text;
using System.Reflection;
using System.Collections;
using System.Collections.Generic;
using Eplan.EplApi.Base;
using Eplan.EplApi.Scripting;

public class '''


_HELPERS = '''
{
    static string Flatten(Exception ex)
    {
        StringBuilder sb = new StringBuilder();
        Exception e = ex;
        int depth = 0;
        while (e != null && depth < 6)
        {
            if (sb.Length > 0) sb.Append(" <- ");
            sb.Append(e.GetType().Name);
            sb.Append(": ");
            sb.Append(e.Message);
            e = e.InnerException;
            depth++;
        }
        return sb.ToString();
    }

    static bool Matches(string value, string contains)
    {
        if (string.IsNullOrEmpty(contains)) return true;
        if (value == null) return false;
        return value.IndexOf(contains, StringComparison.OrdinalIgnoreCase) >= 0;
    }

    // Type.GetProperty(name) is NOT safe on EPLAN types - it throws
    // AmbiguousMatchException for two independent reasons:
    //  1. A derived class re-declares a member with a narrower return type
    //     (Function.Properties returns FunctionPropertyList, the base returns
    //     PropertyList), so the name exists at two levels of the hierarchy.
    //  2. A property list declares BOTH a plain and an indexed form of the same
    //     property on the SAME type (FUNC_TEXT and FUNC_TEXT[int]).
    // So: walk from the most-derived type up, DeclaredOnly (fixes 1), and ask
    // for the non-indexed declaration via Type.EmptyTypes (fixes 2) - together
    // that is what C# member lookup would have picked.
    static PropertyInfo GetPropInfo(Type t, string name)
    {
        BindingFlags bf = BindingFlags.Public | BindingFlags.Instance | BindingFlags.DeclaredOnly;
        Type cur = t;
        while (cur != null)
        {
            PropertyInfo pi = null;
            try { pi = cur.GetProperty(name, bf, null, null, Type.EmptyTypes, null); }
            catch { }
            if (pi != null) return pi;
            // No non-indexed form here; accept a unique declaration of any shape.
            try { pi = cur.GetProperty(name, bf); }
            catch { pi = null; }
            if (pi != null) return pi;
            cur = cur.BaseType;
        }
        return null;
    }

    // The indexed sibling of GetPropInfo. Some property-list members are
    // declared BOTH plain and as [int] - FUNC_CONNECTIONDESIGNATION is one -
    // and there the indexed form is the useful one: it addresses a single
    // connection point. GetPropInfo deliberately prefers the non-indexed
    // declaration, so an indexed property needs this explicit lookup.
    static PropertyInfo GetPropInfoIdx(Type t, string name)
    {
        BindingFlags bf = BindingFlags.Public | BindingFlags.Instance | BindingFlags.DeclaredOnly;
        Type cur = t;
        while (cur != null)
        {
            PropertyInfo pi = null;
            try { pi = cur.GetProperty(name, bf, null, null, new Type[] { typeof(int) }, null); }
            catch { }
            if (pi != null) return pi;
            cur = cur.BaseType;
        }
        return null;
    }

    static string PropText(object o, string prop)
    {
        try
        {
            PropertyInfo pi = GetPropInfo(o.GetType(), prop);
            if (pi == null) return null;
            object v = pi.GetValue(o, null);
            if (v == null) return null;
            return v.ToString();
        }
        catch { return null; }
    }

    // Resolve an EPLAN type from the assemblies already loaded in the process.
    // Deliberately NOT Assembly.Load("Eplan.EplApi.DataModelu"): on 2027 that
    // name belongs to the mixed-mode native twin and throws
    // BadImageFormatException, while the managed object model lives in
    // Eplan.EplApi.DataModelNetu. Scanning covers both naming schemes.
    static Assembly[] _asms;
    static Type FindType(string fullName)
    {
        if (_asms == null) _asms = AppDomain.CurrentDomain.GetAssemblies();
        foreach (Assembly a in _asms)
        {
            try
            {
                Type t = a.GetType(fullName);
                if (t != null) return t;
            }
            catch { }
        }
        // Not loaded yet: try the managed assembly names, newest scheme first.
        string[] candidates = new string[] {
            "Eplan.EplApi.DataModelNetu", "Eplan.EplApi.HEServicesNetu",
            "Eplan.EplApi.DataModelu", "Eplan.EplApi.HEServicesu" };
        foreach (string c in candidates)
        {
            try
            {
                Assembly a = Assembly.Load(c);
                if (a == null) continue;
                Type t = a.GetType(fullName);
                if (t != null)
                {
                    _asms = AppDomain.CurrentDomain.GetAssemblies();
                    return t;
                }
            }
            catch { }
        }
        throw new Exception("Could not resolve type " + fullName +
            " in any loaded EPLAN assembly.");
    }

    [Start]
    public void Run()
    {
        Dictionary<string, object> results = new Dictionary<string, object>();
        Type lsType = null;
        object lockStep = null;
        try
        {
            // Required before any project access, read or write.
            lsType = FindType("Eplan.EplApi.DataModel.LockingStep");
            lockStep = Activator.CreateInstance(lsType);

            // Current project without ProjectManager.
            Type ssType = FindType("Eplan.EplApi.HEServices.SelectionSet");
            object ss = Activator.CreateInstance(ssType);
            MethodInfo getCur = ssType.GetMethod("GetCurrentProject", new Type[] { typeof(bool) });
            object project = getCur.Invoke(ss, new object[] { false });
            if (project == null)
                throw new Exception("No project is currently open/selected in EPLAN.");

            results["project"] = PropText(project, "ProjectName");

'''


_FOOTER = '''
        }
        catch (Exception ex)
        {
            results["success"] = false;
            results["error"] = Flatten(ex);
        }
        finally
        {
            if (lockStep != null)
            {
                try { lsType.GetMethod("Dispose").Invoke(lockStep, null); }
                catch { }
            }
        }

        string json = Newtonsoft.Json.JsonConvert.SerializeObject(results);
        File.WriteAllText(@"{{RESULT_PATH}}", json);
    }
}
'''


def _script(class_name, body):
    """
    Wrap a reflection body in the standard script scaffold.

    The body may use: project (the open project) and results (the dict
    serialized to {{RESULT_PATH}}). Helpers FindType, Flatten, Matches and
    PropText are available as static methods.
    """
    return _HEADER + class_name + _HELPERS + body + _FOOTER


# Shared body fragment: build a DMObjectsFinder over the open project.
_FINDER = '''            Type finderType = FindType("Eplan.EplApi.DataModel.DMObjectsFinder");
            object finder = Activator.CreateInstance(finderType, new object[] { project });
'''


def live_query_functions(contains: str = None, limit: int = 100,
                         timeout_seconds: float = 60.0) -> dict:
    """
    List the functions (devices) of the project currently open in EPLAN.

    Reads the live object model, so it sees the project as it stands in the
    session - including unsaved changes - which the file-based export_functions
    and the action-based search_devices do not.

    Args:
        contains: Optional case-insensitive substring filter on the function's
            identifying name (e.g. "-K" for contactors). Omit for all.
        limit: Max functions to return (default 100). "matched" in the result
            reports how many passed the filter before this cap.
        timeout_seconds: Max seconds to wait for the script (default 60).
            A full walk of a large project can exceed the standard 30s.

    Returns:
        dict with "functions" (list of {"name": ...}), "matched", "returned".
    """
    # limit is interpolated into the C# source outside any string literal, so
    # it must be a real integer - anything else would be code injection.
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        return {"success": False, "error": f"Invalid limit: {limit!r}. Must be an integer."}

    body = _FINDER + '''            Type filterType = FindType("Eplan.EplApi.DataModel.FunctionsFilter");
            object filter = Activator.CreateInstance(filterType);
            MethodInfo getFunctions = finderType.GetMethod("GetFunctions", new Type[] { filterType });
            IEnumerable found = (IEnumerable)getFunctions.Invoke(finder, new object[] { filter });

            List<Dictionary<string, object>> list = new List<Dictionary<string, object>>();
            int matched = 0;
            foreach (object fn in found)
            {
                if (fn == null) continue;
                string name = PropText(fn, "Name");
                if (!Matches(name, "''' + cs_escape(contains or "") + '''")) continue;
                matched++;
                if (list.Count < ''' + str(int(limit)) + ''')
                {
                    Dictionary<string, object> d = new Dictionary<string, object>();
                    d["name"] = name == null ? "" : name;
                    list.Add(d);
                }
            }

            results["success"] = true;
            results["matched"] = matched;
            results["returned"] = list.Count;
            results["functions"] = list;
'''
    return _execute_script(
        _script("LiveQueryFunctions_" + uuid.uuid4().hex[:6], body),
        timeout=timeout_seconds,
    )


def live_query_pages(contains: str = None, limit: int = 100,
                     timeout_seconds: float = 60.0) -> dict:
    """
    List the pages of the project currently open in EPLAN.

    Reads the live object model, so unlike export_pages it reflects unsaved
    session state and needs no export file.

    Args:
        contains: Optional case-insensitive substring filter on the page name.
        limit: Max pages to return (default 100).
        timeout_seconds: Max seconds to wait for the script (default 60).

    Returns:
        dict with "pages" (list of {"name": ..., "pageType": ...}),
        "matched", "returned".
    """
    # limit is interpolated into the C# source outside any string literal, so
    # it must be a real integer - anything else would be code injection.
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        return {"success": False, "error": f"Invalid limit: {limit!r}. Must be an integer."}

    body = _FINDER + '''            Type filterType = FindType("Eplan.EplApi.DataModel.PagesFilter");
            object filter = Activator.CreateInstance(filterType);
            MethodInfo getPages = finderType.GetMethod("GetPages", new Type[] { filterType });
            IEnumerable found = (IEnumerable)getPages.Invoke(finder, new object[] { filter });

            List<Dictionary<string, object>> list = new List<Dictionary<string, object>>();
            int matched = 0;
            foreach (object pg in found)
            {
                if (pg == null) continue;
                string name = PropText(pg, "Name");
                if (!Matches(name, "''' + cs_escape(contains or "") + '''")) continue;
                matched++;
                if (list.Count < ''' + str(int(limit)) + ''')
                {
                    Dictionary<string, object> d = new Dictionary<string, object>();
                    d["name"] = name == null ? "" : name;
                    string pt = PropText(pg, "PageType");
                    d["pageType"] = pt == null ? "" : pt;
                    list.Add(d);
                }
            }

            results["success"] = true;
            results["matched"] = matched;
            results["returned"] = list.Count;
            results["pages"] = list;
'''
    return _execute_script(
        _script("LiveQueryPages_" + uuid.uuid4().hex[:6], body),
        timeout=timeout_seconds,
    )


def live_set_function_text(name: str, text: str, limit: int = 1,
                           timeout_seconds: float = 60.0) -> dict:
    """
    Set the function text (FUNC_TEXT) of functions whose identifying name
    exactly equals `name`, in the project currently open in EPLAN.

    This is a WRITE to the live object model. The change lands on EPLAN's
    normal undo stack and is not persisted until the project is saved. Each
    modified function's previous text is returned so the edit is reversible.

    Args:
        name: Exact identifying name of the target function, e.g. "+TEST-K1".
            Matching is exact, not substring - use live_query_functions first
            to find the name you want.
        text: New function text.
        limit: Max functions to modify (default 1). "matched" reports how many
            functions carry the name; anything beyond `limit` is left alone, so
            a mistaken name cannot mass-edit the project.
        timeout_seconds: Max seconds to wait for the script (default 60).

    Returns:
        dict with "matched", "modified", and "details" (per-function
        {"name", "previous", "new"}).

    Note:
        FUNC_TEXT (property 20011) is backed by MultiLangString. "previous" is
        EPLAN's internal MultiLangString encoding (language markers + text), not
        clean display text - a faithful but opaque snapshot, good for detecting
        "was empty", less so for reading. Beware round-tripping it: writing a
        plain string stores it under the "no language set" key, so a value read
        back can carry a literal "??_??@" prefix. Restoring an empty value is
        clean; restoring a previously-set one may need that prefix stripped.
    """
    # limit is interpolated into the C# source outside any string literal, so
    # it must be a real integer - anything else would be code injection.
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        return {"success": False, "error": f"Invalid limit: {limit!r}. Must be an integer."}

    if not name:
        return {"success": False, "message": "name is required"}

    body = _FINDER + '''            Type filterType = FindType("Eplan.EplApi.DataModel.FunctionsFilter");
            object filter = Activator.CreateInstance(filterType);
            MethodInfo getFunctions = finderType.GetMethod("GetFunctions", new Type[] { filterType });
            IEnumerable found = (IEnumerable)getFunctions.Invoke(finder, new object[] { filter });

            string targetName = "''' + cs_escape(name) + '''";
            string newText = "''' + cs_escape(text or "") + '''";
            List<Dictionary<string, object>> details = new List<Dictionary<string, object>>();
            int matched = 0;

            foreach (object fn in found)
            {
                if (fn == null) continue;
                string fname = PropText(fn, "Name");
                if (fname != targetName) continue;

                matched++;
                if (details.Count >= ''' + str(int(limit)) + ''') continue;

                // fn.Properties is a FunctionPropertyList, which exposes
                // FUNC_TEXT (function text, property 20011) directly as a
                // PropertyValue. Per the PropertyValue.Set docs, a PropertyValue
                // "accrued from property list or from StorableObject" writes
                // through to the original location on Set() - so no indexer and
                // no PropertyIdentifier plumbing is needed.
                PropertyInfo propsProp = GetPropInfo(fn.GetType(), "Properties");
                if (propsProp == null)
                    throw new Exception("Function has no Properties member.");
                object props = propsProp.GetValue(fn, null);
                PropertyInfo ftProp = GetPropInfo(props.GetType(), "FUNC_TEXT");
                if (ftProp == null)
                    throw new Exception("Property list " + props.GetType().FullName +
                        " has no FUNC_TEXT property.");
                object pv = ftProp.GetValue(props, null);

                string previous = "";
                try
                {
                    PropertyInfo isEmpty = GetPropInfo(pv.GetType(), "IsEmpty");
                    bool empty = isEmpty != null && (bool)isEmpty.GetValue(pv, null);
                    if (!empty) previous = pv.ToString();
                }
                catch { previous = ""; }

                MethodInfo setter = pv.GetType().GetMethod("Set", new Type[] { typeof(string) });
                if (setter == null)
                    throw new Exception("PropertyValue has no Set(string) overload.");
                setter.Invoke(pv, new object[] { newText });

                Dictionary<string, object> d = new Dictionary<string, object>();
                d["name"] = fname;
                d["previous"] = previous;
                d["new"] = newText;
                details.Add(d);
            }

            results["success"] = true;
            results["matched"] = matched;
            results["modified"] = details.Count;
            results["details"] = details;
'''
    return _execute_script(
        _script("LiveSetFunctionText_" + uuid.uuid4().hex[:6], body),
        timeout=timeout_seconds,
    )


def live_set_connection_designations(name: str, designations: list, limit: int = 1,
                                     timeout_seconds: float = 60.0) -> dict:
    """
    Set the connection point designations of a function in the project
    currently open in EPLAN.

    These are the connection point numbers shown on the multi-line schematic
    (e.g. "Y09" and "Y10" on a two-point function). EPLAN's dialogs show the
    whole set joined with a pilcrow separator - "Y09<pilcrow>Y10" - but the API
    stores one designation PER INDEX, so pass them as a list and this tool
    writes index 1, 2, ... in order. Do NOT pass a pre-joined string; it would
    land entirely in index 1.

    This is a WRITE to the live object model. The change lands on EPLAN's
    normal undo stack and is not persisted until the project is saved. The
    previous designations are returned so the edit is reversible.

    Note this changes the connection point NUMBERS, not their geometry.
    Connection point position and direction (PinBase.Location / .Direction) are
    read-only in the object model; moving a connection point means authoring
    the symbol itself, or swapping the function's SymbolVariant.

    Args:
        name: Exact identifying name of the target function, e.g. "+-TEST".
            Matching is exact, not substring - use live_query_functions first.
        designations: Designations in connection point order, e.g.
            ["Y11", "Y12"]. Written to indices 1..N. Passing fewer than the
            function has leaves the remaining points untouched.
        limit: Max functions to modify (default 1). "matched" reports how many
            functions carry the name; anything beyond `limit` is left alone.
        timeout_seconds: Max seconds to wait for the script (default 60).

    Returns:
        dict with "matched", "modified", and "details" (per-function
        {"name", "page", "previous", "new"}, previous/new being lists in
        index order).
    """
    if not name:
        return {"success": False, "message": "name is required"}

    if isinstance(designations, str) or not isinstance(designations, (list, tuple)):
        return {"success": False,
                "error": "designations must be a list of strings, one per connection "
                         "point (e.g. [\"Y11\", \"Y12\"]). Do not pass a single joined string."}
    designations = list(designations)
    if not designations:
        return {"success": False, "error": "designations must not be empty."}
    if any(d is None for d in designations):
        return {"success": False, "error": "designations must not contain None."}
    designations = [str(d) for d in designations]
    if any(PILCROW in d for d in designations):
        return {"success": False,
                "error": "a single designation must not contain the pilcrow separator - "
                         "pass one list element per connection point."}

    # limit is interpolated into the C# source outside any string literal, so
    # it must be a real integer - anything else would be code injection.
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        return {"success": False, "error": f"Invalid limit: {limit!r}. Must be an integer."}

    cs_list = ", ".join('"' + cs_escape(d) + '"' for d in designations)

    body = _FINDER + '''            Type filterType = FindType("Eplan.EplApi.DataModel.FunctionsFilter");
            object filter = Activator.CreateInstance(filterType);
            MethodInfo getFunctions = finderType.GetMethod("GetFunctions", new Type[] { filterType });
            IEnumerable found = (IEnumerable)getFunctions.Invoke(finder, new object[] { filter });

            string targetName = "''' + cs_escape(name) + '''";
            string[] wanted = new string[] { ''' + cs_list + ''' };
            List<Dictionary<string, object>> details = new List<Dictionary<string, object>>();
            int matched = 0;

            foreach (object fn in found)
            {
                if (fn == null) continue;
                string fname = PropText(fn, "Name");
                if (fname != targetName) continue;

                matched++;
                if (details.Count >= ''' + str(int(limit)) + ''') continue;

                PropertyInfo propsProp = GetPropInfo(fn.GetType(), "Properties");
                if (propsProp == null)
                    throw new Exception("Function has no Properties member.");
                object props = propsProp.GetValue(fn, null);

                // Connection point designation is property 20022, declared both
                // plain and indexed; the INDEXED form addresses one connection
                // point, so it is the one to use here.
                PropertyInfo cdIdx = GetPropInfoIdx(props.GetType(), "FUNC_CONNECTIONDESIGNATION");
                if (cdIdx == null)
                    throw new Exception("Property list " + props.GetType().FullName +
                        " has no indexed FUNC_CONNECTIONDESIGNATION property.");

                List<string> before = new List<string>();
                List<string> after = new List<string>();

                for (int i = 0; i < wanted.Length; i++)
                {
                    int slot = i + 1;

                    object pv = cdIdx.GetValue(props, new object[] { slot });
                    PropertyInfo ie = GetPropInfo(pv.GetType(), "IsEmpty");
                    bool empty = ie != null && (bool)ie.GetValue(pv, null);
                    before.Add(empty ? "" : pv.ToString());

                    MethodInfo setter = pv.GetType().GetMethod("Set", new Type[] { typeof(string) });
                    if (setter == null)
                        throw new Exception("PropertyValue has no Set(string) overload.");
                    setter.Invoke(pv, new object[] { wanted[i] });

                    // Read back through a FRESH fetch of the same slot, so the
                    // reported value proves the write reached the object rather
                    // than a local PropertyValue wrapper.
                    object pv2 = cdIdx.GetValue(props, new object[] { slot });
                    PropertyInfo ie2 = GetPropInfo(pv2.GetType(), "IsEmpty");
                    bool empty2 = ie2 != null && (bool)ie2.GetValue(pv2, null);
                    after.Add(empty2 ? "" : pv2.ToString());
                }

                string pageName = "";
                PropertyInfo pgProp = GetPropInfo(fn.GetType(), "Page");
                if (pgProp != null)
                {
                    object pg = pgProp.GetValue(fn, null);
                    if (pg != null)
                    {
                        string pn = PropText(pg, "Name");
                        if (pn != null) pageName = pn;
                    }
                }

                Dictionary<string, object> d = new Dictionary<string, object>();
                d["name"] = fname;
                d["page"] = pageName;
                d["previous"] = before;
                d["new"] = after;
                details.Add(d);
            }

            results["success"] = true;
            results["matched"] = matched;
            results["modified"] = details.Count;
            results["details"] = details;
'''
    return _execute_script(
        _script("LiveSetConnDesignations_" + uuid.uuid4().hex[:6], body),
        timeout=timeout_seconds,
    )
