"""Deterministic in-memory broker for tests and offline demos (BROKER=fake)."""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from .base import Account, AssetInfo, Bar, OpenOrder, OrderNotFound, OrderState, Position, Quote


class BrokerReject(Exception):
    """Mimics alpaca APIError: carries an HTTP status_code."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


class FakeBroker:
    source = "fake"

    def __init__(self, now: datetime | None = None) -> None:
        self.now = now or datetime.now(timezone.utc)
        self.equity = 100_000.0
        self.cash = 100_000.0
        self._positions: list[Position] = []
        self._orders: list[OpenOrder] = []
        self._bars: dict[str, list[Bar]] = {}
        self._quotes: dict[str, Quote] = {}
        self.missing_bar_ts: set[datetime] = set()
        self.orders_by_client: dict[str, OrderState] = {}
        self.submit_calls = 0
        self.fill_ratio = 1.0          # 0.4 → partial fill
        self.fee_rate = 0.0025         # charged in the ACQUIRED asset on buys
        self.fail_mode: str | None = None  # "before_send" | "after_send"

    # --- scenario controls -------------------------------------------------
    def set_bars(self, symbol: str, bars: list[Bar]) -> None:
        self._bars[symbol] = bars

    def set_quote(self, symbol: str, bid: float, ask: float, age_s: float = 1.0) -> None:
        self._quotes[symbol] = Quote(self.now - timedelta(seconds=age_s), bid, ask)

    def add_position(self, p: Position) -> None:
        self._positions.append(p)

    def add_open_order(self, o: OpenOrder) -> None:
        self._orders.append(o)

    # --- Broker protocol -----------------------------------------------------
    def account(self) -> Account:
        return Account("****TEST", "paper", "ACTIVE", self.equity, self.cash)

    def positions(self) -> list[Position]:
        return list(self._positions)

    def open_orders(self) -> list[OpenOrder]:
        return list(self._orders)

    def asset(self, symbol: str) -> AssetInfo:
        # Venue minimums as reported by the real Alpaca paper account (2026-11-07).
        mins = {"BTC/USD": 1.317e-05, "ETH/USD": 0.000445692}
        return AssetInfo(symbol, True, mins.get(symbol, 0.0001), 1e-09, 1e-09)

    def bars(self, symbol: str, start: datetime, end: datetime) -> list[Bar]:
        return [b for b in self._bars.get(symbol, []) if start <= b.ts < end and b.ts not in self.missing_bar_ts]

    def latest_quote(self, symbol: str) -> Quote:
        if symbol not in self._quotes:
            raise LookupError(f"no quote for {symbol}")
        return self._quotes[symbol]


    # --- orders ---------------------------------------------------------------
    def submit_limit_ioc(self, symbol, side, qty, limit_price, client_order_id) -> OrderState:
        self.submit_calls += 1
        if self.fail_mode == "before_send":
            raise TimeoutError("simulated timeout before the order reached the broker")
        if client_order_id in self.orders_by_client:
            raise BrokerReject(422, "client_order_id must be unique")
        q = self.latest_quote(symbol)
        if qty * limit_price < 10:
            raise BrokerReject(403, "cost basis must be >= minimal amount of order 10")
        crosses = limit_price >= q.ask if side == "buy" else limit_price <= q.bid
        filled = round(qty * self.fill_ratio, 9) if crosses else 0.0
        px = (q.ask if side == "buy" else q.bid) if filled else None
        status = "filled" if filled == qty and filled > 0 else "canceled"
        o = OrderState(f"ord-{len(self.orders_by_client) + 1}", client_order_id, symbol, side, qty, filled, px, status)
        self.orders_by_client[client_order_id] = o
        if filled:
            self._apply_fill(symbol, side, filled, px)
        if self.fail_mode == "after_send":
            raise TimeoutError("simulated timeout after the broker accepted the order")
        return o

    def get_order_by_client_id(self, client_order_id) -> OrderState:
        if client_order_id not in self.orders_by_client:
            raise OrderNotFound(client_order_id)
        return self.orders_by_client[client_order_id]

    def _apply_fill(self, symbol, side, filled, px):
        pos = next((p for p in self._positions if p.symbol == symbol), None)
        if side == "buy":
            got = filled * (1 - self.fee_rate)
            self.cash -= filled * px
            if pos:
                pos.qty += got
            else:
                self._positions.append(Position(symbol, got, px, got * px))
        else:
            self.cash += filled * px * (1 - self.fee_rate)
            if pos:
                pos.qty = round(pos.qty - filled, 12)
                if pos.qty <= 1e-12:
                    self._positions.remove(pos)


def uptrend_breakout_bars(end_open: datetime, n: int = 600, base: float = 60_000.0) -> list[Bar]:
    """Gentle uptrend with noise, then the last bar closes above the prior 20-bar high."""
    bars = []
    for i in range(n):
        ts = end_open - timedelta(hours=n - 1 - i)
        mid = base * (1 + 0.0004 * i) + 150 * math.sin(i / 3)
        o, c = mid - 20, mid + 20
        bars.append(Bar(ts, o, c + 40, o - 40, c, 10.0))
    last = bars[-1]
    top = max(b.high for b in bars[-21:-1])
    bars[-1] = Bar(last.ts, last.open, top + 300, last.low, top + 200, 25.0)
    return bars


def downtrend_bars(end_open: datetime, n: int = 600, base: float = 3_000.0) -> list[Bar]:
    bars = []
    for i in range(n):
        ts = end_open - timedelta(hours=n - 1 - i)
        mid = base * (1 - 0.0004 * i)
        bars.append(Bar(ts, mid + 2, mid + 5, mid - 5, mid - 2, 50.0))
    return bars
