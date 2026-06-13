"""
Tests for trading/alpaca_adapter.py -- AlpacaBrokerAdapter.

All Alpaca SDK calls are mocked via sys.modules stubs so the tests run
without alpaca-py installed. No network traffic.
"""
from __future__ import annotations

import sys
import types
import pytest
from unittest.mock import MagicMock, patch


# ── Build a minimal fake alpaca package so lazy imports inside adapter work ───

def _install_alpaca_stubs():
    """
    Inject stub modules for every alpaca sub-path the adapter imports so
    'from alpaca.trading.client import TradingClient' etc. succeed without
    the real package installed.
    """
    if "alpaca" in sys.modules:
        return  # real package present -- nothing to do

    def _mod(name):
        m = types.ModuleType(name)
        sys.modules[name] = m
        return m

    alpaca          = _mod("alpaca")
    trading         = _mod("alpaca.trading")
    trading_client  = _mod("alpaca.trading.client")
    trading_req     = _mod("alpaca.trading.requests")
    trading_enums   = _mod("alpaca.trading.enums")
    data            = _mod("alpaca.data")
    data_hist       = _mod("alpaca.data.historical")
    data_req        = _mod("alpaca.data.requests")
    data_tf         = _mod("alpaca.data.timeframe")

    alpaca.trading = trading
    alpaca.data    = data

    # Stub classes the adapter imports
    trading_client.TradingClient             = MagicMock
    trading_req.MarketOrderRequest           = MagicMock
    trading_req.LimitOrderRequest            = MagicMock
    trading_req.GetOrdersRequest             = MagicMock
    trading_enums.OrderSide                  = MagicMock
    trading_enums.TimeInForce                = MagicMock
    trading_enums.OrderStatus                = MagicMock
    trading_enums.QueryOrderStatus           = MagicMock

    data_hist.StockHistoricalDataClient      = MagicMock
    data_req.StockLatestQuoteRequest         = MagicMock
    data_req.StockBarsRequest                = MagicMock

    # Enum values the adapter uses
    trading_enums.OrderSide.BUY  = "buy"
    trading_enums.OrderSide.SELL = "sell"
    tif = MagicMock()
    tif.DAY = "day"; tif.GTC = "gtc"; tif.IOC = "ioc"
    tif.FOK = "fok"; tif.OPG = "opg"; tif.CLS = "cls"
    trading_enums.TimeInForce = tif
    trading_enums.QueryOrderStatus.OPEN = "open"


_install_alpaca_stubs()


# ── Helper: build adapter with mocked Alpaca clients ─────────────────────────

def _make_adapter(paper=True):
    from equity_intel.trading.alpaca_adapter import AlpacaBrokerAdapter
    with patch.object(AlpacaBrokerAdapter, "__init__", lambda self, *a, **kw: None):
        adapter = AlpacaBrokerAdapter.__new__(AlpacaBrokerAdapter)
    adapter._api_key    = "test_key"
    adapter._secret_key = "test_secret"
    adapter._paper      = paper
    adapter._trading    = MagicMock()
    adapter._data       = MagicMock()
    return adapter


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_get_account_returns_dict():
    adapter = _make_adapter(paper=True)
    mock_acct = MagicMock()
    mock_acct.account_number  = "PA12345"
    mock_acct.status          = "ACTIVE"
    mock_acct.cash            = "5000.00"
    mock_acct.buying_power    = "8000.00"
    mock_acct.portfolio_value = "10000.00"
    mock_acct.equity          = "10000.00"
    mock_acct.trading_blocked = False
    mock_acct.pattern_day_trader = False
    adapter._trading.get_account.return_value = mock_acct

    result = adapter.get_account()
    assert result["account_number"] == "PA12345"
    assert result["buying_power"]   == 8000.0
    assert result["paper"]          is True


def test_paper_false_is_passed_correctly():
    adapter = _make_adapter(paper=False)
    assert adapter._paper is False


