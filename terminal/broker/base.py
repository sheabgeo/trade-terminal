"""Broker interface. Orders are IOC limit only (stage 4)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass
class Account:
    masked_id: str
    environment: str      # always "paper" in this build
    status: str
    equity: float
    cash: float


@dataclass
class Position:
    symbol: str
    qty: float
    avg_entry_price: float
    market_value: float


@dataclass
class OpenOrder:
    id: str
    client_order_id: str
    symbol: str
    side: str
    qty: float
    status: str


@dataclass
class AssetInfo:
    symbol: str
    tradable: bool
    min_order_size: float
    min_trade_increment: float
    price_increment: float


@dataclass
class Bar:
    ts: datetime          # bar OPEN time, UTC
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class Quote:
    ts: datetime          # market timestamp, UTC
    bid: float
    ask: float


@dataclass
class OrderState:
    id: str
    client_order_id: str
    symbol: str
    side: str
    qty: float
    filled_qty: float
    filled_avg_price: float | None
    status: str           # broker status string, lower-case


TERMINAL_STATUSES = {"filled", "canceled", "expired", "rejected", "done_for_day", "stopped", "suspended"}


class OrderNotFound(LookupError):
    pass


class Broker(Protocol):
    source: str

    def account(self) -> Account: ...
    def positions(self) -> list[Position]: ...
    def open_orders(self) -> list[OpenOrder]: ...
    def asset(self, symbol: str) -> AssetInfo: ...
    def bars(self, symbol: str, start: datetime, end: datetime) -> list[Bar]: ...
    def latest_quote(self, symbol: str) -> Quote: ...
    def submit_limit_ioc(self, symbol: str, side: str, qty: float, limit_price: float, client_order_id: str) -> OrderState: ...
    def get_order_by_client_id(self, client_order_id: str) -> OrderState: ...   # raises OrderNotFound


def mask(account_id: str) -> str:
    return f"****{account_id[-4:]}" if len(account_id) >= 4 else "****"
