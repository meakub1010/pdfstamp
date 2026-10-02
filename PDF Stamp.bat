@echo off
REM Double-click to open the PDF Stamp menu.
cd /d "%~dp0"
python pdfstamp.py
if errorlevel 1 pause
