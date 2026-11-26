@echo off
set PATH=D:\AstrBot\bin;%PATH%
rem Start the whole stack (proxy -> SnowLuma -> AstrBot -> QQ -> pet)
rem each step checks the port/process first and skips if already running
set PYD=D:\AstrBot\AstrBot_4.29.1_windows_amd64_portable\backend\python
set AB=D:\AstrBot\AstrBot_4.29.1_windows_amd64_portable\backend\app

netstat -ano | findstr ":8787" | findstr "LISTENING" >nul
if errorlevel 1 (
  start "" /b "%PYD%\pythonw.exe" "D:\pet-shared\proxy.py"
) else (
  echo [skip] proxy already running
)
ping -n 3 127.0.0.1 >nul

netstat -ano | findstr ":3000" | findstr "LISTENING" >nul
if errorlevel 1 (
  pushd D:\SnowLuma
  start "" /b wscript.exe "D:\pet-shared\run-hidden.vbs" "D:\SnowLuma\node.exe" "index.mjs" "D:\SnowLuma"
  popd
) else (
  echo [skip] SnowLuma already running
)
ping -n 5 127.0.0.1 >nul

netstat -ano | findstr ":6185" | findstr "LISTENING" >nul
if errorlevel 1 (
  pushd "%AB%"
  start "" /b "%PYD%\python.exe" main.py
  popd
) else (
  echo [skip] AstrBot already running
)
ping -n 7 127.0.0.1 >nul

tasklist /FI "IMAGENAME eq QQ.exe" | findstr /I "QQ.exe" >nul
if errorlevel 1 (
  start "" "D:\qq\QQ.exe"
) else (
  echo [skip] QQ already running
)
ping -n 6 127.0.0.1 >nul

rem Keep-alive watchdog: checks 8787 every 15s and restarts proxy if down.
rem Single-instance (127.0.0.1:8798), runs via pythonw.exe with no window.
start "" /b "%PYD%\pythonw.exe" "D:\pet-shared\proxy_watchdog.pyw"
