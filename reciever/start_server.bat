@echo off
setlocal
cd /d "%~dp0"

rem Both programs inherit this folder and your Windows user environment variables.
rem hvac_plot.py is imported by slack_app.py; it is not a background service.
for %%F in (server.py slack_app.py hvac_plot.py) do (
    if not exist "%%F" (
        echo Missing file: %%F
        echo Put this launcher beside server.py, slack_app.py and hvac_plot.py.
        goto failed
    )
)

python -c "import sys; print('Python:', sys.executable)"
if errorlevel 1 (
    echo Python is unavailable. Check your Python installation and PATH.
    goto failed
)

python -c "import slack_bolt, matplotlib"
if errorlevel 1 (
    echo Install dependencies first:
    echo python -m pip install slack-bolt matplotlib
    goto failed
)

if not defined SLACK_BOT_TOKEN (
    echo Missing Windows user environment variable: SLACK_BOT_TOKEN
    goto failed
)
if not defined SLACK_APP_TOKEN (
    echo Missing Windows user environment variable: SLACK_APP_TOKEN
    goto failed
)
if not defined HVAC_SLACK_WEBHOOK_URL (
    echo Missing Windows user environment variable: HVAC_SLACK_WEBHOOK_URL
    goto failed
)

rem Prepare the shared schema before Slack starts, avoiding a startup race.
python -c "import server; server.init_database(); server.read_settings(); print('Database ready.')"
if errorlevel 1 goto failed

start "HVAC Receiver" /D "%~dp0" "%ComSpec%" /d /k python -u server.py
if errorlevel 1 goto failed
start "HVAC Slack App" /D "%~dp0" "%ComSpec%" /d /k python -u slack_app.py
if errorlevel 1 goto failed
exit /b 0

:failed
echo.
echo Startup did not complete. See the message above.
pause
exit /b 1
