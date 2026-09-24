@echo off
chcp 65001 >nul
cd /d "%~dp0"
title KOSell x Starvell - автоаренда

echo.
echo   ============================================
echo     KOSell x Starvell — автоаренда аккаунтов
echo   ============================================
echo.

rem --- ищем Python ---
set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY (
    where python >nul 2>&1 && set "PY=python"
)
if not defined PY (
    echo   [!] Python не найден.
    echo.
    echo       Скачайте его с https://www.python.org/downloads/
    echo       и при установке обязательно отметьте галочку
    echo       "Add Python to PATH", затем запустите этот файл снова.
    echo.
    pause
    exit /b 1
)

rem --- создаём окружение при первом запуске ---
if not exist "venv\Scripts\python.exe" (
    echo   Первый запуск: готовлю окружение, это займёт минуту...
    echo.
    %PY% -m venv venv
    if errorlevel 1 (
        echo   [!] Не удалось создать виртуальное окружение.
        pause
        exit /b 1
    )
    "venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
    echo   Устанавливаю библиотеки...
    "venv\Scripts\python.exe" -m pip install -r requirements.txt --quiet
    if errorlevel 1 (
        echo   [!] Не удалось установить библиотеки. Проверьте интернет.
        pause
        exit /b 1
    )
    echo   Готово.
    echo.
)

rem --- страховка: библиотеки могли не доустановиться ---
"venv\Scripts\python.exe" -c "import aiogram, aiohttp" >nul 2>&1
if errorlevel 1 (
    echo   Доустанавливаю библиотеки...
    "venv\Scripts\python.exe" -m pip install -r requirements.txt --quiet
)

echo   Запускаю бота. Закрыть — Ctrl+C или крестик окна.
echo.
"venv\Scripts\python.exe" main.py

echo.
echo   Бот остановлен.
pause
