param([switch]$SkipApplication)
$ErrorActionPreference='Stop'
$quantRoot=Split-Path -Parent $PSScriptRoot
$quantPython='E:\AI-Tools\quant-packaging\venv\Scripts\python.exe'
$quantWork='D:\量化数据\work\packaging'
$quantTools='E:\AI-Tools\quant-packaging'
$env:PYTHONIOENCODING='utf-8'
if (-not $SkipApplication) {
    & $quantPython -m PyInstaller --noconfirm --distpath "$quantTools\dist" --workpath "$quantWork\pyinstaller" "$PSScriptRoot\LiangYan.spec" *> "$quantWork\build.log"
    if ($LASTEXITCODE) {throw '程序打包失败，查看build.log。'}
}
$env:BUILD_LIANGYAN_SETUP='0'
& $quantPython -m PyInstaller --noconfirm --distpath "$quantTools\uninstaller" --workpath "$quantWork\uninstaller" "$PSScriptRoot\Setup.spec" *> "$quantWork\uninstaller-build.log"
if ($LASTEXITCODE) {throw '卸载器打包失败。'}
Copy-Item -LiteralPath "$quantTools\uninstaller\卸载量研.exe" -Destination "$quantTools\dist\LiangYan\卸载量研.exe" -Force
& $quantPython "$PSScriptRoot\prepare_payload.py"
if ($LASTEXITCODE) {throw '安装内容准备失败。'}
$env:BUILD_LIANGYAN_SETUP='1'
& $quantPython -m PyInstaller --noconfirm --distpath "$quantTools\setup" --workpath "$quantWork\setup" "$PSScriptRoot\Setup.spec" *> "$quantWork\setup-build.log"
if ($LASTEXITCODE) {throw '安装器打包失败。'}
Write-Host "安装程序：$quantTools\setup\量研安装程序-0.4.0-Windows-x64.exe"
