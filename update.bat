@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Обновление бота

echo.
echo   Обновление KOSell x Starvell
echo   ============================
echo   Сначала закройте окно с работающим ботом.
echo.

where git >nul 2>&1
if errorlevel 1 goto nogit
if not exist ".git" goto nogit

echo   Скачиваю новую версию с GitHub...
git pull --ff-only
if errorlevel 1 (
    echo.
    echo   [!] Не получилось обновиться автоматически.
    echo       Скорее всего, файлы бота меняли вручную. Настройки в папке
    echo       storage не затронуты. Пришлите текст выше — разберёмся.
    pause
    exit /b 1
)
goto deps

:nogit
echo   [!] Эта папка не связана с GitHub.
echo       Запустите setup_github.bat или скачайте архив новой версии
echo       со страницы Releases репозитория и распакуйте поверх
echo       (папку storage не трогайте — там настройки).
pause
exit /b 1

:deps
if exist "venv\Scripts\python.exe" (
    echo   Обновляю библиотеки...
    "venv\Scripts\python.exe" -m pip install -r requirements.txt --quiet
)
for /f "delims=" %%v in ('venv\Scripts\python.exe -c "from version import VERSION; print(VERSION)" 2^>nul') do set VER=%%v
echo.
echo   Готово. Версия: %VER%
echo   Запустите start.bat.
echo.
pause
