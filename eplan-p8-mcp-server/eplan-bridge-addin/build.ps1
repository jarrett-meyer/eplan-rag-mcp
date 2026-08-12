<#
.SYNOPSIS
    Builds the EPLAN bridge add-in (EplanBridge.dll).

.DESCRIPTION
    Compiles the in-process EPLAN add-in against the EPLAN 2027 (.NET 8) API
    assemblies and produces bin\EplanBridge.dll, which is committed to the repo
    and loaded at runtime by mcp_server/api/v2/actions/bridge.py.

    IMPORTANT: EPLAN file-locks a loaded add-in DLL until the process restarts.
    If a build fails with "file is locked by Eplan", the DLL is currently loaded
    in a running EPLAN -- fully restart EPLAN, then rebuild. (For dev iteration
    without restarting, build to a new AssemblyName/OutputPath instead.)

.PARAMETER EplanBin
    Path to the EPLAN Platform Bin folder to compile against. Defaults to the
    2027.0.1 install. Override for a different version, e.g.:
        ./build.ps1 -EplanBin "C:\Program Files\EPLAN\Platform\2027.0.3\Bin"
#>
param(
    [string]$EplanBin = "C:\Program Files\EPLAN\Platform\2027.0.1\Bin"
)

$ErrorActionPreference = "Stop"
$proj = Join-Path $PSScriptRoot "EplanBridge.csproj"

if (-not (Test-Path $EplanBin)) {
    throw "EPLAN Bin not found: $EplanBin. Pass -EplanBin <path> for your install."
}

Write-Host "Building EplanBridge.dll against $EplanBin ..."
dotnet build $proj -c Release -p:EplanBin="$EplanBin" -v minimal
if ($LASTEXITCODE -ne 0) { throw "Build failed (exit $LASTEXITCODE)." }

$dll = Join-Path $PSScriptRoot "bin\EplanBridge.dll"
Write-Host "Built: $dll"
