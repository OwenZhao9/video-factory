@echo off
chcp 65001 >nul
title 视频工厂
cd /d "%~dp0"

py -3 --version >nul 2>nul
if %errorlevel%==0 goto runpy

python --version >nul 2>nul
if %errorlevel%==0 goto runpython

echo.
echo  [提示] 没检测到 Python，需要先安装（只需一次，免费）：
echo    1. 即将自动打开下载页，点击黄色 Download Python 按钮下载安装
echo    2. 安装第一步务必勾选底部的 "Add python.exe to PATH"
echo    3. 安装完成后，重新双击本文件即可
echo.
start https://www.python.org/downloads/
pause
exit /b

:runpy
py -3 webapp.py
goto end

:runpython
python webapp.py
goto end

:end
echo.
echo 视频工厂已退出，可以关闭本窗口。
pause