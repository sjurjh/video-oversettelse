Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

Write-Host "Installing Windows Package Manager (winget)..." -ForegroundColor Cyan

$workDir = Join-Path $env:TEMP "winget-install"
New-Item -ItemType Directory -Force -Path $workDir | Out-Null

$releaseApi = "https://api.github.com/repos/microsoft/winget-cli/releases/latest"
$release = Invoke-RestMethod -Uri $releaseApi -Headers @{ "User-Agent" = "winget-local-installer" }

$bundleAsset = $release.assets | Where-Object { $_.name -eq "Microsoft.DesktopAppInstaller_8wekyb3d8bbwe.msixbundle" } | Select-Object -First 1
if (-not $bundleAsset) {
    throw "Could not find Desktop App Installer msixbundle in latest winget release."
}

$bundlePath = Join-Path $workDir $bundleAsset.name
Write-Host "Downloading $($bundleAsset.name)..."
Invoke-WebRequest -Uri $bundleAsset.browser_download_url -OutFile $bundlePath

$dependencyAsset = $release.assets | Where-Object { $_.name -eq "DesktopAppInstaller_Dependencies.zip" } | Select-Object -First 1
$dependencyPaths = @()

if ($dependencyAsset) {
    $dependencyZip = Join-Path $workDir $dependencyAsset.name
    $dependencyDir = Join-Path $workDir "dependencies"
    Write-Host "Downloading dependencies..."
    Invoke-WebRequest -Uri $dependencyAsset.browser_download_url -OutFile $dependencyZip
    Expand-Archive -Path $dependencyZip -DestinationPath $dependencyDir -Force

    $arch = switch ($env:PROCESSOR_ARCHITECTURE) {
        "AMD64" { "x64" }
        "ARM64" { "arm64" }
        "x86" { "x86" }
        default { "x64" }
    }

    $dependencyPaths = Get-ChildItem -Path $dependencyDir -Recurse -Include *.appx,*.msix |
        Where-Object { $_.FullName -match "\\$arch\\" -or $_.Name -match "_$arch" } |
        Select-Object -ExpandProperty FullName
}

Write-Host "Installing App Installer package..."
try {
    if ($dependencyPaths.Count -gt 0) {
        Add-AppxPackage -Path $bundlePath -DependencyPath $dependencyPaths -ForceApplicationShutdown
    } else {
        Add-AppxPackage -Path $bundlePath -ForceApplicationShutdown
    }
} catch {
    Write-Host ""
    Write-Host "Install failed. If Windows says resources are in use, restart the PC and run this script again before opening Microsoft Store, Phone Link, Widgets, or other Microsoft apps." -ForegroundColor Yellow
    Write-Host "Original error:" -ForegroundColor Yellow
    Write-Host $_
    throw
}

Write-Host ""
Write-Host "Done. Open a NEW PowerShell window and run:" -ForegroundColor Green
Write-Host "winget --version"
