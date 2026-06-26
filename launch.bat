@echo off
rem Launcher for the Hospital Price Explorer - API version (Windows).
rem Double-click this file to start a local server and open the viewer.
rem Requires Python 3 installed and on PATH.

cd /d "%~dp0api"

set PORT=8765

echo ================================================
echo  Hospital Price Explorer - API Viewer
echo ================================================
echo.
echo Starting local server on http://localhost:%PORT%
echo.
echo The viewer will open in your browser shortly.
echo Keep this window open while using the viewer.
echo.
echo To STOP: press Ctrl+C or close this window.
echo ================================================
echo.

rem Open browser after a 2-second delay
start /min cmd /c "timeout /t 2 >nul & start http://localhost:%PORT%"

rem Start the server in the foreground
python -m http.server %PORT%
