@echo off
call conda activate live

:run_live_recorder
cls
python "live_recorder.py"
if errorlevel 1 goto :error
goto :run_live_recorder

:error
echo An error occurred.
pause
