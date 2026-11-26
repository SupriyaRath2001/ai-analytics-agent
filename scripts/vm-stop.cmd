@echo off
rem Stops the analytics-agent VM. Double-click this file, or run it in a terminal.
rem Extra options go to vm.ps1, for example: vm-stop.cmd -Zone us-central1-a
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0vm.ps1" -Action stop %*
pause
