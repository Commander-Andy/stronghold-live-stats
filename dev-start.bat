@echo off
REM Startet die App direkt aus dem Quellcode (kein Build noetig) - zeigt
REM also immer den aktuellen Stand von app/, im Gegensatz zur gebauten
REM .exe unter dist/, die einen Schnappschuss vom letzten Build zeigt.
REM Nur zum Testen/Entwickeln gedacht, nicht fuer den echten Stream-Einsatz.

cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo Python wurde nicht gefunden. Ist es installiert und im PATH?
    pause
    exit /b 1
)

python app\main.py

echo.
echo App wurde beendet.
pause
