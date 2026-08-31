# 在指定文件夹（及子文件夹）中查找所有 .json 文件
# 用法:
#   .\tools\find_local_json.ps1 "D:\Telegram导出\ChatExport_2026-08-24"
#   .\tools\find_local_json.ps1 "C:\Users\你的用户名\Downloads" -Keyword config

param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$Path,

    [string]$Keyword = "",

    [string]$OutCsv = ""
)

if (-not (Test-Path -LiteralPath $Path)) {
    Write-Error "路径不存在: $Path"
    exit 1
}

$files = Get-ChildItem -LiteralPath $Path -Recurse -File -Filter *.json -ErrorAction SilentlyContinue

if ($Keyword) {
    $files = $files | Where-Object { $_.Name -like "*$Keyword*" }
}

if (-not $files) {
    Write-Host "未找到 .json 文件。"
    exit 0
}

$rows = $files | Sort-Object LastWriteTime -Descending | ForEach-Object {
    [pscustomobject]@{
        Date     = $_.LastWriteTime.ToString("yyyy-MM-dd HH:mm:ss")
        SizeKB   = [math]::Round($_.Length / 1KB, 1)
        Name     = $_.Name
        FullPath = $_.FullName
    }
}

Write-Host "共找到 $($rows.Count) 个 .json 文件:`n"
$rows | Format-Table Date, SizeKB, Name, FullPath -AutoSize

if (-not $OutCsv) {
    $OutCsv = Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) "local_json_list_$(Get-Date -Format 'yyyyMMdd_HHmmss').csv"
}

$rows | Export-Csv -LiteralPath $OutCsv -NoTypeInformation -Encoding UTF8
Write-Host "`n已导出: $OutCsv"
