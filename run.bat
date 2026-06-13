@echo off
setlocal EnableDelayedExpansion
title EquityIntel -- Full Stack

set "ROOT=C:\Users\noleg\Desktop\Claude\Projects\Stocks"
set "PORTFOLIO_DIR=C:\Users\noleg\Desktop\Claude\Projects\AI Portfolio"
set "SOLO_INTEL=C:\Users\noleg\Desktop\Claude\Projects\SOLO BUILDS\solo-intel"
set "PODCASTS_ANALYSIS=C:\Users\noleg\Desktop\Claude\Projects\Podcasts Pull\analysis"
set "PYTHON=C:\Users\noleg\Desktop\.venv\Scripts\python.exe"
set "PYTHONW=C:\Users\noleg\Desktop\.venv\Scripts\pythonw.exe"
set "SCRIPTS=C:\Users\noleg\Desktop\.venv\Scripts"
set "SOLO_DOMAIN=US equity catalyst intelligence from financial podcasts. Focus on which US stocks are getting attention, investment theses forming around specific tickers, risks and warning signals, and actionable signals for a US equities watchlist."

cd /d "%ROOT%"

echo.
echo  ============================================================
echo   EquityIntel  ^|  Full Stack  ^|  %date%  %time%
echo  ============================================================
echo.
echo   Signal streams:
echo     1. SEC filings + XBRL + prices  (steps 1-8)
echo     2. Trade signals + rebalance    (step 9)
echo     3. Podcast intelligence         (step 10)
echo     4. Local news via OpenAI       (step 11)
echo     5. o4-mini synthesis            (step 12)
echo.
echo   Paper trading: ALPACA_PAPER=true
echo   To go live: set ALPACA_PAPER=false in .env  (confirm first)
echo.

:: Step 0 -- Dashboard (background)
echo  [0] Starting dashboard in background...
taskkill /f /im equity-dashboard.exe >nul 2>&1
taskkill /f /im pythonw.exe >nul 2>&1
for /f "tokens=5" %%P in ('netstat -ano 2^>nul ^| findstr /r ":5173 "') do (
    taskkill /f /pid %%P >nul 2>&1
)
timeout /t 3 /nobreak >nul
"%PYTHON%" -c "import subprocess; p=subprocess.Popen([r'%SCRIPTS%\equity-dashboard.exe','--no-open','--port','5173'],creationflags=0x08000000,close_fds=True); open(r'%TEMP%\dash_pid.txt','w').write(str(p.pid))"
timeout /t 3 /nobreak >nul
for /f "usebackq" %%i in ("%TEMP%\dash_pid.txt") do set "DASH_PID=%%i"
del "%TEMP%\dash_pid.txt" >nul 2>&1
echo         http://localhost:5173  (PID !DASH_PID!)
echo.

:: Step 0b -- Database migrations
echo  [0b] Running database migrations...
"%PYTHON%" -m alembic upgrade head
if errorlevel 1 echo         WARNING: migration step had issues, continuing...
echo.

:: 12-hour ingestion cache -- uses Python to avoid PowerShell (blocked on this machine)
:: Delete pipeline_last_run.txt to force a full re-run
set "SKIP_PIPELINE=0"
if exist "pipeline_last_run.txt" (
    "%PYTHON%" -c "import os,time,sys; age=(time.time()-os.path.getmtime('pipeline_last_run.txt'))/3600; sys.exit(0 if age.__lt__(12) else 1)"
    if not errorlevel 1 set "SKIP_PIPELINE=1"
)

if "!SKIP_PIPELINE!"=="1" (
    echo  Pipeline data is fresh ^(under 12 h old^) -- skipping ingestion workers.
    echo  Delete pipeline_last_run.txt to force a full re-run.
    echo.
    goto :POST_PIPELINE
)

:: Step 1 -- Companies
echo  [1/8] Syncing companies (ticker to CIK mapping)...
"%SCRIPTS%\equity-sync-companies.exe"
if errorlevel 1 echo         WARNING: sync-companies had errors, continuing...
echo.

