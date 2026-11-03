[CmdletBinding()]
param([string]$SkillsDir = '')

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if (-not $SkillsDir) {
    $codexRoot = if ($env:CODEX_HOME) { $env:CODEX_HOME } else {
        Join-Path ([Environment]::GetFolderPath('UserProfile')) '.codex'
    }
    $SkillsDir = Join-Path $codexRoot 'skills'
}
$source = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../skills/nah'))
$destination = [IO.Path]::GetFullPath((Join-Path $SkillsDir 'nah'))
if ($source -eq $destination) { throw 'Choose an installation directory outside the repository skill source.' }
$files = @('SKILL.md', 'agents/openai.yaml')
$manifestPath = Join-Path $destination '.nah-skill-install.json'

function Assert-RegularPath([string]$Path) {
    $current = [IO.Path]::GetFullPath($Path)
    while ($current) {
        if (Test-Path -LiteralPath $current) {
            $item = Get-Item -LiteralPath $current -Force
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw "Linked installation paths are not supported: $current"
            }
        }
        $parent = [IO.Path]::GetDirectoryName($current)
        if ($parent -eq $current) { break }
        $current = $parent
    }
}

function Write-AtomicBytes([string]$Path, [byte[]]$Bytes) {
    $parent = [IO.Path]::GetDirectoryName($Path)
    [IO.Directory]::CreateDirectory($parent) | Out-Null
    $temporary = Join-Path $parent ('.nah-install-' + [Guid]::NewGuid().ToString('N') + '.tmp')
    try {
        [IO.File]::WriteAllBytes($temporary, $Bytes)
        Move-Item -LiteralPath $temporary -Destination $Path -Force
    } finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary }
    }
}

Assert-RegularPath $manifestPath
$installed = $null
if (Test-Path -LiteralPath $manifestPath) {
    $installed = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($installed.owner -ne 'codex-native-agent-hub/nah' -or $installed.version -ne 1) {
        throw 'The existing nah skill is not owned by this installer.'
    }
} elseif (Test-Path -LiteralPath $destination) {
    if (@(Get-ChildItem -LiteralPath $destination -Force).Count -gt 0) {
        throw 'An unmanaged nah skill already exists. Preserve it and choose another -SkillsDir.'
    }
}

# Check every managed file before writing anything; preserve unrelated user files.
$hashes = [ordered]@{}
foreach ($relative in $files) {
    $from = Join-Path $source $relative
    $to = Join-Path $destination $relative
    Assert-RegularPath $from
    Assert-RegularPath $to
    if (-not (Test-Path -LiteralPath $from -PathType Leaf)) { throw "Missing skill source: $relative" }
    $hashes[$relative] = (Get-FileHash -LiteralPath $from -Algorithm SHA256).Hash
    if (Test-Path -LiteralPath $to) {
        if (-not (Test-Path -LiteralPath $to -PathType Leaf)) { throw "Expected a skill file: $to" }
        $currentHash = (Get-FileHash -LiteralPath $to -Algorithm SHA256).Hash
        $property = if ($null -ne $installed) { $installed.files.PSObject.Properties[$relative] } else { $null }
        if (($null -eq $property -or $currentHash -ne $property.Value) -and $currentHash -ne $hashes[$relative]) {
            throw "Local changes in $relative were preserved. Reconcile them before updating, or choose another -SkillsDir."
        }
    }
}
foreach ($relative in $files) {
    Write-AtomicBytes (Join-Path $destination $relative) ([IO.File]::ReadAllBytes((Join-Path $source $relative)))
}
$manifest = [ordered]@{ owner = 'codex-native-agent-hub/nah'; version = 1; files = $hashes } | ConvertTo-Json -Depth 4
Write-AtomicBytes $manifestPath ([Text.UTF8Encoding]::new($false).GetBytes($manifest + "`n"))
Write-Host "Installed nah skill: $destination"
Write-Host 'Use $nah or ask Codex to open NAH. Reload skills or start a new chat if it is not visible yet.'
