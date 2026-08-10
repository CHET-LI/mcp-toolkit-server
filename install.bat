@echo off
setlocal
title MCP Toolkit Server - Installer

echo ==================================================
echo   MCP Toolkit Server - One-click Installer
echo ==================================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python not found.
    echo Please install Python 3.12+ from https://www.python.org/downloads/
    echo and make sure "Add Python to PATH" is checked during install.
    pause
    exit /b 1
)

echo [1/2] Creating virtual environment...
python -m venv .venv
if errorlevel 1 (
    echo [ERROR] Failed to create virtual environment.
    pause
    exit /b 1
)

echo [2/2] Installing dependency: mcp...
call ".venv\Scripts\activate.bat"
pip install --quiet mcp==1.29.0
if errorlevel 1 (
    echo [ERROR] pip install failed. Check your network and retry.
    pause
    exit /b 1
)

echo.
echo ==================================================
echo   Installation complete!
echo ==================================================
echo.
echo   Start the server with:
echo       .venv\Scripts\python.exe server.py
echo.
echo   Or run the end-to-end test:
echo       .venv\Scripts\python.exe test_client.py
echo.
echo   To register it in your MCP client, point the
echo   command to: .venv\Scripts\python.exe server.py
echo.
pause
