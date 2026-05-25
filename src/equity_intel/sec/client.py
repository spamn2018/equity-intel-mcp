"""
SEC EDGAR HTTP client.

- Respects the SEC's 10 req/s rate limit (configurable, default 8 req/s)
- Retries on 429, 5xx, and transient network errors with exponential back-off
- Disk-based caching via diskcache to avoid re-fetching stable responses
- Sets a proper User-Agent as required by SEC EDGAR
"""
from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional

import diskcache
import httpx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from equity_intel.config import settings
from equity_intel.logging_config import get_logger

logger = get_logger(__name__)

SEC_BASE = "https://data.sec.gov"
SEC_ARCHIVES = "https://www.sec.gov/Archives/edgar/data"
SEC_SUBMISSIONS_URL = f"{SEC_BASE}/submissions/CIK{{cik}}.json"
SEC_COMPANY_FACTS_URL = f"{SEC_BASE}/api/xbrl/companyfacts/CIK{{cik}}.json"
SEC_COMPANY_CONCEPT_URL = (
    f"{SEC_BASE}/api/xbrl/companyconcept/CIK{{cik}}/{{taxonomy}}/{{concept}}.json"
)
SEC_TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_TICKER_EXCHANGE_URL = "https://www.sec.gov/files/company_tickers_exchange.json"


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (429, 500, 502, 503, 504)
    if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError)):
        return True
    return False


