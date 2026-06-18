Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

Write-Host "Installing local FFmpeg for this project..." -ForegroundColor Cyan

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$toolsDir = Join-Path $scriptDir "tools"
$targetDir = Join-Path $toolsDir "ffmpeg"
$workDirName = "ffmpeg-local-install-{0}-{1}" -f $PID, (Get-Date -Format "yyyyMMddHHmmss")
$workDir = Join-Path $env:TEMP $workDirName
$zipPath = Join-Path $workDir "ffmpeg-release-essentials.zip"
$extractDir = Join-Path $workDir "extract"
$url = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"

New-Item -ItemType Directory -Force -Path $toolsDir | Out-Null
New-Item -ItemType Directory -Force -Path $workDir | Out-Null

if (Test-Path $extractDir) {
    Remove-Item -Path $extractDir -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $extractDir | Out-Null

Write-Host "Downloading FFmpeg essentials build..."
Invoke-WebRequest -Uri $url -OutFile $zipPath

Write-Host "Extracting..."
Expand-Archive -Path $zipPath -DestinationPath $extractDir -Force

$ffmpegExe = Get-ChildItem -Path $extractDir -Recurse -Filter ffmpeg.exe | Select-Object -First 1
if (-not $ffmpegExe) {
    throw "Could not find ffmpeg.exe in the downloaded archive."
}

$sourceRoot = $ffmpegExe.Directory.Parent.FullName
if (Test-Path $targetDir) {
    Remove-Item -Path $targetDir -Recurse -Force
}

Copy-Item -Path $sourceRoot -Destination $targetDir -Recurse -Force

$installedFfmpeg = Join-Path $targetDir "bin\ffmpeg.exe"
$installedFfprobe = Join-Path $targetDir "bin\ffprobe.exe"

if (-not (Test-Path $installedFfmpeg) -or -not (Test-Path $installedFfprobe)) {
    throw "FFmpeg install did not produce both ffmpeg.exe and ffprobe.exe."
}

Write-Host ""
Write-Host "Done. Local FFmpeg installed here:" -ForegroundColor Green
Write-Host $targetDir
Write-Host ""
& $installedFfmpeg -version | Select-Object -First 1
& $installedFfprobe -version | Select-Object -First 1
Write-Host ""
Write-Host "You can now run start_ui.bat again."
