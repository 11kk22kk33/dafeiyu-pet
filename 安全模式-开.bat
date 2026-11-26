@echo off
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "D:\pet-shared\set-mode.ps1" -Mode safe
ping -n 4 127.0.0.1 >nul
