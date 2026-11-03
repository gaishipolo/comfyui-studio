<#
.SYNOPSIS
    把 ComfyUI 工作台注册成 Windows 服务：开机自启、崩溃自动拉起、日志落盘。

.DESCRIPTION
    需要 NSSM 作为服务包装器。原因：Windows 服务要求进程实现服务控制协议
    （响应 SCM 的 start/stop 请求），直接注册 python.exe 会报
    「服务没有响应控制功能」。NSSM 就是干这层的。
    下载 https://nssm.cc/download ，把 win64\nssm.exe 放进 PATH 或用 -NssmPath 指定。

    注册后工作目录是 backend\（相对路径基准），所以 .env、workflows、storage
    都会按 backend 下的位置解析，与手工启动完全一致。

.EXAMPLE
    .\install-service.ps1

.EXAMPLE
    .\install-service.ps1 -Port 8080 -ServiceName ComfyUI-Workbench -NssmPath C:\tools\nssm.exe
#>
[CmdletBinding()]
param(
    [string]$ServiceName = "ComfyUI-Workbench",
    [int]$Port = 8000,
    [string]$Python = "",
    [string]$NssmPath = ""
)

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$Backend = Join-Path $RepoRoot "backend"
$Dist = Join-Path $RepoRoot "frontend\dist"
$LogDir = Join-Path $RepoRoot "logs"

# ── 前置检查：宁可现在报清楚，也别等服务装好启动失败再回来查 ──
if (-not (Test-Path (Join-Path $Backend "app\main.py"))) {
    throw "找不到后端入口：$Backend\app\main.py"
}
if (-not (Test-Path (Join-Path $Dist "index.html"))) {
    throw "前端未构建：$Dist\index.html 不存在。先在 frontend\ 执行 npm ci; npm run build"
}
if (-not (Test-Path (Join-Path $Backend ".env"))) {
    Write-Warning "backend\.env 不存在。SECRET_KEY 未配置时，生产模式会拒绝启动（这是故意的）。"
}

if (-not $Python) {
    $venvPy = Join-Path $Backend ".venv\Scripts\python.exe"
    if (Test-Path $venvPy) {
        $Python = $venvPy
    }
    else {
        $found = Get-Command python -ErrorAction SilentlyContinue
        if ($found) { $Python = $found.Source }
    }
}
if (-not $Python -or -not (Test-Path $Python)) {
    throw "找不到 python.exe。用 -Python 指定解释器路径。"
}

if (-not $NssmPath) {
    $found = Get-Command nssm -ErrorAction SilentlyContinue
    if ($found) { $NssmPath = $found.Source }
}
if (-not $NssmPath -or -not (Test-Path $NssmPath)) {
    throw "找不到 nssm.exe。从 https://nssm.cc/download 下载，把 win64\nssm.exe 放进 PATH，或用 -NssmPath 指定。"
}

if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
    throw "服务 $ServiceName 已存在。先运行 .\uninstall-service.ps1，或用 -ServiceName 换一个名字。"
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

# --proxy-headers：放在反代后面时还原真实 IP，否则登录限速按 IP 锁定会失效
$appArgs = "-m uvicorn app.main:app --host 0.0.0.0 --port $Port --proxy-headers --forwarded-allow-ips *"

Write-Host "注册服务 $ServiceName ..."
& $NssmPath install $ServiceName $Python $appArgs | Out-Null
& $NssmPath set $ServiceName AppDirectory $Backend | Out-Null
& $NssmPath set $ServiceName DisplayName "ComfyUI Workbench" | Out-Null
& $NssmPath set $ServiceName Description "ComfyUI 生成工作台（单进程，API + 前端托管）" | Out-Null
& $NssmPath set $ServiceName Start SERVICE_AUTO_START | Out-Null

# 日志交给 nssm 滚动，避免长期运行把磁盘写满
& $NssmPath set $ServiceName AppStdout (Join-Path $LogDir "service.out.log") | Out-Null
& $NssmPath set $ServiceName AppStderr (Join-Path $LogDir "service.err.log") | Out-Null
& $NssmPath set $ServiceName AppRotateFiles 1 | Out-Null
& $NssmPath set $ServiceName AppRotateOnline 1 | Out-Null
& $NssmPath set $ServiceName AppRotateBytes 10485760 | Out-Null

# 崩溃 5 秒后自动重启。任务在重启时会被收割：有引擎任务号的接回继续跑，
# 没提交成功的标成可重试（见 task_service.reap_stale_tasks）
& $NssmPath set $ServiceName AppExit Default Restart | Out-Null
& $NssmPath set $ServiceName AppRestartDelay 5000 | Out-Null

# 生产模式：关键配置缺失时拒绝启动，而不是带着临时密钥跑（重启全员掉线）
& $NssmPath set $ServiceName AppEnvironmentExtra "APP_ENV=production" | Out-Null

Start-Service -Name $ServiceName
Start-Sleep -Seconds 3

$svc = Get-Service -Name $ServiceName
if ($svc.Status -ne "Running") {
    Write-Warning "服务已注册但未进入 Running 状态。检查日志：$LogDir\service.err.log"
}
else {
    Write-Host "服务已启动：http://127.0.0.1:$Port"
    Write-Host "健康检查：http://127.0.0.1:$Port/api/health"
    Write-Host "就绪检查：http://127.0.0.1:$Port/api/ready"
}
