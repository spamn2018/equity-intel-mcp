```markdown
# Equity Intelligence MCP Project

## Workspace Tooling

Always use the Node REPL tool for local inspection and command work in this workspace. The standard shell runner is unreliable here and may fail before commands execute.

## Python Runtime

Follows the global rule — see `C:\Users\noleg\.claude\CLAUDE.md`.
Use `C:\Users\noleg\Desktop\.venv\Scripts\python.exe` in all bat files. No per-project venvs.

## Mission

Build an equities-only financial intelligence system that ingests SEC filings, company disclosures, financial news, press releases, and price/volume context, then exposes the data through an MCP server so an AI assistant can answer stock research questions with fresh, source-linked evidence.

This project is **not** interested in crypto. Do not build crypto ingestion, crypto schemas, crypto tools, or crypto UI.

The goal is to reproduce the useful parts of a financial-data MCP service ourselves: filings, news, fundamentals, market-moving events, company info, and source-grounded research workflows.

## Core Product

Create a local-first system with:

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

## Non-Goals

Do not implement:

- Crypto data
- Trading execution
- Brokerage integrations
- Portfolio management
- Investment advice generation
- Options data in the first version
- International equities in the first version
- A Bloomberg Terminal clone UI

## Tech Preferences

Use a pragmatic stack:

- Language: Python preferred for ingestion and parsing
- API/MCP: Python MCP server or TypeScript MCP server, choose based on repo conventions
- Database: PostgreSQL preferred, SQLite acceptable for MVP if simpler
- Search: PostgreSQL full-text search for MVP
- Vector search: optional later
- Background jobs: simple scheduled workers first
- Config: `.env`
- Tests: focused tests for parsers, SEC client, storage, and MCP tools

If the existing repo already has a framework, follow it.

## Data Sources

### SEC EDGAR

Use official SEC public data APIs.

Important endpoints:

- Company submissions:
  `https://data.sec.gov/submissions/CIK##########.json`

- Company facts:
  `https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json`

- Company concept:
  `https://data.sec.gov/api/xbrl/companyconcept/CIK##########/us-gaap/AccountsPayableCurrent.json`

- SEC bulk submissions:
  `https://www.sec.gov/Archives/edgar/daily-index/bulkdata/submissions.zip`

- SEC bulk company facts:
  `https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip`

SEC access rules:

- Set a real `User-Agent` header with app name and contact email.
- Keep requests under 10 requests per second.
- Cache aggressively.
- Respect retries and backoff.
- Do not scrape wastefully.

### News

Design a provider interface. Start with one provider, but keep it swappable.

Possible providers:

- Polygon.io Stocks News API
- Nasdaq Data Link
- Benzinga
- Finnhub
- Alpha Vantage news
- NewsAPI only if licensing/use case allows
- Company IR RSS feeds

The system should store source URLs and timestamps. Every summary must be traceable back to original sources.

### Prices

Use equities-only price data for context.

Purpose:

- Detect abnormal price moves
- Show price/volume reaction around filings/news
- Rank events by observed market impact

Possible providers:

- Polygon.io
- IEX Cloud or successor equivalents
- Tiingo
- Nasdaq Data Link
- Alpha Vantage
- Stooq/Yahoo only for local experimentation, not a durable production dependency

## Data Model

Implement a normalized schema.

### companies

Fields:

- id
- ticker
- cik
- name
- exchange
- sic
- sector
- industry
- fiscal_year_end
- is_active
- created_at
- updated_at

### filings

Fields:

- id
- company_id
- accession_number
- form_type
- filing_date
- report_date
- acceptance_datetime
- primary_document
- filing_url
- primary_document_url
- sec_index_url
- items
- raw_metadata_json
- created_at
- updated_at

### filing_documents

Fields:

- id
- filing_id
- document_url
- document_type
- filename
- html_text
- plain_text
- parsed_sections_json
- created_at
- updated_at

### company_facts

Fields:

- id
- company_id
- taxonomy
- concept
- label
- description
- unit
- value
- fiscal_year
- fiscal_period
- form_type
- filed_date
- end_date
- accession_number
- raw_json
- created_at

### news_articles

Fields:

- id
- provider
- provider_id
- ticker
- company_id
- title
- summary
- body
- url
- publisher
- author
- published_at
- tickers_json
- sentiment_json
- raw_json
- created_at

### press_releases

Fields:

- id
- company_id
- ticker
- title
- body
- url
- source
- published_at
- raw_json
- created_at

### market_prices

Fields:

- id
- ticker
- timestamp
- open
- high
- low
- close
- volume
- adjusted_close
- interval
- provider
- raw_json
- created_at

### events

Fields:

- id
- company_id
- ticker
- event_type
- event_subtype
- title
- summary
- source_type
- source_id
- source_url
- occurred_at
- detected_at
- materiality_score
- novelty_score
- confidence_score
- price_reaction_json
- evidence_json
- created_at
- updated_at

