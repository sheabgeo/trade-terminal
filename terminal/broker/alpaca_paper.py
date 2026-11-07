"""Alpaca PAPER adapter.

There is deliberately no way to construct a live client from this module:
paper=True is hard-coded and the base URL is asserted after construction.
"""
from __future__ import annotations

import os
from datetime import datetime

from alpaca.data.historical import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest, CryptoLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.common.exceptions import APIError
from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import GetOrdersRequest, LimitOrderRequest

from .base import Account, AssetInfo, Bar, OpenOrder, OrderNotFound, OrderState, Position, Quote, mask

PAPER_URL = "https://paper-api.alpaca.markets"


class BrokerConfigError(RuntimeError):
    pass


class AlpacaPaper:
    source = "alpaca-paper/us-crypto"

    def __init__(self) -> None:
        key = os.environ.get("ALPACA_API_KEY", "").strip()
        secret = os.environ.get("ALPACA_SECRET_KEY", "").strip()
        if not key or not secret or "xxxx" in key:
            raise BrokerConfigError("ALPACA_API_KEY / ALPACA_SECRET_KEY missing in .env — no fallback account is used.")
        self._trading = TradingClient(key, secret, paper=True)
        if str(getattr(self._trading._base_url, "value", self._trading._base_url)) != PAPER_URL:
            raise BrokerConfigError("Trading client is not pointed at the paper endpoint. Refusing.")
        self._data = CryptoHistoricalDataClient(key, secret)

    def account(self) -> Account:
        a = self._trading.get_account()
        return Account(
            masked_id=mask(str(a.account_number)),
            environment="paper",
            status=str(getattr(a.status, "value", a.status)),
            equity=float(a.equity),
            cash=float(a.cash),
        )

    def positions(self) -> list[Position]:
        return [
            Position(
                symbol=_slash(p.symbol),
                qty=float(p.qty),
                avg_entry_price=float(p.avg_entry_price),
                market_value=float(p.market_value or 0),
            )
            for p in self._trading.get_all_positions()
        ]

    def open_orders(self) -> list[OpenOrder]:
        orders = self._trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))
        return [
            OpenOrder(
                id=str(o.id),
                client_order_id=o.client_order_id,
                symbol=_slash(o.symbol),
                side=str(getattr(o.side, "value", o.side)),
                qty=float(o.qty or 0),
                status=str(getattr(o.status, "value", o.status)),
            )
            for o in orders
        ]

    def asset(self, symbol: str) -> AssetInfo:
        a = self._trading.get_asset(symbol)
        return AssetInfo(
            symbol=symbol,
            tradable=bool(a.tradable),
            min_order_size=float(a.min_order_size or 0),
            min_trade_increment=float(a.min_trade_increment or 0),
            price_increment=float(a.price_increment or 0),
        )

    def bars(self, symbol: str, start: datetime, end: datetime) -> list[Bar]:
        req = CryptoBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Hour, start=start, end=end)
        data = self._data.get_crypto_bars(req).data.get(symbol, [])
        return [Bar(b.timestamp, b.open, b.high, b.low, b.close, b.volume) for b in data]

    def latest_quote(self, symbol: str) -> Quote:
        q = self._data.get_crypto_latest_quote(CryptoLatestQuoteRequest(symbol_or_symbols=symbol))[symbol]
        return Quote(q.timestamp, float(q.bid_price), float(q.ask_price))


    # ---- orders (IOC limit only; crypto has no bracket orders) ----
    def submit_limit_ioc(self, symbol: str, side: str, qty: float, limit_price: float, client_order_id: str) -> OrderState:
        req = LimitOrderRequest(
            symbol=symbol,
            qty=round(qty, 9),
            side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
            time_in_force=TimeInForce.IOC,
            limit_price=round(limit_price, 2),
            client_order_id=client_order_id,
        )
        return _order_state(self._trading.submit_order(req))

    def get_order_by_client_id(self, client_order_id: str) -> OrderState:
        try:
            return _order_state(self._trading.get_order_by_client_id(client_order_id))
        except APIError as e:
            if getattr(e, "status_code", None) == 404:
                raise OrderNotFound(client_order_id) from e
            raise


def _order_state(o) -> OrderState:
    return OrderState(
        id=str(o.id),
        client_order_id=o.client_order_id,
        symbol=_slash(o.symbol),
        side=str(getattr(o.side, "value", o.side)).lower(),
        qty=float(o.qty or 0),
        filled_qty=float(o.filled_qty or 0),
        filled_avg_price=float(o.filled_avg_price) if o.filled_avg_price else None,
        status=str(getattr(o.status, "value", o.status)).lower(),
    )


def _slash(sym: str) -> str:
    """Positions/orders may come back as BTCUSD; normalize to BTC/USD."""
    if "/" in sym or not sym.endswith("USD"):
        return sym
    return f"{sym[:-3]}/USD"
