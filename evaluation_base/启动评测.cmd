@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
if exist "evaluation_base\server.py" cd /d "%~dp0evaluation_base"
set "PYEXE="
where py >nul 2>nul
if not errorlevel 1 set "PYEXE=py -3"
if defined PYEXE goto havepy
where python >nul 2>nul
if not errorlevel 1 set "PYEXE=python"
:havepy
if not defined PYEXE goto nopy
%PYEXE% -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if errorlevel 1 goto nopy
%PYEXE% server.py student --open
goto end
:nopy
echo.
echo 没有找到可用的 Python（需要 3.10 或更新版本）。
echo 请先安装 Python，或联系组织者，然后重新双击本文件。
:end
echo.
pause
