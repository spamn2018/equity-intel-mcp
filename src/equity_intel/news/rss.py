"""RSS news feed ingestion with watchlist ticker/entity matching."""
from __future__ import annotations

import datetime
import email.utils
import hashlib
import re
import xml.etree.ElementTree as ET
from typing import Any, Dict, Iterable, List, Optional

import httpx

from equity_intel.db.models import Company
from equity_intel.logging_config import get_logger

logger = get_logger(__name__)


_ALIASES: Dict[str, List[str]] = {
    "GOOGL": ["Alphabet", "Google"],
    "META": ["Meta Platforms", "Meta", "Facebook"],
    "NVDA": ["Nvidia"],
    "AMD": ["Advanced Micro Devices"],
    "AVGO": ["Broadcom"],
    "MSFT": ["Microsoft"],
    "AMZN": ["Amazon"],
    "TSLA": ["Tesla"],
    "PLTR": ["Palantir"],
    "MSTR": ["MicroStrategy", "Strategy"],
    "SMCI": ["Super Micro", "Supermicro"],
    "ISRG": ["Intuitive Surgical"],
    "SYM": ["Symbotic"],
    "AI": ["C3.ai", "C3 AI"],
    "BOTZ": ["BOTZ"],
    "ROBO": ["ROBO"],
}


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", " ", text or "").strip()


def _child_text(node: ET.Element, names: Iterable[str]) -> str:
    for name in names:
        child = node.find(name)
        if child is not None and child.text:
            return child.text.strip()
    return ""


def _parse_date(value: str) -> Optional[datetime.datetime]:
    if not value:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.timezone.utc)
        return parsed.astimezone(datetime.timezone.utc)
    except Exception:
        try:
            return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except Exception:
            return None


def _aliases_for(company: Company) -> List[str]:
    ticker = company.ticker.upper()
    aliases = [] if ticker == "AI" else [ticker]
    if company.name:
        aliases.append(company.name)
        cleaned = re.sub(
            r"\b(inc|inc\.|corp|corp\.|corporation|class a|common stock|plc|ltd|co\.)\b",
            "",
            company.name,
            flags=re.I,
        ).strip(" ,.-")
        if cleaned and cleaned != company.name:
            aliases.append(cleaned)
    aliases.extend(_ALIASES.get(ticker, []))
    return [a for a in dict.fromkeys(aliases) if a]


def _matches_company(text: str, company: Company) -> bool:
    for alias in _aliases_for(company):
        if re.search(rf"(?<![A-Za-z0-9]){re.escape(alias)}(?![A-Za-z0-9])", text, re.I):
            return True
    return False


async def fetch_rss_articles(
    feed_urls: List[str],
    companies: List[Company],
    days: int = 1,
) -> List[Dict[str, Any]]:
    """Fetch RSS entries and return normalized articles matched to companies."""
    if not feed_urls:
        return []

    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
    articles: List[Dict[str, Any]] = []

    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        for feed_url in feed_urls:
            try:
                resp = await client.get(feed_url)
                resp.raise_for_status()
                root = ET.fromstring(resp.content)
            except Exception as exc:
                logger.warning("rss_news_fetch_failed", feed_url=feed_url, error=str(exc))
                continue

            items = root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")
            for item in items:
                title = _child_text(item, ["title", "{http://www.w3.org/2005/Atom}title"])
                link = _child_text(item, ["link", "guid", "{http://www.w3.org/2005/Atom}id"])
                atom_link = item.find("{http://www.w3.org/2005/Atom}link")
                if atom_link is not None and atom_link.attrib.get("href"):
                    link = atom_link.attrib["href"]
                summary = _strip_html(
                    _child_text(
                        item,
                        [
                            "description",
                            "summary",
                            "{http://www.w3.org/2005/Atom}summary",
                            "{http://purl.org/rss/1.0/modules/content/}encoded",
                        ],
                    )
                )
                published_at = _parse_date(
                    _child_text(
                        item,
                        [
                            "pubDate",
                            "published",
                            "updated",
                            "{http://www.w3.org/2005/Atom}published",
                            "{http://www.w3.org/2005/Atom}updated",
                        ],
                    )
                )
                if published_at and published_at < cutoff:
                    continue

                text = f"{title}\n{summary}"
                matched = [c for c in companies if _matches_company(text, c)]
                if not matched:
                    continue

                for company in matched:
                    ticker = company.ticker.upper()
                    stable = hashlib.sha1(f"{feed_url}|{link}|{ticker}".encode("utf-8")).hexdigest()
                    articles.append(
                        {
                            "provider": "rss",
                            "provider_id": stable,
                            "ticker": ticker,
                            "title": title,
                            "summary": summary,
                            "url": link,
                            "publisher": "RSS",
                            "author": "",
                            "published_at": published_at,
                            "tickers": [ticker],
                            "sentiment": None,
                            "raw": {"feed_url": feed_url, "matched_aliases": _aliases_for(company)},
                        }
                    )

    logger.info("rss_news_fetched", feeds=len(feed_urls), count=len(articles))
    return articles
