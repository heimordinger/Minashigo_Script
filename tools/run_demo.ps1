$ErrorActionPreference = "Stop"

Set-Location (Join-Path $PSScriptRoot "..")

$env:MINASHIGO_DEMO = "1"

Write-Host ""

Write-Host "[run_demo] 演示模式"

Write-Host "  - 示例账号：编队-01 / 02 / 03"

Write-Host "  - 不读写 json/accounts.json"

Write-Host "  - 浏览器数据：browser_data_demo/"

Write-Host ""

python main.py