class RateLimiter:
    """Simple token-bucket rate limiter."""

    def __init__(self, rps: float) -> None:
        self.rps = rps
        self._min_interval = 1.0 / rps
        self._last_call: float = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = self._min_interval - (now - self._last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = time.monotonic()


class SECClient:
    """Async SEC EDGAR client with rate-limiting, retries, and caching."""

    def __init__(
        self,
        user_agent: Optional[str] = None,
        rps: Optional[float] = None,
        cache_dir: Optional[str | Path] = None,
        cache_ttl: Optional[int] = None,
    ) -> None:
        self.user_agent = user_agent or settings.sec_user_agent
        self.rps = rps or settings.sec_rate_limit_rps
        self.cache_ttl = cache_ttl if cache_ttl is not None else settings.sec_cache_ttl_seconds

        cache_path = Path(cache_dir or settings.sec_cache_path)
        cache_path.mkdir(parents=True, exist_ok=True)
        self._cache: diskcache.Cache = diskcache.Cache(str(cache_path))

        self._limiter = RateLimiter(self.rps)
        self._client: Optional[httpx.AsyncClient] = None

    def _make_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            headers={
                "User-Agent": self.user_agent,
                "Accept-Encoding": "gzip, deflate",
                "Accept": "application/json, text/html, */*",
            },
            timeout=httpx.Timeout(30.0),
            follow_redirects=True,
        )

    async def __aenter__(self) -> "SECClient":
        self._client = self._make_client()
        return self

    async def __aexit__(self, *args: Any) -> None:
        if self._client:
            await self._client.aclose()

    @retry(
        retry=retry_if_exception(_is_retryable),
        wait=wait_exponential(multiplier=1, min=2, max=60),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    async def _fetch_raw(self, url: str) -> bytes:
        """Raw fetch with rate-limiting, no cache."""
        await self._limiter.acquire()
        client = self._client or self._make_client()
        logger.debug("sec_fetch", url=url)
        resp = await client.get(url)
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", "10"))
            logger.warning("sec_rate_limited", url=url, retry_after=retry_after)
            await asyncio.sleep(retry_after)
            resp.raise_for_status()
        resp.raise_for_status()
        return resp.content

    async def get_json(self, url: str, cache: bool = True) -> Any:
        """Fetch JSON from SEC, with optional disk cache."""
        if cache and url in self._cache:
            logger.debug("sec_cache_hit", url=url)
            return self._cache[url]

        raw = await self._fetch_raw(url)
        import json

        data = json.loads(raw)

        if cache:
            self._cache.set(url, data, expire=self.cache_ttl)

        return data

    async def get_text(self, url: str, cache: bool = True) -> str:
        """Fetch text/HTML from SEC, with optional disk cache."""
        cache_key = f"text:{url}"
        if cache and cache_key in self._cache:
            logger.debug("sec_cache_hit", url=url)
            return self._cache[cache_key]

        raw = await self._fetch_raw(url)
        text = raw.decode("utf-8", errors="replace")

        if cache:
            self._cache.set(cache_key, text, expire=self.cache_ttl)

        return text

    # ------------------------------------------------------------------ #
    # High-level helpers                                                   #
    # ------------------------------------------------------------------ #

    async def get_ticker_map(self) -> Dict[str, Any]:
        """Return the SEC ticker-to-CIK mapping JSON."""
        return await self.get_json(SEC_TICKER_MAP_URL)

    async def get_ticker_exchange_map(self) -> Dict[str, Any]:
        """Return the extended ticker-exchange-CIK mapping JSON."""
        return await self.get_json(SEC_TICKER_EXCHANGE_URL)

    async def get_submissions(self, cik: str) -> Dict[str, Any]:
        """Return the submissions JSON for a given 10-digit CIK."""
        url = SEC_SUBMISSIONS_URL.format(cik=cik)
        return await self.get_json(url, cache=True)

    async def get_company_facts(self, cik: str) -> Dict[str, Any]:
        """Return the full XBRL company facts JSON for a given 10-digit CIK."""
        url = SEC_COMPANY_FACTS_URL.format(cik=cik)
        return await self.get_json(url, cache=True)

    async def get_filing_document(self, url: str) -> str:
        """Download a filing document (HTML or text) from SEC EDGAR.

        Normalises the CIK segment of Archives URLs before fetching.
        The SEC Archives web server returns 301 Moved Permanently for
        zero-padded CIKs (e.g. /edgar/data/0001045810/) and redirects to
        the un-padded form (/edgar/data/1045810/).  Stripping leading zeros
        here avoids the extra round-trip for both newly-built and DB-stored URLs.
        """
        url = _normalize_archives_url(url)
        return await self.get_text(url, cache=True)

    def invalidate(self, url: str) -> None:
        """Remove a URL from cache."""
        self._cache.delete(url)
        self._cache.delete(f"text:{url}")


def normalize_cik(cik: Any) -> str:
    """Pad CIK to 10 digits."""
    return str(int(cik)).zfill(10)


def _cik_for_archives(cik: str) -> str:
    """SEC Archives URLs use the CIK without leading zeros (e.g. 1035267, not 0001035267).
    The submissions/facts APIs use the zero-padded form; the Archives web server
    returns 301 Moved Permanently when the padded form is used there."""
    return str(int(cik))


# Matches the zero-padded CIK segment in an Archives URL:
#   /Archives/edgar/data/0001045810/  →  /Archives/edgar/data/1045810/
_ARCHIVES_CIK_RE = re.compile(r"(/Archives/edgar/data/)0+(\d+)/")


def _normalize_archives_url(url: str) -> str:
    """Strip leading zeros from the CIK segment of an SEC Archives URL.

    Safe to call on any URL — returns unchanged if it doesn't match the pattern.
    Covers both DB-stored URLs (built before the zero-pad fix) and any future
    code path that may supply a padded CIK.
    """
    return _ARCHIVES_CIK_RE.sub(r"\g<1>\2/", url)


def build_filing_index_url(cik: str, accession_number: str) -> str:
    """Build the SEC EDGAR index URL for a filing."""
    accession_clean = accession_number.replace("-", "")
    return f"{SEC_ARCHIVES}/{_cik_for_archives(cik)}/{accession_clean}/{accession_number}-index.htm"


def build_filing_document_url(cik: str, accession_number: str, filename: str) -> str:
    """Build the full URL for a specific filing document."""
    accession_clean = accession_number.replace("-", "")
    return f"{SEC_ARCHIVES}/{_cik_for_archives(cik)}/{accession_clean}/{filename}"
