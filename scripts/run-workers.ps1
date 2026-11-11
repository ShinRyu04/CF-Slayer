# 启动 N 个 cf-slayer worker（默认 2 个：8878、8879），共享同一个 SQLite 缓存。
#
#   powershell -ExecutionPolicy Bypass -File scripts/run-workers.ps1
#   powershell -ExecutionPolicy Bypass -File scripts/run-workers.ps1 -Count 3 -BasePort 8880
#
# 令牌取自 -Token、CF_SLAYER_AUTH_TOKEN 或 .env。加 -NoWait 只打印 PID 后退出，
# 进程继续运行，便于测试；否则脚本会等待所有 worker 退出。

param(
    [int]$Count = 2,
    [int]$BasePort = 8878,
    [string]$BindHost = "127.0.0.1",
    [string]$Python = ".venv/Scripts/python.exe",
    [string]$CachePath = "cache.sqlite3",
    [string]$Token = "",
    [switch]$NoWait
)

$ErrorActionPreference = "Stop"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not $Token) { $Token = $env:CF_SLAYER_AUTH_TOKEN }
if (-not $Token) {
    $envFile = Join-Path $root ".env"
    if (Test-Path $envFile) {
        $match = Select-String -Path $envFile -Pattern '^CF_SLAYER_AUTH_TOKEN=(.+)$' | Select-Object -First 1
        if ($match) { $Token = $match.Matches[0].Groups[1].Value.Trim() }
    }
}
if (-not $Token) {
    throw "缺少令牌：设置 CF_SLAYER_AUTH_TOKEN、传 -Token，或在 .env 中配置"
}

$pythonPath = Join-Path $root $Python
if (-not (Test-Path $pythonPath)) { throw "找不到 Python：$pythonPath" }

# 绝对路径，确保不同工作目录的 worker 写到同一个缓存文件。
if ([System.IO.Path]::IsPathRooted($CachePath)) {
    $cacheFile = [System.IO.Path]::GetFullPath($CachePath)
} else {
    $cacheFile = [System.IO.Path]::GetFullPath((Join-Path $root $CachePath))
}
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $cacheFile) | Out-Null
$logDir = Join-Path $root "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

$env:CF_SLAYER_AUTH_TOKEN = $Token
$env:CF_SLAYER_CACHE_PATH = $cacheFile

$started = @()
for ($i = 0; $i -lt $Count; $i++) {
    $port = $BasePort + $i
    $env:CF_SLAYER_PORT = "$port"
    $outLog = Join-Path $logDir "worker-$port.log"
    $errLog = Join-Path $logDir "worker-$port.err.log"
    $proc = Start-Process -FilePath $pythonPath `
        -ArgumentList @("-m", "uvicorn", "solver.main:app", "--host", $BindHost, "--port", "$port") `
        -WorkingDirectory $root `
        -RedirectStandardOutput $outLog `
        -RedirectStandardError $errLog `
        -PassThru
    $started += $proc
    Write-Output ("worker {0}: http://{1}:{2} pid={3} log={4}" -f ($i + 1), $BindHost, $port, $proc.Id, $outLog)
}

Write-Output ("缓存文件: {0}（WAL，多进程共享）" -f $cacheFile)
Write-Output ("令牌: 已设置（不打印）")

if ($NoWait) {
    Write-Output "已后台启动；用 Stop-Process -Id <pid> 结束。"
    return
}

Write-Output "等待 worker 退出（Ctrl+C 结束脚本；再单独结束下列 PID）。"
try {
    Wait-Process -Id ($started | ForEach-Object { $_.Id })
} finally {
    Write-Output "worker 已退出。"
}
