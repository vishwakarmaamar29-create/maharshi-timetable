@echo off
rem STREAMING_CHUNK:Creating a robust batch script for Windows environments...
echo ===================================================
echo   Starting Intelligent School Timetable Generator
echo ===================================================
echo.
echo Launching your dashboard in your web browser...
echo (Please do not close this black window while using the app)
echo.

rem We use "python -m streamlit" because it automatically bypasses the broken Windows shortcut errors.
python -m streamlit run app.py

pause