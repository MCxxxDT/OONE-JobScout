@echo off
setlocal
chcp 65001 >nul 2>&1
set "OONE_ROOT=%~dp0"
:: Keep cmd's working directory outside the tree being removed.
cd /d "%TEMP%"
where py >nul 2>&1
if not errorlevel 1 (
    py -3 -B "%OONE_ROOT%boss_apply\uninstall.py" --root "%OONE_ROOT%." %*
    goto done
)
where python >nul 2>&1
if not errorlevel 1 (
    python -B "%OONE_ROOT%boss_apply\uninstall.py" --root "%OONE_ROOT%." %*
    goto done
)
if exist "%OONE_ROOT%.venv\Scripts\python.exe" (
    "%OONE_ROOT%.venv\Scripts\python.exe" -B "%OONE_ROOT%boss_apply\uninstall.py" --root "%OONE_ROOT%." %*
    goto done
)
echo [失败] 找不到 Python 3.10+。请用已有系统 Python 运行卸载脚本。
exit /b 1
:done
set "OONE_RESULT=%ERRORLEVEL%"
echo.
pause
exit /b %OONE_RESULT%
