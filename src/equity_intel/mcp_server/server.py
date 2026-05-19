"""
Equity Intelligence MCP Server.

Exposes financial research tools to AI agents via the Model Context Protocol.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional, Sequence

import mcp.server.stdio
import mcp.types as types
from mcp.server import Server
from mcp.server.models import InitializationOptions

from equity_intel.config import settings
from equity_intel.db.session import SessionLocal, create_all_tables
from equity_intel.logging_config import configure_logging, get_logger
from equity_intel.mcp_server.tools import (
    explain_stock_move,
    get_company,
    get_company_facts,
    get_events,
    get_filing,
    get_institutional_holders,
    get_manager_holdings,
    get_recent_filings,
    get_recent_news,
    screen_catalysts,
    get_event_cluster,
    get_watchlist_brief,
    search_filings_tool,
    search_news_tool,
)

logger = get_logger(__name__)

app = Server(settings.mcp_server_name)


def _run_tool(tool_fn, **kwargs) -> str:
    """Execute a tool function with a fresh DB session and return JSON string."""
    session = SessionLocal()
    try:
        result = tool_fn(session=session, **kwargs)
        return json.dumps(result, default=str, indent=2)
    except Exception as exc:
        logger.error("tool_error", tool=tool_fn.__name__, error=str(exc))
        return json.dumps({"error": str(exc), "tool": tool_fn.__name__})
    finally:
        session.close()


# ------------------------------------------------------------------ #
# Tool registry                                                         #
# ------------------------------------------------------------------ #


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="get_company",
            description=(
                "Look up a company's profile, CIK, exchange, sector/industry, "
                "and latest filing dates by ticker symbol."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "ticker": {
                        "type": "string",
                        "description": "Stock ticker symbol (e.g. AAPL, MSFT, NVDA)",
                    }
                },
                "required": ["ticker"],
            },
        ),
        types.Tool(
            name="get_recent_filings",
            description=(
                "Return recent SEC filings for a company. Includes accession numbers, "
                "form types, filing dates, 8-K item details, and direct SEC URLs."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "ticker": {"type": "string", "description": "Stock ticker symbol"},
                    "form_types": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Filter by form types e.g. ['8-K', '10-Q']",
                    },
                    "days": {
                        "type": "integer",
                        "description": "Look-back window in days (default 90)",
                        "default": 90,
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 20)",
                        "default": 20,
                    },
                },
                "required": ["ticker"],
            },
        ),
        types.Tool(
            name="get_filing",
            description=(
                "Retrieve full details and parsed text for a specific filing by accession number. "
                "Includes 8-K sections, detected keywords, and source URLs."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "accession_number": {
                        "type": "string",
                        "description": "SEC accession number e.g. 0001234567-24-000001",
                    }
                },
                "required": ["accession_number"],
            },
        ),
        types.Tool(
            name="search_filings",
            description=(
                "Full-text search over SEC filing documents. "
                "Returns snippets with source links. "
                "Example: search for 'going concern' or 'AI capital expenditure' or 'FDA rejection'."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "ticker": {
                        "type": "string",
                        "description": "Restrict to a specific ticker",
                    },
                    "form_types": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Filter by form types",
                    },
                    "start_date": {
                        "type": "string",
                        "description": "Start date YYYY-MM-DD",
                    },
                    "end_date": {
                        "type": "string",
                        "description": "End date YYYY-MM-DD",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 20)",
                        "default": 20,
                    },
                },
                "required": ["query"],
            },
        ),
        types.Tool(
            name="get_company_facts",
            description=(
                "Return normalized XBRL financial facts (revenue, net income, EPS, cash, debt, etc.) "
                "sourced from SEC EDGAR. Includes fiscal period, accession number, and source URL."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "ticker": {"type": "string", "description": "Stock ticker symbol"},
                    "concepts": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "XBRL concept names e.g. ['Revenues', 'NetIncomeLoss']",
                    },
                    "fiscal_periods": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Filter by fiscal period e.g. ['FY', 'Q1', 'Q2', 'Q3']",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max facts to return (default 50)",
                        "default": 50,
                    },
                },
                "required": ["ticker"],
            },
        ),
        types.Tool(
            name="get_recent_news",
            description=(
                "Return recent news articles for a ticker or matching a keyword query. "
                "Includes publisher, title, summary, and source URL."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "ticker": {
                        "type": "string",
                        "description": "Stock ticker symbol (optional)",
                    },
                    "query": {
                        "type": "string",
                        "description": "Keyword to filter articles (optional)",
                    },
                    "days": {
                        "type": "integer",
                        "description": "Look-back window in days (default 7)",
                        "default": 7,
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 20)",
                        "default": 20,
                    },
                },
            },
        ),
        types.Tool(
            name="search_news",
            description=(
                "Full-text search over news articles with optional ticker and date filters."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "ticker": {"type": "string", "description": "Restrict to ticker"},
                    "start_date": {"type": "string", "description": "Start date YYYY-MM-DD"},
                    "end_date": {"type": "string", "description": "End date YYYY-MM-DD"},
                    "limit": {"type": "integer", "default": 20},
                },
                "required": ["query"],
            },
        ),
        types.Tool(
            name="get_events",
            description=(
                "Return ranked market events (earnings, M&A, filings, insider transactions, etc.) "
                "for a ticker or across the market. Events are scored by materiality."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "ticker": {
                        "type": "string",
                        "description": "Stock ticker (optional; if omitted returns cross-market events)",
                    },
                    "event_types": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Filter by event type: earnings, guidance, merger_acquisition, "
                            "offering_or_dilution, insider_transaction, activist_stake, "
                            "management_change, regulatory, litigation, bankruptcy_or_going_concern, "
                            "restatement, buyback, other"
                        ),
                    },
                    "days": {
                        "type": "integer",
                        "description": "Look-back window in days (default 30)",
                        "default": 30,
                    },
                    "min_materiality": {
                        "type": "number",
                        "description": "Minimum materiality score 0-1 (default 0.0)",
                        "default": 0.0,
                    },
                    "limit": {"type": "integer", "default": 20},
                },
            },
        ),
        types.Tool(
            name="explain_stock_move",
            description=(
                "Attempt to explain a stock price move by surfacing nearby filings and events. "
                "Uses 'likely related to' language — does not assert causation."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "ticker": {"type": "string", "description": "Stock ticker symbol"},
                    "date": {
                        "type": "string",
                        "description": "Date to analyze YYYY-MM-DD (default: today)",
                    },
                    "window": {
                        "type": "integer",
                        "description": "Days before/after date to search (default 3)",
                        "default": 3,
                    },
                },
                "required": ["ticker"],
            },
        ),
        types.Tool(
            name="screen_catalysts",
            description=(
                "Screen across the market for high-materiality events in the last N days. "
                "Filter by event type, sector, or specific tickers. Ranked by materiality score."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "event_types": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Filter by event type",
                    },
                    "days": {
                        "type": "integer",
                        "description": "Look-back window in days (default 7)",
                        "default": 7,
                    },
                    "min_materiality": {
                        "type": "number",
                        "description": "Minimum materiality score (default 0.5)",
                        "default": 0.5,
                    },
                    "tickers": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Restrict to specific tickers",
                    },
                    "sectors": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Filter by sector",
                    },
                    "limit": {"type": "integer", "default": 30},
                },
            },
        ),
        types.Tool(
            name="get_event_cluster",
            description=(
                "Return full details for a single event cluster: all linked filings, "
                "news articles, price reaction, and aggregate scores. "
                "Use cluster_id from get_events or screen_catalysts output."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "cluster_id": {
                        "type": "integer",
                        "description": "Cluster ID from get_events or screen_catalysts",
                    },
                    "cluster_key": {
                        "type": "string",
                        "description": "Cluster key string (alternative to cluster_id)",
                    },
                },
            },
        ),
        types.Tool(
            name="get_institutional_holders",
            description=(
                "Return institutional holders (13F-HR data) for a stock ticker. "
                "Shows which investment managers hold the stock, how many shares, "
                "the market value, and quarter-over-quarter changes. "
                "Data comes from SEC 13F-HR filings parsed by the sync_13f worker."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "ticker": {"type": "string", "description": "Stock ticker symbol"},
                    "quarters": {
                        "type": "integer",
                        "description": "Number of past quarters to return (default 4)",
                        "default": 4,
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max holders per quarter (default 25)",
                        "default": 25,
                    },
                },
                "required": ["ticker"],
            },
        ),
        types.Tool(
            name="get_manager_holdings",
            description=(
                "Return all equity holdings for a specific institutional manager "
                "from their most recent (or a specified) 13F-HR filing quarter. "
                "Identify the manager by CIK (e.g. '0001067983' for Berkshire Hathaway) "
                "or by a partial name string. "
                "Returns positions sorted by market value, with CUSIP, shares, and value."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "manager_cik": {
                        "type": "string",
                        "description": "SEC CIK for the manager (e.g. '0001067983'). Preferred over name.",
                    },
                    "manager_name": {
                        "type": "string",
                        "description": "Partial manager name for lookup (e.g. 'Berkshire', 'BlackRock'). "
                                       "Used only if manager_cik is not provided.",
                    },
                    "report_date": {
                        "type": "string",
                        "description": "Quarter-end date YYYY-MM-DD (default: most recent on record)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max positions to return (default 50)",
                        "default": 50,
                    },
                },
            },
        ),
        types.Tool(
            name="get_watchlist_brief",
            description=(
                "Generate a ranked catalyst brief for a watchlist of tickers. "
                "Answers: 'What are the most important stock-moving catalysts across my watchlist right now?' "
                "Returns a brief summary plus per-catalyst evidence: price move, volume context, "
                "linked filings, linked news, materiality/confidence scores, and cautious interpretation. "
                "Prefers cluster-level data (multi-source, price-enriched) over raw events. "
                "Uses 'likely related', 'may reflect', 'available evidence suggests' language throughout — "
                "never asserts causation."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "tickers": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of ticker symbols to include (e.g. ['AAPL', 'MSFT', 'NVDA'])",
                    },
                    "days": {
                        "type": "integer",
                        "description": "Look-back window in calendar days (default 7)",
                        "default": 7,
                    },
                    "min_materiality": {
                        "type": "number",
                        "description": "Minimum materiality score [0, 1] to include a catalyst (default 0.3)",
                        "default": 0.3,
                    },
                    "include_low_confidence": {
                        "type": "boolean",
                        "description": "Include catalysts with confidence_score < 0.3 (default false)",
                        "default": False,
                    },
                    "max_items": {
                        "type": "integer",
                        "description": "Maximum number of catalysts to return (default 20)",
                        "default": 20,
                    },
                    "event_types": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional event type filter: earnings, guidance, merger_acquisition, "
                            "offering_or_dilution, insider_transaction, activist_stake, "
                            "management_change, regulatory, litigation, "
                            "bankruptcy_or_going_concern, restatement, buyback, other"
                        ),
                    },
                    "include_price_context": {
                        "type": "boolean",
                        "description": "Include price move and volume data in each catalyst (default true)",
                        "default": True,
                    },
                    "include_news": {
                        "type": "boolean",
                        "description": "Include linked news articles in each catalyst (default true)",
                        "default": True,
                    },
                    "include_filings": {
                        "type": "boolean",
                        "description": "Include linked SEC filings in each catalyst (default true)",
                        "default": True,
                    },
                },
                "required": ["tickers"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(
    name: str, arguments: Dict[str, Any]
) -> list[types.TextContent]:
    logger.info("tool_called", tool=name, args=arguments)

    dispatch = {
        "get_company": lambda: _run_tool(get_company, ticker=arguments["ticker"]),
        "get_recent_filings": lambda: _run_tool(
            get_recent_filings,
            ticker=arguments["ticker"],
            form_types=arguments.get("form_types"),
            days=int(arguments.get("days", 90)),
            limit=int(arguments.get("limit", 20)),
        ),
        "get_filing": lambda: _run_tool(
            get_filing, accession_number=arguments["accession_number"]
        ),
        "search_filings": lambda: _run_tool(
            search_filings_tool,
            query=arguments["query"],
            ticker=arguments.get("ticker"),
            form_types=arguments.get("form_types"),
            start_date=arguments.get("start_date"),
            end_date=arguments.get("end_date"),
            limit=int(arguments.get("limit", 20)),
        ),
        "get_company_facts": lambda: _run_tool(
            get_company_facts,
            ticker=arguments["ticker"],
            concepts=arguments.get("concepts"),
            fiscal_periods=arguments.get("fiscal_periods"),
            limit=int(arguments.get("limit", 50)),
        ),
        "get_recent_news": lambda: _run_tool(
            get_recent_news,
            ticker=arguments.get("ticker"),
            query=arguments.get("query"),
            days=int(arguments.get("days", 7)),
            limit=int(arguments.get("limit", 20)),
        ),
        "search_news": lambda: _run_tool(
            search_news_tool,
            query=arguments["query"],
            ticker=arguments.get("ticker"),
            start_date=arguments.get("start_date"),
            end_date=arguments.get("end_date"),
            limit=int(arguments.get("limit", 20)),
        ),
        "get_events": lambda: _run_tool(
            get_events,
            ticker=arguments.get("ticker"),
            event_types=arguments.get("event_types"),
            days=int(arguments.get("days", 30)),
            min_materiality=float(arguments.get("min_materiality", 0.0)),
            limit=int(arguments.get("limit", 20)),
        ),
        "explain_stock_move": lambda: _run_tool(
            explain_stock_move,
            ticker=arguments["ticker"],
            date=arguments.get("date"),
            window=int(arguments.get("window", 3)),
        ),
        "screen_catalysts": lambda: _run_tool(
            screen_catalysts,
            event_types=arguments.get("event_types"),
            days=int(arguments.get("days", 7)),
            min_materiality=float(arguments.get("min_materiality", 0.5)),
            tickers=arguments.get("tickers"),
            sectors=arguments.get("sectors"),
            limit=int(arguments.get("limit", 30)),
        ),
        "get_event_cluster": lambda: _run_tool(
            get_event_cluster,
            cluster_id=int(arguments["cluster_id"]) if arguments.get("cluster_id") else None,
            cluster_key=arguments.get("cluster_key"),
        ),
        "get_institutional_holders": lambda: _run_tool(
            get_institutional_holders,
            ticker=arguments["ticker"],
            quarters=int(arguments.get("quarters", 4)),
            limit=int(arguments.get("limit", 25)),
        ),
        "get_manager_holdings": lambda: _run_tool(
            get_manager_holdings,
            manager_cik=arguments.get("manager_cik"),
            manager_name=arguments.get("manager_name"),
            report_date=arguments.get("report_date"),
            limit=int(arguments.get("limit", 50)),
        ),
        "get_watchlist_brief": lambda: _run_tool(
            get_watchlist_brief,
            tickers=arguments["tickers"],
            days=int(arguments.get("days", 7)),
            min_materiality=float(arguments.get("min_materiality", 0.3)),
            include_low_confidence=bool(arguments.get("include_low_confidence", False)),
            max_items=int(arguments.get("max_items", 20)),
            event_types=arguments.get("event_types"),
            include_price_context=bool(arguments.get("include_price_context", True)),
            include_news=bool(arguments.get("include_news", True)),
            include_filings=bool(arguments.get("include_filings", True)),
        ),
    }

    if name not in dispatch:
        result = json.dumps({"error": f"Unknown tool: {name}"})
    else:
        try:
            result = dispatch[name]()
        except Exception as exc:
            logger.error("dispatch_error", tool=name, error=str(exc))
            result = json.dumps({"error": str(exc)})

    return [types.TextContent(type="text", text=result)]


async def _serve() -> None:
    configure_logging(settings.log_level)
    logger.info("mcp_server_starting", name=settings.mcp_server_name)

    # Ensure tables exist (idempotent)
    try:
        create_all_tables()
    except Exception as exc:
        logger.warning("create_tables_failed", error=str(exc))

    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await app.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name=settings.mcp_server_name,
                server_version="0.1.0",
                capabilities=app.get_capabilities(
                    notification_options=None,
                    experimental_capabilities={},
                ),
            ),
        )


def main() -> None:
    asyncio.run(_serve())


if __name__ == "__main__":
    main()
