<#
.SYNOPSIS
    ComfyUI 工作台服务日常操作：启停、看状态、看日志、备份。

.DESCRIPTION
    status 会同时打 /api/ready —— 服务在跑不等于能用（数据库只读、存储卷
    挂错、前端产物缺失都会让 ready 返回 503，而服务进程本身活着）。

.EXAMPLE
    .\manage.ps1 status
    .\manage.ps1 restart
    .\manage.ps1 logs -Tail 100
    .\manage.ps1 backup
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet("start", "stop", "restart", "status", "logs", "backup")]
    [string]$Action,

    [string]$ServiceName = "ComfyUI-Workbench",
    [int]$Port = 8000,
    [int]$Tail = 40
)

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$Backend = Join-Path $RepoRoot "backend"
$LogDir = Join-Path $RepoRoot "logs"

function Get-Python {
    $venvPy = Join-Path $Backend ".venv\Scripts\python.exe"
    if (Test-Path $venvPy) { return $venvPy }
    $found = Get-Command python -ErrorAction SilentlyContinue
    if ($found) { return $found.Source }
    throw "找不到 python.exe"
}

switch ($Action) {
    "start" {
        Start-Service -Name $ServiceName
        Write-Host "已启动 $ServiceName"
    }
    "stop" {
        Stop-Service -Name $ServiceName
        Write-Host "已停止 $ServiceName"
    }
    "restart" {
        Restart-Service -Name $ServiceName
        Write-Host "已重启 $ServiceName"
    }
    "status" {
        $svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
        if (-not $svc) {
            Write-Warning "服务 $ServiceName 未注册"
            exit 1
        }
        Write-Host "服务状态：$($svc.Status) / 启动类型：$($svc.StartType)"

        try {
            $r = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/api/ready" -TimeoutSec 5 -UseBasicParsing
            Write-Host "就绪检查：HTTP $($r.StatusCode)"
            Write-Host $r.Content
        }
        catch {
            $resp = $_.Exception.Response
            if ($resp) {
                $reader = New-Object System.IO.StreamReader($resp.GetResponseStream())
                Write-Warning "就绪检查失败：HTTP $([int]$resp.StatusCode)"
                Write-Warning $reader.ReadToEnd()
            }
            else {
                Write-Warning "连不上 http://127.0.0.1:$Port —— $($_.Exception.Message)"
            }
        }
    }
    "logs" {
        $out = Join-Path $LogDir "service.out.log"
        $err = Join-Path $LogDir "service.err.log"
        foreach ($file in @($out, $err)) {
            if (Test-Path $file) {
                Write-Host "===== $file（最后 $Tail 行）====="
                Get-Content -Path $file -Tail $Tail
            }
        }
    }
    "backup" {
        $python = Get-Python
        & $python (Join-Path $Backend "scripts\backup.py") --keep 7
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }
}
