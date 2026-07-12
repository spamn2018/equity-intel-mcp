# Equity Intelligence MCP Project

## Global CLAUDE.md

The global rules file is at: `C:\Users\noleg\.claude\CLAUDE.md`
Always read it before starting work. Project rules here take precedence for project-specific behavior; global rules apply for everything else (tooling priority, file writing, shell execution, etc.).

## CRITICAL: Never Report Something as Fixed Until Tested

Do not say "fixed", "done", or "this will work" until the fix has been run and the output verified.
For batch files: run via Desktop Commander and confirm the expected output appears.
For Python files: run py_compile and confirm OK.
For pipeline changes: run the relevant worker or step and confirm no errors.

## CRITICAL: File Writing Rule

**Never use the Write or Edit tools to write Python source files on this project.**
The Windows filesystem mount silently truncates files that contain multi-byte UTF-8 characters (box-drawing chars, em-dashes, etc.). This causes Python syntax errors that are invisible in the editor but break at runtime.

Required approach for ALL .py file writes:
1. Write via bash using the workspace Python or a heredoc that calls open(..., 'w').write(content).
2. Use ASCII-only characters in comments and strings (no box-drawing chars).
3. After every write, run py_compile to verify.
4. Zero all __pycache__ .pyc files after source changes.
5. Run tests with python -B and PYTHONDONTWRITEBYTECODE=1 to prevent stale bytecode.

## CRITICAL: Batch File (.bat) Rules

Parentheses inside a CMD if/for block body MUST be escaped with ^ or the block will close early.

Bad:   echo Pipeline data is fresh (under 12 h old) -- skipping.
Good:  echo Pipeline data is fresh ^(under 12 h old^) -- skipping.

Never use PowerShell in bat files on this machine -- it is blocked (Access is denied).
Use Python or plain CMD commands instead.

Never use `set /p VAR=<file` inside a parenthesized block -- it redirects stdin for the whole block.
Use `for /f "usebackq" %%i in ("file") do set "VAR=%%i"` instead.

Never use `<` or `>` operators inside a Python -c string passed from a bat file.
CMD interprets them as I/O redirection even inside double quotes.
Use .__lt__(), .__gt__(), or write the Python to a temp .py file instead.

After any bat file change, run it via Desktop Commander and verify the output before reporting it fixed.

## Windows Source View / Write Mismatch Workaround

If Python reports a syntax error near end-of-file but the Read tool shows valid code:

1. Check actual on-disk bytes via windows-mcp FileSystem or PowerShell -- NOT bash.
2. If truncated, the file has multi-byte chars that caused truncation at the mount layer.
3. Rewrite the entire file via bash Python with ASCII-only comments.
4. Run py_compile to confirm before moving on.
5. Do NOT make repeated small edits -- they will not fix truncation.

## Python Runtime

Use `C:\Users\noleg\Desktop\.venv\Scripts\python.exe` in all bat files. No per-project venvs.

## Mission

Build an equities-only financial intelligence and autonomous trading system that ingests SEC filings, company disclosures, financial news, press releases, and price/volume context, generates actionable trade signals from that data, and executes orders automatically via the Alpaca brokerage API.

This project is **not** interested in crypto. Do not build crypto ingestion, crypto schemas, crypto tools, or crypto UI.

The goal is a fully autonomous pipeline: ingest -> analyze -> score signals -> execute trades -> track positions and P&L. Research and intelligence capabilities feed directly into trade decisions made by the system itself.

## Core Product

1. SEC EDGAR ingestion
2. Ticker-to-CIK mapping
3. Recent filings discovery
4. Filing metadata storage
5. Filing document download and parsing
6. XBRL/company facts ingestion
7. News ingestion from configurable providers
8. Press release ingestion where feasible
9. Price/volume reaction context from a configurable equities data provider
10. Event classification and ranking
11. Search over filings/news/events
12. MCP tools for AI agents to query the system
13. Signal generation from scored events
14. Autonomous order execution via Alpaca
15. Position and P&L tracking

## Non-Goals

- Crypto data
- Options data in the first version
- International equities in the first version
- A Bloomberg Terminal clone UI

## Tech Preferences

- Language: Python
- Database: PostgreSQL preferred, SQLite acceptable for MVP
- Search: PostgreSQL full-text search for MVP
- Config: `.env` via pydantic-settings
- Tests: pytest with in-memory SQLite

## Trade Execution Rules

The system executes orders autonomously based on internal signals. Follow these rules without exception:

