@echo off
setlocal
set "URL=http://127.0.0.1:8787/console"
set "PYD=D:\AstrBot\AstrBot_4.29.1_windows_amd64_portable\backend\python"
set "EDGE=C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
netstat -ano | findstr ":8787" | findstr "LISTENING" >nul
if errorlevel 1 (
  start "" /b "%PYD%\pythonw.exe" "D:\pet-shared\proxy.py"
  ping -n 4 127.0.0.1 >nul
)
if not exist "%EDGE%" set "EDGE=C:\Program Files\Microsoft\Edge\Application\msedge.exe"
start "" "%EDGE%" "%URL%"
rem Keep-alive watchdog (single instance, hidden via pythonw.exe)
start "" /b "%PYD%\pythonw.exe" "D:\pet-shared\proxy_watchdog.pyw"
endlocal
