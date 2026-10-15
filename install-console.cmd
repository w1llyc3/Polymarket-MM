@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo NBA MM 本地操作台：一次性安装。无需钱包、私钥或赔率 key。
if exist ".venv\Scripts\python.exe" goto dependencies
py -3.12 -m venv .venv
if errorlevel 1 goto failed
:dependencies
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto failed
echo 安装完成。今后双击 start-console.cmd 即可。
pause
exit /b 0
:failed
echo 安装失败。请安装 Python 3.12 并启用 Python Launcher，检查网络及本目录写入权限，再重试。
echo 未启动操作台或任何账户写者。错误详情见上方输出。
pause
exit /b 1
