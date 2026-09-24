@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion
cd /d "%~dp0"
title Выпуск новой версии

set "PY=venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

for /f "delims=" %%v in ('%PY% -c "from version import VERSION; print(VERSION)"') do set CUR=%%v
echo.
echo   Текущая версия: %CUR%
set NEW=%1
if "!NEW!"=="" set /p NEW="  Новая версия (например 2.1.1): "
if "!NEW!"=="" exit /b 1

findstr /b /c:"## !NEW!" CHANGELOG.md >nul
if errorlevel 1 (
    echo.
    echo   Добавьте в CHANGELOG.md раздел "## !NEW! — дата" со списком изменений.
    echo   Файл сейчас откроется. Сохраните, закройте блокнот — продолжим.
    notepad CHANGELOG.md
    findstr /b /c:"## !NEW!" CHANGELOG.md >nul
    if errorlevel 1 (
        echo   Раздела "## !NEW!" так и нет — выпуск отменён.
        pause
        exit /b 1
    )
)

%PY% tools\bump_version.py !NEW!
if errorlevel 1 (
    pause
    exit /b 1
)

rem --- автотесты и выпуск версий для GitHub Actions ---
if exist "github-workflows\*.yml" (
    if not exist ".github\workflows" mkdir ".github\workflows"
    copy /y "github-workflows\*.yml" ".github\workflows\" >nul
)
git add -A
git commit -m "Версия !NEW!"
git tag "v!NEW!"
git push
git push origin "v!NEW!"
if errorlevel 1 (
    echo   [!] Не удалось отправить на GitHub.
    pause
    exit /b 1
)
echo.
echo   Версия !NEW! отправлена. GitHub прогонит тесты и создаст релиз,
echo   Railway сам перевыложит бота, а в Telegram придёт уведомление.
pause
