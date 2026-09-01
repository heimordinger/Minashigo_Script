@echo off

setlocal

cd /d "%~dp0.."

set MINASHIGO_DEMO=1

echo.

echo [run_demo] 演示模式

echo   - 示例账号：编队-01 / 02 / 03

echo   - 不读写 json/accounts.json

echo   - 浏览器数据：browser_data_demo/

echo.

python main.py

endlocal

