using System;
using System.Collections.Generic;
using Eplan.EplApi.ApplicationFramework;
using Eplan.EplApi.DataModel;
using DMProps = Eplan.EplApi.DataModel.Properties;

namespace EplanBridge.Actions
{
    /// <summary>
    /// LIVE EDIT of the open project: set the function text (FUNC_TEXT, the field
    /// shown on the schematic) of functions whose identifying name exactly equals
    /// NAME.
    ///
    /// This is the write-path counterpart to the BridgeQuery* read actions and the
    /// template for any DataModel mutation: the only thing a write needs beyond a
    /// read is a `using (new LockingStep())` block (the project write lock). The
    /// change lands on EPLAN's normal undo stack.
    ///
    /// Returns each modified function's previous text so the edit is reversible.
    /// LIMIT (default 1) caps how many matching functions are modified, so a
    /// mistaken NAME can't mass-edit the project.
    ///
    /// Call as:
    ///   BridgeSetFunctionText /OUT:"...json" /NAME:"+TEST-TEST" /TEXT:"..." [/LIMIT:1]
    /// </summary>
    public class BridgeSetFunctionText : IEplAction
    {
        public bool OnRegister(ref string Name, ref int Ordinal)
        {
            Name = "BridgeSetFunctionText";
            Ordinal = 20;
            return true;
        }

        public void GetActionProperties(ref ActionProperties actionProperties) { }

        public bool Execute(ActionCallingContext ctx)
        {
            return BridgeResult.Run(ctx, results =>
            {
                string targetName = BridgeResult.GetParam(ctx, "NAME");
                string text = BridgeResult.GetParam(ctx, "TEXT") ?? "";
                int limit = Query.Limit(ctx, 1);

                if (string.IsNullOrEmpty(targetName))
                    throw new ArgumentException("NAME parameter is required.");

                var project = Query.CurrentProject();
                var finder = new DMObjectsFinder(project);
                Function[] functions = finder.GetFunctions(new FunctionsFilter());

                var details = new List<Dictionary<string, object>>();
                int matched = 0;

                // The write lock. Everything mutated inside becomes one undo step.
                using (new LockingStep())
                {
                    foreach (var fn in functions)
                    {
                        string name;
                        try { name = fn.Name; } catch { name = null; }
                        if (name != targetName)
                            continue;

                        matched++;
                        if (details.Count >= limit)
                            continue;

                        PropertyValue pv = fn.Properties[DMProps.Function.FUNC_TEXT];
                        // NOTE: FUNC_TEXT is a multi-language property. ToString()
                        // returns EPLAN's internal MultiLangString encoding (language
                        // markers + text), not clean display text. It is a faithful,
                        // opaque snapshot -- fine for detecting "was empty", less so
                        // for round-tripping. A language-aware read/write is a future
                        // refinement if clean per-language text is needed.
                        string previous = pv.IsEmpty ? "" : pv.ToString();
                        fn.Properties[DMProps.Function.FUNC_TEXT].Set(text);

                        details.Add(new Dictionary<string, object>
                        {
                            { "name", name },
                            { "previous", previous },
                            { "new", text }
                        });
                    }
                }

                results["success"] = true;
                results["matched"] = matched;
                results["modified"] = details.Count;
                results["details"] = details;
            });
        }
    }
}
