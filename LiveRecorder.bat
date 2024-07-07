@echo off
call conda activate live

:run_live_recorder
cls
python "live_recorder.py"
goto :run_live_recorder
