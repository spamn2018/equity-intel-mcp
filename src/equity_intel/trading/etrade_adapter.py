"""
EtradeBrokerAdapter -- importable adapter for the E*TRADE v1 brokerage API.

Mirrors the public interface of trading/alpaca_adapter.AlpacaBrokerAdapter so
that trading/risk.py, trading/execution.py, workers/auto_rebalance.py, etc.
can use either broker interchangeably via trading/broker_factory.py and the
BROKER_PROVIDER=alpaca|etrade setting in .env.

Design notes
------------
- Auth: OAuth1 access token pair produced by etrade_auth.py and saved to
  .etrade_token.json (consumer_key, consumer_secret, access_token,
  access_secret, sandbox). This module only *reads* that file -- it never
  runs the login flow itself. Run etrade_auth.py first (or via a scheduled
  task) to (re)mint a token before trading.
- E*TRADE addresses accounts by an opaque "accountIdKey", not the plain
  account number -- __init__ resolves ETRADE_ACCOUNT_ID to its accountIdKey
  once (via the Accounts List endpoint) and caches it.
- E*TRADE has NO notional/fractional order support (unlike Alpaca). Buys
  passed as `notional` are floored to a whole-share qty here; if that
  floors to 0 shares (price > notional), submit_limit_order raises
  ValueError rather than silently rounding up into a bigger-than-intended
  position -- trading/execution.py already catches submission exceptions
  and records them as a failed order, so no caller changes are needed.
- IMPORTANT: this adapter has been written against E*TRADE's documented v1
  API shapes but has NOT been exercised against a live E*TRADE account from
  this environment (no live credentials available here, and this code
  should not be smoke-tested by placing real orders). Before relying on
  autonomous execution through this adapter, test it manually end-to-end
  with a small order (ideally against E*TRADE's sandbox first, then a
  single tiny live order) and watch the logs / TradeOrder rows closely.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional


class EtradeBrokerAdapter:
    """
    Thin wrapper around the E*TRADE v1 Accounts/Market/Order REST APIs.

    Parameters
    ----------
    token_file : path to the JSON file written by etrade_auth.py, containing
                 consumer_key, consumer_secret, access_token, access_secret,
                 and sandbox (bool).
    account_id : the plain E*TRADE account number (e.g. "913307581") to
                 trade against -- resolved to its accountIdKey lazily.
    """

    def __init__(self, token_file: str, account_id: str) -> None:
        try:
            from requests_oauthlib import OAuth1Session
        except ImportError as exc:
            raise ImportError(
                "requests-oauthlib is not installed. Run: pip install requests-oauthlib"
            ) from exc

        token_path = Path(token_file)
        if not token_path.exists():
            raise FileNotFoundError(
                f"E*TRADE token file not found: {token_path}. "
                "Run etrade_auth.py first to authenticate."
            )
        token = json.loads(token_path.read_text(encoding="utf-8"))

        self._account_id = str(account_id)
        self._sandbox = bool(token.get("sandbox", False))
        self._base_url = "https://apisb.etrade.com" if self._sandbox else "https://api.etrade.com"

        self._session = OAuth1Session(
            token["consumer_key"],
            client_secret=token["consumer_secret"],
            resource_owner_key=token["access_token"],
            resource_owner_secret=token["access_secret"],
        )
        self._session.headers.update({"Accept": "application/json"})

        self._account_id_key: Optional[str] = None
        self._account_status: Optional[str] = None

    # ------------------------------------------------------------------
    # Account resolution
    # ------------------------------------------------------------------

    def _resolve_account(self) -> str:
        """Look up and cache the accountIdKey for self._account_id."""
        if self._account_id_key is not None:
            return self._account_id_key

        url = f"{self._base_url}/v1/accounts/list.json"
        resp = self._session.get(url, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        accounts = (
            (data.get("AccountListResponse") or {}).get("Accounts") or {}
        ).get("Account") or []
        if isinstance(accounts, dict):
            accounts = [accounts]

        for acct in accounts:
            if str(acct.get("accountId")) == self._account_id:
                self._account_id_key = acct.get("accountIdKey")
                self._account_status = acct.get("accountStatus")
                break

        if not self._account_id_key:
            raise ValueError(
                f"E*TRADE account {self._account_id} not found in accounts/list "
                f"response (found {[a.get('accountId') for a in accounts]!r})"
            )
        return self._account_id_key

    # ------------------------------------------------------------------
    # Account / positions / quotes
    # ------------------------------------------------------------------

    def get_account(self) -> Dict[str, Any]:
        """Return account info dict shaped like AlpacaBrokerAdapter.get_account()."""
        account_id_key = self._resolve_account()
        url = f"{self._base_url}/v1/accounts/{account_id_key}/balance.json"
        resp = self._session.get(
            url, params={"instType": "BROKERAGE", "realTimeNAV": "true"}, timeout=30
        )
        resp.raise_for_status()
        data = resp.json()

        computed = (data.get("BalanceResponse") or {}).get("Computed") or {}
        real_time = computed.get("RealTimeValues") or {}

        cash = computed.get("cashAvailableForInvestment")
        if cash is None:
            cash = computed.get("netCash")
        buying_power = computed.get("cashBuyingPower")
        if buying_power is None:
            buying_power = computed.get("marginBuyingPower")
        if buying_power is None:
            buying_power = cash
        equity = real_time.get("totalAccountValue")
        if equity is None:
            equity = (data.get("BalanceResponse") or {}).get("accountBalance")

        status = self._account_status or "UNKNOWN"

        return {
            "account_number": self._account_id,
            "status": status,
            "cash": float(cash) if cash is not None else 0.0,
            "buying_power": float(buying_power) if buying_power is not None else 0.0,
            "portfolio_value": float(equity) if equity is not None else 0.0,
            "equity": float(equity) if equity is not None else 0.0,
            # E*TRADE has no direct trading_blocked flag -- fail closed if the
            # account isn't reported ACTIVE rather than assuming it's tradeable.
            "trading_blocked": status.upper() != "ACTIVE",
            # Not exposed by the E*TRADE API -- always False (informational only).
            "pattern_day_trader": False,
            "paper": self._sandbox,
            "sandbox": self._sandbox,
        }

    def get_positions(self) -> List[Dict[str, Any]]:
        """Return a list of open position dicts."""
        account_id_key = self._resolve_account()
        url = f"{self._base_url}/v1/accounts/{account_id_key}/portfolio.json"
        resp = self._session.get(url, timeout=30)
        if resp.status_code == 204:
            return []  # no positions
        resp.raise_for_status()
        data = resp.json()

        portfolios = (data.get("PortfolioResponse") or {}).get("AccountPortfolio") or []
        positions: List[Dict[str, Any]] = []
        for portfolio in portfolios:
            for pos in portfolio.get("Position") or []:
                product = pos.get("Product") or {}
                quick = pos.get("Quick") or {}
                symbol = product.get("symbol")
                if not symbol:
                    continue
                side = str(pos.get("positionType") or "LONG").lower()
                qty = float(pos.get("quantity") or 0.0)
                positions.append({
                    "symbol": symbol,
                    "qty": -qty if side == "short" else qty,
                    "avg_entry_price": float(pos.get("pricePaid") or 0.0),
                    "current_price": float(quick["lastTrade"]) if quick.get("lastTrade") is not None else None,
                    "market_value": float(pos["marketValue"]) if pos.get("marketValue") is not None else None,
                    "unrealized_pl": float(pos["totalGain"]) if pos.get("totalGain") is not None else None,
                    "unrealized_plpc": float(pos["totalGainPct"]) if pos.get("totalGainPct") is not None else None,
                    "side": side,
                })
        return positions

    def get_position(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Return the position dict for *symbol*, or None if not held."""
        for pos in self.get_positions():
            if pos["symbol"].upper() == symbol.upper():
                return pos
        return None

    def get_quote(self, symbol: str) -> Dict[str, Any]:
        """
        Return a quote dict for *symbol*, shaped like AlpacaBrokerAdapter.get_quote().

        Raises ValueError if no quote is available.
        """
        url = f"{self._base_url}/v1/market/quote/{symbol}.json"
        resp = self._session.get(url, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        quote_data = (data.get("QuoteResponse") or {}).get("QuoteData") or []
        if not quote_data:
            raise ValueError(f"No quote available for {symbol}")
        all_data = quote_data[0].get("All") or {}

        bid = float(all_data.get("bid") if all_data.get("bid") is not None else all_data.get("bidPrice") or 0.0)
        ask = float(all_data.get("ask") if all_data.get("ask") is not None else all_data.get("askPrice") or 0.0)
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

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------

    def get_open_orders(self) -> List[Dict[str, Any]]:
        """Return a list of open/pending order dicts."""
        account_id_key = self._resolve_account()
        url = f"{self._base_url}/v1/accounts/{account_id_key}/orders.json"
        resp = self._session.get(url, params={"status": "OPEN"}, timeout=30)
        if resp.status_code == 204:
            return []  # no orders
        resp.raise_for_status()
        data = resp.json()
        return [self._parse_order(o) for o in (data.get("OrdersResponse") or {}).get("Order") or []]

    def has_open_order(self, symbol: str) -> bool:
        """Return True if there is already an open order for *symbol*."""
        sym = symbol.upper()
        return any((o.get("symbol") or "").upper() == sym for o in self.get_open_orders())

    def get_order(self, broker_order_id: str) -> Dict[str, Any]:
        """Return broker order details for one E*TRADE order id."""
        account_id_key = self._resolve_account()
        url = f"{self._base_url}/v1/accounts/{account_id_key}/orders.json"
        resp = self._session.get(url, params={"orderId": str(broker_order_id)}, timeout=30)
        if resp.status_code == 204:
            return {"broker_order_id": str(broker_order_id), "status": "unknown"}
        resp.raise_for_status()
        data = resp.json()
        orders = (data.get("OrdersResponse") or {}).get("Order") or []
        for o in orders:
            if str(o.get("orderId")) == str(broker_order_id):
                return self._parse_order(o)
        return {"broker_order_id": str(broker_order_id), "status": "unknown"}

    @staticmethod
    def _parse_order(order: Dict[str, Any]) -> Dict[str, Any]:
        detail = (order.get("OrderDetail") or [{}])[0]
        instrument = (detail.get("Instrument") or [{}])[0]
        product = instrument.get("Product") or {}
        return {
            "broker_order_id": str(order.get("orderId")),
            "symbol": product.get("symbol"),
            "side": (instrument.get("orderAction") or "").lower() or None,
            "qty": str(instrument.get("orderedQuantity")) if instrument.get("orderedQuantity") is not None else None,
            "filled_qty": str(instrument.get("filledQuantity")) if instrument.get("filledQuantity") is not None else None,
            "notional": None,  # E*TRADE orders are always share-qty based
            "filled_avg_price": str(instrument.get("averageExecutionPrice")) if instrument.get("averageExecutionPrice") is not None else None,
            "order_type": "limit",
            "time_in_force": "day",
            "limit_price": str(detail.get("limitPrice")) if detail.get("limitPrice") is not None else None,
            "status": str(detail.get("status") or "").lower() or None,
            "submitted_at": None,
            "filled_at": None,
            "expired_at": None,
            "canceled_at": None,
            "failed_at": None,
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
        Submit a limit DAY order via E*TRADE's Preview -> Place flow and
        return a dict shaped like AlpacaBrokerAdapter.submit_limit_order().

        E*TRADE has no notional/fractional order type: if `notional` is
        given, it is floored to a whole-share qty here. If that floors to
        0 shares, raises ValueError instead of silently buying nothing or
        rounding up past the intended notional -- callers (trading/
        execution.py) already catch submission exceptions and record them
        as a failed order with the message as failure_reason.
        """
        import datetime

        if notional is None and qty is None:
            raise ValueError("Either notional or qty must be provided")

        if qty is None:
            whole_shares = int(notional // limit_price) if limit_price else 0
            if whole_shares < 1:
                raise ValueError(
                    f"etrade_buy_too_small: cannot buy any whole shares of {symbol} "
                    f"with notional ${notional:.2f} at limit ${limit_price:.4f} "
                    "(E*TRADE does not support fractional/notional orders)"
                )
            qty = float(whole_shares)

        account_id_key = self._resolve_account()
        order_action = "BUY" if side.lower() == "buy" else "SELL"
        client_order_id = uuid.uuid4().hex[:20]

        order_payload = {
            "orderType": "EQ",
            "clientOrderId": client_order_id,
            "Order": [{
                "allOrNone": False,
                "priceType": "LIMIT",
                "orderTerm": "GOOD_FOR_DAY",
                "marketSession": "REGULAR",
                "limitPrice": round(limit_price, 2),
                "Instrument": [{
                    "Product": {"securityType": "EQ", "symbol": symbol},
                    "orderAction": order_action,
                    "quantityType": "QUANTITY",
                    "quantity": int(qty),
                }],
            }],
        }

        preview_url = f"{self._base_url}/v1/accounts/{account_id_key}/orders/preview.json"
        preview_resp = self._session.post(
            preview_url, json={"PreviewOrderRequest": order_payload}, timeout=30
        )
        preview_resp.raise_for_status()
        preview_data = preview_resp.json()
        preview_ids = (
            (preview_data.get("PreviewOrderResponse") or {}).get("PreviewIds") or []
        )
        if not preview_ids:
            raise ValueError(f"E*TRADE preview order returned no PreviewIds: {preview_data!r}")

        place_payload = dict(order_payload)
        place_payload["PreviewIds"] = [{"previewId": pid.get("previewId")} for pid in preview_ids]

        place_url = f"{self._base_url}/v1/accounts/{account_id_key}/orders/place.json"
        place_resp = self._session.post(
            place_url, json={"PlaceOrderRequest": place_payload}, timeout=30
        )
        place_resp.raise_for_status()
        place_data = place_resp.json()

        place_response = place_data.get("PlaceOrderResponse") or {}
        order_ids = place_response.get("OrderIds") or []
        broker_order_id = str(order_ids[0].get("orderId")) if order_ids else None
        placed_order = (place_response.get("Order") or [{}])[0]

        return {
            "broker_order_id": broker_order_id,
            "symbol": symbol,
            "side": side.lower(),
            "qty": str(qty),
            "notional": str(round(qty * limit_price, 2)),
            "order_type": "limit",
            "time_in_force": "day",
            "limit_price": str(round(limit_price, 2)),
            "status": str(placed_order.get("status") or "submitted").lower(),
            "submitted_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }

    def submit_market_order(
        self,
        symbol: str,
        side: str,
        notional=None,
        qty=None,
    ) -> dict:
        """Submit a MARKET DAY order via E*TRADE preview -> place flow.

        E*TRADE does not support fractional/notional orders, so when
        `notional` is given the adapter fetches the current mid-price and
        floors to whole shares.  Raises ValueError if that floors to 0
        (price > notional) so execution.py can record a clean failure.

        Returns a dict shaped like AlpacaBrokerAdapter.submit_market_order().
        """
        import datetime

        if notional is None and qty is None:
            raise ValueError("Either notional or qty must be provided")

        if qty is None:
            # Fetch current price to convert notional -> whole shares
            quote = self.get_quote(symbol)
            mid = quote.get("mid") or quote.get("ask") or quote.get("bid") or 0.0
            if not mid:
                raise ValueError(
                    f"Cannot size market order for {symbol}: no price available"
                )
            whole_shares = int(notional // mid)
            if whole_shares < 1:
                raise ValueError(
                    f"etrade_buy_too_small: cannot buy any whole shares of {symbol} "
                    f"with notional ${notional:.2f} at ~${mid:.4f} "
                    "(E*TRADE does not support fractional/notional orders)"
                )
            qty = float(whole_shares)

        account_id_key = self._resolve_account()
        order_action = "BUY" if side.lower() == "buy" else "SELL"
        import uuid as _uuid
        client_order_id = _uuid.uuid4().hex[:20]

        order_payload = {
            "orderType": "EQ",
            "clientOrderId": client_order_id,
            "Order": [{
                "allOrNone": False,
                "priceType": "MARKET",
                "orderTerm": "GOOD_FOR_DAY",
                "marketSession": "REGULAR",
                "Instrument": [{
                    "Product": {"securityType": "EQ", "symbol": symbol},
                    "orderAction": order_action,
                    "quantityType": "QUANTITY",
                    "quantity": int(qty),
                }],
            }],
        }

        preview_url = f"{self._base_url}/v1/accounts/{account_id_key}/orders/preview.json"
        preview_resp = self._session.post(
            preview_url, json={"PreviewOrderRequest": order_payload}, timeout=30
        )
        preview_resp.raise_for_status()
        preview_data = preview_resp.json()
        preview_ids = (
            (preview_data.get("PreviewOrderResponse") or {}).get("PreviewIds") or []
        )
        if not preview_ids:
            raise ValueError(
                f"E*TRADE preview order returned no PreviewIds: {preview_data!r}"
            )

        place_payload = dict(order_payload)
        place_payload["PreviewIds"] = [
            {"previewId": pid.get("previewId")} for pid in preview_ids
        ]

        place_url = f"{self._base_url}/v1/accounts/{account_id_key}/orders/place.json"
        place_resp = self._session.post(
            place_url, json={"PlaceOrderRequest": place_payload}, timeout=30
        )
        place_resp.raise_for_status()
        place_data = place_resp.json()

        place_response = place_data.get("PlaceOrderResponse") or {}
        order_ids = place_response.get("OrderIds") or []
        broker_order_id = str(order_ids[0].get("orderId")) if order_ids else None
        placed_order = (place_response.get("Order") or [{}])[0]

        return {
            "broker_order_id": broker_order_id,
            "symbol": symbol,
            "side": side.lower(),
            "qty": str(int(qty)),
            "notional": None,
            "order_type": "market",
            "time_in_force": "day",
            "limit_price": None,
            "status": str(placed_order.get("status") or "submitted").lower(),
            "submitted_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }

