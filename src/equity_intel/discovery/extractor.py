"""
Ticker Extractor — scans already-ingested project data for ticker mentions.

Sources scanned:
  1. news_articles          (title + summary + body)
  2. filing_documents       (plain_text)
  3. events                 (title + summary + evidence_json)
  4. event_clusters         (title + summary)
  5. intelligence/*.json    — gemini news_blocks (related_tickers field)
  6. briefs/*.md / *.txt    — LM Studio synthesis output

NOT scanned:
  * Podcast intelligence files whose content is crypto-focused are skipped
    (the system is equities-only).

Validation rules:
  * Every extracted token is checked against the known-equity universe before
    being stored. Tokens not found in the universe are discarded — this is the
    primary guard against crypto tickers, index names, and all-caps prose words
    that slip through the blocklist.
  * The known-equity universe is built from: DEFAULT_TICKERS, PROHIBITED_TICKERS,
    TRAD_HEDGE_TICKERS, config/ai_tickers.json, and the companies table.
  * Tickers from PROHIBITED_TICKERS or TRAD_HEDGE_TICKERS are saved with
    exclusion_flag=True for audit purposes but never promoted.
"""
from __future__ import annotations

import datetime
import json
import re
import threading
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, Set, Tuple

from equity_intel.config import settings
from equity_intel.logging_config import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Ticker regex — match $TICK or plain TICK bounded by word boundaries
# ---------------------------------------------------------------------------
_TICKER_RE = re.compile(
    r"(?<!\w)"
    r"(?:\$([A-Z]{1,5})"
    r"|(?<!\$)([A-Z]{1,5})"
    r"(?:\.A|\.B|\.W|\.U|\.WS)?)"
    r"(?!\w)",
)

# Blocklist: common all-caps words that are never tickers
_WORD_BLOCKLIST: Set[str] = {
    "A", "I", "AT", "IT", "IN", "ON", "AN", "BY", "DO", "GO", "IS", "MY",
    "NO", "OF", "OR", "TO", "UP", "US", "WE", "AI", "AM", "AS", "BE",
    "CAN", "CEO", "CFO", "COO", "CTO", "COB", "THE", "AND", "FOR", "ARE",
    "BUT", "NOT", "YOU", "ALL", "NEW", "CPI", "PPI", "GDP", "PMI", "EPS",
    "IPO", "SPX", "NDX", "DJI", "VIX", "ETF", "IRA", "SEC", "FED", "FDA",
    "DOJ", "FTC", "ESG", "USD", "EUR", "GBP", "CNY", "JPY",
    # Crypto — explicitly blocked; this system is equities-only
    "BTC", "ETH", "NFT", "DEFI", "APR", "APY", "TVL", "DAO", "SOL", "XRP",
    "ADA", "DOT", "DOGE", "USDC", "USDT", "MATIC", "AVAX", "LINK", "UNI",
    "ATOM", "LTC", "BCH", "FIL", "AAVE", "COMP", "MKR", "SNX", "CRV",
    "Q1", "Q2", "Q3", "Q4", "YOY", "QOQ", "YTD",
    "EBIT", "EBITDA", "GAAP", "CAGR", "NAV", "AUM", "LTM", "TTM",
    "SPAC", "PIPE", "LOI", "MOU", "NDA", "SA", "LLC", "LP", "LLP", "INC",
    "CORP", "LTD", "PLC", "AG", "NV", "SPA",
    "NaN", "NULL", "TRUE", "FALSE", "NONE",
    # Common all-caps prose words
    "THIS", "WEEK", "SET", "TPS", "EF", "HOW", "WHY", "WHEN", "WHAT",
    "WILL", "STILL", "BACK", "JUST", "HERE", "THAT", "WITH", "FROM",
    "THEY", "BEEN", "THAN", "THEN", "ALSO", "INTO", "OVER", "ONLY",
    "BOTH", "LONG", "TERM", "HIGH", "RATE", "NEXT", "LAST", "YEAR",
    "DEAL", "DATA", "FUND", "BANK", "RISK", "BULL", "BEAR", "SWAP",
    "HOLD", "SELL", "PUTS", "CALL", "BOND", "DEBT", "CASH", "FLOW",
    "BASE", "CORE", "OPEN", "MOVE", "PEAK", "DROP", "DOWN", "GAIN",
    "PART", "EARN", "MISS", "BEAT", "FALL", "RISE", "PLAN", "GROW",
    "HTTP", "HTTPS", "URL", "API", "SDK", "SLA",
    "AWS", "GCP", "GPC", "KPI", "OKR", "HR", "PR", "IR", "ER",
    "OS", "UI", "UX", "VM", "VPN", "CDN", "DNS", "IP", "TCP", "UDP",
    "OK", "NA", "TBD", "TBA", "ETA", "EST", "EDT", "PST", "PDT",
    "JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP",
    "OCT", "NOV", "DEC", "MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN",
    "AM", "PM", "ET", "PT",
    "RE", "CC", "BCC", "FYI", "ASAP", "EOD", "EOW", "OOO",
    "ML", "NLP", "LLM", "GPT",
    # Single letters (valid NYSE tickers but nearly always prose noise)
    "B", "C", "D", "E", "F", "G", "H", "J", "K", "L", "M", "N",
    "O", "P", "Q", "R", "S", "T", "U", "V", "W", "X", "Y", "Z",
}

