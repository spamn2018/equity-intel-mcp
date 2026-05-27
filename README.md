# Equity Intelligence MCP

An equities-only financial intelligence pipeline that ingests SEC EDGAR filings, parses
disclosures, classifies market-moving events, and exposes everything through an MCP server
so an AI assistant can answer stock research questions with fresh, source-linked evidence.

The active research scope is an equities watch system for:

- **AI infrastructure and picks-and-shovels**: semiconductors, semiconductor equipment,
  data centers, power/grid, networking, memory/storage, and critical minerals.
- **401k overlap replacement ideas**: individual-stock watch buckets that avoid simply
  duplicating large positions already owned through broad 401k index funds.
- **TradHedge**: a separate durability-oriented equity watch bucket for old-line,
  tradable U.S. company lineages.

**This project has no crypto assets, no trading execution, and no brokerage integrations.**
Bitcoin-miner tickers may appear only when researched as equities with data-center,
power, or high-performance-computing optionality.

---

## What's Built

| Component | What it does |
|---|---|
| `sec/client.py` | Rate-limited (≤10 req/s), retry-backed, disk-cached HTTP client for SEC EDGAR |
| `sec/cik.py` | Maps ticker symbols to 10-digit SEC CIK numbers |
| `sec/filings.py` | Fetches recent filings from the submissions JSON API |
| `sec/parser.py` | Converts filing HTML → plain text, extracts 8-K item sections, detects high-impact keywords |
| `sec/facts.py` | Fetches XBRL companyfacts JSON, normalizes key financial concepts |
| `events/classify.py` | Maps form type + 8-K items + keywords → event type + subtype |
| `events/score.py` | Computes materiality score [0,1] from form, items, keywords, recency |
| `events/build.py` | Creates `events` records from filings, deduplicates |
| `news/polygon.py` | News ingestion from Polygon.io / Massive — paginated, deduplicated |
| `prices/polygon.py` | OHLCV price bars from Polygon.io, event-window reaction computation |
| `search/service.py` | PostgreSQL full-text search + LIKE fallback over filings and news |
| `mcp_server/server.py` | MCP server exposing 10 research tools over stdio |
| `mcp_server/tools.py` | Tool implementations (all DB queries, all source-cited) |
| Workers | CLI scripts to run each sync step independently |
| `briefs/watchlist.py` | Watchlist Catalyst Brief service — ranked catalysts with evidence, hedged language |
| `events/dedup.py` | Deterministic semantic deduplication — title normalization, Jaccard similarity, cross-week cluster matching |
| `export/` | Delivery adapter abstraction — `LocalFileDelivery` (JSON/Markdown); Email/Slack adapters planned |
| `dashboard/` | Local Flask research dashboard — evidence viewer with filtering, score rings, source links, and personal bias layer |
| `config/ai_tickers.json` | Thematic ticker universe including AI infrastructure, replacement candidates, critical minerals, and TradHedge |

---

## Quick Start

### 1. Prerequisites

- Python 3.10+
- PostgreSQL running locally (or adjust `DATABASE_URL` for SQLite in dev)

### 2. Install

```bash
git clone <repo>
cd equity-intelligence-mcp
pip install -e ".[dev]"
```

### 3. Configure

```bash
cp .env.example .env
# Edit .env — set at minimum APP_CONTACT_EMAIL (required for SEC User-Agent)
```

Minimum required fields:

```env
APP_CONTACT_EMAIL=your-email@example.com
DATABASE_URL=postgresql://localhost/equity_intel
SEC_USER_AGENT=equity-intelligence-mcp your-email@example.com
```

### 4. (Optional) Enable Polygon.io news and prices

