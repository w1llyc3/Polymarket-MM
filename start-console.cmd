@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" goto missing
".venv\Scripts\python.exe" -m nba_recorder.console
if errorlevel 1 goto failed
exit /b 0
:missing
echo 请先双击 install-console.cmd 完成一次性安装。
pause
exit /b 1
:failed
echo 操作台启动或运行失败。请根据上方中文提示检查端口、网络、权限与账本；不要假定撤单成功。
pause
exit /b 1
