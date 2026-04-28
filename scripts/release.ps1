#!/usr/bin/env pwsh
# Release script for granny-devops (PowerShell).
#
# Usage:
#   .\scripts\release.ps1 patch   # 0.3.0 -> 0.3.1
#   .\scripts\release.ps1 minor   # 0.3.0 -> 0.4.0
#   .\scripts\release.ps1 major   # 0.3.0 -> 1.0.0
#
# What it does:
#   1. Bumps the version in pyproject.toml
#   2. Commits the bump
#   3. Merges the current branch into main
#   4. Tags main with vX.Y.Z
#   5. Pushes main + tag (CI publishes to PyPI + GitLab registry)
#   6. Switches back to dev

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet('patch', 'minor', 'major')]
    [string]$Bump
)

$ErrorActionPreference = 'Stop'

function Invoke-Git {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$GitArgs)
    & git @GitArgs
    if ($LASTEXITCODE -ne 0) {
        throw "git $($GitArgs -join ' ') failed with exit code $LASTEXITCODE"
    }
}

# Must be on dev branch
$branch = (& git branch --show-current).Trim()
if ($branch -ne 'dev') {
    Write-Error "Error: must be on the dev branch (currently on '$branch')"
    exit 1
}

# Working tree must be clean
$status = & git status --porcelain
if ($status) {
    Write-Error "Error: working tree is dirty - commit or stash changes first"
    exit 1
}

# Read current version from pyproject.toml
$current = & python -c "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])"
if ($LASTEXITCODE -ne 0 -or -not $current) {
    throw "Could not read version from pyproject.toml"
}
$current = $current.Trim()

$parts = $current.Split('.')
if ($parts.Count -ne 3) {
    throw "Unexpected version format: $current"
}
[int]$major = $parts[0]
[int]$minor = $parts[1]
[int]$patch = $parts[2]

switch ($Bump) {
    'major' { $major++; $minor = 0; $patch = 0 }
    'minor' { $minor++; $patch = 0 }
    'patch' { $patch++ }
}

$newVersion = "$major.$minor.$patch"
$tag = "v$newVersion"

Write-Host "Bumping $current -> $newVersion"

# Bump version in pyproject.toml (preserve original encoding / line endings)
$pyprojectPath = 'pyproject.toml'
$content = Get-Content $pyprojectPath -Raw
$updated = [regex]::Replace($content, '(?m)^version = "[^"]*"', "version = `"$newVersion`"")
if ($updated -eq $content) {
    throw "Failed to update version in pyproject.toml"
}
Set-Content -Path $pyprojectPath -Value $updated -NoNewline

Invoke-Git add pyproject.toml
Invoke-Git commit -m "Bump version to $newVersion"

# Merge into main and tag
Invoke-Git checkout main
Invoke-Git merge dev --no-edit
Invoke-Git tag -a $tag -m "Release $tag"

# Push
Invoke-Git push origin main $tag

# Back to dev and fast-forward
Invoke-Git checkout dev
Invoke-Git merge main --ff-only

Write-Host ""
Write-Host "Released $tag - CI will publish to PyPI and GitLab registry."