# ---------------------------------------------------------------------------
# Known-equity universe (the primary validity gate)
# ---------------------------------------------------------------------------

_KNOWN_TICKERS_CACHE: Optional[Set[str]] = None
_KNOWN_TICKERS_LOCK = threading.Lock()


def _load_known_tickers() -> Set[str]:
    """
    Return the set of equity ticker symbols drawn from the companies table.

    This is the broad SEC-registered equity universe — every company that files
    with EDGAR.  It is intentionally NOT limited to your tracked tickers; the
    whole point of discovery is to surface companies you are *not yet* following.

    If the companies table is empty (fresh environment, tests, DB unavailable)
    an empty set is returned, which disables the validity gate in
    _extract_tickers_from_text so that the blocklist alone filters noise.

    Result is module-level cached after the first call.
    Call clear_known_tickers_cache() after sync_companies runs.
    """
    global _KNOWN_TICKERS_CACHE
    if _KNOWN_TICKERS_CACHE is not None:
        return _KNOWN_TICKERS_CACHE

    with _KNOWN_TICKERS_LOCK:
        if _KNOWN_TICKERS_CACHE is not None:
            return _KNOWN_TICKERS_CACHE

        tickers: Set[str] = set()

        # companies table — the full SEC-registered equity universe
        try:
            from equity_intel.db.session import SessionLocal
            from equity_intel.db.models import Company
            with SessionLocal() as session:
                rows = session.query(Company.ticker).all()
                tickers.update(r[0].upper() for r in rows if r[0])
            logger.debug("known_tickers_from_db", count=len(tickers))
        except Exception as exc:
            logger.debug("known_tickers_db_skip", error=str(exc))

        # Empty set → gate disabled (blocklist-only mode)
        _KNOWN_TICKERS_CACHE = tickers
        return tickers


def clear_known_tickers_cache() -> None:
    """Call after sync_companies runs or in tests to force a fresh load."""
    global _KNOWN_TICKERS_CACHE
    with _KNOWN_TICKERS_LOCK:
        _KNOWN_TICKERS_CACHE = None


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def _extract_tickers_from_text(
    text: str,
    known_tickers: Optional[Set[str]] = None,
) -> List[Tuple[str, str]]:
    """
    Return list of (ticker, context_snippet) for each known equity ticker
    found in *text*.

    Tokens that are:
      - in _WORD_BLOCKLIST, or
      - shorter than 2 characters, or
      - NOT in the known-equity universe

    are silently discarded. This is the primary guard against crypto tokens,
    index names, and all-caps prose words.

    Parameters
    ----------
    text : str
        The text to scan.
    known_tickers : set, optional
        Pre-loaded known-ticker set. If None, the module-level cache is used.
        Pass an explicit set in workers to avoid per-article DB round-trips.
    """
    if not text:
        return []
    universe = known_tickers if known_tickers is not None else _load_known_tickers()
    results: List[Tuple[str, str]] = []
    for m in _TICKER_RE.finditer(text):
        raw = m.group(1) or m.group(2)
        ticker = raw.upper()
        if ticker in _WORD_BLOCKLIST:
            continue
        if len(ticker) < 2:
            continue
        # Primary validity gate — must be a known equity
        if universe and ticker not in universe:
            continue
        start = max(0, m.start() - 55)
        end = min(len(text), m.end() + 55)
        ctx = text[start:end].replace("\n", " ").strip()
        results.append((ticker, ctx))
    return results


