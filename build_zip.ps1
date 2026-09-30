# Package astrbot_plugin_mine_chat into an AstrBot-installable zip.
#
# Design notes (learned from sibling plugins, keep them):
#   1) EXPLICIT include list. New top-level .py modules MUST be added here too,
#      otherwise the repo has the file but the released zip silently misses it
#      (the feature then just does not work for users). A guard below fails the
#      build loudly if a top-level module is missing from the list.
#   2) Refuse to overwrite an existing zip of the same version: bump the version
#      in metadata.yaml (and add a CHANGELOG entry) before repacking. Historical
#      zips in dist/ are kept forever so users can roll back.
#   3) Archive layout: wrapped in a top-level folder named after the plugin, with
#      explicit directory entries and forward slashes. Do NOT use PowerShell
#      Compress-Archive (it omits directory entries).
#   4) dist/ is git-ignored: the zip is a local install artifact, not source.
#
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File .\build_zip.ps1
#
# NOTE: keep this file ASCII-only. PS 5.1 on a non-UTF8 code page can mis-parse
#       UTF-8 no-BOM files that contain CJK comments.

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem

if ($PSScriptRoot) {
    $root = $PSScriptRoot
} elseif ($MyInvocation.MyCommand.Path) {
    $root = Split-Path -Parent $MyInvocation.MyCommand.Path
} else {
    $root = (Get-Location).Path
}

$pluginName = "astrbot_plugin_mine_chat"
$distDir = Join-Path $root "dist"

# --- version from metadata.yaml (UTF8) ---
$metaPath = Join-Path $root "metadata.yaml"
if (-not (Test-Path $metaPath)) {
    Write-Host "ERROR: metadata.yaml not found" -ForegroundColor Red
    exit 1
}
$metaRaw = Get-Content -Raw -Encoding UTF8 $metaPath
$version = ""
if ($metaRaw -match '(?m)^\s*version:\s*v?([0-9][^\s#]*)') {
    $version = $Matches[1].Trim()
}
if (-not $version) {
    Write-Host "ERROR: cannot read version from metadata.yaml" -ForegroundColor Red
    exit 1
}

if (-not (Test-Path $distDir)) {
    New-Item -ItemType Directory -Path $distDir | Out-Null
}
$zipName = "${pluginName}_v${version}.zip"
$zipPath = Join-Path $distDir $zipName

if (Test-Path $zipPath) {
    Write-Host "ERROR: $zipName already exists. Bump 'version' in metadata.yaml and add a CHANGELOG entry before repacking." -ForegroundColor Red
    exit 1
}

# --- files / directories that go into the release ---
# Keep this in sync with main.py's _RELOAD_MODULES and the modules under this folder.
$includeList = @(
    "main.py",
    "config.py",
    "store.py",
    "scope.py",
    "schedule.py",
    "schedule_view.py",
    "proactive.py",
    "proactive_gen.py",
    "llm.py",
    "kb.py",
    "prompts.py",
    "webui_api.py",
    "_conf_schema.json",
    "metadata.yaml",
    "requirements.txt",
    "README.md",
    "CHANGELOG.md",
    "LICENSE",
    "pages"
)

# --- self-check: every top-level .py in the repo MUST be listed above ---
# This guards a trap that was hit for real in sibling plugins: new modules were
# added to the repo but not to this list, so the released zip shipped without
# them and users saw "No module named 'xxx'" instead of an obvious build error.
$topPy = @(Get-ChildItem $root -Filter *.py -File | Select-Object -ExpandProperty Name)
$notListed = @($topPy | Where-Object { $includeList -notcontains $_ })
if ($notListed.Count -gt 0) {
    Write-Host ("ERROR: top-level module(s) missing from includeList: " + ($notListed -join ", ")) -ForegroundColor Red
    Write-Host "       Add them to the list at the top of this script before packaging." -ForegroundColor Red
    exit 1
}
Write-Host ("includeList check OK: all " + $topPy.Count + " top-level .py files are listed")

