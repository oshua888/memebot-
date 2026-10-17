@echo off
REM Lance le bot SIMULÉ automatiquement chaque minute (Planificateur de tâches).
REM Désinstaller : installer_windows.bat remove
set DIR=%~dp0
if /I "%1"=="remove" (
  schtasks /Delete /TN "MemeScanner" /F
  echo Agent desinstalle.
  goto :eof
)
schtasks /Create /TN "MemeScanner" /SC MINUTE /MO 1 /F ^
  /TR "cmd /c cd /d \"%DIR%\" && python memescanner.py bot --cycles 1 >> bot.log 2>&1"
echo Agent installe : il tourne chaque minute. Suivi : ouvre bot.log
pause