:: Step 2 -- Filings
echo  [2/8] Syncing recent SEC filings...
"%SCRIPTS%\equity-sync-filings.exe"
if errorlevel 1 echo         WARNING: sync-filings had errors, continuing...
echo.

:: Step 3 -- Filing documents
echo  [3/8] Downloading and parsing filing documents...
"%SCRIPTS%\equity-sync-docs.exe"
if errorlevel 1 echo         WARNING: sync-docs had errors, continuing...
echo.

:: Step 4 -- XBRL facts
echo  [4/8] Syncing XBRL company facts...
"%SCRIPTS%\equity-sync-facts.exe"
if errorlevel 1 echo         WARNING: sync-facts had errors, continuing...
echo.

:: Step 5 -- News
echo  [5/8] Syncing news articles...
"%SCRIPTS%\equity-sync-news.exe"
if errorlevel 1 echo         WARNING: sync-news had errors, continuing...
echo.

:: Step 6 -- Prices
echo  [6/8] Syncing price and volume data...
"%SCRIPTS%\equity-sync-prices.exe"
if errorlevel 1 echo         WARNING: sync-prices had errors, continuing...
echo.

:: Step 7 -- Events
echo  [7/8] Building catalyst events...
"%SCRIPTS%\equity-build-events.exe"
if errorlevel 1 echo         WARNING: build-events had errors, continuing...
echo.

:: Step 8 -- Cluster + dedup
echo  [8/8] Clustering and deduplicating events...
"%SCRIPTS%\equity-cluster-events.exe"
if errorlevel 1 echo         WARNING: cluster-events had errors, continuing...
echo.

:: Stamp cache (plain echo -- no PowerShell needed)
echo %DATE% %TIME% > pipeline_last_run.txt
echo  Ingestion complete. Cache stamped -- will skip in next 12 h.
echo  (Delete pipeline_last_run.txt to force a full re-run.)
echo.

:POST_PIPELINE

:: Step 8b -- News cleanup
echo  [8b] Cleaning up news older than 60 days...
"%SCRIPTS%\equity-cleanup-news.exe" --days 60
if errorlevel 1 echo         NOTE: news-cleanup had errors (non-critical)
echo.

:: Step 8c -- Daily briefs
echo  [8c] Generating 7-day catalyst brief...
"%SCRIPTS%\equity-run-daily-brief.exe"
if errorlevel 1 echo         WARNING: run-daily-brief failed (synthesis will use API fallback)
echo.

echo  [8d] Generating today-only brief (last 24 h)...
"%SCRIPTS%\equity-run-daily-brief.exe" --days 1 --output-dir "%ROOT%\briefs\today"
if errorlevel 1 echo         NOTE: today-only brief had no catalysts (quiet 24 h is normal)
echo.

:: Step 9 -- Trade signals + auto-rebalance (paper)
echo  [9a] Generating trade signals from catalyst brief...
"%SCRIPTS%\equity-generate-trade-signals.exe"
if errorlevel 1 echo         WARNING: generate-trade-signals had errors, continuing...
echo.

echo  [9b] Executing trade signals (paper)...
"%SCRIPTS%\equity-execute-trade-signals.exe"
if errorlevel 1 echo         WARNING: execute-trade-signals had errors, continuing...
echo.

echo  [9c] Auto-rebalancing portfolio (paper -- loss-protected, cash-directed)...
"%SCRIPTS%\equity-auto-rebalance.exe"
if errorlevel 1 echo         WARNING: auto-rebalance had errors, continuing...
echo.

