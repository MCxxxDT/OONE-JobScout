<#
.SYNOPSIS
  BOSS直聘消息巡检后台守护与 Chrome 自愈保活启动脚本。
.DESCRIPTION
  1. 自检 Chrome CDP 9335 端口：若未拉起，自动带专用 Profile 与调试参数拉起 Chrome；
  2. 探测登录与 CDP 通信可用性；
  3. 拉起 daemon_auto_reply.py 执行后台巡检守护（崩溃自动重启：异常退出退避 60s 后重启）；
  4. 全程日志落盘 state/daemon.log（已 gitignore）；
  5. 轮询间隔：不显式传 -IntervalMin/-IntervalMax 时由 config.json 的 daemon 段决定（默认 3-5 分钟）。
  6. 开机自启（可选，需管理员 PowerShell 手动执行一次，本脚本不自动改动系统）：
     Register-ScheduledTask -TaskName "boss-apply-daemon" -Trigger (New-ScheduledTaskTrigger -AtLogOn) `
       -Action (New-ScheduledTaskAction -Execute "powershell.exe" `
         -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$PSScriptRoot\start_daemon.ps1`" -Loop") `
       -Settings (New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5))
.EXAMPLE
  .\scripts\start_daemon.ps1 -Loop
  .\scripts\start_daemon.ps1 -Once -DryRun
  .\scripts\start_daemon.ps1 -Loop -IntervalMin 15 -IntervalMax 25   # 显式覆盖 config
#>

param(
    [switch]$Once,
    [switch]$Loop,
    [switch]$DryRun,
    [switch]$Silent,
    [string]$ActiveHours = "",
    [double]$IntervalMin = 0.0,
    [double]$IntervalMax = 0.0
)

$ErrorActionPreference = "Continue"

# 控制台输出统一 UTF-8：daemon 进程输出为 UTF-8，若按系统默认 GBK 解码，
# Tee-Object 落盘的 daemon.log 中文会变乱码（2026-09-09 detached 拉起实测复现）
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host "  BOSS直聘 自动守护与自愈启动器" -ForegroundColor Cyan
Write-Host "  工作区: $RepoRoot" -ForegroundColor Gray
Write-Host "==========================================================" -ForegroundColor Cyan

# 1. 检测 Chrome 9335 端口
$cdpUrl = "http://127.0.0.1:9335/json/version"
$chromeAlive = $false

try {
    $resp = Invoke-RestMethod -Uri $cdpUrl -TimeoutSec 2 -ErrorAction Stop
    if ($resp.Browser) {
        $chromeAlive = $true
        Write-Host "[自检通过] 调试 Chrome 已在运行中 (9335端口): $($resp.Browser)" -ForegroundColor Green
    }
} catch {
    $chromeAlive = $false
}

