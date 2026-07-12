"""
AlpacaBrokerAdapter -- importable adapter for the Alpaca trading API.

Design notes
------------
- Clients are created lazily inside the constructor so this module is
  importable without alpaca-py installed (tests can mock the class).
- All methods return plain dicts -- never print-only output.
- paper= is passed directly from the caller; no hard-coding of mode.
- The standalone CLI in alpaca_trader.py can continue to wrap this adapter.
"""
from __future__ import annotations

import functools
from typing import Any, Dict, List, Optional


class AlpacaBrokerAdapter:
    """
    Thin wrapper around the Alpaca trading + data clients.

    Parameters
    ----------
    api_key    : Alpaca API key
    secret_key : Alpaca secret key
    paper      : True = paper endpoint, False = live endpoint.
                 Passed directly to TradingClient; never overridden.
    """

    def __init__(self, api_key: str, secret_key: str, paper: bool) -> None:
        try:
            from alpaca.trading.client import TradingClient
            from alpaca.data.historical import StockHistoricalDataClient
        except ImportError as exc:
            raise ImportError(
                "alpaca-py is not installed. "
                "Run: pip install alpaca-py  (or use the alpaca_venv)"
            ) from exc

        self._api_key = api_key
        self._secret_key = secret_key
        self._paper = paper
        self._trading = TradingClient(api_key, secret_key, paper=paper)
        self._data = StockHistoricalDataClient(api_key, secret_key)
        # Prevent network calls from hanging indefinitely.
        # Alpaca uses requests.Session internally; wrap request() with a 30-second timeout.
        _orig = self._trading._session.request
        self._trading._session.request = functools.partial(_orig, timeout=30)

    def get_account(self) -> Dict[str, Any]:
        """Return account info dict with cash, buying_power, equity, etc."""
        acct = self._trading.get_account()
        return {
            "account_number": acct.account_number,
            "status": str(acct.status),
            "cash": float(acct.cash),
            "buying_power": float(acct.buying_power),
            "portfolio_value": float(acct.portfolio_value),
            "equity": float(acct.equity),
            "trading_blocked": acct.trading_blocked,
            "pattern_day_trader": acct.pattern_day_trader,
            "paper": self._paper,
        }

    def get_positions(self) -> List[Dict[str, Any]]:
        """Return a list of open position dicts."""
        positions = self._trading.get_all_positions()
        return [
            {
                "symbol": p.symbol,
                "qty": float(p.qty),
                "avg_entry_price": float(p.avg_entry_price),
                "current_price": float(p.current_price) if p.current_price else None,
                "market_value": float(p.market_value) if p.market_value else None,
                "unrealized_pl": float(p.unrealized_pl) if p.unrealized_pl else None,
                "unrealized_plpc": float(p.unrealized_plpc) if p.unrealized_plpc else None,
                "side": str(p.side),
            }
            for p in positions
        ]

    def get_position(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Return the position dict for *symbol*, or None if not held."""
        for pos in self.get_positions():
            if pos["symbol"].upper() == symbol.upper():
                return pos
        return None

    def get_open_orders(self) -> List[Dict[str, Any]]:
        """Return a list of open/pending order dicts."""
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus

        req = GetOrdersRequest(status=QueryOrderStatus.OPEN)
        orders = self._trading.get_orders(filter=req)
        return [
            {
                "id": str(o.id),
                "symbol": o.symbol,
                "side": str(o.side.value) if o.side else None,
                "qty": str(o.qty),
                "order_type": str(o.order_type.value) if o.order_type else None,
                "status": str(o.status.value) if o.status else None,
                "submitted_at": o.submitted_at.isoformat() if o.submitted_at else None,
            }
            for o in orders
        ]

    def has_open_order(self, symbol: str) -> bool:
        """Return True if there is already an open order for *symbol*."""
        sym = symbol.upper()
        return any(o["symbol"].upper() == sym for o in self.get_open_orders())

    def get_order(self, broker_order_id: str) -> Dict[str, Any]:
        """Return broker order details for one Alpaca order id."""
        order = self._trading.get_order_by_id(broker_order_id)
        return {
            "broker_order_id": str(order.id),
            "symbol": order.symbol,
            "side": str(order.side.value) if order.side else None,
            "qty": str(order.qty) if order.qty is not None else None,
            "filled_qty": str(order.filled_qty) if getattr(order, "filled_qty", None) is not None else None,
            "notional": str(order.notional) if getattr(order, "notional", None) is not None else None,
            "filled_avg_price": str(order.filled_avg_price) if getattr(order, "filled_avg_price", None) is not None else None,
            "order_type": str(order.order_type.value) if order.order_type else None,
            "time_in_force": str(order.time_in_force.value) if order.time_in_force else None,
            "limit_price": str(order.limit_price) if getattr(order, "limit_price", None) is not None else None,
            "status": str(order.status.value) if order.status else None,
            "submitted_at": order.submitted_at.isoformat() if getattr(order, "submitted_at", None) else None,
            "filled_at": order.filled_at.isoformat() if getattr(order, "filled_at", None) else None,
            "expired_at": order.expired_at.isoformat() if getattr(order, "expired_at", None) else None,
            "canceled_at": order.canceled_at.isoformat() if getattr(order, "canceled_at", None) else None,
            "failed_at": order.failed_at.isoformat() if getattr(order, "failed_at", None) else None,
        }

    def get_quote(self, symbol: str) -> Dict[str, Any]:
        """
        Return a quote dict for *symbol*.

        Raises ValueError if no quote is available.
        """
        from alpaca.data.requests import StockLatestQuoteRequest

        req = StockLatestQuoteRequest(symbol_or_symbols=symbol)
        quotes = self._data.get_stock_latest_quote(req)
        if symbol not in quotes:
            raise ValueError(f"No quote available for {symbol}")
        q = quotes[symbol]
        bid = float(q.bid_price) if q.bid_price else 0.0
        ask = float(q.ask_price) if q.ask_price else 0.0
        mid = (bid + ask) / 2 if (bid and ask) else 0.0
        spread = ask - bid
        spread_pct = (spread / mid * 100) if mid else 0.0
        return {
            "symbol": symbol,
            "bid": bid,
            "ask": ask,
            "mid": mid,
            "spread": spread,
            "spread_pct": round(spread_pct, 4),
        }

    def submit_limit_order(
        self,
        symbol: str,
        side: str,
        limit_price: float,
        notional: Optional[float] = None,
        qty: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Submit a limit DAY order and return the broker response as a dict.

        Buys use notional (dollar amount) for fractional support.
        Sells use qty (share count from existing position).
        """
        from alpaca.trading.requests import LimitOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce

        if notional is None and qty is None:
            raise ValueError("Either notional or qty must be provided")

        side_enum = OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL

        req = LimitOrderRequest(
            symbol=symbol,
            side=side_enum,
            time_in_force=TimeInForce.DAY,
            limit_price=round(limit_price, 2),
            notional=round(notional, 2) if notional is not None else None,
            qty=round(qty, 9) if qty is not None else None,
        )
        order = self._trading.submit_order(req)
        return {
            "broker_order_id": str(order.id),
            "symbol": order.symbol,
            "side": str(order.side.value) if order.side else side,
            "qty": str(order.qty),
            "notional": str(order.notional),
            "order_type": "limit",
            "time_in_force": "day",
            "limit_price": str(order.limit_price),
            "status": str(order.status.value) if order.status else "pending_new",
            "submitted_at": order.submitted_at.isoformat() if order.submitted_at else None,
        }

    def submit_market_order(
        self,
        symbol: str,
        side: str,
        notional: Optional[float] = None,
        qty: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Submit a market DAY order and return the broker response as a dict.

        Buys use notional for fractional support; sells use qty.
        Market orders fill immediately at the prevailing price --
        expected_price (mid at submission time) is recorded by the caller
        for slippage tracking, not passed to the broker.
        """
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce

        if notional is None and qty is None:
            raise ValueError("Either notional or qty must be provided")

        side_enum = OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL

        req = MarketOrderRequest(
            symbol=symbol,
            side=side_enum,
            time_in_force=TimeInForce.DAY,
            notional=round(notional, 2) if notional is not None else None,
            qty=round(qty, 9) if qty is not None else None,
        )
        order = self._trading.submit_order(req)
        return {
            "broker_order_id": str(order.id),
            "symbol": order.symbol,
            "side": str(order.side.value) if order.side else side,
            "qty": str(order.qty),
            "notional": str(order.notional),
            "order_type": "market",
            "time_in_force": "day",
            "status": str(order.status.value) if order.status else "pending_new",
            "submitted_at": order.submitted_at.isoformat() if order.submitted_at else None,
        }
