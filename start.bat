@echo off
:: Переходим в папку, где лежит сам .bat файл
cd /d "%~dp0"

:: Проверяем, существует ли папка venv
if not exist "venv\Scripts\activate.bat" (
    echo [ОШИБКА] Папка venv не найдена.
    echo Сначала создайте её командой: python -m venv venv
    pause
    exit /b
)

:: Активируем виртуальное окружение
call venv\Scripts\activate.bat

:: Очищаем экран от лишнего мусора
cls

:: Выводим подсказку
echo working

:: Запускаем командную строку в этом же окне
cmd /k