from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable


OPTION_RE = re.compile(r"^([A-Z]{1,6})(\d{6})([CP])(\d{8})$")


def number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def option_mid(snapshot: dict[str, Any]) -> float:
    quote = snapshot.get("latestQuote") or snapshot.get("latest_quote") or {}
    bid = number(quote.get("bp", quote.get("bid_price")))
    ask = number(quote.get("ap", quote.get("ask_price")))
    if bid > 0 and ask > 0:
        return round((bid + ask) / 2, 2)
    trade = snapshot.get("latestTrade") or snapshot.get("latest_trade") or {}
    return number(trade.get("p", trade.get("price")))


def parse_option_symbol(symbol: str) -> dict[str, Any] | None:
    match = OPTION_RE.match(symbol.upper())
    if not match:
        return None
    root, yymmdd, right, strike_raw = match.groups()
    expiration = datetime.strptime(yymmdd, "%y%m%d").date().isoformat()
    return {
        "underlying": root,
        "expiration": expiration,
        "type": "call" if right == "C" else "put",
        "strike": int(strike_raw) / 1000,
    }


def _otm_anchor(spot: float, right: str) -> float:
    if right == "call":
        anchor = math.floor((spot + 1e-12) / 5) * 5 + 5
        return anchor + 5 if anchor <= spot else anchor
    anchor = math.ceil((spot - 1e-12) / 5) * 5 - 5
    return anchor - 5 if anchor >= spot else anchor


def snap_otm_grid(strikes: Iterable[float], spot: float, right: str, count: int = 10) -> list[float]:
    """Port of the source app's 5-point OTM anchor / 2.50-point snapped grid."""
    pool = {number(strike) for strike in strikes}
    pool = {strike for strike in pool if strike > spot} if right == "call" else {strike for strike in pool if strike < spot}
    anchor = _otm_anchor(spot, right)
    picked: list[float] = []
    for index in range(max(1, count)):
        desired = anchor + (index * 2.5 if right == "call" else -index * 2.5)
        candidates = pool.difference(picked)
        if not candidates:
            break
        picked.append(min(candidates, key=lambda strike: abs(strike - desired)))
    return picked


@dataclass(frozen=True)
class WheelScenario:
    shares: float
    average_cost: float
    spot: float
    call_strike: float | None = None
    call_bid: float | None = None
    put_strike: float | None = None
    put_bid: float | None = None

    @property
    def contracts(self) -> int:
        return max(1, int(abs(self.shares) // 100))

    def as_dict(self) -> dict[str, Any]:
        contracts = self.contracts
        stock_pl = (self.spot - self.average_cost) * self.shares
        call_premium = number(self.call_bid) * 100 * contracts if self.call_bid is not None else None
        put_premium = number(self.put_bid) * 100 * contracts if self.put_bid is not None else None
        called_away = None
        if self.call_strike is not None:
            called_away = (self.call_strike - self.average_cost) * 100 * contracts
        put_cash = None
        put_basis = None
        if self.put_strike is not None:
            added = 100 * contracts
            put_cash = self.put_strike * added
            if self.shares > 0:
                put_basis = (self.shares * self.average_cost + added * self.put_strike) / (self.shares + added)
        return {
            "contracts": contracts,
            "stock_pl": round(stock_pl, 2),
            "call_premium": None if call_premium is None else round(call_premium, 2),
            "put_premium": None if put_premium is None else round(put_premium, 2),
            "called_away_pl": None if called_away is None else round(called_away, 2),
            "put_cash_required": None if put_cash is None else round(put_cash, 2),
            "post_assignment_basis": None if put_basis is None else round(put_basis, 2),
        }