# --- self-check: the config schema must be loadable by AstrBot ---
# Real incident (v1.0.4/v1.0.5): group nodes were missing "type": "object", so
# AstrBot's schema parser raised KeyError 'type' and the plugin could not be
# installed at all. Every top-level node must declare a supported type.
$schemaPath = Join-Path $root "_conf_schema.json"
$supportedTypes = @("int", "float", "bool", "string", "text", "list", "file", "object", "template_list", "dict")
$schema = Get-Content -Raw -Encoding UTF8 $schemaPath | ConvertFrom-Json
$schemaErrors = @()
foreach ($prop in $schema.PSObject.Properties) {
    $node = $prop.Value
    if ($node -isnot [PSCustomObject]) {
        $schemaErrors += "$($prop.Name): node is not an object"
        continue
    }
    $nodeType = $node.type
    if (-not $nodeType) {
        $schemaErrors += "$($prop.Name): missing 'type'"
        continue
    }
    if ($supportedTypes -notcontains $nodeType) {
        $schemaErrors += "$($prop.Name): unsupported type '$nodeType'"
        continue
    }
    if ($nodeType -eq "object" -and -not $node.items) {
        $schemaErrors += "$($prop.Name): object node has no items"
    }
}
if ($schemaErrors.Count -gt 0) {
    Write-Host "ERROR: _conf_schema.json is not loadable:" -ForegroundColor Red
    foreach ($err in $schemaErrors) { Write-Host ("  - " + $err) -ForegroundColor Red }
    exit 1
}
Write-Host "schema check OK: _conf_schema.json is loadable"

# Built console must exist before packaging (run build_webui.ps1 first).
$consoleIndex = Join-Path $root "pages/schedule-console/index.html"
if (-not (Test-Path $consoleIndex)) {
    Write-Host "ERROR: pages/schedule-console/index.html missing. Run .\build_webui.ps1 first." -ForegroundColor Red
    exit 1
}

function Add-ItemToZip($zip, $fsPath, $entryPath) {
    if (Test-Path -PathType Container $fsPath) {
        # directory entry must end with '/'
        $null = $zip.CreateEntry($entryPath + "/")
        Get-ChildItem $fsPath |
            Where-Object { $_.Name -ne "__pycache__" -and $_.Extension -notin @(".pyc", ".pyo") } |
            ForEach-Object {
                Add-ItemToZip $zip $_.FullName ($entryPath + "/" + $_.Name)
            }
    } else {
        # entries are named with forward slashes so Linux-side zipfile sees a real tree
        $entry = $zip.CreateEntry($entryPath, [System.IO.Compression.CompressionLevel]::Optimal)
        $stream = $entry.Open()
        try {
            $bytes = [System.IO.File]::ReadAllBytes($fsPath)
            $stream.Write($bytes, 0, $bytes.Length)
        } finally {
            $stream.Dispose()
        }
    }
}

$fs = [System.IO.File]::Open($zipPath, [System.IO.FileMode]::Create)
$zip = New-Object System.IO.Compression.ZipArchive($fs, [System.IO.Compression.ZipArchiveMode]::Create, $false)
try {
    foreach ($name in $includeList) {
        $fsPath = Join-Path $root $name
        if (-not (Test-Path $fsPath)) {
            Write-Host "ERROR: missing required item: $name" -ForegroundColor Red
            $zip.Dispose(); $fs.Dispose()
            Remove-Item $zipPath -Force -ErrorAction SilentlyContinue
            exit 1
        }
        Add-ItemToZip $zip $fsPath ($pluginName + "/" + $name)
    }
} finally {
    $zip.Dispose()
    $fs.Dispose()
}

# NOTE: read the entries back with .NET instead of `tar -tf`: the tar shipped
# with Git Bash on this machine does not understand zip archives.
$read = [System.IO.Compression.ZipFile]::OpenRead($zipPath)
try {
    $entries = @($read.Entries | ForEach-Object { $_.FullName })
} finally {
    $read.Dispose()
}
$sizeKb = [math]::Round((Get-Item $zipPath).Length / 1024, 1)
Write-Host "Packaged: $zipPath" -ForegroundColor Green
Write-Host ("Entries : {0}   Size: {1} KB" -f $entries.Count, $sizeKb)

# --- verify the archive layout (top level must be exactly the plugin folder) ---
# @() is required: with a single top-level name, Sort-Object returns a string and
# $top[0] would then address the first *character*.
$top = @($entries | ForEach-Object { ($_ -split '/')[0] } | Sort-Object -Unique)
Write-Host ("Top-level: " + ($top -join ","))
if ($top.Count -eq 1 -and $top[0] -eq $pluginName) {
    Write-Host "OK: wrapped layout as expected." -ForegroundColor Green
} else {
    Write-Host "WARN: unexpected archive layout, check packaging!" -ForegroundColor Yellow
}
