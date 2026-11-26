@echo off
rem Shows if the analytics-agent VM is running. Double-click this file, or run it in a terminal.
rem Extra options go to vm.ps1, for example: vm-status.cmd -Zone us-central1-a
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0vm.ps1" -Action status %*
pause
