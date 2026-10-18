# cfnb notification wrapper: run main.py, then push result to Telegram
# 通知链路（逐级降级，任一成功即止）:
#   1) config.json 里配置的代理 (Invoke-RestMethod http 形式)
#   2) 同一端口协议自适应 (curl socks5h:// -> http://)
#   3) 常见代理端口自动探测 (7890/7891/10808/10809/10810/10811/1080/1081/20171...)
#   4) 直连 (Invoke-RestMethod -> curl)
# 每轮结果写入 <脚本目录>\notify.log (保留最近300行)
param([switch]$TestOnly)

$ErrorActionPreference = 'Continue'
# 用脚本自身所在目录，项目搬家后无需改脚本
$root = $PSScriptRoot
if (-not $root) { $root = Split-Path -Parent $MyInvocation.MyCommand.Path }
if (-not $root) { $root = 'C:\cfnb' }
try { Set-Location -Path $root } catch { }
$logFile = Join-Path $root 'notify.log'
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)

function Resolve-PythonPath {
    $cmd = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source) { return $cmd.Source }
    foreach ($p in @('C:\Program Files\Python312\python.exe', 'C:\Program Files\Python311\python.exe')) {
        if (Test-Path $p) { return $p }
    }
    return 'python'
}

function Write-Log($msg) {
    $line = (Get-Date -Format 'MM-dd HH:mm:ss') + ' ' + $msg
    try { [System.IO.File]::AppendAllText($logFile, $line + "`r`n", $utf8NoBom) } catch { }
    try {
        $all = [System.IO.File]::ReadAllLines($logFile)
        if ($all.Count -gt 300) { [System.IO.File]::WriteAllLines($logFile, ($all | Select-Object -Last 300), $utf8NoBom) }
    } catch { }
}

$cfg = $null
try { $cfg = Get-Content (Join-Path $root 'config.json') -Raw -Encoding UTF8 | ConvertFrom-Json } catch { }

$startTime = Get-Date
if ($TestOnly) {
    $exit = 0
} else {
    $pyExe = Resolve-PythonPath
    # 用调用运算符而不是 Start-Process -NoNewWindow：
    # 后者在环境里同时存在 http_proxy / HTTP_PROXY 时会抛
    # "字典中的关键字重复" 而直接崩掉，导致 main.py 根本不执行。
    & $pyExe (Join-Path $root 'main.py')
    $exit = $LASTEXITCODE
    if ($null -eq $exit) { $exit = 0 }
}
$endTime = Get-Date

# 注意：Get-Content 读单行文件时返回的是 String 而不是数组，
# 直接 $lines[0] 会取到第一个「字符」（曾导致通知显示 "最快: 1"）。
$lines = @()
try { $lines = @(Get-Content (Join-Path $root 'ip.txt') -EA Stop | Where-Object { $_.Trim() -ne '' }) } catch { }
$cnt = $lines.Count
$top = 'n/a'
if ($cnt -gt 0) { $top = $lines[0] }
$elapsed = [int]($endTime - $startTime).TotalSeconds

# 读取本次运行的漏斗统计（main.py 写出），用于定位是哪一层塌了
$funnel = ''
$blocked = $false
try {
    $sumPath = Join-Path $root 'run_summary.json'
    if (Test-Path $sumPath) {
        $sum = Get-Content $sumPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $blocked = [bool]$sum.blocked
        $parts = @()
        foreach ($p in $sum.stages.PSObject.Properties) { $parts += ('{0} {1}' -f $p.Name, $p.Value) }
        if ($parts.Count -gt 0) { $funnel = ($parts -join ' -> ') }
    }
} catch { }

if ($TestOnly) {
    $text = "[cfnb] 通知链路测试消息 | " + $startTime.ToString('MM-dd HH:mm')
} elseif ($blocked) {
    $text = "[cfnb] 产出异常已拦截：本次低于安全阈值，已保留旧列表（" + $cnt + " 条）" + [char]10 +
        "漏斗: " + $funnel + [char]10 +
        "耗时 " + $elapsed + "s | " + $startTime.ToString('MM-dd HH:mm')
} elseif ($exit -eq 0 -and $cnt -gt 0) {
    $text = "[cfnb] 优选完成 " + $cnt + " 条" + [char]10 + "最快: " + $top + [char]10 +
        "耗时 " + $elapsed + "s | " + $startTime.ToString('MM-dd HH:mm')
    if ($funnel -ne '') { $text += [char]10 + "漏斗: " + $funnel }
} else {
    $text = "[cfnb] 运行失败 exit=" + $exit + " 节点数=" + $cnt + " | " + $startTime.ToString('MM-dd HH:mm')
    if ($funnel -ne '') { $text += [char]10 + "漏斗: " + $funnel }
}