- Use the Alpaca SDK (`alpaca-py`). Paper vs live is controlled by `ALPACA_PAPER` in `.env`.
- Paper keys: `Alpaca Creds/API.txt` (start with PK). Live keys also in same file (start with AK).
- All order submission goes through `TradingClient(paper=cfg.alpaca_paper)` -- never hardcode paper=False.
- Order type is controlled by `TRADING_ORDER_TYPE` in `.env` ("limit" or "market"). Default is "limit". When set to "market", AlpacaBrokerAdapter.submit_market_order() is used; E*TRADE always forces limit regardless of this setting. Expected fill price (mid at submission) is always captured for slippage tracking.
- Buys use `notional` (dollar amount) for fractional share support. Never use qty for buys.
- Sells use `qty` (share count from existing position). Never use notional for sells.
- Never hardcode order sizes -- buys are sized as TRADING_MAX_ORDER_NOTIONAL (currently $100) scaled by signal strength (floor 0.5x), then capped by remaining position capacity and buying power. Sells always close the existing position qty and are exempt from the notional cap.
- Every submitted order must be logged with its signal source, symbol, side, qty/notional, order type, and timestamp.
- Always check `account.buying_power` before submitting a buy order.
- Always check existing positions before submitting a sell order.
- Kill switch: `TRADING_EXECUTION_ENABLED=true` must be set in `.env` for any order to be submitted. Default false.
- Short gate: `TRADING_ALLOW_SHORTS=false` (default). Sell/reduce signals are generated, scored by the backtest, and logged, but never submitted to the broker until this is set to true. Do not change this default without explicit instruction.
- Approval gate: `TRADING_REQUIRE_APPROVAL=true` means only signals with status=approved to execute.
- Never submit orders when `TRADING_EXECUTION_ENABLED=false`, even in test code paths.

## Signal-to-Trade Pipeline

1. Event builder scores incoming filings, news, and price moves.
2. Signal generator reads high-materiality events -> BUY/SELL signals with confidence scores.
3. Risk layer checks account state, open orders, position limits before approving. Spread is NOT gated -- all orders are limit orders, so spread is irrelevant. If the broker has no quote, risk.py falls back to a DeepSeek-quoted price (DEEPSEEK_API_KEY / DEEPSEEK_MODEL).
4. Execution layer submits limit+day orders via Alpaca and records results.
5. Position tracker monitors open positions and P&L.
6. Exit logic closes positions based on target, stop-loss, or time-based rules.

Note: buy signals for tickers with NO existing position bypass TRADING_MIN_SIGNAL_STRENGTH -- the strength threshold only gates adding to existing positions.

Key files:
- `src/equity_intel/trading/alpaca_adapter.py` -- AlpacaBrokerAdapter (get_account, get_quote, submit_limit_order, etc.)
- `src/equity_intel/trading/risk.py` -- evaluate_signal_for_execution() pre-flight checks
- `src/equity_intel/trading/execution.py` -- execute_approved_signals() main loop
- `src/equity_intel/trading/signals.py` -- generate_trade_signals_from_brief()
- `src/equity_intel/workers/generate_trade_signals.py` -- CLI worker
- `src/equity_intel/workers/execute_trade_signals.py` -- CLI worker

## Brief Cache

`get_watchlist_brief()` in `src/equity_intel/briefs/watchlist.py` caches its output to `.cache/brief/` as JSON.
TTL is controlled by `DAILY_BRIEF_CACHE_TTL_SECONDS` in `.env` (0 = disabled, 86400 = 24h).
On cache hit the entire article-pull and LLM synthesis pipeline is skipped.
Cache key = SHA1 of sorted(tickers) + days + min_materiality.

## Configuration

Key `.env` variables:

```
DATABASE_URL=sqlite:///equity_intel.db
ALPACA_API_KEY=<paper key from Alpaca Creds/API.txt>
ALPACA_SECRET_KEY=<paper secret>
ALPACA_PAPER=true
TRADING_EXECUTION_ENABLED=true
TRADING_REQUIRE_APPROVAL=false
TRADING_MAX_POSITION_PCT=5.0
TRADING_MAX_ORDER_NOTIONAL=100.0
DEEPSEEK_API_KEY=<key>
DEEPSEEK_MODEL=deepseek-v4-flash
DAILY_BRIEF_CACHE_TTL_SECONDS=86400
LLM_PROVIDER=openai
LMSTUDIO_MODEL=o4-mini
POLYGON_API_KEY=<key>
```

## Data Sources

### SEC EDGAR

- Company submissions: `https://data.sec.gov/submissions/CIK##########.json`
- Company facts: `https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json`
- Rate limit: under 10 req/s. Set real User-Agent. Cache aggressively.

### News

Provider interface in `news/base.py`. Current provider: Polygon.io (`news/polygon.py`).
News sync has a 24h TTL stamp (`news_sync_last_run.txt`). Delete to force re-fetch.
Scheduled nightly at 3 AM ET via Windows Task Scheduler task `EquityIntel-NewSync-3AM`.

### Prices

Provider interface in `prices/base.py`. Current provider: Polygon.io (`prices/polygon.py`).

## Filing Forms To Prioritize

8-K, 10-Q, 10-K, S-1, S-3, 424B, DEF 14A, 13D, 13G, 4, 144.

Important 8-K items: 1.01, 1.02, 2.02, 2.05, 2.06, 3.01, 3.02, 4.01, 4.02, 5.02, 7.01, 8.01, 9.01.

## Important Implementation Notes

- Do not hardcode API keys.
- Make all external providers optional.
- Preserve raw JSON from upstream APIs.
- Store source URLs everywhere.
- Use idempotent upserts for ingestion.
- Make workers safe to rerun.
- Normalize all tickers uppercase.
- Normalize all CIKs to 10-digit strings.
- Do not exceed SEC rate limits.
- Log enough to debug ingestion failures.
