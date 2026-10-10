# VideoTrX 一键安装（Windows x64）。
# 所有工具都装在项目目录内；可重复运行，已存在的项跳过，yt-dlp 每次升级。
param([switch]$Elevated)

$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$FfmpegBranch = '9.0'
$UvRelease = 'https://github.com/astral-sh/uv/releases/latest/download'
$FfmpegRelease = 'https://github.com/BtbN/FFmpeg-Builds/releases/download/latest'
$DenoRelease = 'https://github.com/denoland/deno/releases/latest/download'
$SpacyModel = 'https://github.com/explosion/spacy-models/releases/download/en_core_web_md-3.7.1/en_core_web_md-3.7.1-py3-none-any.whl'

function Exit-Script([int]$Code) {
    if ($Elevated) { Read-Host '按回车键退出' | Out-Null }
    exit $Code
}

function Fail([string]$Message) {
    Write-Host "安装失败：$Message" -ForegroundColor Red
    Exit-Script 1
}

function Get-File([string]$Url, [string]$OutFile) {
    Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $OutFile -ErrorAction Stop
}

$arch = if ($env:PROCESSOR_ARCHITEW6432) { $env:PROCESSOR_ARCHITEW6432 } else { $env:PROCESSOR_ARCHITECTURE }
if ($arch -ne 'AMD64') {
    Write-Host "暂不支持此平台：$arch"
    Exit-Script 1
}

# 提权：项目目录不可写时，以管理员身份重新运行本脚本
$Root = $PSScriptRoot
$probe = Join-Path $Root ('.write-test-' + [guid]::NewGuid().ToString('N'))
try {
    [IO.File]::WriteAllText($probe, '')
    Remove-Item -LiteralPath $probe -Force
} catch {
    if ($Elevated) { Fail '以管理员身份运行仍无法写入项目目录' }
    Write-Host '项目目录不可写，正在以管理员身份重新运行安装脚本'
    try {
        Start-Process powershell -Verb RunAs -ErrorAction Stop -ArgumentList @(
            '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"", '-Elevated'
        )
    } catch {
        Fail '未获得管理员权限，无法写入项目目录'
    }
    exit 0
}

# 1. 切换到脚本所在目录
Set-Location -LiteralPath $Root

$BinDir = Join-Path $Root 'bin'
$TmpDir = Join-Path $Root 'tools\.download'
try {
    if (Test-Path -LiteralPath $TmpDir) { Remove-Item -LiteralPath $TmpDir -Recurse -Force -ErrorAction Stop }
    New-Item -ItemType Directory -Force -Path $TmpDir, $BinDir -ErrorAction Stop | Out-Null
} catch {
    Fail "无法在项目目录中创建文件夹：$($_.Exception.Message)"
}

# 2. uv
$Uv = Join-Path $Root 'tools\uv\uv.exe'
if (Test-Path -LiteralPath $Uv) {
    Write-Host '==> uv 已存在，跳过'
} else {
    Write-Host '==> 下载 uv'
    try {
        $zip = Join-Path $TmpDir 'uv.zip'
        Get-File "$UvRelease/uv-x86_64-pc-windows-msvc.zip" $zip
        Expand-Archive -LiteralPath $zip -DestinationPath (Join-Path $Root 'tools\uv') -Force -ErrorAction Stop
    } catch {
        Fail "下载或解压 uv 失败：$($_.Exception.Message)"
    }
}

# 3. Python 与虚拟环境
$env:UV_PYTHON_INSTALL_DIR = Join-Path $Root 'tools\python'
if (Test-Path -LiteralPath '.venv') {
    Write-Host '==> .venv 已存在，跳过'
} else {
    Write-Host '==> 安装 Python 3.12 并创建 .venv'
    & $Uv venv .venv --python 3.12 --python-preference only-managed --seed
    if ($LASTEXITCODE -ne 0) { Fail '安装 Python 或创建虚拟环境失败' }
}

# 4. Python 依赖
Write-Host '==> 安装 Python 依赖'
& $Uv pip install --python .venv -r requirements.txt
if ($LASTEXITCODE -ne 0) { Fail '安装 Python 依赖失败' }
Write-Host '==> 升级 yt-dlp'
& $Uv pip install --python .venv --upgrade-package yt-dlp --upgrade-package yt-dlp-ejs 'yt-dlp[default]'
if ($LASTEXITCODE -ne 0) { Fail '升级 yt-dlp 失败' }
Write-Host '==> 安装 spaCy 英文模型'
& $Uv pip install --python .venv $SpacyModel
if ($LASTEXITCODE -ne 0) { Fail '安装 spaCy 英文模型失败' }

# 5. ffmpeg / ffprobe
$Ffmpeg = Join-Path $BinDir 'ffmpeg.exe'
$Ffprobe = Join-Path $BinDir 'ffprobe.exe'
if ((Test-Path -LiteralPath $Ffmpeg) -and (Test-Path -LiteralPath $Ffprobe)) {
    Write-Host '==> ffmpeg 已存在，跳过'
} else {
    Write-Host "==> 下载 ffmpeg $FfmpegBranch"
    $name = "ffmpeg-n$FfmpegBranch-latest-win64-gpl-$FfmpegBranch"
    try {
        $zip = Join-Path $TmpDir 'ffmpeg.zip'
        Get-File "$FfmpegRelease/$name.zip" $zip
        $extract = Join-Path $TmpDir 'ffmpeg'
        Expand-Archive -LiteralPath $zip -DestinationPath $extract -Force -ErrorAction Stop
        foreach ($exe in 'ffmpeg.exe', 'ffprobe.exe') {
            Copy-Item -LiteralPath (Join-Path $extract "$name\bin\$exe") -Destination $BinDir -Force -ErrorAction Stop
        }
    } catch {
        Fail "下载或解压 ffmpeg 失败：$($_.Exception.Message)"
    }
}

# 6. deno
if (Test-Path -LiteralPath (Join-Path $BinDir 'deno.exe')) {
    Write-Host '==> deno 已存在，跳过'
} else {
    Write-Host '==> 下载 deno'
    try {
        $zip = Join-Path $TmpDir 'deno.zip'
        Get-File "$DenoRelease/deno-x86_64-pc-windows-msvc.zip" $zip
        Expand-Archive -LiteralPath $zip -DestinationPath $BinDir -Force -ErrorAction Stop
    } catch {
        Fail "下载或解压 deno 失败：$($_.Exception.Message)"
    }
}
Remove-Item -LiteralPath $TmpDir -Recurse -Force -ErrorAction SilentlyContinue

# 7. 配置文件
if (Test-Path -LiteralPath 'config.yaml') {
    Write-Host '==> config.yaml 已存在，保留不动'
} else {
    try {
        Copy-Item -LiteralPath 'config.example.yaml' -Destination 'config.yaml' -ErrorAction Stop
    } catch {
        Fail '生成 config.yaml 失败'
    }
    Write-Host '==> 已从 config.example.yaml 生成 config.yaml'
    Write-Host '请在 config.yaml 中填写 api、polish、mai 的 key' -ForegroundColor Yellow
}

# 8. 启动方式
Write-Host ''
Write-Host '安装完成。启动：双击 start.bat' -ForegroundColor Green
Exit-Script 0
