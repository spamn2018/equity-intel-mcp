"""Tests for the SEC EDGAR client."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import pytest_asyncio

from equity_intel.sec.client import (
    RateLimiter,
    SECClient,
    build_filing_document_url,
    build_filing_index_url,
    normalize_cik,
)


# ------------------------------------------------------------------ #
# normalize_cik                                                         #
# ------------------------------------------------------------------ #


def test_normalize_cik_pads_short():
    assert normalize_cik(12345) == "0000012345"


def test_normalize_cik_already_ten_digits():
    assert normalize_cik("0001234567") == "0001234567"


def test_normalize_cik_strips_leading_zeros_then_repads():
    # normalize_cik always pads to exactly 10 digits
    assert normalize_cik("0000001") == "0000000001"


def test_normalize_cik_string_int():
    assert normalize_cik("789") == "0000000789"


# ------------------------------------------------------------------ #
# URL builders                                                         #
# ------------------------------------------------------------------ #


def test_build_filing_index_url():
    url = build_filing_index_url("0001234567", "0001234567-24-000001")
    assert "0001234567" in url
    assert "000123456724000001" in url
    assert url.startswith("https://www.sec.gov/Archives/edgar/")


def test_build_filing_document_url():
    url = build_filing_document_url("0001234567", "0001234567-24-000001", "form8k.htm")
    assert url.endswith("form8k.htm")
    assert "000123456724000001" in url


# ------------------------------------------------------------------ #
# RateLimiter                                                          #
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_rate_limiter_does_not_exceed_rps():
    limiter = RateLimiter(rps=10)
    import time

    start = time.monotonic()
    for _ in range(3):
        await limiter.acquire()
    elapsed = time.monotonic() - start
    # 3 calls at 10 rps = at least 2 * 0.1s interval = 0.2s minimum
    assert elapsed >= 0.18, f"Rate limiter too fast: {elapsed:.3f}s"


# ------------------------------------------------------------------ #
# SECClient.get_json (mocked)                                          #
# ------------------------------------------------------------------ #


@pytest.fixture
def mock_sec_client(tmp_path):
    """Return a SECClient with a temp cache dir."""
    client = SECClient(
        user_agent="test-agent test@example.com",
        rps=100,  # no rate limiting in tests
        cache_dir=str(tmp_path / "cache"),
        cache_ttl=60,
    )
    return client


@pytest.mark.asyncio
async def test_get_json_returns_parsed_json(mock_sec_client, respx_mock):
    payload = {"key": "value", "items": [1, 2, 3]}
    respx_mock.get("https://data.sec.gov/test.json").mock(
        return_value=httpx.Response(200, json=payload)
    )

    async with mock_sec_client:
        result = await mock_sec_client.get_json("https://data.sec.gov/test.json", cache=False)

    assert result == payload


@pytest.mark.asyncio
async def test_get_json_uses_cache(mock_sec_client, respx_mock):
    payload = {"cached": True}
    url = "https://data.sec.gov/cached.json"
    respx_mock.get(url).mock(return_value=httpx.Response(200, json=payload))

    async with mock_sec_client:
        first = await mock_sec_client.get_json(url, cache=True)
        # Second call should not hit network (respx would error if called again)
        second = await mock_sec_client.get_json(url, cache=True)

    assert first == second == payload


@pytest.mark.asyncio
async def test_get_json_retries_on_500(mock_sec_client, respx_mock):
    url = "https://data.sec.gov/flaky.json"
    payload = {"ok": True}
    call_count = 0

    def side_effect(request):
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            return httpx.Response(500)
        return httpx.Response(200, json=payload)

    respx_mock.get(url).mock(side_effect=side_effect)

    async with mock_sec_client:
        result = await mock_sec_client.get_json(url, cache=False)

    assert result == payload
    assert call_count == 3