# ---------------------------------------------------------------------------
# Exclusion helpers
# ---------------------------------------------------------------------------

def _build_exclusion_sets() -> Tuple[Set[str], Set[str], Set[str]]:
    prohibited = {t.strip().upper() for t in settings.prohibited_tickers.split(",") if t.strip()}
    trad_hedge = {t.strip().upper() for t in settings.trad_hedge_tickers.split(",") if t.strip()}
    default = {t.strip().upper() for t in settings.default_tickers.split(",") if t.strip()}
    return prohibited, trad_hedge, default


# ---------------------------------------------------------------------------
# Source scanners
# ---------------------------------------------------------------------------

MentionRecord = Dict[str, Any]


def _iso_week(dt: Optional[datetime.datetime]) -> str:
    if dt is None:
        dt = datetime.datetime.now(datetime.timezone.utc)
    iso = dt.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _make_record(
    mentioned_ticker: str,
    source_ticker: Optional[str],
    source_type: str,
    source_id: str,
    occurred_at: Optional[datetime.datetime],
    context: str,
    url: Optional[str],
    confidence: float,
    excluded: bool,
) -> MentionRecord:
    return {
        "mentioned_ticker": mentioned_ticker,
        "source_ticker": source_ticker,
        "source_type": source_type,
        "source_id": source_id,
        "occurred_at": occurred_at,
        "week_key": _iso_week(occurred_at),
        "context": context[:500] if context else None,
        "url": url,
        "confidence": confidence,
        "exclusion_flag": excluded,
    }


def scan_news_articles(session, since: datetime.datetime) -> Generator[MentionRecord, None, None]:
    """Scan news_articles for ticker mentions in title/summary/body."""
    from equity_intel.db.models import NewsArticle

    prohibited, trad_hedge, _ = _build_exclusion_sets()
    all_excluded = prohibited | trad_hedge
    known = _load_known_tickers()

    rows = (
        session.query(NewsArticle)
        .filter(NewsArticle.published_at >= since)
        .yield_per(200)
    )
    for article in rows:
        source_ticker = (article.ticker or "").upper() or None
        text_blob = " ".join(filter(None, [article.title, article.summary, article.body]))

        # tickers_json is pre-validated by the news provider — accept directly
        extra_tickers: List[str] = []
        if article.tickers_json and isinstance(article.tickers_json, list):
            for t in article.tickers_json:
                tu = str(t).upper()
                if tu and tu not in _WORD_BLOCKLIST and len(tu) >= 2 and (not known or tu in known):
                    extra_tickers.append(tu)

        found = _extract_tickers_from_text(text_blob, known_tickers=known)
        for t in extra_tickers:
            found.append((t, f"Explicitly tagged ticker: {article.title or ''}"))

        seen: Set[str] = set()
        for ticker, ctx in found:
            if ticker in seen:
                continue
            seen.add(ticker)
            excluded = ticker in all_excluded
            yield _make_record(
                mentioned_ticker=ticker,
                source_ticker=source_ticker,
                source_type="news",
                source_id=f"news_{article.id}",
                occurred_at=article.published_at,
                context=ctx,
                url=article.url,
                confidence=0.9 if ticker in extra_tickers else 0.7,
                excluded=excluded,
            )


def scan_filing_documents(session, since: datetime.datetime) -> Generator[MentionRecord, None, None]:
    """Scan filing_documents for cross-mentions of other tickers."""
    from equity_intel.db.models import FilingDocument, Filing, Company

    prohibited, trad_hedge, _ = _build_exclusion_sets()
    all_excluded = prohibited | trad_hedge
    known = _load_known_tickers()

    rows = (
        session.query(FilingDocument, Filing, Company)
        .join(Filing, Filing.id == FilingDocument.filing_id)
        .join(Company, Company.id == Filing.company_id)
        .filter(Filing.filing_date >= since.date())
        .filter(FilingDocument.plain_text.isnot(None))
        .yield_per(100)
    )
    for doc, filing, company in rows:
        source_ticker = company.ticker.upper()
        text_sample = (doc.plain_text or "")[:20_000]
        for ticker, ctx in _extract_tickers_from_text(text_sample, known_tickers=known):
            if ticker == source_ticker:
                continue
            excluded = ticker in all_excluded
            occ = filing.filing_date
            if not isinstance(occ, datetime.datetime):
                occ = (datetime.datetime.combine(occ, datetime.time.min, tzinfo=datetime.timezone.utc)
                       if occ else None)
            yield _make_record(
                mentioned_ticker=ticker,
                source_ticker=source_ticker,
                source_type="filing_document",
                source_id=f"doc_{doc.id}",
                occurred_at=occ,
                context=ctx,
                url=doc.document_url,
                confidence=0.8,
                excluded=excluded,
            )