Polygon.io was formerly branded as Massive. The same API key and endpoints work under both names.
Sign up at [polygon.io](https://polygon.io) (or [massive.com](https://massive.com)) and add to `.env`:

```env
NEWS_PROVIDER=polygon
PRICE_PROVIDER=polygon
POLYGON_API_KEY=your_key_here
```

Without this the pipeline runs in **SEC-only mode**: all SEC EDGAR data is ingested for free,
but `get_recent_news`, `search_news`, and price-reaction data in `explain_stock_move` will
return empty results.

### 5. Create the database

```bash
createdb equity_intel
alembic upgrade head
```

### 6. Populate data

Run workers in order. Each is a standalone CLI script; re-run any step at any time to pick
up new data.

```bash
# Step 1: Map tickers to CIKs (reads DEFAULT_TICKERS from .env if --tickers omitted)
python -m equity_intel.workers.sync_companies --tickers AAPL,MSFT,NVDA,TSLA,META

# Step 2: Fetch recent SEC filings (default: last 90 days)
python -m equity_intel.workers.sync_filings --tickers AAPL,MSFT

# Step 3: Download and parse filing documents (HTML → plain text, 8-K sections)
python -m equity_intel.workers.sync_documents --limit 50

# Step 4: Fetch XBRL financial facts (revenue, EPS, cash, debt, ...)
python -m equity_intel.workers.sync_facts --tickers AAPL,MSFT

# Step 5: Classify and score events from filings (and news articles if synced)
python -m equity_intel.workers.build_events

# Step 6 (optional, requires POLYGON_API_KEY): Sync news articles
python -m equity_intel.workers.sync_news --tickers AAPL,MSFT --days 7

# Step 7 (optional, requires POLYGON_API_KEY): Sync daily price bars
python -m equity_intel.workers.sync_prices --tickers AAPL,MSFT --days 90

# Step 8: Cluster events by ticker + event_type + ISO week (one-time or recurring)
python -m equity_intel.workers.cluster_events
# Scope to specific tickers:
python -m equity_intel.workers.cluster_events --tickers AAPL,MSFT
```

All workers are idempotent — re-running skips already-ingested records.

### 7. Start the MCP server

```bash
python -m equity_intel.mcp_server.server
```

The server speaks the Model Context Protocol over stdio. Wire it up to Claude Desktop, the
Claude API, or any MCP-compatible client.

### 8. Generate a watchlist brief (on-demand)

```bash
# Use tickers from DEFAULT_TICKERS in .env
equity-generate-watchlist-brief

# Specify tickers explicitly
equity-generate-watchlist-brief --tickers AAPL,MSFT,NVDA,TSLA --days 7

# Restrict to high-materiality events only
equity-generate-watchlist-brief --tickers AAPL,MSFT --min-materiality 0.6

# Filter by event type, render as Markdown
equity-generate-watchlist-brief --tickers NVDA,AMD --event-types earnings,guidance --markdown

# Save JSON output to file
equity-generate-watchlist-brief --tickers AAPL,MSFT,GOOGL --output brief.json

# Suppress price/news/filing detail for a compact view
equity-generate-watchlist-brief --tickers AAPL,MSFT --no-price --no-news --no-filings
```

All `--` options mirror the `get_watchlist_brief` MCP tool parameters.
Run `equity-generate-watchlist-brief --help` for the full list.

### 9. Run the scheduled daily brief

```bash
# Write today's brief using settings from .env
equity-run-daily-brief

# Preview without writing to disk (dry-run)
equity-run-daily-brief --dry-run

# Force Markdown output for today's brief
equity-run-daily-brief --format markdown
```

Output files land in `briefs/` (or `DAILY_BRIEF_OUTPUT_DIR` from `.env`):

```
briefs/brief_20240115.json   # JSON (default)
briefs/brief_20240115.md     # Markdown
```

See [Export and Delivery](#export-and-delivery) for format details and the delivery adapter API.

### 10. Open the local research dashboard

```bash
equity-dashboard
# Dashboard opens automatically at http://localhost:5173
# Press Ctrl+C to stop.

# Custom port
equity-dashboard --port 8080

# Don't open browser automatically
equity-dashboard --no-open
```

See [Local Research Dashboard](#local-research-dashboard) for full details.

---

## Local Research Dashboard

The dashboard is a **read-only browser-based evidence viewer** for watchlist catalyst briefs.
It does not execute trades, connect to brokerages, or provide investment advice.

### Start the dashboard

```bash
# Default: http://localhost:5173 (opens browser automatically)
equity-dashboard

# Custom port
equity-dashboard --port 8080

# Bind to all interfaces (LAN access)
equity-dashboard --host 0.0.0.0 --port 5173

# Suppress auto-open
equity-dashboard --no-open
```

### What it shows

The dashboard displays the latest watchlist catalyst brief, including:

- Ranked catalysts sorted by materiality score
- Per-catalyst: ticker, company name, event type/subtype, materiality score (ring indicator), confidence score (ring indicator)
- Price move and volume context where available
- Source-quality tier summary
- Related SEC filings with accession numbers, form types, and direct SEC links
- Related news articles with publisher, date, and URL
- Evidence caution notes (hedged language — never asserts causation)
- Collapsible catalyst cards for compact review

### Filtering controls

All filters are applied server-side on each refresh:

| Control | Description |
|---|---|
| **Tickers** | Comma-separated symbols — defaults to `DEFAULT_TICKERS` from `.env` |
| **Days lookback** | Look-back window 1–90 days (slider) |
| **Min materiality** | Minimum materiality score 0.0–1.0 (slider) |
| **Max results** | Cap catalyst count 1–100 |
| **Include low-confidence** | Toggle catalysts with `confidence_score < 0.3` |
| **Event types** | Multi-select checklist — uncheck all for all types |

Click **Apply filters** or **⟳ Refresh** to reload with current settings.

### Personal market-bias layer

The dashboard has a clearly separated **personal market-bias layer** — a section for your own
political and geopolitical views that may colour how you interpret evidence. It is:

- **Not derived from** SEC filings, news ingestion, event scoring, or any system inference
- **Clearly labelled** with a disclaimer in both the API response and the UI
- **Optional** — if no file is configured the section renders an empty-state prompt

To configure it, copy the example file and edit it:

```bash
cp bias_layer.example.json bias_layer.json
# Edit bias_layer.json with your own views
```

Example `bias_layer.json` structure:

```json
{
  "author": "Your name",
  "updated_at": "2026-05-11",
  "views": [
    {
      "title": "US-China tariff trajectory",
      "body": "Escalating tariff rhetoric likely to weigh on semiconductor supply chains.",
      "tickers": ["NVDA", "INTC", "QCOM"]
    }
  ]
}
```

The file path can be overridden with the `BIAS_LAYER_FILE` environment variable.

### No live network calls

The dashboard reads from the local PostgreSQL database only. Opening or refreshing the
dashboard never makes outbound HTTP requests to SEC, Polygon, or any external service.
Run the sync workers separately (before or on a schedule) to keep data fresh.

### Dashboard tests

```bash
pytest tests/test_dashboard.py -v
```

Tests cover: index route, filter parameter passing, empty state, catalyst schema,
source links, bias layer endpoint, and the no-network-calls guarantee.

---

## Scheduled Daily Brief

The `equity-run-daily-brief` command is a thin orchestration layer over the Watchlist Catalyst
Brief service. It reads configuration from `.env`, calls `get_watchlist_brief()`, and writes a
dated output file. All ranking, filtering, and formatting logic lives in the brief service — the
daily worker adds only scheduling and file-persistence.

> **Disclaimer:** This is research workflow output, not investment advice. Catalysts are described
> as "likely related to" or "may reflect" market moves — not as confirmed causes. Always verify
> with primary sources before making any decisions.

### Configuration (`.env`)

```env
# Watchlist for the daily brief (leave blank to use DEFAULT_TICKERS)
DAILY_BRIEF_WATCHLIST=AAPL,MSFT,NVDA,TSLA,META,GOOGL

# Output directory for brief files (created automatically)
DAILY_BRIEF_OUTPUT_DIR=briefs

# Look-back window in calendar days (default 1 = today's catalysts)
DAILY_BRIEF_DAYS=1

# Minimum materiality score [0, 1]
DAILY_BRIEF_MIN_MATERIALITY=0.3

# Output format: json | markdown
DAILY_BRIEF_FORMAT=json

# Maximum catalysts per brief
DAILY_BRIEF_MAX_ITEMS=30
```

### Output files

Briefs are written as:

```
briefs/brief_20240115.json    # format=json (default)
briefs/brief_20240115.md      # format=markdown
```

Re-running on the same calendar date **overwrites** the existing file (idempotent — safe to
schedule multiple times per day).

### Running manually

```bash
# Use settings from .env
equity-run-daily-brief

# Override tickers for a one-off run
equity-run-daily-brief --tickers AAPL,MSFT,NVDA --days 3

# Markdown output
equity-run-daily-brief --format markdown

# Dry-run (preview only — nothing written to disk)
equity-run-daily-brief --dry-run

# Custom output directory
equity-run-daily-brief --output-dir /mnt/reports/equity
```

### Scheduling — Windows Task Scheduler

1. Open **Task Scheduler** → **Create Basic Task**.
2. Set the trigger to **Daily**, time **07:00**.
3. Action → **Start a Program**:
   - **Program/script:** `python`
   - **Arguments:** `-m equity_intel.workers.run_daily_brief`
   - **Start in:** `C:\path\to\your\project`
4. Click **Finish**.

To use the installed script instead:

```
Program/script: C:\path\to\venv\Scripts\equity-run-daily-brief.exe
Start in:       C:\path\to\your\project
```

### Scheduling — Linux / macOS cron

```bash
# Edit crontab
crontab -e

# Run daily at 07:00 local time
0 7 * * * cd /path/to/project && python -m equity_intel.workers.run_daily_brief >> /var/log/equity_brief.log 2>&1
```

Or with the installed entry point:

```bash
0 7 * * * cd /path/to/project && equity-run-daily-brief >> /var/log/equity_brief.log 2>&1
```

### Recommended daily workflow

For a useful daily brief, run the sync workers first so data is fresh:

```bash
# 1. Pick up any new filings since yesterday
equity-sync-filings

# 2. Download and parse new documents
equity-sync-docs --limit 20

# 3. Sync news (requires POLYGON_API_KEY)
equity-sync-news --days 1

# 4. Sync price bars (requires POLYGON_API_KEY)
equity-sync-prices --days 2

# 5. Build and cluster events from new data
equity-build-events
equity-cluster-events

# 6. Generate the brief
equity-run-daily-brief
```

Steps 1–5 can be combined into a single shell script or chained in Task Scheduler as a sequence.

---

## Export and Delivery

Brief output is produced by a layered stack:

```
get_watchlist_brief()           ← ranks and scores catalysts
    ↓
_render_markdown()              ← single shared Markdown renderer
    ↓
LocalFileDelivery.deliver()     ← writes to local disk
```

All formatting logic lives in one place (`workers/generate_watchlist_brief.py`). The daily
brief worker and the on-demand CLI both call the same renderer.

### Output formats

| Format | Extension | Description |
|---|---|---|
| `json` | `.json` | Full structured brief — all scores, evidence, source URLs, raw catalyst data |
| `markdown` | `.md` | Human-readable report with sections, tables, caution blocks, and source links |

### Markdown output structure

A Markdown brief contains:

| Section | Content |
|---|---|
| **Header** | Generated timestamp, watchlist tickers, time window, total catalysts |
| **Query Parameters** | Filters applied: min materiality, event types, max items |
| **Summary** | Prose overview of the window — catalyst count, top-ranked item, materiality distribution |
| **Caution** | Disclaimer block — correlation vs. causation, not investment advice |
| **Catalysts** | Ranked list — per catalyst: ticker, company, event type, materiality/confidence scores, first-seen date, why-it-matters narrative, price move, source links, related filings, related news |
| **Footer** | Source note and explicit not-investment-advice statement |

### Where files are written

Daily brief files land in the configured output directory (default: `briefs/`):

```
briefs/
  brief_20240115.json    # JSON (default)
  brief_20240115.md      # Markdown (format=markdown)
```

Re-running on the same calendar date **overwrites** the existing file (idempotent).

The directory is created automatically if it does not exist.

### Delivery adapters

The `equity_intel.export` package provides a `DeliveryAdapter` abstract base class.
`LocalFileDelivery` is the built-in implementation:

```python
from pathlib import Path
from equity_intel.export import LocalFileDelivery

adapter = LocalFileDelivery()
result = adapter.deliver(brief, Path("briefs/brief_20240115.json"), "json")
print(result["status"])       # "ok"
print(result["destination"])  # "briefs/brief_20240115.json"
print(result["bytes_written"]) # e.g. 14382
```

Future delivery channels (not yet active — no credentials required):

| Adapter | Status | What it would do |
|---|---|---|
| `LocalFileDelivery` | ✅ Active | Writes to local filesystem |
| `EmailDelivery` | 🔲 Planned | SMTP / SendGrid; requires `EMAIL_*` env keys |
| `SlackDelivery` | 🔲 Planned | Slack Incoming Webhook; requires `SLACK_WEBHOOK_URL` |
| `WebhookDelivery` | 🔲 Planned | Generic HTTP POST to any URL |

None of the planned adapters are defaults. The system works fully offline with local files only.

---

## Research Universe vs Active Watchlist

### Two-layer design

The system separates *what you're watching* from *what you're actively monitoring*:

**Research universe** — `config/ai_tickers.json`

The broad, thesis-driven list of every name that fits the identified investment themes (semiconductors, power, data centers, rare earths, AI software, robotics, and more).
It is allowed to contain emerging, speculative, or early-stage names.
This file is the structured source of truth for the full universe and is read at runtime by `equity_intel.research_universe`.

**Active watchlist** — `.env`

The smaller operational set used by daily briefs, ingestion workers, and the dashboard.

```env
DEFAULT_TICKERS=NVDA,AMD,AVGO,MSFT,GOOGL,AMZN,TSLA,ISRG,SYM,META,PLTR,AI,BOTZ,ROBO
DAILY_BRIEF_WATCHLIST=
```

`DEFAULT_TICKERS` is used unless `DAILY_BRIEF_WATCHLIST` is also set, in which case that takes priority for briefs.

### Ticker stages

Every ticker entry in `config/ai_tickers.json` can carry an optional `stage` field:

| Stage | Meaning |
|---|---|
| `probe` | Early idea — collect data, do not treat as high-conviction |
| `watch` | Credible thesis fit — monitor catalysts |
| `active` | Part of the active research watchlist |
| `core` | Established high-conviction name |
| `archived` | No longer relevant, kept for history |

Tickers without an explicit `stage` use the minimal shape and are treated as unclassified.

### Ticker metadata shape

Minimal shape (existing entries are fine as-is):

```json
{ "ticker": "NVDA", "name": "NVIDIA Corporation", "why": "..." }
```

Richer shape for emerging or developing opportunities:

```json
{
  "ticker": "POWL",
  "name": "Powell Industries Inc",
  "why": "Electrical equipment exposure to power infrastructure buildout.",
  "stage": "watch",
  "conviction": "medium",
  "thesis_tags": ["power", "data_centers", "electrification"],
  "risk_tags": ["cyclical", "small_cap", "execution"],
  "source": "manual_research",
  "added_at": "2026-05-25",
  "review_after": "2026-08-25",
  "max_position_pct": 3
}
```

### How to add a new opportunity

1. Add the ticker to the appropriate category in `config/ai_tickers.json`.
2. Include `stage`, `conviction`, `thesis_tags`, `risk_tags`, `added_at`, and `review_after`.
3. If the ticker should be actively monitored every day, add it to `DEFAULT_TICKERS` or `DAILY_BRIEF_WATCHLIST` in `.env`.
4. Run the ingestion pipeline for the new ticker before expecting catalysts or 13F signals to appear:

```bash
python -m equity_intel.workers.sync_companies --tickers POWL
python -m equity_intel.workers.sync_filings --tickers POWL
python -m equity_intel.workers.sync_documents --tickers POWL
python -m equity_intel.workers.build_events --tickers POWL
```

> **Note:** 13F institutional-holdings signals only resolve for companies already present in the `companies` table.
> New tickers must be synced via `sync_companies` first.

### Language for probe and watch names

The system labels probe-stage catalysts with a clear note:

> *"This is an early-stage research candidate (probe). Treat with extra caution — primary-source confirmation required before drawing any conclusions. Not comparable to established names."*

Avoid language that implies a buy/sell signal.
Prefer: *early-stage research candidate*, *probe-stage ticker*, *developing thesis*, *watchlist candidate*, *requires primary-source confirmation*.

### Research universe API

The dashboard exposes the full universe at:

```
GET /api/research_universe
```

Returns categories, ticker metadata, stage, thesis tags, risk tags, and the total ticker count. Read-only.

### Programmatic access

```python
from equity_intel.research_universe import (
    get_all_research_tickers,
    get_ticker_category_map,
    get_ticker_metadata,
    get_tickers_by_stage,
)

# All tickers in the universe (deduplicated, uppercase)
tickers = get_all_research_tickers()

# ticker → human-readable category label
cat_map = get_ticker_category_map()   # {"NVDA": "Semiconductors Compute", ...}

# Full metadata dict keyed by ticker
meta = get_ticker_metadata()          # {"NVDA": {"stage": "core", ...}, ...}

# Filter by stage
probes = get_tickers_by_stage("probe")  # early-stage names only
```

Results are cached per config-file path and thread-safe.

---

## Event Deduplication

The system uses a two-layer deduplication strategy to avoid showing the same
market-moving story multiple times.

### Layer 1 — Cluster key grouping (strict)

Every event is assigned a cluster key of the form `TICKER:event_type:YYYYWww`.
Events sharing the same ticker, event type, and ISO calendar week are automatically
merged into one `EventCluster`.  This handles the common case of multiple news
articles, press releases, or repeated provider entries about the same story arriving
within the same week.

### Layer 2 — Cross-week title deduplication (conservative)

An earnings announcement on Friday evening and the resulting news coverage starting
Monday morning would land in different ISO weeks and produce two separate clusters
under Layer 1 alone.  Layer 2 addresses this:

Before creating a new cluster, the engine calls `find_similar_cluster()` (in
`events/dedup.py`) to search for a near-duplicate cluster within a 10-day window.
The comparison uses **normalized Jaccard similarity** on event titles:

1. Both titles are normalized: lowercased, punctuation stripped, ticker symbol
   removed, company suffixes (`Inc.`, `Corp.`) removed, finance boilerplate verbs
   (`announces`, `reports`) removed, stop words removed, and tokens sorted.
2. Token-set Jaccard similarity is computed on the normalized strings.
3. If similarity ≥ 0.60, the incoming event is merged into the existing cluster
   rather than creating a new one.

This threshold is deliberately conservative.  When in doubt the engine creates
a separate cluster rather than risking an incorrect merge.

### Evidence preservation

When events are merged — whether by cluster key or cross-week dedup — all source
evidence is retained:

- `filing_ids` accumulates every linked SEC filing ID
- `news_ids` accumulates every linked news article ID
- `source_urls` accumulates every source URL

No evidence is discarded.  A catalyst shown in the watchlist brief may be
supported by multiple filings and news articles; all are listed in
`related_filings` and `related_news`.

### What deduplication does not do

- It does not use vector search or embeddings (no external ML infrastructure required).
- It does not merge events of unrelated types (e.g. `earnings` and
  `bankruptcy_or_going_concern` for the same ticker are always separate).
- It does not merge events for different tickers.
- It does not deduplicate across tickers even if titles overlap.

---
## Source-Quality Weighting

Not all event sources carry the same informational weight.  An SEC 8-K filing is
a primary regulatory disclosure; a syndicated wire summary of the same event is
secondary.  The system tracks this distinction through a deterministic tier system
and embeds quality metadata in every event and cluster.

### Quality tiers

| Tier | Score | Typical source |
|---|---|---|
| `SEC_FILING` | 1.00 | Direct SEC/EDGAR filings (`sec.gov` domain) |
| `COMPANY_IR` | 0.80 | Official press releases (PR Newswire, Business Wire, GlobeNewswire) |
| `REPUTABLE_FINANCIAL` | 0.70 | Established outlets (Reuters, Bloomberg, WSJ, CNBC, FT, MarketWatch) |
| `SYNDICATED` | 0.50 | Named but unclassified news publishers |
| `UNKNOWN` | 0.30 | Missing or unrecognisable source information |

Tier assignment is **deterministic and offline** — it uses only `source_type`,
`publisher`, and `url` with no ML models or network calls.

### How quality influences scoring

Quality is fed into the confidence score with a modest weight so that it cannot
override the more important signals (event type, keywords, price/volume reaction):

```
quality_adj = (source_quality_score - 0.5) * 0.20   # event-level: max ±0.10
quality_adj = (primary_source_quality - 0.5) * 0.10  # cluster-level: max ±0.05
```

A cluster anchored by an SEC filing therefore receives a small confidence boost
over an equivalent cluster backed only by news articles.  The adjustment is capped
so that a high-quality unknown source never inflates confidence unreasonably.

### Transparency in output

Every event's `evidence_json` and every catalyst in the watchlist brief includes:

```json
{
  "source_quality_tier":  "sec_filing",
  "source_quality_score": 1.0,
  "source_quality_label": "Primary SEC filing"
}
```

The `has_primary_source` field on each catalyst brief item is `true` when at
least one SEC filing backs the event — a quick human-readable signal that the
evidence chain reaches back to a primary regulatory document.

The `source_summary` field gives a concise count: `"1 SEC filing, 3 news articles"`.

### Implementation

| File | Role |
|---|---|
| `events/source_quality.py` | `SourceTier` enum, tier lookup, `source_quality_score()`, `source_quality_metadata()` |
| `events/score.py` | `compute_confidence_score()` and `compute_cluster_confidence()` accept `source_quality` param |
| `events/build.py` | Computes quality and embeds `source_quality_metadata()` in `evidence_json` |
| `events/cluster.py` | Derives `primary_source_quality` (best quality in cluster) for cluster confidence |
| `briefs/watchlist.py` | Adds `has_primary_source` and `source_summary` to every catalyst item |

---
## MCP Tools

| Tool | Description |
|---|---|
| `get_company` | Company profile, CIK, exchange, sector, latest filing dates |
| `get_recent_filings` | Recent SEC filings with accession numbers and SEC links |
| `get_filing` | Full filing metadata, parsed text, 8-K sections, detected keywords |
| `search_filings` | Full-text search over parsed filing documents |
| `get_company_facts` | XBRL financial facts (revenue, EPS, cash, debt, etc.) |
| `get_recent_news` | Recent news articles (requires Polygon API key) |
| `search_news` | Full-text search over news articles |
| `get_events` | Ranked event clusters (or raw events) by materiality score, with price reaction data |
| `get_event_cluster` | Full detail for a single cluster: linked filings, news, price reaction, aggregate scores |
| `explain_stock_move` | Price-move analysis with nearby clusters, filings, and news — cautious "likely related" language |
| `screen_catalysts` | Cross-market screening for high-materiality event clusters |
| `get_watchlist_brief` | **[New]** Ranked catalyst brief for a watchlist — "what's moving my stocks right now?" |

### `get_watchlist_brief` — Watchlist Catalyst Briefs

The flagship research tool. Generates a ranked, evidence-backed report for a list of tickers,
answering the question: *"What are the most important stock-moving catalysts across my watchlist
right now?"*

**Key properties:**

- Prefers `EventCluster` data (multi-source, price-enriched) when available; falls back to raw
  `Event` records automatically for tickers without clusters.
- Ranked by `materiality_score` descending so the most significant catalysts appear first.
- Every catalyst includes a `why_it_matters` narrative generated with cautious language
  ("may reflect", "the available evidence suggests", "likely related").
- Never asserts causation. Each item carries a `caution` field.

**Parameters:**

| Parameter | Default | Description |
|---|---|---|
| `tickers` | *(required)* | List of ticker symbols |
| `days` | `7` | Look-back window in calendar days |
| `min_materiality` | `0.3` | Minimum materiality score [0, 1] |
| `include_low_confidence` | `false` | Include catalysts with confidence < 0.3 |
| `max_items` | `20` | Cap on number of catalysts returned |
| `event_types` | `null` | Optional filter (e.g. `["earnings","guidance"]`) |
| `include_price_context` | `true` | Include price move and volume data |
| `include_news` | `true` | Include linked news articles |
| `include_filings` | `true` | Include linked SEC filings |

**Example output (abbreviated):**

```json
{
  "generated_at": "2024-01-16T10:30:00Z",
  "watchlist": ["AAPL", "NVDA", "TSL
