@echo off
setlocal EnableDelayedExpansion
title EquityIntel - Full Stack

:: ─── Paths ────────────────────────────────────────────────────────────────
set "ROOT=C:\Users\noleg\Desktop\Claude\Projects\Stocks"
set "PYTHON=C:\Users\noleg\Desktop\.venv\Scripts\python.exe"
set "PYTHONW=C:\Users\noleg\Desktop\.venv\Scripts\pythonw.exe"
set "SCRIPTS=C:\Users\noleg\Desktop\.venv\Scripts"
set "SOLO_INTEL=C:\Users\noleg\Desktop\Claude\Projects\SOLO BUILDS\solo-intel"
set "PODCASTS_ANALYSIS=C:\Users\noleg\Desktop\Claude\Projects\Podcasts Pull\analysis"
set "SOLO_DOMAIN=US equity catalyst intelligence from financial podcasts. Focus on which US stocks are getting attention, investment theses forming around specific tickers, risks and warning signals, and actionable signals for a US equities watchlist."

cd /d "%ROOT%"

:: ─── Header ───────────────────────────────────────────────────────────────
echo.
echo  ============================================================
echo   EquityIntel  ^|  Full Stack Runner
echo   %date%  %time%
echo  ============================================================
echo.
echo   Signal streams:
echo     1. SEC filings + prices  (steps 1-9)
echo     2. Podcast intelligence  (step 10 - solo-intel)
echo     3. Real-time news        (step 11 - Gemini Flash)
echo     4. LM Studio synthesis   (step 12 - all three combined)
echo.

:: ─── 0. Dashboard (background, no browser yet) ────────────────────────────
echo  [0/12] Starting dashboard in background...
taskkill /f /im pythonw.exe >nul 2>&1
start "" /B "%PYTHONW%" -m equity_intel.dashboard.cli --no-open --port 5173
timeout /t 3 /nobreak >nul
echo         http://localhost:5173  (browser will open after sync)
echo.

:: ─── 1. Companies ─────────────────────────────────────────────────────────
echo  [1/12] Syncing companies (ticker → CIK mapping)...
"%SCRIPTS%\equity-sync-companies.exe"
if errorlevel 1 echo         WARNING: sync-companies had errors, continuing...
echo.

:: ─── 2. Filings ───────────────────────────────────────────────────────────
echo  [2/12] Syncing recent SEC filings...
"%SCRIPTS%\equity-sync-filings.exe"
if errorlevel 1 echo         WARNING: sync-filings had errors, continuing...
echo.

:: ─── 3. Documents ─────────────────────────────────────────────────────────
echo  [3/12] Downloading and parsing filing documents...
"%SCRIPTS%\equity-sync-docs.exe"
if errorlevel 1 echo         WARNING: sync-docs had errors, continuing...
echo.

:: ─── 4. XBRL Facts ────────────────────────────────────────────────────────
echo  [4/12] Syncing XBRL company facts...
"%SCRIPTS%\equity-sync-facts.exe"
if errorlevel 1 echo         WARNING: sync-facts had errors, continuing...
echo.

:: ─── 5. News ──────────────────────────────────────────────────────────────
echo  [5/12] Syncing news articles...
"%SCRIPTS%\equity-sync-news.exe"
if errorlevel 1 echo         WARNING: sync-news had errors, continuing...
echo.

:: ─── 6. Prices ────────────────────────────────────────────────────────────
echo  [6/12] Syncing price and volume data...
"%SCRIPTS%\equity-sync-prices.exe"
if errorlevel 1 echo         WARNING: sync-prices had errors, continuing...
echo.

:: ─── 7. Events ────────────────────────────────────────────────────────────
echo  [7/12] Building catalyst events...
"%SCRIPTS%\equity-build-events.exe"
if errorlevel 1 echo         WARNING: build-events had errors, continuing...
echo.

:: ─── 8. Clustering ────────────────────────────────────────────────────────
echo  [8/12] Clustering and deduplicating events...
"%SCRIPTS%\equity-cluster-events.exe"
if errorlevel 1 echo         WARNING: cluster-events had errors, continuing...
echo.

:: ─── 9. Daily Brief ───────────────────────────────────────────────────────
echo  [9/12] Generating daily catalyst brief...
"%SCRIPTS%\equity-run-daily-brief.exe"
if errorlevel 1 (
  echo.
  echo         WARNING: run-daily-brief failed.
  echo         synthesize.py will try the dashboard API fallback.
  echo         If the dashboard is not running, synthesis will also fail.
  echo.
)
echo.

:: ─── 9b. Today-only brief (last 24 h) — freshness layer ────────────────────
echo  [9b] Generating today-only brief (last 24 h)...
"%SCRIPTS%\equity-run-daily-brief.exe" --days 1 --output-dir "%ROOT%\briefs\today"
if errorlevel 1 echo         NOTE: today-only brief had no catalysts (quiet 24 h window is normal)
echo.

:: ─── 10. Podcast intelligence (solo-intel → Podcasts Pull analysis/) ───────
echo  [10/12] Running solo-intel against Podcasts Pull...
"%PYTHON%" "%SOLO_INTEL%\solo.py" ^
  --folder "%PODCASTS_ANALYSIS%" ^
  --name "Podcasts Pull - Equity" ^
  --domain "%SOLO_DOMAIN%"
if errorlevel 1 echo         WARNING: solo-intel had errors (LM Studio may not be running)
echo.

:: ─── 11. Real-time news (Gemini Flash + Google Search grounding) ────────────
echo  [11/12] Fetching real-time news via Gemini Flash...
"%PYTHON%" "%ROOT%\gemini_news.py"
if errorlevel 1 echo         WARNING: gemini_news had errors (check GEMINI_API_KEY in .env)
echo.

:: ─── 12. Synthesize (LM Studio — all three signal streams) ─────────────────
echo  [12/12] Running LM Studio synthesis (SEC + podcasts + Gemini)...
"%PYTHON%" "%ROOT%\synthesize.py"
if errorlevel 1 (
  echo.
  echo  ERROR: synthesize.py failed.
  echo  Intelligence output was not generated.
  echo  Check that LM Studio is running, the local server is enabled, and the expected model is loaded.
  echo.
  exit /b 1
)
if not exist "%ROOT%\intelligence" (
  echo.
  echo  ERROR: intelligence\ was not created.
  echo  synthesize.py completed without producing output. Check logs above.
  echo.
  exit /b 1
)
echo.

:: ─── Done ──────────────────────────────────────────────────────────────
echo  ============================================================
echo   Sync complete.  Opening dashboard...
echo  ============================================================
echo.
start "" http://localhost:5173

:: ─── MCP Server config block ──────────────────────────────────────────────
echo.
echo  ── MCP Server (one-time Claude Desktop setup) ──────────────────────────
echo.
echo  Add this to %%APPDATA%%\Claude\claude_desktop_config.json
echo  under "mcpServers":
echo.
echo    "equity-intelligence": {
echo      "command": "%SCRIPTS%\equity-mcp.exe",
echo      "env": { "PYTHONPATH": "%ROOT%\src" }
echo    }
echo.
echo  ────────────────────────────────────────────────────────────
echo.
pause
endlocal
