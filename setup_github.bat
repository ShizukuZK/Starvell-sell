@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion
cd /d "%~dp0"
title Подключение к GitHub

echo.
echo   Подключение бота к GitHub
echo   =========================
echo.

where git >nul 2>&1
if errorlevel 1 (
    echo   [!] Git не установлен.
    echo       Скачайте: https://git-scm.com/download/win
    echo       Установите с настройками по умолчанию и запустите этот файл снова.
    start "" https://git-scm.com/download/win
    pause
    exit /b 1
)

echo   1. Откройте https://github.com/new
echo   2. Имя — например kosell-starvell-bot, обязательно Private,
echo      галочки README / .gitignore / license НЕ ставьте.
echo   3. Нажмите Create repository и скопируйте ссылку вида
echo      https://github.com/ВАШ_НИК/kosell-starvell-bot.git
echo.
start "" https://github.com/new
set /p REPO="  Вставьте ссылку на репозиторий: "
if "!REPO!"=="" (
    echo   Ссылка не введена.
    pause
    exit /b 1
)

git config user.name >nul 2>&1
if errorlevel 1 (
    set /p GNAME="  Ваш ник на GitHub: "
    git config --global user.name "!GNAME!"
)
git config user.email >nul 2>&1
if errorlevel 1 (
    set /p GMAIL="  Почта от GitHub-аккаунта: "
    git config --global user.email "!GMAIL!"
)

if not exist ".git" git init -b main >nul
rem --- автотесты и выпуск версий для GitHub Actions ---
if exist "github-workflows\*.yml" (
    if not exist ".github\workflows" mkdir ".github\workflows"
    copy /y "github-workflows\*.yml" ".github\workflows\" >nul
)
git add -A

rem --- страховка: настройки с ключами не должны попасть в репозиторий ---
set LEAK=
for /f "delims=" %%f in ('git diff --cached --name-only ^| findstr /b /i "storage/ data/ .env venv/"') do set LEAK=%%f
if defined LEAK (
    echo   [!] В репозиторий попадает !LEAK! — это секретные данные. Остановлено.
    git reset >nul
    pause
    exit /b 1
)

git commit -m "Первая версия бота" >nul 2>&1
git branch -M main
git remote remove origin >nul 2>&1
git remote add origin "!REPO!"

echo.
echo   Отправляю код. Если откроется окно входа GitHub — войдите в браузере.
git push -u origin main
if errorlevel 1 (
    echo.
    echo   [!] Не удалось отправить. Проверьте ссылку и что репозиторий пустой.
    pause
    exit /b 1
)
echo.
echo   Готово! Код на GitHub, папка storage с ключами осталась только у вас.
echo   Дальше: update.bat — обновиться, release.bat — выпустить версию.
pause
