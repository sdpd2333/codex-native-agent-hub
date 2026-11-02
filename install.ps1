[CmdletBinding()]
param([switch]$SkipMcpRegistration)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$environmentPath = Join-Path $PSScriptRoot '.venv'
$runtimePython = Join-Path $environmentPath 'Scripts\python.exe'

if (-not (Test-Path -LiteralPath $runtimePython)) {
    & python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.11 or newer is required.' }
    & python -m venv $environmentPath
    if ($LASTEXITCODE -ne 0) { throw 'Python environment creation failed.' }
}

& $runtimePython -m pip install -r (Join-Path $PSScriptRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
& $runtimePython -c "import mcp; from google.antigravity import Agent; from claude_agent_sdk import ClaudeSDKClient; from deepseek_harness import DeepSeekHarness; import json, runtimes; print(json.dumps(runtimes.doctor()))"
if ($LASTEXITCODE -ne 0) { throw 'Runtime verification failed.' }

if (-not $SkipMcpRegistration) {
    if (-not (Get-Command codex -ErrorAction SilentlyContinue)) {
        throw 'Codex CLI was not found. Install it, then run this script again.'
    }
    & codex mcp add native-agent-hub -- $runtimePython (Join-Path $PSScriptRoot 'server.py')
    if ($LASTEXITCODE -ne 0) { throw 'MCP registration failed.' }
}

Write-Host 'Ready. Ask Codex to open the Native Agent Hub panel on the right, or run configure.cmd.'