Event types should include:

- earnings
- guidance
- merger_acquisition
- offering_or_dilution
- insider_transaction
- activist_stake
- management_change
- regulatory
- litigation
- bankruptcy_or_going_concern
- restatement
- buyback
- dividend
- product_announcement
- analyst_rating
- macro_sensitive_news
- unusual_price_volume
- other

## Filing Forms To Prioritize

MVP priority:

- 8-K
- 10-Q
- 10-K
- S-1
- S-3
- 424B
- DEF 14A
- 13D
- 13G
- 4
- 144

For 8-K filings, parse item numbers when available.

Important 8-K items:

- Item 1.01: Material definitive agreement
- Item 1.02: Termination of material agreement
- Item 2.02: Results of operations and financial condition
- Item 2.05: Exit or disposal costs
- Item 2.06: Material impairments
- Item 3.01: Notice of delisting
- Item 3.02: Unregistered sales of equity securities
- Item 4.01: Change in accountant
- Item 4.02: Non-reliance on prior financials
- Item 5.02: Departure/election of directors or officers
- Item 7.01: Regulation FD disclosure
- Item 8.01: Other events
- Item 9.01: Financial statements and exhibits

## MCP Tools

Expose the following tools.

### get_company

Input:

- ticker

Returns:

- company profile
- CIK
- exchange
- sector/industry if available
- latest filing dates
- source metadata

### get_recent_filings

Input:

- ticker
- form_types optional
- days optional
- limit optional

Returns:

- recent filings
- accession numbers
- form types
- filing dates
- filing URLs
- parsed 8-K items if available

### get_filing

Input:

- accession_number

Returns:

- filing metadata
- primary document text
- parsed sections
- source URLs

### search_filings

Input:

- ticker optional
- query
- form_types optional
- start_date optional
- end_date optional
- limit optional

Returns:

- matching filing snippets
- accession numbers
- form types
- dates
- URLs

### get_company_facts

Input:

- ticker
- concepts optional
- fiscal_periods optional
- limit optional

Returns:

- normalized XBRL facts
- concept labels
- values
- units
- dates
- source accession numbers

### get_recent_news

Input:

- ticker optional
- query optional
- days optional
- limit optional

Returns:

- recent articles
- publisher
- title
- summary
- URL
- published timestamp
- associated tickers

### search_news

Input:

- ticker optional
- query
- start_date optional
- end_date optional
- limit optional

Returns:

- matching news articles
- snippets
- source links

### get_events

Input:

- ticker optional
- event_types optional
- days optional
- min_materiality optional
- limit optional

Returns:

- ranked events
- summaries
- evidence
- source URLs
- price reaction when available

### explain_stock_move

Input:

- ticker
- date optional
- window optional

Returns:

- price move
- volume move
- likely related filings/news/events
- evidence list
- confidence score

### screen_catalysts

Input:

- event_types optional
- days optional
- min_materiality optional
- tickers optional
- sectors optional
- limit optional

Returns:

- cross-market catalyst list
- ranked by materiality, novelty, and reaction

## Event Ranking

Create a simple scoring system first.

Materiality score inputs:

- Form type importance
- 8-K item importance
- Source quality
- Ticker specificity
- Novelty versus recent similar events
- Keywords indicating severity
- Price/volume reaction if available
- Recency

Example high-impact keywords:

- bankruptcy
- going concern
- restatement
- subpoena
- investigation
- SEC investigation
- DOJ
- FDA approval
- FDA rejection
- complete response letter
- merger
- acquisition
- tender offer
- offering
- dilution
- reverse split
- delisting
- resignation
- termination
- guidance lowered
- guidance raised
- strategic alternatives
- material weakness

## Ingestion Workflow

Build these scripts/workers:

### 1. Company Universe Sync

- Fetch ticker/CIK mapping
- Upsert companies
- Normalize CIK to 10 digits
- Preserve ticker history if possible

### 2. Recent SEC Filings Sync

For each tracked company:

- Fetch submissions JSON
- Extract recent filings
- Upsert filings
- Build SEC document URLs
- Queue important filings for document download

### 3. Filing Document Fetch

For queued filings:

- Download primary document
- Convert HTML to plain text
- Extract 8-K item sections where possible
- Store raw and parsed text

### 4. Company Facts Sync

For each tracked company:

- Fetch companyfacts JSON
- Normalize selected concepts
- Store facts with accession/date metadata

Start with concepts:

- Revenue
- Net income
- Gross profit
- Operating income
- EPS basic
- EPS diluted
- Cash and equivalents
- Assets
- Liabilities
- Debt
- Operating cash flow
- Capital expenditures
- Shares outstanding

### 5. News Sync

- Pull recent news from provider
- Upsert by provider article ID or canonical URL
- Link tickers to companies
- Store raw provider JSON

### 6. Price Sync

