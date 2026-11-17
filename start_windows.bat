@echo off
REM Double-click to start the app. First run installs the requirements into a local .venv folder.
cd /d "%~dp0"
if not exist .venv (
    echo Setting up Python environment, one moment...
    python -m venv .venv || (echo Python 3.10+ is required: https://www.python.org/downloads/ & pause & exit /b 1)
    .venv\Scripts\python -m pip install --upgrade pip
    .venv\Scripts\python -m pip install -r requirements.txt
)
.venv\Scripts\python -m streamlit run app.py
pause
