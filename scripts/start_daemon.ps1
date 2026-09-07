<#
.SYNOPSIS
  BOSS直聘消息巡检后台守护与 Chrome 自愈保活启动脚本。
.DESCRIPTION
  1. 自检 Chrome CDP 9335 端口：若未拉起，自动带专用 Profile 与调试参数拉起 Chrome；
  2. 探测登录与 CDP 通信可用性；
  3. 拉起 daemon_auto_reply.py 执行后台巡检守护；
  4. 支持 Windows 开机自启 / 计划任务调用。
.EXAMPLE
  .\scripts\start_daemon.ps1 -Loop
  .\scripts\start_daemon.ps1 -Once -DryRun
#>

param(
    [switch]$Once,
    [switch]$Loop,
    [switch]$DryRun,
    [string]$ActiveHours = "09:30-20:30",
    [double]$IntervalMin = 15.0,
    [double]$IntervalMax = 25.0
)

$ErrorActionPreference = "Continue"

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

    $chromeArgs = @(
        "--remote-debugging-port=9335",
        "--user-data-dir=$profilePath",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        "--remote-allow-origins=*",
        "--start-maximized",
        "https://www.zhipin.com/web/geek/chat"
    )

    Start-Process -FilePath $chromePath -ArgumentList $chromeArgs
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

# 3. 启动守护脚本
$daemonArgs = @("scripts/daemon_auto_reply.py")

if ($Once) {
    $daemonArgs += "--once"
} elseif ($Loop -or (-not $Once)) {
    $daemonArgs += "--loop"
}

if ($DryRun) {
    $daemonArgs += "--dry-run"
}

$daemonArgs += @(
    "--active-hours", $ActiveHours,
    "--interval-min", $IntervalMin.ToString(),
    "--interval-max", $IntervalMax.ToString()
)

Write-Host "`n[守护启动] 正在执行: python $($daemonArgs -join ' ')" -ForegroundColor Cyan
& python @daemonArgs