- Pull daily and intraday bars where provider supports it
- Store prices
- Compute event-window reactions

### 7. Event Builder

Create events from:

- Important filings
- News articles
- Press releases
- Insider filings
- Abnormal price/volume moves

Deduplicate similar events.

## Research Output Rules

Any AI-facing summary should:

- Cite source URLs
- Include dates
- Distinguish facts from interpretation
- Include confidence when inferring causality
- Avoid giving investment advice
- Avoid saying a stock moved because of an event unless evidence supports it
- Prefer “likely related to” over overconfident causal language

## Configuration

Use environment variables:

```env
APP_NAME=equity-intelligence-mcp
APP_CONTACT_EMAIL=your-email@example.com
DATABASE_URL=postgresql://localhost/equity_intel
SEC_USER_AGENT=equity-intelligence-mcp your-email@example.com

NEWS_PROVIDER=polygon
POLYGON_API_KEY=

PRICE_PROVIDER=polygon
PRICE_API_KEY=

MCP_SERVER_NAME=equity-intelligence
LOG_LEVEL=info
```

## Project Structure

Suggested structure:

```text
.
├── README.md
├── .env.example
├── pyproject.toml
├── src/
│   └── equity_intel/
│       ├── config.py
│       ├── db/
│       │   ├── models.py
│       │   ├── session.py
│       │   └── migrations/
│       ├── sec/
│       │   ├── client.py
│       │   ├── cik.py
│       │   ├── filings.py
│       │   ├── facts.py
│       │   └── parser.py
│       ├── news/
│       │   ├── base.py
│       │   └── polygon.py
│       ├── prices/
│       │   ├── base.py
│       │   └── polygon.py
│       ├── events/
│       │   ├── classify.py
│       │   ├── score.py
│       │   └── build.py
│       ├── search/
│       │   └── service.py
│       ├── mcp_server/
│       │   ├── server.py
│       │   └── tools.py
│       └── workers/
│           ├── sync_companies.py
│           ├── sync_filings.py
│           ├── sync_facts.py
│           ├── sync_news.py
│           ├── sync_prices.py
│           └── build_events.py
└── tests/
    ├── test_sec_client.py
    ├── test_filing_parser.py
    ├── test_event_scoring.py
    └── test_mcp_tools.py
```

## MVP Acceptance Criteria

The MVP is complete when:

1. User can track a list of tickers.
2. System can map ticker to CIK.
3. System can fetch recent SEC filings for tracked tickers.
4. System stores filings in a database.
5. System downloads and parses primary filing documents.
6. System extracts useful text from 8-K, 10-Q, and 10-K filings.
7. System can ingest recent stock news from at least one provider.
8. System creates basic events from filings and news.
9. System exposes MCP tools for:
   - company lookup
   - recent filings
   - filing search
   - recent news
   - event timeline
10. Tool responses include source URLs and timestamps.
11. Tests cover SEC client, parser, event scoring, and at least one MCP tool.

## First Implementation Pass

Start by building the SEC-only MVP before adding paid news or price providers.

Build in this order:

1. Inspect the repo and identify existing language/framework conventions.
2. Create `.env.example`.
3. Add database models.
4. Add SEC client with rate limiting, retries, caching, and User-Agent.
5. Add ticker-to-CIK sync.
6. Add recent filings sync.
7. Add filing document download.
8. Add filing text extraction.
9. Add simple filing search.
10. Add MCP server with company, filings, filing detail, and filing search tools.
11. Add tests.
12. Document how to run locally.

## Important Implementation Notes

- Do not hardcode API keys.
- Do not commit secrets.
- Make all external providers optional.
- Keep provider interfaces clean.
- Preserve raw JSON from upstream APIs for debugging.
- Store source URLs everywhere.
- Use idempotent upserts for ingestion.
- Make workers safe to rerun.
- Add timestamps to all records.
- Normalize all tickers uppercase.
- Normalize all CIKs to 10-digit strings.
- Do not exceed SEC rate limits.
- Add backoff for 429, 403, 5xx, and network failures.
- Log enough to debug ingestion failures.
- Avoid broad refactors unrelated to this project.

## Example User Questions The Finished System Should Answer

- “What 8-Ks did TSLA file this week?”
- “Summarize Apple’s latest 10-Q and cite the source.”
- “Search Microsoft filings for AI capex mentions.”
- “What market-moving events happened for NVDA in the last 14 days?”
- “Why did AMD move yesterday?”
- “Show recent dilution-related filings across small-cap biotech.”
- “Find companies that recently mentioned going concern risk.”
- “What companies filed Item 2.02 8-Ks today?”
- “Show insider buying events from the last week.”
- “Compare recent risk factor changes for META and GOOGL.”

## Deliverables

When finished with each phase, report:

- What was built
- Files changed
- How to run it
- What tests passed
- Any limitations or follow-up work

Begin with the SEC-only MVP.
```