# 2. 若 Chrome 未启动，自动拉起
if (-not $chromeAlive) {
    Write-Host "[自检告警] 端口 9335 未就绪，正在自动拉起专有 Chrome 实例..." -ForegroundColor Yellow
    $chromePath = "C:\Program Files\Google\Chrome\Application\chrome.exe"
    $profilePath = "C:\Users\LENOVO\chrome-cdp-profile"

    if (-not (Test-Path $chromePath)) {
        Write-Host "[错误] 未找到 Chrome 路径: $chromePath" -ForegroundColor Red
        exit 1
    }

    # 读取配置确认是否使用最小化/静默模式
    $isSilent = $Silent
    try {
        $cfgJsonPath = Join-Path $RepoRoot "config.json"
        $localCfgJsonPath = Join-Path $RepoRoot "config.local.json"
        $bMode = $null
        if (Test-Path $localCfgJsonPath) {
            $lData = Get-Content $localCfgJsonPath -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($lData.browser) { $bMode = $lData.browser }
        }
        if (-not $bMode -and (Test-Path $cfgJsonPath)) {
            $gData = Get-Content $cfgJsonPath -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($gData.browser) { $bMode = $gData.browser }
        }
        if ($bMode -and ($bMode.minimize_on_start -or $bMode.silent_mode)) {
            $isSilent = $true
        }
    } catch {}

    $windowArg = if ($isSilent) { "--start-minimized" } else { "--start-maximized" }
    $procWindowStyle = if ($isSilent) { "Minimized" } else { "Normal" }

    $chromeArgs = @(
        "--remote-debugging-port=9335",
        "--user-data-dir=$profilePath",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        "--remote-allow-origins=*",
        $windowArg,
        "https://www.zhipin.com/web/geek/chat"
    )

    Start-Process -FilePath $chromePath -ArgumentList $chromeArgs -WindowStyle $procWindowStyle
    if ($isSilent) {
        Write-Host "[静默模式] 浏览器已在后台最小化启动，避免抢占焦点与弹窗遮挡。" -ForegroundColor Magenta
    }
    Write-Host "[拉起中] 等待 Chrome 9335 CDP 接口响应..." -ForegroundColor Cyan

    $maxWait = 15
    $waited = 0
    while ($waited -lt $maxWait) {
        Start-Sleep -Seconds 2
        $waited += 2
        try {
            $resp = Invoke-RestMethod -Uri $cdpUrl -TimeoutSec 2 -ErrorAction Stop
            if ($resp.Browser) {
                $chromeAlive = $true
                Write-Host "[拉起成功] Chrome 9335 已正常响应: $($resp.Browser)" -ForegroundColor Green
                break
            }
        } catch {}
    }

    if (-not $chromeAlive) {
        Write-Host "[警告] Chrome 9335 仍未响应，请检查端口是否被占用或手动确认浏览器。" -ForegroundColor Red
    }
}

# 3. 启动守护脚本（仅显式传参时透传，否则由 config.json daemon 段决定：
#    active_hours / max_age_hours / soft_close_hard_limit / interval）
# -u：关闭 stdout 块缓冲（管道下默认块缓冲会导致 daemon.log 延迟成块落盘，台账为准但日志滞后）
$daemonArgs = @("-u", "scripts/daemon_auto_reply.py")

if ($Once) {
    $daemonArgs += "--once"
} elseif ($Loop -or (-not $Once)) {
    $daemonArgs += "--loop"
}

if ($DryRun) {
    $daemonArgs += "--dry-run"
}

if (-not [string]::IsNullOrWhiteSpace($ActiveHours)) {
    $daemonArgs += @("--active-hours", $ActiveHours)
}
if ($IntervalMin -gt 0) {
    $daemonArgs += @("--interval-min", $IntervalMin.ToString())
}
if ($IntervalMax -gt 0) {
    $daemonArgs += @("--interval-max", $IntervalMax.ToString())
}

# 4. 日志落盘 state/daemon.log（已 gitignore），控制台与文件同步留存
$logDir = Join-Path $RepoRoot "state"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$logPath = Join-Path $logDir "daemon.log"

Write-Host "`n[守护启动] 正在执行: python $($daemonArgs -join ' ')" -ForegroundColor Cyan
Write-Host "[守护日志] $logPath（控制台与文件双写）" -ForegroundColor Gray

if ($Once) {
    # 单次模式：直接执行，输出双写日志
    & python @daemonArgs 2>&1 | Tee-Object -FilePath $logPath -Append
} else {
    # 常驻模式：崩溃自动重启（正常退出码 0 不重启；异常退出退避 60 秒后重启并记日志）
    $restartCount = 0
    while ($true) {
        $startTime = Get-Date
        & python @daemonArgs 2>&1 | Tee-Object -FilePath $logPath -Append
        $exitCode = $LASTEXITCODE
        if ($exitCode -eq 0) {
            Write-Host "[守护退出] daemon 正常退出（exit 0），不重启。" -ForegroundColor Yellow
            break
        }
        $restartCount++
        $uptime = (Get-Date) - $startTime
        Write-Host "[守护重启] daemon 异常退出（exit $exitCode，运行 $($uptime.ToString('hh\:mm\:ss'))），60 秒后自动重启（第 $restartCount 次）..." -ForegroundColor Red
        "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') [RESTART] exit=$exitCode uptime=$($uptime.ToString('hh\:mm\:ss')) count=$restartCount" | Tee-Object -FilePath $logPath -Append
        Start-Sleep -Seconds 60
    }
}
