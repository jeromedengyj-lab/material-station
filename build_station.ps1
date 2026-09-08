# 素材准备站 - 构建脚本（独立仓库版）
# 用法：
#   绿色版：    powershell -ExecutionPolicy Bypass -File build_station.ps1
#   安装包版：  powershell -ExecutionPolicy Bypass -File build_station.ps1 -ForceLicense -OutName build_station_install
# 绿色版产物：build_station_portable\素材准备站\（整个文件夹拷到目标设备即用，免激活）
# 安装包版产物：build_station_install\素材准备站\（exe 强制授权，必须密钥才能使用，
#               供 setup 单文件安装包内嵌；直接解压/拷贝也绕不过授权）
param(
    [switch]$ForceLicense,
    [string]$OutName = "build_station_portable"
)
$ErrorActionPreference = "Stop"

$root = "D:\漫剧剪辑工具\material-station"
$venvPython = "D:\漫剧剪辑工具\app\.venv\Scripts\python.exe"
$spec = "$root\build_station.spec"
$work = "$root\build_station_work"
$dist = "$root\build_station_dist"
$portable = "$root\$OutName"
$stage = "$portable\素材准备站"

$ollamaSrc = "D:\漫剧剪辑工具\素材准备站\runtime\ollama"
$modelsSrc = "D:\漫剧剪辑工具\素材准备站\models\ollama"

# 构建常量：安装包版强制授权（绿色版豁免）
$cfgValue = if ($ForceLicense) { "True" } else { "False" }
Set-Content -Path "$root\_build_config.py" -Value "FORCE_LICENSE = $cfgValue" -Encoding UTF8
Write-Host "构建模式：$($(if ($ForceLicense) {'强制授权版（安装包）'} else {'绿色版（免激活）'}))" -ForegroundColor Cyan

Write-Host "== 1/5 PyInstaller 构建 ==" -ForegroundColor Cyan
Push-Location $root
& $venvPython -m PyInstaller --noconfirm --clean --workpath $work --distpath $dist $spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 构建失败 (exit=$LASTEXITCODE)" }
Pop-Location

Write-Host "== 2/5 组装绿色文件夹 ==" -ForegroundColor Cyan
if (Test-Path $portable) { Remove-Item $portable -Recurse -Force }
New-Item -ItemType Directory -Force -Path $stage | Out-Null
Copy-Item "$dist\素材准备站\*" $stage -Recurse -Force

Write-Host "== 3/5 复制 Ollama（GPU 版：vulkan 后端，与已验证环境一致，N 卡免 CUDA 安装） ==" -ForegroundColor Cyan
$ollamaDst = "$stage\runtime\ollama"
New-Item -ItemType Directory -Force -Path $ollamaDst | Out-Null
Copy-Item "$ollamaSrc\*" $ollamaDst -Recurse -Force
Write-Host "  Ollama GPU 版体积：$([math]::Round((Get-ChildItem $ollamaDst -Recurse -File | Measure-Object Length -Sum).Sum/1MB)) MB"

Write-Host "== 4/5 复制 qwen3 模型（8b + 4b-instruct，GPU 推理） ==" -ForegroundColor Cyan
$modelDst = "$stage\models\ollama"
New-Item -ItemType Directory -Force -Path $modelDst | Out-Null
Copy-Item "$modelsSrc\*" $modelDst -Recurse -Force
Write-Host "  模型体积：$([math]::Round((Get-ChildItem $modelDst -Recurse -File | Measure-Object Length -Sum).Sum/1MB)) MB"

Write-Host "== 5/5 生成使用说明与空 data 目录 ==" -ForegroundColor Cyan
New-Item -ItemType Directory -Force -Path "$stage\data" | Out-Null
New-Item -ItemType Directory -Force -Path "$stage\runtime\browser_profiles" | Out-Null
New-Item -ItemType Directory -Force -Path "$stage\runtime\platform_adapter" | Out-Null

$readme = @"
素材准备站 - 使用说明
========================================

功能：输入平台 BookID，自动完成「下载原剧视频 + 下载封面 + 申请三端关键词别名」
（三端 = 红果短剧 / 番茄小说 / 红果漫剧）

【绿色免安装】
本文件夹已内置 Node.js、Ollama 和 qwen3:4b-instruct 模型，直接整体拷贝到任意
Windows 设备即可使用，无需安装任何运行时。
唯一外部依赖：系统安装 Google Chrome 或 Microsoft Edge（首次运行需在专用浏览器
窗口内登录一次达人接单平台，登录态保存在本文件夹 runtime\browser_profiles）。

【使用方法】
1. 双击 素材准备站.exe 启动
2. 输入平台 BookID（16~20 位纯数字，例如 7670000000000000001），点「添加任务」
3. 程序自动串行执行：下载原剧+封面 → 本地生成候选别名 → 申请三端并轮询审核
4. 任务完成后，产出在 data 目录下：
   - data\选剧文件夹\原剧视频\{剧名}\     原剧视频（001_第1集.mp4 ...）
   - data\封面-原图\{剧名}.jpg            封面
   - data\选剧文件夹\原剧视频\{剧名}\剧目信息\三端别名任务.json   三端审核结果

【一键下载到表格】
- 点「下载到表格」按钮，选择保存位置，当前任务列表导出为 CSV（Excel 可直接打开）
- 只读导出，不会清除或修改界面任务信息

【注意事项】
- 程序串行执行任务：同一时刻只处理一个 BookID，其余排队等待
- 「需人工处理」状态 = 平台出现滑块/安全验证或候选均未通过，请登录平台处理后
  选中该任务点「重试选中」
- 日志在 data\logs\ 下，排障时查看对应 BookID 的日志文件
- 首次运行会自动启动内置 Ollama（端口 11434），请勿关闭
"@
Set-Content -Path "$stage\使用说明.txt" -Value $readme -Encoding UTF8

Write-Host "== 完成 ==" -ForegroundColor Green
$total = [math]::Round((Get-ChildItem $stage -Recurse -File | Measure-Object Length -Sum).Sum/1MB)
Write-Host "绿色文件夹：$stage"
Write-Host "总大小：$total MB"
