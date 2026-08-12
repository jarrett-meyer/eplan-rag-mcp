using Eplan.EplApi.ApplicationFramework;

namespace EplanBridge
{
    /// <summary>
    /// Add-in lifecycle entry point. EPLAN instantiates this when the assembly is
    /// loaded (via EplApiModuleAction /register or the Utilities > API > Add-ins
    /// dialog). Every IEplAction in this assembly is auto-registered as a callable
    /// EPLAN action once the add-in is initialized.
    /// </summary>
    public class BridgeAddIn : IEplAddIn, IEplAddInShadowCopy
    {
        private string m_originalAssemblyPath;

        // IEplAddInShadowCopy: receives the original assembly path when EPLAN loads
        // a shadow copy. NOTE: implementing this interface does NOT by itself make
        // EPLAN shadow-copy the assembly (that depends on EPLAN's add-in config); a
        // loaded DLL is still file-locked until EPLAN restarts. We implement it so
        // the add-in behaves correctly under configs where shadow-copy IS enabled.
        public void OnBeforeInit(string strOriginalAssemblyPath)
        {
            m_originalAssemblyPath = strOriginalAssemblyPath;
        }

        public string GetOriginalAssemblyPath() => m_originalAssemblyPath;

        public bool OnRegister(ref bool bLoadOnStart)
        {
            bLoadOnStart = true; // load automatically on EPLAN startup
            return true;
        }

        public bool OnUnregister() => true;

        public bool OnInit() => true;

        public bool OnInitGui() => true;

        public bool OnExit() => true;
    }
}