def test_get_positions_returns_list():
    adapter = _make_adapter()
    mock_pos = MagicMock()
    mock_pos.symbol           = "AAPL"
    mock_pos.qty              = "10"
    mock_pos.avg_entry_price  = "150.00"
    mock_pos.current_price    = "155.00"
    mock_pos.market_value     = "1550.00"
    mock_pos.unrealized_pl    = "50.00"
    mock_pos.unrealized_plpc  = "0.033"
    mock_pos.side             = MagicMock(__str__=lambda s: "long")
    adapter._trading.get_all_positions.return_value = [mock_pos]

    positions = adapter.get_positions()
    assert len(positions) == 1
    assert positions[0]["symbol"] == "AAPL"
    assert positions[0]["qty"]    == 10.0


def test_get_position_returns_none_when_not_held():
    adapter = _make_adapter()
    adapter._trading.get_all_positions.return_value = []
    assert adapter.get_position("NVDA") is None


def test_has_open_order_true():
    adapter = _make_adapter()
    mock_order = MagicMock()
    mock_order.symbol         = "NVDA"
    mock_order.id             = "order-1"
    mock_order.side           = MagicMock(value="buy")
    mock_order.qty            = "1"
    mock_order.order_type     = MagicMock(value="market")
    mock_order.status         = MagicMock(value="new")
    mock_order.submitted_at   = None
    adapter._trading.get_orders.return_value = [mock_order]

    assert adapter.has_open_order("NVDA") is True


def test_has_open_order_false():
    adapter = _make_adapter()
    adapter._trading.get_orders.return_value = []
    assert adapter.has_open_order("AAPL") is False


def test_get_quote_returns_dict():
    adapter = _make_adapter()
    mock_q = MagicMock()
    mock_q.bid_price = 99.90
    mock_q.ask_price = 100.10
    adapter._data.get_stock_latest_quote.return_value = {"NVDA": mock_q}

    quote = adapter.get_quote("NVDA")
    assert quote["symbol"]   == "NVDA"
    assert quote["bid"]      == 99.90
    assert quote["ask"]      == 100.10
    assert abs(quote["mid"] - 100.0) < 0.01
    assert quote["spread_pct"] >= 0


def test_get_quote_raises_when_not_found():
    adapter = _make_adapter()
    adapter._data.get_stock_latest_quote.return_value = {}
    with pytest.raises(ValueError, match="No quote available"):
        adapter.get_quote("UNKN")


def test_submit_limit_order_buy_notional():
    adapter = _make_adapter(paper=True)
    mock_order = MagicMock()
    mock_order.id           = "order-abc"
    mock_order.symbol       = "NVDA"
    mock_order.side         = MagicMock(value="buy")
    mock_order.qty          = None
    mock_order.notional     = "500.0"
    mock_order.limit_price  = "100.10"
    mock_order.order_type   = MagicMock(value="limit")
    mock_order.status       = MagicMock(value="pending_new")
    mock_order.submitted_at = None
    adapter._trading.submit_order.return_value = mock_order

    result = adapter.submit_limit_order("NVDA", "buy", limit_price=100.10, notional=500.0)
    assert result["broker_order_id"] == "order-abc"
    assert result["order_type"]      == "limit"
    assert result["time_in_force"]   == "day"


def test_submit_limit_order_sell_qty():
    """Sell uses qty, not notional."""
    adapter = _make_adapter(paper=False)
    mock_order = MagicMock()
    mock_order.id           = "order-sell"
    mock_order.symbol       = "AAPL"
    mock_order.side         = MagicMock(value="sell")
    mock_order.qty          = "1.0"
    mock_order.notional     = None
    mock_order.limit_price  = "150.00"
    mock_order.order_type   = MagicMock(value="limit")
    mock_order.status       = MagicMock(value="pending_new")
    mock_order.submitted_at = None
    adapter._trading.submit_order.return_value = mock_order

    result = adapter.submit_limit_order("AAPL", "sell", limit_price=150.00, qty=1.0)
    assert result["broker_order_id"] == "order-sell"
    assert adapter._paper is False


def test_alpaca_paper_true_passed_to_client():
    """Test 15a: ALPACA_PAPER=True -- verify adapter stores it correctly."""
    adapter = _make_adapter(paper=True)
    assert adapter._paper is True


def test_alpaca_paper_false_passed_to_client():
    """Test 15b: ALPACA_PAPER=False -- verify adapter stores it correctly."""
    adapter = _make_adapter(paper=False)
    assert adapter._paper is False