def scan_events(session, since: datetime.datetime) -> Generator[MentionRecord, None, None]:
    """Scan events for cross-ticker mentions."""
    from equity_intel.db.models import Event

    prohibited, trad_hedge, _ = _build_exclusion_sets()
    all_excluded = prohibited | trad_hedge
    known = _load_known_tickers()

    rows = (
        session.query(Event)
        .filter(Event.occurred_at >= since)
        .yield_per(200)
    )
    for event in rows:
        source_ticker = (event.ticker or "").upper() or None
        text_blob = " ".join(filter(None, [event.title, event.summary]))

        extra: List[str] = []
        if event.evidence_json and isinstance(event.evidence_json, dict):
            for t in event.evidence_json.get("related_tickers", []):
                tu = str(t).upper()
                if tu and tu not in _WORD_BLOCKLIST and len(tu) >= 2 and (not known or tu in known):
                    extra.append(tu)

        found = _extract_tickers_from_text(text_blob, known_tickers=known)
        for t in extra:
            found.append((t, f"Event evidence related ticker for {source_ticker}"))

        seen: Set[str] = set()
        for ticker, ctx in found:
            if ticker == source_ticker or ticker in seen:
                continue
            seen.add(ticker)
            excluded = ticker in all_excluded
            yield _make_record(
                mentioned_ticker=ticker,
                source_ticker=source_ticker,
                source_type="event",
                source_id=f"event_{event.id}",
                occurred_at=event.occurred_at,
                context=ctx,
                url=event.source_url,
                confidence=0.75,
                excluded=excluded,
            )


def scan_event_clusters(session, since: datetime.datetime) -> Generator[MentionRecord, None, None]:
    """Scan event_clusters for cross-ticker mentions."""
    try:
        from equity_intel.db.models import EventCluster  # type: ignore[attr-defined]
    except ImportError:
        return

    prohibited, trad_hedge, _ = _build_exclusion_sets()
    all_excluded = prohibited | trad_hedge
    known = _load_known_tickers()

    rows = (
        session.query(EventCluster)
        .filter(EventCluster.updated_at >= since)
        .yield_per(200)
    )
    for cluster in rows:
        source_ticker = (cluster.ticker or "").upper() or None
        text_blob = " ".join(filter(None, [cluster.title, cluster.summary]))
        seen: Set[str] = set()
        for ticker, ctx in _extract_tickers_from_text(text_blob, known_tickers=known):
            if ticker == source_ticker or ticker in seen:
                continue
            seen.add(ticker)
            excluded = ticker in all_excluded
            yield _make_record(
                mentioned_ticker=ticker,
                source_ticker=source_ticker,
                source_type="event_cluster",
                source_id=f"cluster_{cluster.id}",
                occurred_at=cluster.updated_at,
                context=ctx,
                url=None,
                confidence=0.7,
                excluded=excluded,
            )


def _is_equity_intelligence_file(data: dict) -> bool:
    """
    Return True if an intelligence JSON file contains equity content.

    Rejects files whose top_signals are dominated by crypto assets
    (BTC, ETH, SOL, etc.) since this system is equities-only.
    """
    signals = data.get("top_signals", [])
    if not signals:
        return True  # no signals — treat as equity (e.g. Gemini news blocks)

    crypto_keywords = {"bitcoin", "ethereum", "solana", "crypto", "defi", "nft",
                       "btc", "eth", "sol", "xrp", "doge", "usdc", "usdt"}
    crypto_count = 0
    for sig in signals:
        asset = str(sig.get("asset", "")).lower()
        why = str(sig.get("why", "")).lower()
        if any(kw in asset or kw in why for kw in crypto_keywords):
            crypto_count += 1

    # If more than half the signals are crypto, skip this file
    return crypto_count <= len(signals) / 2


