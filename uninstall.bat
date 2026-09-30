@echo off
setlocal
chcp 65001 >nul 2>&1
set "OONE_ROOT=%~dp0"
:: Keep cmd's working directory outside the tree being removed.
cd /d "%TEMP%"
where py >nul 2>&1
if not errorlevel 1 (
    py -3 -B "%OONE_ROOT%boss_apply\uninstall.py" --root "%OONE_ROOT%." %*
    exit /b
)
where python >nul 2>&1
if not errorlevel 1 (
    python -B "%OONE_ROOT%boss_apply\uninstall.py" --root "%OONE_ROOT%." %*
    exit /b
)
if exist "%OONE_ROOT%.venv\Scripts\python.exe" (
    for /f "usebackq delims=" %%P in (`"%OONE_ROOT%.venv\Scripts\python.exe" -c "import sys; print(sys._base_executable)"`) do set "OONE_BASE_PY=%%P"
    goto base_python

)
echo [失败] 找不到 Python 3.10+。请用已有系统 Python 运行卸载脚本。
exit /b 1
:base_python
if defined OONE_BASE_PY (
    "%OONE_BASE_PY%" -B "%OONE_ROOT%boss_apply\uninstall.py" --root "%OONE_ROOT%." %*
    exit /b
)
echo [失败] 无法定位安装目录外的基础 Python。
exit /b 1
