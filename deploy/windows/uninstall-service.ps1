<#
.SYNOPSIS
    卸载 ComfyUI 工作台 Windows 服务。

.DESCRIPTION
    只停服务、移除注册，不动 backend\ 下的数据库和素材 —— 卸载服务不等于删数据。
    需要重新安装时再跑 install-service.ps1。

.EXAMPLE
    .\uninstall-service.ps1
#>
[CmdletBinding()]
param(
    [string]$ServiceName = "ComfyUI-Workbench",
    [string]$NssmPath = ""
)

$ErrorActionPreference = "Stop"

$svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if (-not $svc) {
    Write-Host "服务 $ServiceName 不存在，无需卸载。"
    exit 0
}

if (-not $NssmPath) {
    $found = Get-Command nssm -ErrorAction SilentlyContinue
    if ($found) { $NssmPath = $found.Source }
}

Write-Host "停止并移除服务 $ServiceName ..."
if ($NssmPath -and (Test-Path $NssmPath)) {
    # 优先用 nssm remove：它会连同自己写的注册表项一起清掉
    & $NssmPath stop $ServiceName | Out-Null
    & $NssmPath remove $ServiceName confirm | Out-Null
}
else {
    Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
    # 没有 nssm 时退回 sc.exe；服务本体仍会被删除，残留的 nssm 参数无害
    & sc.exe delete $ServiceName | Out-Null
}

Write-Host "已卸载。数据库与素材仍在 backend\ 下，未删除。"
