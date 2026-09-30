<#
.SYNOPSIS
  Builds the Windows release: dist\SpoTerm\SpoTerm.exe and dist\SpoTerm-<version>-windows-x64.zip.

.DESCRIPTION
  1. Builds spoterm-engine (Rust) in release mode from Cargo.lock.
  2. Freezes the Python UI with PyInstaller (one folder, no UPX) in a private venv.
  3. Puts the engine next to SpoTerm.exe, zips the folder and writes its SHA-256.

  Needs Rust (rustup) and Python 3.10+. Run from anywhere:
    powershell -ExecutionPolicy Bypass -File build.ps1
  -SkipEngine reuses an existing engine\target\release\spoterm-engine.exe.
#>
param([switch]$SkipEngine)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$dist = Join-Path $root "dist"
$work = Join-Path $root "build"
$venv = Join-Path $root ".venv-build"

function Invoke-Checked([string]$what, [scriptblock]$cmd) {
    & $cmd
    if ($LASTEXITCODE -ne 0) { throw "$what failed (exit $LASTEXITCODE)" }
}

$version = (Select-String -Path (Join-Path $root "spoterm\__init__.py") -Pattern '__version__ = "([^"]+)"').Matches[0].Groups[1].Value
Write-Host "Building SpoTerm $version" -ForegroundColor Green

# -- 1. Engine --
$engineExe = Join-Path $root "engine\target\release\spoterm-engine.exe"
if (-not $SkipEngine) {
    # The GNU toolchain links only DLLs that ship with Windows. With MSVC, link the C
    # runtime statically so users don't need the Visual C++ redistributable.
    $toolchain = @()
    $haveGnu = (rustup toolchain list) -match "x86_64-pc-windows-gnu"
    if ($haveGnu -and (Get-Command gcc -ErrorAction SilentlyContinue)) {
        $toolchain = @("+stable-x86_64-pc-windows-gnu")
    } else {
        $env:RUSTFLAGS = "-C target-feature=+crt-static"
    }
    Push-Location (Join-Path $root "engine")
    try {
        Invoke-Checked "cargo build" { cargo @toolchain build --release --locked }
    } finally {
        Pop-Location
        Remove-Item Env:RUSTFLAGS -ErrorAction SilentlyContinue
    }
}
if (-not (Test-Path $engineExe)) { throw "No engine at $engineExe; run without -SkipEngine" }

# -- 2. Python UI --
$py = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $py)) {
    $sysPy = (Get-Command python -ErrorAction SilentlyContinue)
    if (-not $sysPy) { $sysPy = (Get-Command py -ErrorAction Stop) }
    Invoke-Checked "venv" { & $sysPy.Source -m venv $venv }
}
Invoke-Checked "pip install" { & $py -m pip install --quiet --disable-pip-version-check -r (Join-Path $root "packaging\requirements-build.txt") }

$app = Join-Path $dist "SpoTerm"
if (Test-Path $app) { Remove-Item -Recurse -Force $app }
Invoke-Checked "PyInstaller" {
    & $py -m PyInstaller --noconfirm --clean --log-level WARN `
        --onedir --console --noupx --name SpoTerm `
        --paths $root `
        --distpath $dist --workpath $work --specpath $work `
        --exclude-module tkinter --exclude-module unittest --exclude-module pydoc `
        --exclude-module sqlite3 --exclude-module xmlrpc --exclude-module multiprocessing `
        (Join-Path $root "packaging\launcher.py")
}

# -- 3. Assemble and zip --
Copy-Item $engineExe $app
Copy-Item (Join-Path $root "README.md") $app
Copy-Item (Join-Path $root ".env.example") $app

# Smoke test: the frozen app starts and finds its bundled modules.
$out = & (Join-Path $app "SpoTerm.exe") --version
if ($LASTEXITCODE -ne 0 -or $out -notmatch [regex]::Escape($version)) { throw "SpoTerm.exe --version failed: $out" }

$zip = Join-Path $dist "SpoTerm-$version-windows-x64.zip"
if (Test-Path $zip) { Remove-Item -Force $zip }
# Antivirus often still holds freshly written files for a moment, so retry briefly.
for ($try = 1; ; $try++) {
    try { Compress-Archive -Path $app -DestinationPath $zip -Force; break }
    catch { if ($try -ge 5) { throw }; Start-Sleep -Seconds 2 }
}
$hash = (Get-FileHash -Algorithm SHA256 $zip).Hash.ToLower()
Set-Content -Path "$zip.sha256" -Value "$hash  $(Split-Path -Leaf $zip)" -Encoding ascii

Write-Host ""
Write-Host "Done:" -ForegroundColor Green
Write-Host "  $app\SpoTerm.exe"
Write-Host "  $zip"
Write-Host "  sha256 $hash"
