using System;
using System.Collections.Generic;
using Eplan.EplApi.ApplicationFramework;
using Eplan.EplApi.DataModel;
using Eplan.EplApi.HEServices;

namespace EplanBridge.Actions
{
    /// <summary>
    /// Shared helpers for reading the currently-open project and its optional
    /// LIMIT / CONTAINS query parameters. Runs on EPLAN's main thread (in-process
    /// add-in), where the live DataModel is legal — this is the whole point.
    /// </summary>
    internal static class Query
    {
        public static Project CurrentProject()
        {
            var project = new SelectionSet().GetCurrentProject(true);
            if (project == null)
                throw new InvalidOperationException("No project is currently open/selected in EPLAN.");
            return project;
        }

        public static int Limit(ActionCallingContext ctx, int fallback = 100)
        {
            string raw = BridgeResult.GetParam(ctx, "LIMIT");
            return int.TryParse(raw, out int n) && n > 0 ? n : fallback;
        }

        public static bool Matches(string value, string contains)
        {
            if (string.IsNullOrEmpty(contains))
                return true;
            return value != null &&
                   value.IndexOf(contains, StringComparison.OrdinalIgnoreCase) >= 0;
        }
    }

    /// <summary>
    /// Live query of the open project's functions (devices) via DMObjectsFinder.
    /// This is the deadlock-free replacement for the search_* actions.
    ///
    /// Call as:  BridgeQueryFunctions /OUT:"...json" [/CONTAINS:text] [/LIMIT:100]
    /// </summary>
    public class BridgeQueryFunctions : IEplAction
    {
        public bool OnRegister(ref string Name, ref int Ordinal)
        {
            Name = "BridgeQueryFunctions";
            Ordinal = 20;
            return true;
        }

        public void GetActionProperties(ref ActionProperties actionProperties) { }

        public bool Execute(ActionCallingContext ctx)
        {
            return BridgeResult.Run(ctx, results =>
            {
                string contains = BridgeResult.GetParam(ctx, "CONTAINS");
                int limit = Query.Limit(ctx);

                var project = Query.CurrentProject();
                var finder = new DMObjectsFinder(project);
                Function[] functions = finder.GetFunctions(new FunctionsFilter());

                var list = new List<Dictionary<string, object>>();
                int total = 0;
                foreach (var fn in functions)
                {
                    string name;
                    try { name = fn.Name; } catch { name = null; }
                    if (!Query.Matches(name, contains))
                        continue;
                    total++;
                    if (list.Count < limit)
                        list.Add(new Dictionary<string, object> { { "name", name ?? "" } });
                }

                results["success"] = true;
                results["matched"] = total;
                results["returned"] = list.Count;
                results["functions"] = list;
            });
        }
    }

    /// <summary>
    /// Live query of the open project's pages via DMObjectsFinder.
    ///
    /// Call as:  BridgeQueryPages /OUT:"...json" [/CONTAINS:text] [/LIMIT:100]
    /// </summary>
    public class BridgeQueryPages : IEplAction
    {
        public bool OnRegister(ref string Name, ref int Ordinal)
        {
            Name = "BridgeQueryPages";
            Ordinal = 20;
            return true;
        }

        public void GetActionProperties(ref ActionProperties actionProperties) { }

        public bool Execute(ActionCallingContext ctx)
        {
            return BridgeResult.Run(ctx, results =>
            {
                string contains = BridgeResult.GetParam(ctx, "CONTAINS");
                int limit = Query.Limit(ctx);

                var project = Query.CurrentProject();
                var finder = new DMObjectsFinder(project);
                Page[] pages = finder.GetPages(new PagesFilter());

                var list = new List<Dictionary<string, object>>();
                int total = 0;
                foreach (var pg in pages)
                {
                    string name;
                    try { name = pg.Name; } catch { name = null; }
                    if (!Query.Matches(name, contains))
                        continue;
                    total++;
                    if (list.Count < limit)
                    {
                        string pageType;
                        try { pageType = pg.PageType.ToString(); } catch { pageType = ""; }
                        list.Add(new Dictionary<string, object>
                        {
                            { "name", name ?? "" },
                            { "pageType", pageType }
                        });
                    }
                }

                results["success"] = true;
                results["matched"] = total;
                results["returned"] = list.Count;
                results["pages"] = list;
            });
        }
    }
}