:: Step 10 -- Podcast intelligence
echo  [10] Running solo-intel against Podcasts Pull...
set "_PIPELINE_LLM_PROVIDER=%LLM_PROVIDER%"
set "_PIPELINE_LMSTUDIO_MODEL=%LMSTUDIO_MODEL%"
set "_PIPELINE_OPENAI_MODEL=%OPENAI_MODEL%"
set "_PIPELINE_LLM_TIMEOUT=%LLM_TOKEN_IDLE_TIMEOUT_SECONDS%"
set "_PIPELINE_MAX_OUTPUT=%LLM_MAX_OUTPUT_TOKENS%"
set "LLM_PROVIDER=openai"
set "OPENAI_MODEL=gpt-4o-mini"
set "LLM_TOKEN_IDLE_TIMEOUT_SECONDS=120"
set "LLM_MAX_OUTPUT_TOKENS=1800"
set "SOLO_INTEL_MAX_INPUT_CHARS="
set "PYTHONUNBUFFERED=1"
"%PYTHON%" -u "%SOLO_INTEL%\solo.py" ^
  --folder "%PODCASTS_ANALYSIS%" ^
  --name "Podcasts Pull - Equity" ^
  --domain "%SOLO_DOMAIN%" ^
  --days 30
if errorlevel 1 echo         WARNING: solo-intel had errors (OpenAI synthesis failed)
set "LLM_PROVIDER=%_PIPELINE_LLM_PROVIDER%"
set "LMSTUDIO_MODEL=%_PIPELINE_LMSTUDIO_MODEL%"
set "OPENAI_MODEL=%_PIPELINE_OPENAI_MODEL%"
set "LLM_TOKEN_IDLE_TIMEOUT_SECONDS=%_PIPELINE_LLM_TIMEOUT%"
set "LLM_MAX_OUTPUT_TOKENS=%_PIPELINE_MAX_OUTPUT%"
set "PYTHONUNBUFFERED="
set "SOLO_INTEL_MAX_INPUT_CHARS="
set "_PIPELINE_LLM_PROVIDER="
set "_PIPELINE_LMSTUDIO_MODEL="
set "_PIPELINE_OPENAI_MODEL="
set "_PIPELINE_LLM_TIMEOUT="
set "_PIPELINE_MAX_OUTPUT="
echo.

:: Step 11 -- Local news summary via OpenAI
echo  [11] Summarizing local news via OpenAI...
"%PYTHON%" "%ROOT%\gemini_news.py"
if errorlevel 1 echo         WARNING: local news summary had errors (check OPENAI_API_KEY and news DB)
echo.

:: Step 11b -- Synthesize 24 h news blocks
echo  [11b] Synthesizing 24 h news blocks for My Views tab...
"%SCRIPTS%\equity-synthesize-news-blocks.exe" --hours 24 --blocks 8
if errorlevel 1 echo         NOTE: news-blocks synthesis failed (My Views will show diagnostic)
echo.

:: Step 12 -- o4-mini synthesis (SEC + podcasts + local news)
echo  [12] Running o4-mini synthesis (SEC + podcasts + local news)...
"%PYTHON%" "%ROOT%\synthesize.py"
if errorlevel 1 (
    echo.
    echo   ERROR: synthesize.py failed.
    echo   Intelligence output was not generated.
    echo.
)
if not exist "%ROOT%\intelligence" (
    echo.
    echo   WARNING: intelligence\ was not created. Check logs above.
    echo.
)
echo.

:: Open UIs
echo  ============================================================
echo   Pipeline complete. Opening dashboard and portfolio...
echo  ============================================================
echo.
start "" http://localhost:5173
start "" "%PORTFOLIO_DIR%\ai_portfolio.html"
echo.

echo  Window stays open while the dashboard runs.
echo  To stop: close this window or run  taskkill /f /im pythonw.exe
echo.

:monitor_dashboard
if not defined DASH_PID (
    echo  WARNING: Dashboard PID not captured. Open http://localhost:5173 manually.
    pause
    goto :done
)
tasklist /fi "pid eq !DASH_PID!" /fo csv /nh 2>nul | find /i "!DASH_PID!" >nul 2>&1
if errorlevel 1 goto :done
timeout /t 5 /nobreak >nul
goto :monitor_dashboard

:done
echo.
echo  Dashboard stopped. Closing.
endlocal