$enabled = $false; $token = ''; $chat = ''; $cfgProxy = ''
try {
    $enabled  = [bool]$cfg.TELEGRAM_ENABLED
    $token    = [string]$cfg.TELEGRAM_BOT_TOKEN
    $chat     = [string]$cfg.TELEGRAM_CHAT_ID
    $cfgProxy = [string]$cfg.TELEGRAM_PROXY
} catch { }

if ($enabled -and $token -ne '' -and $token -notlike 'YOUR_*' -and $chat -ne '' -and $chat -notlike 'YOUR_*') {
    $uri = 'https://api.telegram.org/bot' + $token + '/sendMessage'
    $bodyJson = @{ chat_id = $chat; text = $text } | ConvertTo-Json -Compress
    $bodyFile = Join-Path $env:TEMP 'cfnb_tg_body.json'
    try { [System.IO.File]::WriteAllText($bodyFile, $bodyJson, $utf8NoBom) } catch { }
    $bodyFileCurl = $bodyFile -replace '\\', '/'

    function Try-Curl($proxyArg) {
        $args = @('-s', '-m', '15', '-o', 'NUL', '-w', '%{http_code}',
                  '-H', 'Content-Type: application/json',
                  '--data-binary', ('@' + $bodyFileCurl))
        if ($proxyArg -ne '') { $args += @('-x', $proxyArg) }
        $args += $uri
        try {
            $code = & curl.exe @args 2>$null
            return ($code -eq '200')
        } catch { return $false }
    }

    $sent = $false; $how = ''

    # 1) 配置的代理: Invoke-RestMethod (http形式)
    if (-not $sent -and $cfgProxy -ne '' -and $cfgProxy -notlike 'YOUR_*') {
        try {
            $r = Invoke-RestMethod -Uri $uri -Method Post -Body $bodyJson -ContentType 'application/json' -Proxy $cfgProxy -TimeoutSec 15
            if ($r.ok) { $sent = $true; $how = '配置代理(' + $cfgProxy + ')' }
        } catch { }
    }

    # 2) 同端口协议自适应: socks5h -> http (curl, 规避.NET代理协议限制)
    if (-not $sent -and $cfgProxy -ne '' -and $cfgProxy -notlike 'YOUR_*') {
        $m = [regex]::Match($cfgProxy, '^[a-z0-9]+://([^:]+):(\d+)$')
        if ($m.Success) {
            foreach ($sch in @('socks5h', 'http')) {
                if ($sent) { break }
                $px = $sch + '://' + $m.Groups[1].Value + ':' + $m.Groups[2].Value
                if (Try-Curl $px) { $sent = $true; $how = '协议自适应 ' + $px }
            }
        }
    }

    # 3) 常见代理端口自动探测 (配置失效/端口变更时)
    if (-not $sent) {
        foreach ($pt in @(7890, 7891, 10808, 10809, 10810, 10811, 1080, 1081, 20171, 33210, 4781)) {
            if ($sent) { break }
            $alive = $false
            try {
                $tcp = New-Object System.Net.Sockets.TcpClient
                $iar = $tcp.BeginConnect('127.0.0.1', $pt, $null, $null)
                $alive = $iar.AsyncWaitHandle.WaitOne(400, $false)
                $tcp.Close()
            } catch { }
            if (-not $alive) { continue }
            foreach ($sch in @('socks5h', 'http')) {
                $px = $sch + '://127.0.0.1:' + $pt
                if (Try-Curl $px) { $sent = $true; $how = '自动探测 ' + $px; break }
            }
        }
    }

    # 4) 直连兜底
    if (-not $sent) {
        try {
            $r = Invoke-RestMethod -Uri $uri -Method Post -Body $bodyJson -ContentType 'application/json' -TimeoutSec 15
            if ($r.ok) { $sent = $true; $how = '直连' }
        } catch { }
    }
    if (-not $sent) {
        if (Try-Curl '') { $sent = $true; $how = '直连(curl)' }
    }

    if ($sent) { Write-Log ('通知发送成功 [' + $how + ']: ' + ($text -replace "`r?`n", ' / ')) }
    else { Write-Log ('通知发送失败(所有通道均不可用): ' + ($text -replace "`r?`n", ' / ')) }
    try { Remove-Item $bodyFile -Force -EA SilentlyContinue } catch { }
} elseif (-not $enabled) {
    Write-Log 'Telegram 通知未启用(TELEGRAM_ENABLED=false)'
}
exit $exit
