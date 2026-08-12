using System;
using System.Collections.Generic;
using System.IO;
using System.Text.Json;
using System.Text.Encodings.Web;
using Eplan.EplApi.ApplicationFramework;

namespace EplanBridge
{
    /// <summary>
    /// Shared helpers for the bridge actions: read the caller-supplied output path
    /// and always write a JSON result file (on success OR caught exception), so the
    /// Python side never has to distinguish a failure from a hang. Uses
    /// System.Text.Json from the net8 runtime — no EPLAN/Newtonsoft dependency.
    /// </summary>
    internal static class BridgeResult
    {
        private static readonly JsonSerializerOptions Options =
            new JsonSerializerOptions
            {
                WriteIndented = true,
                // EPLAN identifying names contain +, -, &, /, <, > etc. The default
                // encoder escapes these to \uXXXX; relax it so the JSON is readable
                // (still valid, still safe for a file we control and re-parse).
                Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
            };

        /// <summary>Read a string parameter from the action calling context.</summary>
        public static string GetParam(ActionCallingContext ctx, string key)
        {
            string val = "";
            ctx.GetParameter(key, ref val);
            return val ?? "";
        }

        /// <summary>Write the payload as JSON to <paramref name="outPath"/>.</summary>
        public static void Write(string outPath, IDictionary<string, object> payload)
        {
            if (string.IsNullOrEmpty(outPath))
                return;
            File.WriteAllText(outPath, JsonSerializer.Serialize(payload, Options));
        }

        /// <summary>
        /// Run <paramref name="body"/>, writing a success/error result file either way.
        /// The body populates the results dictionary; exceptions are captured into it.
        /// </summary>
        public static bool Run(ActionCallingContext ctx, Action<Dictionary<string, object>> body)
        {
            string outPath = GetParam(ctx, "OUT");
            var results = new Dictionary<string, object>();
            try
            {
                body(results);
                if (!results.ContainsKey("success"))
                    results["success"] = true;
            }
            catch (Exception ex)
            {
                results["success"] = false;
                results["error"] = ex.Message;
                results["type"] = ex.GetType().FullName;
            }
            Write(outPath, results);
            return true;
        }
    }
}
