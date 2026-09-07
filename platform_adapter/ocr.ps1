# 素材准备站 - Windows 系统 OCR（零依赖，Win10+ 自带中文识别）
# 用法: powershell -NoProfile -ExecutionPolicy Bypass -File ocr.ps1 -ImagePath <图片绝对路径>
# 输出: 每行一行识别文本（按行号），输出 JSON 便于 Python 解析
param([string]$ImagePath)

$ErrorActionPreference = 'Stop'
# 强制 UTF-8 输出：Windows PowerShell 5.1 默认按 GBK 输出中文，Python 按 utf-8 解码会得到乱码
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType=WindowsRuntime]
$null = [Windows.Storage.StorageFile, Windows.Foundation, ContentType=WindowsRuntime]
$null = [Windows.Storage.Streams.RandomAccessStream, Windows.Foundation, ContentType=WindowsRuntime]

function Await($WinRtTask, $ResultType) {
  $asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
  })[0]
  $netTask = $asTaskGeneric.MakeGenericMethod($ResultType).Invoke($null, @($WinRtTask))
  $netTask.Wait(-1) | Out-Null
  return $netTask.Result
}

if (-not $ImagePath -or -not (Test-Path $ImagePath)) {
  Write-Host '{"ok":false,"message":"图片不存在"}'
  exit 1
}

try {
  $abs = (Resolve-Path $ImagePath).Path
  $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($abs)) ([Windows.Storage.StorageFile])
  $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
  $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
  $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
  $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
  if (-not $engine) {
    Write-Host '{"ok":false,"message":"系统无可用OCR语言包"}'
    exit 1
  }
  $result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
  $lines = @()
  $idx = 0
  foreach ($line in $result.Lines) {
    $idx++
    $words = @()
    foreach ($w in $line.Words) {
      $conf = [math]::Round($w.Confidence * 100)
      $words += @{ text = $w.Text; conf = $conf }
    }
    $lines += @{ line = $idx; text = $line.Text; words = $words }
  }
  $out = @{ ok = $true; lines = $lines } | ConvertTo-Json -Depth 5 -Compress
  Write-Host $out
} catch {
  Write-Host ('{"ok":false,"message":"' + ($_.Exception.Message -replace '"', '\"') + '"}')
  exit 1
}
