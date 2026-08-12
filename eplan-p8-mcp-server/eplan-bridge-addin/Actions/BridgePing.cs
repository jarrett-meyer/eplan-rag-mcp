using System.Collections.Generic;
using Eplan.EplApi.ApplicationFramework;

namespace EplanBridge.Actions
{
    /// <summary>
    /// Minimal liveness action with no DataModel access. Proves the whole loop:
    /// compile -> load add-in -> dispatch action by name -> result file written.
    /// If this returns and the Python side reads the file, the in-process
    /// architecture is working and the RegisterScript/ExecuteScript deadlock is
    /// bypassed.
    ///
    /// Call as:  BridgePing /OUT:"C:\...\result.json"
    /// </summary>
    public class BridgePing : IEplAction
    {
        public bool OnRegister(ref string Name, ref int Ordinal)
        {
            Name = "BridgePing";
            Ordinal = 20;
            return true;
        }

        public void GetActionProperties(ref ActionProperties actionProperties)
        {
        }

        public bool Execute(ActionCallingContext ctx)
        {
            return BridgeResult.Run(ctx, results =>
            {
                results["pong"] = true;
            });
        }
    }
}
