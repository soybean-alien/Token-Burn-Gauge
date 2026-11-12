# Token Burn Gauge by soybean_alien. Draws the app icon: the Quotalis "Gauge Q" mark in black with red accents.
# Writes assets/TokenGauge.ico, assets/TokenGauge-256.png and the installer wizard images (installer/wizard-*.bmp).
# Runs in Windows PowerShell (WPF): powershell -STA -ExecutionPolicy Bypass -File tools/make-icon.ps1
param(
    [string]$AssetsDir = "$PSScriptRoot\..\assets",
    [string]$InstallerDir = "$PSScriptRoot\..\installer"
)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName PresentationFramework, PresentationCore, WindowsBase, System.Xaml

$graphite = '#050505'

# The icon on a 256-unit canvas. $small: no glow or highlight, thicker ring and needle for 32 px and below.
function Get-IconXaml([bool]$small, [string]$background = 'Transparent') {
    $c = if ($small) { 120 } else { 122 }
    $r = if ($small) { 66 } else { 62 }
    $ring = if ($small) { 34 } else { 26 }
    $arc = if ($small) { 30 } else { 22 }
    $needle = if ($small) { 30 } else { 20 }
    # The gauge runs clockwise from 12 o'clock through 268 degrees, ending just past 9 o'clock.
    $end = 268 - 90
    $ex = [Math]::Round($c + $r * [Math]::Cos($end * [Math]::PI / 180), 2)
    $ey = [Math]::Round($c + $r * [Math]::Sin($end * [Math]::PI / 180), 2)
    $gauge = "M$c,$($c - $r) A$r,$r 0 1 1 $ex,$ey"
    $n0 = if ($small) { 154 } else { 150 }; $n1 = if ($small) { 204 } else { 196 }
    $mint = "<LinearGradientBrush StartPoint='0,0' EndPoint='1,1'><GradientStop Offset='0' Color='#FFD2CC'/><GradientStop Offset='0.45' Color='#FF5A4F'/><GradientStop Offset='1' Color='#A3121B'/></LinearGradientBrush>"
    $extras = if ($small) { '' } else { @"
  <Path Data='$gauge' StrokeThickness='26' StrokeStartLineCap='Round' StrokeEndLineCap='Round' Stroke='#FF5A4F' Opacity='0.55'>
    <Path.Effect><BlurEffect Radius='14'/></Path.Effect>
  </Path>
"@ }
    $shine = if ($small) { '' } else { @"
  <Path Data='M86,84 A62,62 0 0 1 158,76' StrokeThickness='6' StrokeStartLineCap='Round' StrokeEndLineCap='Round' Opacity='0.8'>
    <Path.Stroke><LinearGradientBrush StartPoint='0,0' EndPoint='0,1'><GradientStop Offset='0' Color='#BFFFFFFF'/><GradientStop Offset='1' Color='#00FFFFFF'/></LinearGradientBrush></Path.Stroke>
  </Path>
  <Ellipse Canvas.Left='191' Canvas.Top='191' Width='10' Height='10' Fill='#B3FFFFFF'/>
"@ }
    $tile = if ($small) { "<Rectangle Canvas.Left='4' Canvas.Top='4' Width='248' Height='248' RadiusX='56' RadiusY='56'" } else { "<Rectangle Canvas.Left='12' Canvas.Top='12' Width='232' Height='232' RadiusX='56' RadiusY='56'" }
    @"
<Canvas xmlns='http://schemas.microsoft.com/winfx/2006/xaml/presentation' Width='256' Height='256' Background='$background'>
  $tile>
    <Rectangle.Fill><LinearGradientBrush StartPoint='0,0' EndPoint='0,1'><GradientStop Offset='0' Color='#1A1A1A'/><GradientStop Offset='1' Color='$graphite'/></LinearGradientBrush></Rectangle.Fill>
  </Rectangle>
  $tile>
    <Rectangle.Fill><RadialGradientBrush Center='0.5,0.55' GradientOrigin='0.5,0.55' RadiusX='0.6' RadiusY='0.6'><GradientStop Offset='0' Color='#47E03A3A'/><GradientStop Offset='1' Color='#00E03A3A'/></RadialGradientBrush></Rectangle.Fill>
    <Rectangle.Stroke><LinearGradientBrush StartPoint='0,0' EndPoint='0,1'><GradientStop Offset='0' Color='#38FFFFFF'/><GradientStop Offset='0.5' Color='#0AFFFFFF'/><GradientStop Offset='1' Color='#4D000000'/></LinearGradientBrush></Rectangle.Stroke>
  </Rectangle>
  <Path StrokeThickness='$ring'>
    <Path.Data><EllipseGeometry Center='$c,$c' RadiusX='$r' RadiusY='$r'/></Path.Data>
    <Path.Stroke><LinearGradientBrush StartPoint='0,0' EndPoint='0,1'><GradientStop Offset='0' Color='#57FFFFFF'/><GradientStop Offset='1' Color='#14FFFFFF'/></LinearGradientBrush></Path.Stroke>
  </Path>
$extras  <Path Data='$gauge' StrokeThickness='$arc' StrokeStartLineCap='Round' StrokeEndLineCap='Round'><Path.Stroke>$mint</Path.Stroke></Path>
  <Path Data='M$n0,$n0 L$n1,$n1' StrokeThickness='$needle' StrokeStartLineCap='Round' StrokeEndLineCap='Round'>
    <Path.Stroke><LinearGradientBrush StartPoint='0,0' EndPoint='1,1'><GradientStop Offset='0' Color='#F4F7F5'/><GradientStop Offset='1' Color='#8D9A93'/></LinearGradientBrush></Path.Stroke>
  </Path>
$shine</Canvas>
"@
}

function Render-Element($element, [int]$width, [int]$height) {
    $element.Measure([Windows.Size]::new($width, $height))
    $element.Arrange([Windows.Rect]::new(0, 0, $width, $height))
    $element.UpdateLayout()
    $bitmap = New-Object Windows.Media.Imaging.RenderTargetBitmap $width, $height, 96, 96, ([Windows.Media.PixelFormats]::Pbgra32)
    $bitmap.Render($element)
    $bitmap
}

function Get-Icon([int]$size, [string]$background = 'Transparent') {
    $box = New-Object Windows.Controls.Viewbox
    $box.Width = $size; $box.Height = $size
    $box.Child = [Windows.Markup.XamlReader]::Parse((Get-IconXaml ($size -le 32) $background))
    Render-Element $box $size $size
}

function Save-Image($bitmap, [string]$path, [string]$format) {
    $encoder = if ($format -eq 'bmp') { New-Object Windows.Media.Imaging.BmpBitmapEncoder } else { New-Object Windows.Media.Imaging.PngBitmapEncoder }
    $source = if ($format -eq 'bmp') { New-Object Windows.Media.Imaging.FormatConvertedBitmap $bitmap, ([Windows.Media.PixelFormats]::Bgr24), $null, 0 } else { $bitmap }
    $encoder.Frames.Add([Windows.Media.Imaging.BitmapFrame]::Create($source))
    $stream = [IO.File]::Create($path)
    try { $encoder.Save($stream) } finally { $stream.Dispose() }
}

function Get-PngBytes($bitmap) {
    $encoder = New-Object Windows.Media.Imaging.PngBitmapEncoder
    $encoder.Frames.Add([Windows.Media.Imaging.BitmapFrame]::Create($bitmap))
    $stream = New-Object IO.MemoryStream
    $encoder.Save($stream)
    , $stream.ToArray()
}

# Application icon: PNG frames in one .ico.
$sizes = 16, 20, 24, 32, 40, 48, 64, 128, 256
$frames = foreach ($size in $sizes) { , @($size, (Get-PngBytes (Get-Icon $size))) }
$out = New-Object IO.MemoryStream
$writer = New-Object IO.BinaryWriter $out
$writer.Write([UInt16]0); $writer.Write([UInt16]1); $writer.Write([UInt16]$frames.Count)
$offset = 6 + 16 * $frames.Count
foreach ($frame in $frames) {
    $size = $frame[0]; $data = $frame[1]
    $b = if ($size -ge 256) { 0 } else { $size }
    $writer.Write([byte]$b); $writer.Write([byte]$b); $writer.Write([byte]0); $writer.Write([byte]0)
    $writer.Write([UInt16]1); $writer.Write([UInt16]32)
    $writer.Write([UInt32]$data.Length); $writer.Write([UInt32]$offset)
    $offset += $data.Length
}
foreach ($frame in $frames) { $writer.Write([byte[]]$frame[1]) }
$writer.Flush()
[IO.File]::WriteAllBytes((Join-Path $AssetsDir 'TokenGauge.ico'), $out.ToArray())
Save-Image (Get-Icon 256) (Join-Path $AssetsDir 'TokenGauge-256.png') 'png'

# Installer wizard: the tall welcome panel (graphite, icon centred) and the small header image (icon on white),
# each at 100 % and 200 % scale.
foreach ($scale in 1, 2) {
    $w = 164 * $scale; $h = 314 * $scale; $icon = 112 * $scale
    $panel = New-Object Windows.Controls.Canvas
    $panel.Width = $w; $panel.Height = $h; $panel.Background = [Windows.Media.BrushConverter]::new().ConvertFrom($graphite)
    $box = New-Object Windows.Controls.Viewbox
    $box.Width = $icon; $box.Height = $icon
    $box.Child = [Windows.Markup.XamlReader]::Parse((Get-IconXaml $false))
    [Windows.Controls.Canvas]::SetLeft($box, ($w - $icon) / 2); [Windows.Controls.Canvas]::SetTop($box, ($h - $icon) / 2 - 20 * $scale)
    $panel.Children.Add($box) | Out-Null
    $suffix = if ($scale -eq 1) { '' } else { '-200' }
    Save-Image (Render-Element $panel $w $h) (Join-Path $InstallerDir "wizard-large$suffix.bmp") 'bmp'
    Save-Image (Get-Icon (55 * $scale) 'White') (Join-Path $InstallerDir "wizard-small$suffix.bmp") 'bmp'
}

Write-Output "TokenGauge icon written to $AssetsDir; wizard images to $InstallerDir"