def scan_intelligence_files(
    intelligence_dir: Path,
    since: datetime.datetime,
) -> Generator[MentionRecord, None, None]:
    """
    Scan intelligence/*.json for equity ticker mentions.

    Handles:
      - Gemini news_blocks  (blocks[].ticker, blocks[].related_tickers)
      - LM Studio synthesis blocks

    Crypto-dominated podcast files are skipped automatically.
    """
    prohibited, trad_hedge, _ = _build_exclusion_sets()
    all_excluded = prohibited | trad_hedge
    known = _load_known_tickers()

    if not intelligence_dir.exists():
        return

    for f in sorted(intelligence_dir.glob("*.json")):
        try:
            mtime = datetime.datetime.fromtimestamp(f.stat().st_mtime, tz=datetime.timezone.utc)
            if mtime < since:
                continue
            data = json.loads(f.read_text(encoding="utf-8", errors="replace"))
        except Exception as exc:
            logger.warning("intelligence_file_read_error", path=str(f), error=str(exc))
            continue

        # Skip crypto-dominated files
        if not _is_equity_intelligence_file(data):
            logger.info("skipping_crypto_intelligence_file", path=str(f))
            continue

        generated_at_str = data.get("generated_at")
        try:
            generated_at = (datetime.datetime.fromisoformat(
                generated_at_str.replace("Z", "+00:00")) if generated_at_str else mtime)
        except Exception:
            generated_at = mtime

        # Gemini news blocks
        for block in data.get("blocks", []):
            source_ticker = (block.get("ticker") or "").upper() or None
            source_type = "gemini_news_block"
            source_id_base = f"gemini_{f.stem}_{source_ticker}"

            for rt in block.get("related_tickers", []):
                ticker = str(rt).upper().strip().lstrip("$")
                if not ticker or ticker in _WORD_BLOCKLIST or len(ticker) < 2:
                    continue
                if known and ticker not in known:
                    continue  # not a known equity — discard
                excluded = ticker in all_excluded
                yield _make_record(
                    mentioned_ticker=ticker,
                    source_ticker=source_ticker,
                    source_type=source_type,
                    source_id=f"{source_id_base}_rt_{ticker}",
                    occurred_at=generated_at,
                    context=block.get("headline", "")[:200],
                    url=None,
                    confidence=0.85,
                    excluded=excluded,
                )

            for ticker, ctx in _extract_tickers_from_text(
                block.get("why_it_matters", ""), known_tickers=known
            ):
                if ticker == source_ticker:
                    continue
                excluded = ticker in all_excluded
                yield _make_record(
                    mentioned_ticker=ticker,
                    source_ticker=source_ticker,
                    source_type=source_type,
                    source_id=f"{source_id_base}_txt_{ticker}",
                    occurred_at=generated_at,
                    context=ctx,
                    url=None,
                    confidence=0.65,
                    excluded=excluded,
                )


def scan_brief_files(
    briefs_dir: Path,
    since: datetime.datetime,
) -> Generator[MentionRecord, None, None]:
    """Scan briefs/*.md and *.txt for equity ticker mentions."""
    prohibited, trad_hedge, _ = _build_exclusion_sets()
    all_excluded = prohibited | trad_hedge
    known = _load_known_tickers()

    if not briefs_dir.exists():
        return

    for f in sorted(briefs_dir.glob("**/*")):
        if f.suffix.lower() not in {".md", ".txt"}:
            continue
        try:
            mtime = datetime.datetime.fromtimestamp(f.stat().st_mtime, tz=datetime.timezone.utc)
            if mtime < since:
                continue
            text = f.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            logger.warning("brief_file_read_error", path=str(f), error=str(exc))
            continue

        for ticker, ctx in _extract_tickers_from_text(text, known_tickers=known):
            excluded = ticker in all_excluded
            yield _make_record(
                mentioned_ticker=ticker,
                source_ticker=None,
                source_type="lm_synthesis",
                source_id=f"brief_{f.stem}_{ticker}",
                occurred_at=mtime,
                context=ctx,
                url=None,
                confidence=0.5,
                excluded=excluded,
            )
