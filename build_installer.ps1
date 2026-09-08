# 素材准备站 - 单文件安装包构建脚本
# 用法：powershell -ExecutionPolicy Bypass -File build_installer.ps1
# 产物：build_tools\素材准备站安装包.exe（自包含：安装逻辑 + 强制授权版资源，双击即装）
# 依赖：7.5GB 安装版资源（由 build_station.ps1 -ForceLicense 构建）
$ErrorActionPreference = "Stop"

$root = "D:\漫剧剪辑工具\material-station"
$venvPython = "D:\漫剧剪辑工具\app\.venv\Scripts\python.exe"
$installStage = "$root\build_station_install\素材准备站"
$tools = "$root\build_tools"
$zipPath = "$tools\resources.zip"
$setupExe = "$tools\setup.exe"
$finalExe = "$tools\素材准备站安装包.exe"

Write-Host "== 1/4 构建安装包版资源（强制授权） ==" -ForegroundColor Cyan
if (-not (Test-Path "$installStage\素材准备站.exe")) {
    Push-Location $root
    powershell -ExecutionPolicy Bypass -File build_station.ps1 -ForceLicense -OutName build_station_install
    if ($LASTEXITCODE -ne 0) { throw "安装版资源构建失败" }
    Pop-Location
} else {
    Write-Host "  安装版资源已存在，跳过构建"
}

Write-Host "== 2/4 打包 resources.zip ==" -ForegroundColor Cyan
if (Test-Path $zipPath) { Remove-Item $zipPath -Force }
& $venvPython -c "
import zipfile
from pathlib import Path
src = Path(r'$installStage')
out = Path(r'$zipPath')
names = ['_internal', 'runtime', 'models', '素材准备站.exe', '使用说明.txt']
count = total = 0
with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
    for name in names:
        p = src / name
        if p.is_dir():
            for f in sorted(p.rglob('*')):
                if f.is_file():
                    zf.write(f, f.relative_to(src).as_posix()); count += 1; total += f.stat().st_size
        elif p.is_file():
            zf.write(p, name); count += 1; total += p.stat().st_size
print(f'  完成: {count} 文件, {total/2**30:.1f} GB -> {out.stat().st_size/2**30:.1f} GB')
"
if ($LASTEXITCODE -ne 0) { throw "打包失败" }

Write-Host "== 3/4 构建安装逻辑 setup.exe ==" -ForegroundColor Cyan
Push-Location $root
& $venvPython -m PyInstaller --noconfirm --clean --onefile --windowed --name setup --distpath $tools setup_main.py
if ($LASTEXITCODE -ne 0) { throw "setup 构建失败" }
Pop-Location

Write-Host "== 4/4 合并为单文件安装包 ==" -ForegroundColor Cyan
if (Test-Path $finalExe) { Remove-Item $finalExe -Force }
# copy /b 流式二进制拼接（.NET ReadAllBytes 有 2GB 上限，不适用于 7GB 资源）
cmd /c "copy /b `"$setupExe`" + `"$zipPath`" `"$finalExe`"" | Out-Null
if ($LASTEXITCODE -ne 0 -or -not (Test-Path $finalExe)) { throw "合并失败" }
$size = [math]::Round((Get-Item $finalExe).Length/1GB, 2)
Write-Host "== 完成 ==" -ForegroundColor Green
Write-Host "单文件安装包：$finalExe"
Write-Host "大小：$size GB（含安装逻辑 + 强制授权版完整资源）"
