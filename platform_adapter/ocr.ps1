# 素材准备站 - Windows 系统 OCR（零依赖，Win10+ 自带中文识别）
# 用法: powershell -NoProfile -ExecutionPolicy Bypass -File ocr.ps1 -ImagePath <图片绝对路径>
# 输出: JSON {ok, lines:[{line,text,words}], scales:[...]}
# 多尺度策略：原图 + 2x 整图 + 四角裁剪放大（覆盖角落小标签如"新剧"红标），合并去重
param([string]$ImagePath)

$ErrorActionPreference = 'Stop'
# 强制 UTF-8 输出：Windows PowerShell 5.1 默认按 GBK 输出中文，Python 按 utf-8 解码会得到乱码
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Runtime.WindowsRuntime
Add-Type -AssemblyName System.Drawing
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

function Get-SoftwareBitmapFromDrawing($DrawingBitmap) {
  # System.Drawing.Bitmap -> SoftwareBitmap（经 PNG 内存流）
  $ms = New-Object System.IO.MemoryStream
  $DrawingBitmap.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)
  $bytes = $ms.ToArray()
  $ms.Dispose()
  $ras = New-Object Windows.Storage.Streams.InMemoryRandomAccessStream
  $writer = New-Object Windows.Storage.Streams.DataWriter($ras.GetOutputStreamAt(0))
  $writer.WriteBytes($bytes)
  $null = Await ($writer.StoreAsync()) ([System.UInt32])
  $null = Await ($writer.FlushAsync()) ([System.Boolean])
  $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($ras)) ([Windows.Graphics.Imaging.BitmapDecoder])
  return (Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap]))
}

function Resize-Drawing($Source, $Scale) {
  [int]$w = [math]::Max(1, [int]($Source.Width * $Scale))
  [int]$h = [math]::Max(1, [int]($Source.Height * $Scale))
  $bmp = New-Object System.Drawing.Bitmap($w, $h)
  $g = [System.Drawing.Graphics]::FromImage($bmp)
  $g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
  $g.DrawImage($Source, 0, 0, $w, $h)
  $g.Dispose()
  return $bmp
}

function Crop-Drawing($Source, $left, $top, $width, $height, $Scale) {
  # 裁剪并放大（NearestNeighbor 保持像素锐利，适合小标签文字）
  [int]$cw = [math]::Max(1, [int]$width)
  [int]$ch = [math]::Max(1, [int]$height)
  [int]$ow = [int]($cw * $Scale)
  [int]$oh = [int]($ch * $Scale)
  $bmp = New-Object System.Drawing.Bitmap($ow, $oh)
  $g = [System.Drawing.Graphics]::FromImage($bmp)
  $g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::NearestNeighbor
  $g.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
  $g.DrawImage($Source,
    (New-Object System.Drawing.Rectangle(0, 0, $ow, $oh)),
    (New-Object System.Drawing.Rectangle([int]$left, [int]$top, $cw, $ch)),
    [System.Drawing.GraphicsUnit]::Pixel)
  $g.Dispose()
  return $bmp
}

function Ocr-SoftwareBitmap($Engine, $SoftBitmap) {
  $result = Await ($Engine.RecognizeAsync($SoftBitmap)) ([Windows.Media.Ocr.OcrResult])
  $outLines = @()
  foreach ($line in $result.Lines) {
    $text = $line.Text -replace '\s+', ''
    if (-not $text) { continue }
    $words = @()
    foreach ($w in $line.Words) {
      $conf = [math]::Round($w.Confidence * 100)
      $words += @{ text = $w.Text; conf = $conf }
    }
    $outLines += @{ text = $text; words = $words }
  }
  return $outLines
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
  $origBitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
  $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
  if (-not $engine) {
    Write-Host '{"ok":false,"message":"系统无可用OCR语言包"}'
    exit 1
  }

  # 1) 原图 OCR
  $allLines = @(Ocr-SoftwareBitmap $engine $origBitmap)
  $scales = @('original')

  # 2) 2x 整图 OCR（提高中等文字识别率）
  $drawing = New-Object System.Drawing.Bitmap($abs)
  if ($drawing.Width -gt 200) {
    $zoom2 = Resize-Drawing $drawing 2.0
    $sb2 = Get-SoftwareBitmapFromDrawing $zoom2
    $allLines += @(Ocr-SoftwareBitmap $engine $sb2)
    $scales += @('zoom2x')
    $zoom2.Dispose()

    # 3) 左右边缘窄条裁剪 8x 放大（覆盖角落小标签/红标：新剧、热剧、漫剧角标等）
    #    仅对低矮小图执行（搜索结果卡片/封面角标场景；大截图文字大，原图+2x 已足够）
    if ($drawing.Height -le 700) {
      [int]$stripW = [math]::Max(20, [int]($drawing.Width * 0.18))
      foreach ($side in @('left', 'right')) {
        [int]$left = 0
        if ($side -eq 'right') { $left = $drawing.Width - $stripW }
        $strip = Crop-Drawing $drawing $left 0 $stripW $drawing.Height 8.0
        $sb = Get-SoftwareBitmapFromDrawing $strip
        $allLines += @(Ocr-SoftwareBitmap $engine $sb)
        $scales += @("strip-$side")
        $strip.Dispose()
      }
    }

    # 4) 四角裁剪放大（覆盖角落小标签：新剧/热剧/完结/独播等红标）
    $cw = [int]($drawing.Width * 0.45)
    $ch = [int]($drawing.Height * 0.45)
    $corners = @(
      @{ l = 0;            t = 0;            n = 'tl' },
      @{ l = $drawing.Width - $cw; t = 0;            n = 'tr' },
      @{ l = 0;            t = $drawing.Height - $ch; n = 'bl' },
      @{ l = $drawing.Width - $cw; t = $drawing.Height - $ch; n = 'br' }
    )
    foreach ($c in $corners) {
      $crop = Crop-Drawing $drawing $c.l $c.t $cw $ch 3.0
      $sb = Get-SoftwareBitmapFromDrawing $crop
      $allLines += @(Ocr-SoftwareBitmap $engine $sb)
      $scales += @("corner-$($c.n)")
      $crop.Dispose()
    }
  }
  $drawing.Dispose()

  # 合并去重（按文本，保留首次出现）
  $seen = @{}
  $merged = @()
  foreach ($l in $allLines) {
    $key = $l.text
    if ($seen.ContainsKey($key)) { continue }
    $seen[$key] = $true
    $merged += $l
  }
  $lines = @()
  $idx = 0
  foreach ($l in $merged) {
    $idx++
    $lines += @{ line = $idx; text = $l.text; words = $l.words }
  }
  $out = @{ ok = $true; lines = $lines; scales = $scales } | ConvertTo-Json -Depth 5 -Compress
  Write-Host $out
} catch {
  Write-Host ('{"ok":false,"message":"' + ($_.Exception.Message -replace '"', '\"') + '"}')
  exit 1
}
