"""
IB equity dashboard — single-file bundle (strike resolver + portfolio client + UI).
Regenerate with: python build_positions_dashboard_bundle.py
Requires: ibapi, matplotlib, tkinter.
"""

from __future__ import annotations

import math
import os
import queue
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, TypedDict

import matplotlib

matplotlib.use("TkAgg")

import tkinter as tk
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
from tkinter import messagebox, ttk

from ibapi.client import EClient
from ibapi.common import TickerId
from ibapi.contract import Contract
from ibapi.order import Order
from ibapi.ticktype import TickTypeEnum
from ibapi.wrapper import EWrapper

# ---------------------------------------------------------------------------
# strike_resolver (library portion; CLI entry omitted)
# ---------------------------------------------------------------------------

IB_UNSET_DOUBLE = 1.7976931348623157e308


def _is_otm_call(strike: float, spot: float) -> bool:
    """Equity-style: OTM call has strike above spot."""
    return float(strike) > float(spot)


def _is_otm_put(strike: float, spot: float) -> bool:
    """Equity-style: OTM put has strike below spot."""
    return float(strike) < float(spot)


def _otm_call_anchor_mult5(spot: float) -> float:
    """Nearest tick of 5 strictly above spot (first OTM call grid anchor)."""
    step = 5.0
    s = float(spot)
    x = math.floor((s + 1e-12) / step) * step + step
    if x <= s:
        x += step
    return x


def _otm_put_anchor_mult5(spot: float) -> float:
    """Nearest tick of 5 strictly below spot (first OTM put grid anchor)."""
    step = 5.0
    s = float(spot)
    x = math.ceil((s - 1e-12) / step) * step - step
    if x >= s:
        x -= step
    return x


def _strikes_from_25_grid_snapped(
    chain_strikes: Set[float],
    spot: float,
    count: int,
    increment: float = 2.5,
) -> Tuple[List[float], List[float]]:
    """
    From spot: OTM call anchor = next multiple of 5 above spot; OTM put anchor = next multiple of 5 below.
    Then walk outward in steps of `increment` (default 2.50). Each level is snapped to the nearest
    unused exchange strike that remains OTM (calls: K > S, puts: K < S).
    """
    chain = {float(x) for x in chain_strikes}
    spt = float(spot)
    n = max(1, int(count))

    def snap_call(desired: float, used: Set[float]) -> Optional[float]:
        cands = [k for k in chain if k > spt and k not in used]
        if not cands:
            return None
        return min(cands, key=lambda k: abs(k - desired))

    def snap_put(desired: float, used: Set[float]) -> Optional[float]:
        cands = [k for k in chain if k < spt and k not in used]
        if not cands:
            return None
        return min(cands, key=lambda k: abs(k - desired))

    start_c = _otm_call_anchor_mult5(spt)
    start_p = _otm_put_anchor_mult5(spt)

    calls_out: List[float] = []
    used_c: Set[float] = set()
    for i in range(n):
        desired = start_c + i * float(increment)
        hit = snap_call(desired, used_c)
        if hit is None:
            break
        used_c.add(hit)
        calls_out.append(hit)

    puts_out: List[float] = []
    used_p: Set[float] = set()
    for i in range(n):
        desired = start_p - i * float(increment)
        hit = snap_put(desired, used_p)
        if hit is None:
            break
        used_p.add(hit)
        puts_out.append(hit)

    return calls_out, puts_out


def _is_friday_expiry(exp_yyyymmdd: str) -> bool:
    if len(exp_yyyymmdd) != 8:
        return False
    return datetime.strptime(exp_yyyymmdd, "%Y%m%d").weekday() == 4


def _is_third_friday(exp_yyyymmdd: str) -> bool:
    """Standard monthly equity options often expire on the 3rd Friday (day 15–21)."""
    if len(exp_yyyymmdd) != 8:
        return False
    dt = datetime.strptime(exp_yyyymmdd, "%Y%m%d")
    return dt.weekday() == 4 and 15 <= dt.day <= 21


def _rank_score(c: "OptionCandidate", volume_weight: float) -> Tuple[float, float, float]:
    """Lower is better: delta distance minus liquidity bonus from log(volume)."""
    v = float(c.volume) if c.volume is not None else 0.0
    if v < 0 or v != v:
        v = 0.0
    adjusted = c.distance - volume_weight * math.log1p(v)
    return (adjusted, c.distance, -v)


@dataclass
class OptionCandidate:
    maturity: str
    strike: float
    right: str
    delta: float
    distance: float
    con_id: int
    local_symbol: str
    bid: float = float("nan")
    ask: float = float("nan")
    last: float = float("nan")
    model_price: float = float("nan")
    und_price: float = float("nan")
    volume: int = 0
    exchange: str = ""
    currency: str = "USD"
    trading_class: str = ""
    multiplier: str = "100"
    underlying_symbol: str = ""
    # Total credit $ for this short leg: |IB avgCost (per contract) × position (contracts)|; nan if unknown.
    premium_collected: float = float("nan")


@dataclass
class OptionChain:
    exchange: str
    trading_class: str
    multiplier: str
    expirations: Set[str]
    strikes: Set[float]


class StrikeResolver(EWrapper, EClient):
    """
    Resolve option contracts closest to a target delta per maturity.

    Workflow:
      1) Resolve underlying conId.
      2) Fetch option chain params (expirations/strikes).
      3) For each maturity, request market data snapshots for call/put strikes.
      4) Use model greeks (tickOptionComputation) and pick closest deltas.
    """

    def __init__(self, host: str, port: int, client_id: int):
        EWrapper.__init__(self)
        EClient.__init__(self, wrapper=self)
        self.host = host
        self.port = port
        self.client_id = client_id

        self._next_req_id: Optional[int] = None
        self._req_id_lock = threading.Lock()
        self._next_id_event = threading.Event()

        self._contract_details_event = threading.Event()
        self._underlying_con_id: Optional[int] = None

        self._opt_params_event = threading.Event()
        self._expirations: Set[str] = set()
        self._strikes: Set[float] = set()
        self._trading_class: Optional[str] = None
        self._multiplier: Optional[str] = None
        self._preferred_exchange: Optional[str] = None
        self._chains: List[OptionChain] = []

        self._greeks_lock = threading.Lock()
        self._greeks_by_req_id: Dict[int, OptionCandidate] = {}
        self._pending_req_ids: Set[int] = set()
        self._underlying_req_id: Optional[int] = None
        self._underlying_price_event = threading.Event()
        self._underlying_price: float = float("nan")
        self._underlying_bid: float = float("nan")
        self._underlying_ask: float = float("nan")
        self._underlying_last: float = float("nan")

        self._error_log: List[Tuple[int, int, str]] = []

    # ------------------------------
    # Basic IBAPI lifecycle handlers
    # ------------------------------
    def nextValidId(self, orderId: int):
        self._next_req_id = orderId
        self._next_id_event.set()

    def error(self, reqId: int, errorCode: int, errorString: str, advancedOrderRejectJson: str = ""):
        self._error_log.append((reqId, errorCode, errorString))

    # ------------------------------
    # Contract resolution
    # ------------------------------
    def contractDetails(self, reqId, contractDetails):
        self._underlying_con_id = contractDetails.contract.conId

    def contractDetailsEnd(self, reqId):
        self._contract_details_event.set()

    # ------------------------------
    # Option chain metadata
    # ------------------------------
    def securityDefinitionOptionParameter(
        self,
        reqId: int,
        exchange: str,
        underlyingConId: int,
        tradingClass: str,
        multiplier: str,
        expirations: Set[str],
        strikes: Set[float],
    ):
        chain = OptionChain(
            exchange=exchange,
            trading_class=tradingClass,
            multiplier=multiplier,
            expirations=set(expirations) if expirations else set(),
            strikes=set([float(s) for s in strikes if s is not None]) if strikes else set(),
        )
        self._chains.append(chain)

    def securityDefinitionOptionParameterEnd(self, reqId: int):
        self._opt_params_event.set()

    # ------------------------------
    # Option greeks callback
    # ------------------------------
    def tickOptionComputation(
        self,
        reqId: TickerId,
        tickType: int,
        tickAttrib: int,
        impliedVol: float,
        delta: float,
        optPrice: float,
        pvDividend: float,
        gamma: float,
        vega: float,
        theta: float,
        undPrice: float,
    ):
        # Model option computation is most stable for ranking.
        if tickType not in (
            TickTypeEnum.MODEL_OPTION,
            TickTypeEnum.BID_OPTION_COMPUTATION,
            TickTypeEnum.ASK_OPTION_COMPUTATION,
            TickTypeEnum.LAST_OPTION_COMPUTATION,
        ):
            return
        if delta is None or delta in (-2, -1) or abs(delta) >= IB_UNSET_DOUBLE:
            return

        with self._greeks_lock:
            existing = self._greeks_by_req_id.get(reqId)
            if existing:
                existing.delta = float(delta)
                existing.distance = 0.0  # populated later once target is known
                if optPrice is not None and optPrice not in (-1, IB_UNSET_DOUBLE):
                    existing.model_price = float(optPrice)
                if undPrice is not None and undPrice not in (-1, IB_UNSET_DOUBLE):
                    existing.und_price = float(undPrice)

    def tickPrice(self, reqId: TickerId, tickType: int, price: float, attrib):
        if price is None or price <= 0:
            return
        if self._underlying_req_id is not None and reqId == self._underlying_req_id:
            if tickType in (1, 66):  # BID / DELAYED_BID
                self._underlying_bid = float(price)
            elif tickType in (2, 67):  # ASK / DELAYED_ASK
                self._underlying_ask = float(price)
            elif tickType in (4, 68):  # LAST / DELAYED_LAST
                self._underlying_last = float(price)
                self._underlying_price = float(price)
                self._underlying_price_event.set()
            elif tickType in (9, 75) and self._underlying_price != self._underlying_price:  # CLOSE / DELAYED_CLOSE
                self._underlying_price = float(price)
                self._underlying_price_event.set()
            return

        with self._greeks_lock:
            existing = self._greeks_by_req_id.get(reqId)
            if not existing:
                return
            if tickType in (1, 66):
                existing.bid = float(price)
            elif tickType in (2, 67):
                existing.ask = float(price)
            elif tickType in (4, 68):
                existing.last = float(price)

    def tickSize(self, reqId: TickerId, tickType: int, size: int):
        vol_types = (
            getattr(TickTypeEnum, "VOLUME", 8),
            getattr(TickTypeEnum, "DELAYED_VOLUME", 86),
        )
        if tickType not in vol_types:
            return
        if size is None or size < 0:
            return
        with self._greeks_lock:
            existing = self._greeks_by_req_id.get(reqId)
            if existing:
                existing.volume = int(size)

    # ------------------------------
    # Utility methods
    # ------------------------------
    def _alloc_req_id(self) -> int:
        with self._req_id_lock:
            if self._next_req_id is None:
                raise RuntimeError("nextValidId not initialized yet.")
            rid = self._next_req_id
            self._next_req_id += 1
            return rid

    def _wait_for(self, event: threading.Event, timeout: float, what: str):
        if not event.wait(timeout):
            raise TimeoutError(f"Timeout waiting for {what}.")

    def start(self):
        self.connect(self.host, self.port, self.client_id)
        thread = threading.Thread(target=self.run, daemon=True)
        thread.start()
        self._wait_for(self._next_id_event, 10.0, "nextValidId")

    def stop(self):
        try:
            self.disconnect()
        except Exception:
            pass

    def _build_underlying_contract(self, symbol: str, sec_type: str, exchange: str, currency: str) -> Contract:
        c = Contract()
        c.symbol = symbol
        c.secType = sec_type
        c.exchange = exchange
        c.currency = currency
        return c

    def _build_option_contract(
        self,
        symbol: str,
        exchange: str,
        currency: str,
        expiration: str,
        strike: float,
        right: str,
        trading_class: Optional[str],
        multiplier: Optional[str],
    ) -> Contract:
        c = Contract()
        c.symbol = symbol
        c.secType = "OPT"
        c.exchange = exchange
        c.currency = currency
        c.lastTradeDateOrContractMonth = expiration
        c.strike = float(strike)
        c.right = right
        if trading_class:
            c.tradingClass = trading_class
        if multiplier:
            c.multiplier = multiplier
        return c

    def _build_maturity_pool(
        self,
        symbol: str,
        sec_type: str = "STK",
        exchange: str = "SMART",
        currency: str = "USD",
        request_timeout_sec: float = 8.0,
        exclude_zero_dte: bool = True,
        friday_weeklies_only: bool = True,
        exclude_third_friday_monthly: bool = False,
    ) -> List[str]:
        """
        Load option chain metadata and return filtered future expiries (YYYYMMDD), oldest first.
        Sets _preferred_exchange, _trading_class, _multiplier, _expirations, _strikes.
        """
        self._contract_details_event.clear()
        self._opt_params_event.clear()
        self._expirations.clear()
        self._strikes.clear()
        self._trading_class = None
        self._multiplier = None
        self._preferred_exchange = None
        self._chains.clear()
        self._underlying_con_id = None

        underlying = self._build_underlying_contract(symbol, sec_type, exchange, currency)
        self.reqMarketDataType(3)

        details_req_id = self._alloc_req_id()
        self.reqContractDetails(details_req_id, underlying)
        self._wait_for(self._contract_details_event, request_timeout_sec, "contract details")
        if not self._underlying_con_id:
            raise RuntimeError("Could not resolve underlying conId.")

        opt_req_id = self._alloc_req_id()
        self.reqSecDefOptParams(opt_req_id, symbol, "", sec_type, self._underlying_con_id)
        self._wait_for(self._opt_params_event, request_timeout_sec, "option chain params")
        if not self._chains:
            raise RuntimeError("No option expirations/strikes returned by IB.")

        def _chain_score(c: OptionChain) -> Tuple[int, int, int, int]:
            smart = 1 if c.exchange == "SMART" else 0
            mult100 = 1 if str(c.multiplier) == "100" else 0
            richness = len(c.expirations) * max(len(c.strikes), 1)
            fri = sum(1 for e in c.expirations if _is_friday_expiry(e))
            if friday_weeklies_only:
                return smart, mult100, fri * max(len(c.strikes), 1), richness
            return smart, mult100, richness, fri

        best_chain = max(self._chains, key=_chain_score)
        self._preferred_exchange = best_chain.exchange
        self._trading_class = best_chain.trading_class or symbol
        self._multiplier = best_chain.multiplier or "100"
        self._expirations = set(best_chain.expirations)
        self._strikes = set(best_chain.strikes)

        if not self._expirations or not self._strikes:
            raise RuntimeError(
                f"Selected option chain has no expirations/strikes: "
                f"exchange={self._preferred_exchange} tradingClass={self._trading_class}."
            )

        today = datetime.now().strftime("%Y%m%d")
        dated = sorted([e for e in self._expirations if len(e) == 8 and e >= today])
        if exclude_zero_dte:
            pool = [e for e in dated if e > today]
            if not pool:
                pool = sorted([e for e in self._expirations if len(e) == 8 and e > today])
            if not pool:
                raise RuntimeError(
                    f"No option expiries strictly after today ({today}) while excluding 0DTE. "
                    "Use --include-0dte or pick another symbol/day."
                )
        else:
            pool = dated
            if not pool:
                pool = sorted([e for e in self._expirations if len(e) == 8])[:]
            if not pool:
                pool = sorted(self._expirations)[:]

        if friday_weeklies_only:
            pool = [e for e in pool if _is_friday_expiry(e)]
        if exclude_third_friday_monthly:
            pool = [e for e in pool if not _is_third_friday(e)]
        if not pool:
            raise RuntimeError(
                "No maturities left after filters (0DTE / Friday weekly / exclude 3rd Friday). "
                "Try --include-non-friday-expirations or --include-0dte."
            )
        return pool

    def fetch_maturities(
        self,
        symbol: str,
        sec_type: str = "STK",
        exchange: str = "SMART",
        currency: str = "USD",
        count: int = 10,
        request_timeout_sec: float = 8.0,
        exclude_zero_dte: bool = True,
        friday_weeklies_only: bool = True,
        exclude_third_friday_monthly: bool = False,
    ) -> List[str]:
        """Return up to `count` upcoming filtered expiries without requesting option market data."""
        pool = self._build_maturity_pool(
            symbol=symbol,
            sec_type=sec_type,
            exchange=exchange,
            currency=currency,
            request_timeout_sec=request_timeout_sec,
            exclude_zero_dte=exclude_zero_dte,
            friday_weeklies_only=friday_weeklies_only,
            exclude_third_friday_monthly=exclude_third_friday_monthly,
        )
        return pool[: max(1, int(count))]

    def fetch_closest_otm(
        self,
        symbol: str,
        expiration: str,
        count: int = 10,
        sec_type: str = "STK",
        exchange: str = "SMART",
        currency: str = "USD",
        request_timeout_sec: float = 10.0,
        snapshot_wait_sec: float = 4.0,
        exclude_zero_dte: bool = True,
        friday_weeklies_only: bool = True,
        exclude_third_friday_monthly: bool = False,
        verbose: bool = False,
    ) -> Tuple[float, List[OptionCandidate], List[OptionCandidate]]:
        """
        For one expiry, pick strikes on a 2.50 grid anchored at the nearest multiple of 5 OTM
        (calls: first 5-tick above spot; puts: first 5-tick below), snap each level to the chain,
        then request market data for up to `count` calls and `count` puts.
        """
        pool = self._build_maturity_pool(
            symbol=symbol,
            sec_type=sec_type,
            exchange=exchange,
            currency=currency,
            request_timeout_sec=request_timeout_sec,
            exclude_zero_dte=exclude_zero_dte,
            friday_weeklies_only=friday_weeklies_only,
            exclude_third_friday_monthly=exclude_third_friday_monthly,
        )
        exp = str(expiration).strip()
        if exp not in pool and exp not in self._expirations:
            raise RuntimeError(
                f"Expiry {exp} not in IB chain for {symbol}. Generate maturities and pick a listed expiry."
            )

        underlying = self._build_underlying_contract(symbol, sec_type, exchange, currency)
        self._underlying_price_event.clear()
        self._underlying_req_id = self._alloc_req_id()
        self.reqMktData(self._underlying_req_id, underlying, "", True, False, [])
        self._wait_for(self._underlying_price_event, request_timeout_sec, "underlying spot price")
        self.cancelMktData(self._underlying_req_id)

        if self._underlying_price != self._underlying_price:
            raise RuntimeError("Underlying spot price unavailable for OTM strike selection.")

        spot = float(self._underlying_price)
        n = max(1, int(count))
        call_strikes, put_strikes = _strikes_from_25_grid_snapped(self._strikes, spot, n, increment=2.5)
        specs: List[Tuple[float, str]] = [(s, "C") for s in call_strikes] + [(s, "P") for s in put_strikes]
        if not specs:
            raise RuntimeError("No OTM strikes on chain relative to current spot (check chain data).")

        if verbose:
            print(
                f"[otm] spot={spot:.4f} grid=5+2.5 anchor_call={_otm_call_anchor_mult5(spot):.2f} "
                f"anchor_put={_otm_put_anchor_mult5(spot):.2f} expiry={exp} "
                f"calls={call_strikes} puts={put_strikes} exchange={self._preferred_exchange}"
            )

        with self._greeks_lock:
            self._greeks_by_req_id.clear()
            self._pending_req_ids.clear()
        self._error_log.clear()

        ordered: List[OptionCandidate] = []
        wave_size = 6
        wave_wait = max(2.5, float(snapshot_wait_sec))
        exch = self._preferred_exchange or exchange
        tc = self._trading_class or symbol
        mult = self._multiplier or "100"

        for w0 in range(0, len(specs), wave_size):
            wave = specs[w0 : w0 + wave_size]
            wave_req_ids: List[int] = []
            for strike, right in wave:
                rid = self._alloc_req_id()
                contract = self._build_option_contract(
                    symbol=symbol,
                    exchange=exch,
                    currency=currency,
                    expiration=exp,
                    strike=strike,
                    right=right,
                    trading_class=tc,
                    multiplier=mult,
                )
                cand = OptionCandidate(
                    maturity=exp,
                    strike=float(strike),
                    right=right,
                    delta=float("nan"),
                    distance=abs(float(strike) - spot),
                    con_id=contract.conId,
                    local_symbol="",
                    exchange=exch,
                    currency=currency,
                    trading_class=tc,
                    multiplier=str(mult),
                    underlying_symbol=symbol,
                )
                with self._greeks_lock:
                    self._pending_req_ids.add(rid)
                    self._greeks_by_req_id[rid] = cand
                ordered.append(cand)
                self.reqMktData(rid, contract, "", False, False, [])
                wave_req_ids.append(rid)

            time.sleep(wave_wait)
            for rid in wave_req_ids:
                self.cancelMktData(rid)

        with self._greeks_lock:
            pending = list(self._pending_req_ids)
        for rid in pending:
            try:
                self.cancelMktData(rid)
            except Exception:
                pass

        calls_out = [c for c in ordered if c.right == "C"]
        puts_out = [c for c in ordered if c.right == "P"]
        return spot, calls_out, puts_out

    def resolve_strikes(
        self,
        symbol: str,
        sec_type: str = "STK",
        exchange: str = "SMART",
        currency: str = "USD",
        delta_threshold: float = 0.30,
        maturities_count: int = 3,
        per_side: int = 3,
        strikes_around_spot: int = 12,
        spot_window: float = 30.0,
        request_timeout_sec: float = 8.0,
        snapshot_wait_sec: float = 8.0,
        expand_from_atm: bool = False,
        otm_only: bool = True,
        exclude_zero_dte: bool = True,
        scan_early_exit: bool = False,
        friday_weeklies_only: bool = True,
        exclude_third_friday_monthly: bool = False,
        volume_weight: float = 0.02,
        verbose: bool = True,
        maturities_override: Optional[List[str]] = None,
    ) -> Tuple[float, Dict[str, Dict[str, List[OptionCandidate]]]]:
        pool = self._build_maturity_pool(
            symbol=symbol,
            sec_type=sec_type,
            exchange=exchange,
            currency=currency,
            request_timeout_sec=request_timeout_sec,
            exclude_zero_dte=exclude_zero_dte,
            friday_weeklies_only=friday_weeklies_only,
            exclude_third_friday_monthly=exclude_third_friday_monthly,
        )

        if maturities_override:
            want = [str(m) for m in maturities_override if str(m).strip()]
            maturities = [m for m in want if m in pool]
            if not maturities:
                maturities = want
        else:
            maturities = pool[:maturities_count]
        if not maturities:
            raise RuntimeError(
                "No maturities left after filters (0DTE / Friday weekly / exclude 3rd Friday). "
                "Try --include-non-friday-expirations or --include-0dte."
            )
        underlying = self._build_underlying_contract(symbol, sec_type, exchange, currency)
        today = datetime.now().strftime("%Y%m%d")
        all_strikes = sorted(self._strikes)

        # 2.5) Underlying spot snapshot to focus strike requests near spot.
        self._underlying_price_event.clear()
        self._underlying_req_id = self._alloc_req_id()
        self.reqMktData(self._underlying_req_id, underlying, "", True, False, [])
        self._wait_for(self._underlying_price_event, request_timeout_sec, "underlying spot price")
        self.cancelMktData(self._underlying_req_id)

        if self._underlying_price != self._underlying_price:
            raise RuntimeError("Underlying spot price unavailable for strike filtering.")

        window_strikes = [s for s in all_strikes if abs(float(s) - self._underlying_price) <= float(spot_window)]
        if not window_strikes:
            window_strikes = all_strikes
        by_distance = sorted(window_strikes, key=lambda s: abs(float(s) - self._underlying_price))
        strikes = by_distance[:strikes_around_spot]
        if expand_from_atm:
            rest = by_distance[strikes_around_spot:]
            strikes = strikes + rest[: min(40, len(rest))]
        if verbose:
            print(
                f"[debug] spot={self._underlying_price:.4f} "
                f"bid={_fmt_price(self._underlying_bid)} ask={_fmt_price(self._underlying_ask)} "
                f"last={_fmt_price(self._underlying_last)}"
            )
            print(
                f"[debug] selected_chain exchange={self._preferred_exchange} "
                f"tradingClass={self._trading_class} multiplier={self._multiplier}"
            )
            print(
                f"[debug] maturities_selected={len(maturities)} "
                f"({', '.join(maturities)}) strikes_selected={len(strikes)} "
                f"exclude_zero_dte={exclude_zero_dte} today={today} "
                f"friday_weeklies_only={friday_weeklies_only} "
                f"exclude_third_friday={exclude_third_friday_monthly} volume_weight={volume_weight} "
                f"window=[{self._underlying_price - spot_window:.2f}..{self._underlying_price + spot_window:.2f}] "
                f"chain_sizes(exp={len(self._expirations)},strikes={len(all_strikes)})"
            )

        # 3) ATM scan. By default this is strict ATM-only (no expansion).
        with self._greeks_lock:
            self._greeks_by_req_id.clear()
            self._pending_req_ids.clear()
        self._error_log.clear()

        # Allow callbacks and poll progress, per scan wave.
        total_contracts = len(maturities) * len(strikes) * 2
        wave_size = 6
        wave_wait = max(3.0, float(snapshot_wait_sec))
        target_per_mat_side = per_side

        spot_for_otm = float(self._underlying_price)

        def _have_enough() -> bool:
            with self._greeks_lock:
                vals = list(self._greeks_by_req_id.values())
            for m in maturities:
                c_count = 0
                p_count = 0
                for v in vals:
                    if v.maturity != m or not (v.delta == v.delta) or abs(v.delta) >= IB_UNSET_DOUBLE:
                        continue
                    if v.right == "C":
                        if otm_only and not _is_otm_call(v.strike, spot_for_otm):
                            continue
                        c_count += 1
                    else:
                        if otm_only and not _is_otm_put(v.strike, spot_for_otm):
                            continue
                        p_count += 1
                if c_count < target_per_mat_side or p_count < target_per_mat_side:
                    return False
            return True

        requested = 0
        # Scan every strike in `strikes` (already capped / expanded) so ~30Δ OTM names across the chain are eligible.
        scan_strikes = strikes

        for i in range(0, len(scan_strikes), wave_size):
            wave_strikes = scan_strikes[i : i + wave_size]
            if verbose:
                print(
                    f"[debug] scan_wave={1 + i // wave_size} "
                    f"strike_range=[{wave_strikes[0]:.2f}..{wave_strikes[-1]:.2f}] "
                    f"count={len(wave_strikes)}"
                )

            wave_req_ids: List[int] = []
            for maturity in maturities:
                for strike in wave_strikes:
                    for right in ("C", "P"):
                        rid = self._alloc_req_id()
                        contract = self._build_option_contract(
                            symbol=symbol,
                            exchange=self._preferred_exchange or exchange,
                            currency=currency,
                            expiration=maturity,
                            strike=strike,
                            right=right,
                            trading_class=self._trading_class or symbol,
                            multiplier=self._multiplier or "100",
                        )
                        with self._greeks_lock:
                            self._pending_req_ids.add(rid)
                            self._greeks_by_req_id[rid] = OptionCandidate(
                                maturity=maturity,
                                strike=float(strike),
                                right=right,
                                delta=float("nan"),
                                distance=float("inf"),
                                con_id=contract.conId,
                                local_symbol="",
                                exchange=self._preferred_exchange or exchange,
                                currency=currency,
                                trading_class=self._trading_class or symbol,
                                multiplier=self._multiplier or "100",
                                underlying_symbol=symbol,
                            )
                        # Streaming request is more reliable for option computations than snapshot+generic ticks.
                        self.reqMktData(rid, contract, "", False, False, [])
                        wave_req_ids.append(rid)
                        requested += 1

            time.sleep(wave_wait)
            for rid in wave_req_ids:
                self.cancelMktData(rid)

            with self._greeks_lock:
                with_delta = sum(
                    1 for c in self._greeks_by_req_id.values() if c.delta == c.delta and abs(c.delta) < IB_UNSET_DOUBLE
                )
            if verbose:
                print(f"[debug] option_delta_progress={with_delta}/{requested}")

            if scan_early_exit and _have_enough():
                if verbose:
                    print("[debug] stopping early: enough OTM candidates found")
                break

        # 4) Cancel snapshots and rank per maturity/side by delta distance.
        with self._greeks_lock:
            req_ids = list(self._pending_req_ids)
            candidates = list(self._greeks_by_req_id.values())

        for rid in req_ids:
            self.cancelMktData(rid)

        # Calls: target +delta; Puts: target -delta.
        # Rank only OTM contracts by |delta - target| so ITM wings do not crowd out ~30-delta OTM.
        target_call = abs(delta_threshold)
        target_put = -abs(delta_threshold)
        spot_ref = float(self._underlying_price)

        out: Dict[str, Dict[str, List[OptionCandidate]]] = {}
        total_valid = 0
        for maturity in maturities:
            mat_candidates = [c for c in candidates if c.maturity == maturity and c.delta == c.delta]
            calls: List[OptionCandidate] = []
            puts: List[OptionCandidate] = []
            for c in mat_candidates:
                if abs(c.delta) >= IB_UNSET_DOUBLE:
                    continue
                if c.right == "C":
                    if otm_only and not _is_otm_call(c.strike, spot_ref):
                        continue
                    c.distance = abs(c.delta - target_call)
                    calls.append(c)
                else:
                    if otm_only and not _is_otm_put(c.strike, spot_ref):
                        continue
                    c.distance = abs(c.delta - target_put)
                    puts.append(c)
            calls.sort(key=lambda x: _rank_score(x, volume_weight))
            puts.sort(key=lambda x: _rank_score(x, volume_weight))
            total_valid += len(calls) + len(puts)
            out[maturity] = {"calls": calls[:per_side], "puts": puts[:per_side]}
        if verbose:
            print(f"[debug] valid_option_deltas={total_valid}/{requested} (max_possible={total_contracts})")
            if self._error_log:
                grouped: Dict[int, int] = {}
                for _, code, _ in self._error_log:
                    grouped[code] = grouped.get(code, 0) + 1
                summary = ", ".join([f"{k}:{v}" for k, v in sorted(grouped.items())])
                print(f"[debug] ib_errors={summary}")
                sample = [f"(reqId={r}, code={c}, msg={m})" for (r, c, m) in self._error_log[:5]]
                print(f"[debug] ib_error_samples={' | '.join(sample)}")
        return self._underlying_price, out


def _fmt_delta(d: float) -> str:
    return f"{d:.4f}" if d == d else "nan"


def _fmt_price(p: float) -> str:
    return f"{p:.4f}" if p == p else "nan"


def _print_results(
    symbol: str,
    target: float,
    spot: float,
    data: Dict[str, Dict[str, List[OptionCandidate]]],
    otm_only: bool,
    exclude_zero_dte: bool,
    friday_weeklies_only: bool,
    volume_weight: float,
):
    print(f"\nStrike Resolver :: {symbol} :: target delta={target:.2f}")
    print(f"Spot={_fmt_price(spot)}")
    if exclude_zero_dte:
        print("Maturities: same-calendar-day (0DTE) expiries are excluded.")
    if friday_weeklies_only:
        print("Maturities: Friday expiries only (US-style weekly cycle).")
    if otm_only:
        print(
            "Ranking: OTM only; sort key minimizes distance - volume_weight*log1p(volume) "
            f"(volume_weight={volume_weight})."
        )
    for maturity, sides in data.items():
        print(f"\nMaturity {maturity}")
        print("  OTM Calls (top by |delta - target|)")
        if not sides["calls"]:
            print("    (no OTM call candidates with model delta in scan set)")
        for c in sides["calls"]:
            adj = _rank_score(c, volume_weight)[0]
            print(
                "    "
                f"strike={c.strike:<8.2f} delta={_fmt_delta(c.delta)} distance={c.distance:.4f} "
                f"score={adj:.4f} vol={c.volume} "
                f"bid={_fmt_price(c.bid)} ask={_fmt_price(c.ask)} last={_fmt_price(c.last)} "
                f"model={_fmt_price(c.model_price)} und={_fmt_price(c.und_price)}"
            )
        print("  OTM Puts (top by |delta - target|)")
        if not sides["puts"]:
            print("    (no OTM put candidates with model delta in scan set)")
        for p in sides["puts"]:
            adj = _rank_score(p, volume_weight)[0]
            print(
                "    "
                f"strike={p.strike:<8.2f} delta={_fmt_delta(p.delta)} distance={p.distance:.4f} "
                f"score={adj:.4f} vol={p.volume} "
                f"bid={_fmt_price(p.bid)} ask={_fmt_price(p.ask)} last={_fmt_price(p.last)} "
                f"model={_fmt_price(p.model_price)} und={_fmt_price(p.und_price)}"
            )



# ---------------------------------------------------------------------------
# portfolio_client
# ---------------------------------------------------------------------------

QuoteCallback = Callable[[str, dict], None]


@dataclass
class _Quote:
    bid: float = float("nan")
    ask: float = float("nan")
    last: float = float("nan")
    close: float = float("nan")

    def display_price(self) -> float:
        if self.last == self.last and self.last > 0:
            return float(self.last)
        if self.bid == self.bid and self.ask == self.ask and self.bid > 0 and self.ask > 0:
            return (float(self.bid) + float(self.ask)) / 2.0
        if self.close == self.close and self.close > 0:
            return float(self.close)
        return float("nan")

    def as_dict(self) -> dict:
        return {
            "bid": self.bid if self.bid == self.bid else None,
            "ask": self.ask if self.ask == self.ask else None,
            "last": self.last if self.last == self.last else None,
            "close": self.close if self.close == self.close else None,
            "display": self.display_price() if self.display_price() == self.display_price() else None,
        }


class IBPortfolioClient(EWrapper, EClient):
    """
    One socket: snapshot positions via reqPositions, then reqMktData streams per symbol.

    Position avgCost is treated as average cost per share (IB's usual STK convention);
    multiple lots are merged by symbol with share-weighted average cost.
    """

    def __init__(
        self,
        host: str,
        port: int,
        client_id: int,
        on_quote: QuoteCallback,
        account_filter: Optional[str] = None,
    ):
        EWrapper.__init__(self)
        EClient.__init__(self, wrapper=self)
        self.host = host
        self.port = port
        self.client_id = client_id
        self._on_quote = on_quote
        self._account_filter = account_filter.strip() if account_filter else None

        self._next_req_id: Optional[int] = None
        self._req_id_lock = threading.Lock()
        self._next_id_event = threading.Event()

        self._quotes_lock = threading.Lock()
        self._quotes: Dict[str, _Quote] = {}
        self._req_id_to_symbol: Dict[int, str] = {}
        self._symbol_to_req_id: Dict[str, int] = {}

        self._positions_lock = threading.Lock()
        self._position_rows: List[Tuple[str, str, float, float]] = []
        self._option_position_rows: List[Tuple[str, Contract, float, float]] = []
        self._last_option_snap: List[Tuple[str, Contract, float, float]] = []
        self._position_end = threading.Event()

        self._acct_sum_lock = threading.Lock()
        self._acct_sum_buckets: Dict[str, List[float]] = {}
        self._acct_sum_end = threading.Event()
        self._acct_sum_req_id: Optional[int] = None

        self._error_log: List[tuple] = []

    def nextValidId(self, orderId: int):
        self._next_req_id = orderId
        self._next_id_event.set()

    def error(self, reqId: int, errorCode: int, errorString: str, advancedOrderRejectJson: str = ""):
        self._error_log.append((reqId, errorCode, errorString))

    def position(self, account: str, contract: Contract, position: float, avgCost: float):
        if self._account_filter and account != self._account_filter:
            return
        if position is None or abs(float(position)) < 1e-9:
            return
        try:
            pos = float(position)
            ac = float(avgCost)
        except (TypeError, ValueError):
            return
        if contract.secType == "STK":
            sym = (contract.symbol or "").strip().upper()
            if not sym:
                return
            with self._positions_lock:
                self._position_rows.append((account, sym, pos, ac))
        elif contract.secType == "OPT":
            with self._positions_lock:
                self._option_position_rows.append((account, self._freeze_contract(contract), pos, ac))

    def positionEnd(self):
        self._position_end.set()

    def accountSummary(self, reqId: int, account: str, tag: str, value: str, currency: str):
        # ibapi 10+: (reqId, account, tag, value, currency) — no modelCode.
        if self._acct_sum_req_id is None or int(reqId) != int(self._acct_sum_req_id):
            return
        if self._account_filter and (account or "").strip() != self._account_filter:
            return
        if not tag or value is None:
            return
        try:
            v = float(str(value).replace(",", ""))
        except (TypeError, ValueError):
            return
        if not (v == v and math.isfinite(v)):
            return
        with self._acct_sum_lock:
            self._acct_sum_buckets.setdefault(str(tag), []).append(float(v))

    def accountSummaryEnd(self, reqId: int):
        if self._acct_sum_req_id is None or int(reqId) != int(self._acct_sum_req_id):
            return
        self._acct_sum_end.set()

    def tickPrice(self, reqId: TickerId, tickType: int, price: float, attrib):
        if price is None or price <= 0:
            return
        sym = self._req_id_to_symbol.get(int(reqId))
        if not sym:
            return
        with self._quotes_lock:
            q = self._quotes.setdefault(sym, _Quote())
            if tickType in (1, 66):  # BID / DELAYED_BID
                q.bid = float(price)
            elif tickType in (2, 67):  # ASK / DELAYED_ASK
                q.ask = float(price)
            elif tickType in (4, 68):  # LAST / DELAYED_LAST
                q.last = float(price)
            elif tickType in (9, 75):  # CLOSE / DELAYED_CLOSE
                q.close = float(price)
            payload = q.as_dict()

        self._on_quote(sym, payload)

    def _alloc_req_id(self) -> int:
        with self._req_id_lock:
            if self._next_req_id is None:
                raise RuntimeError("nextValidId not initialized yet.")
            rid = self._next_req_id
            self._next_req_id += 1
            return rid

    def _wait_for(self, event: threading.Event, timeout: float, what: str):
        if not event.wait(timeout):
            raise TimeoutError(f"Timeout waiting for {what}.")

    def start(self):
        self.connect(self.host, self.port, self.client_id)
        thread = threading.Thread(target=self.run, daemon=True)
        thread.start()
        self._wait_for(self._next_id_event, 15.0, "nextValidId")
        self.reqMarketDataType(3)

    def stop(self):
        for rid in list(self._req_id_to_symbol.keys()):
            try:
                self.cancelMktData(rid)
            except Exception:
                pass
        self._req_id_to_symbol.clear()
        self._symbol_to_req_id.clear()
        try:
            self.cancelPositions()
        except Exception:
            pass
        if self._acct_sum_req_id is not None:
            try:
                self.cancelAccountSummary(int(self._acct_sum_req_id))
            except Exception:
                pass
            self._acct_sum_req_id = None
        try:
            self.disconnect()
        except Exception:
            pass

    @staticmethod
    def _freeze_contract(c: Contract) -> Contract:
        """Copy contract fields IB may reuse between callbacks."""
        o = Contract()
        o.conId = int(getattr(c, "conId", 0) or 0)
        o.symbol = c.symbol
        o.secType = c.secType
        o.exchange = c.exchange or "SMART"
        o.currency = c.currency or "USD"
        o.lastTradeDateOrContractMonth = c.lastTradeDateOrContractMonth
        o.strike = float(c.strike or 0.0)
        o.right = c.right
        o.multiplier = c.multiplier
        o.localSymbol = c.localSymbol
        o.tradingClass = c.tradingClass
        return o

    def fetch_stock_positions(self, timeout_sec: float = 20.0) -> List[Dict[str, object]]:
        """Blocking: reqPositions until positionEnd, then merged STK rows; OPT rows stored for snapshot."""
        with self._positions_lock:
            self._position_rows.clear()
            self._option_position_rows.clear()
        self._position_end.clear()
        self.reqPositions()
        if not self._position_end.wait(timeout_sec):
            try:
                self.cancelPositions()
            except Exception:
                pass
            raise TimeoutError("Timed out waiting for IB positionEnd (enable Positions in API settings).")
        try:
            self.cancelPositions()
        except Exception:
            pass
        with self._positions_lock:
            rows = list(self._position_rows)
            self._last_option_snap = [
                (acct, self._freeze_contract(ct), pq, pac)
                for acct, ct, pq, pac in self._option_position_rows
            ]
        return self._merge_by_symbol(rows)

    def get_option_positions_snapshot(self) -> List[Tuple[str, Contract, float, float]]:
        """Frozen OPT rows from the last fetch_stock_positions() (account, contract, qty, avgCost)."""
        with self._positions_lock:
            return [(a, self._freeze_contract(c), q, ac) for a, c, q, ac in self._last_option_snap]

    def fetch_cash_denominator_for_portfolio(
        self,
        timeout_sec: float = 20.0,
    ) -> Tuple[Optional[float], str]:
        """
        Cash-only baseline for portfolio capital % (not NetLiquidation).
        Default tag order: TotalCashValue, AvailableFunds, SettledCash (cash-only; never NetLiquidation).
        Set IB_CASH_DENOMINATOR_TAG to a single tag to try first, then defaults.
        """
        override = (os.environ.get("IB_CASH_DENOMINATOR_TAG") or "").strip()
        tag_order: List[str] = []
        if override:
            tag_order.append(override)
        for t in ("TotalCashValue", "AvailableFunds", "SettledCash"):
            if t not in tag_order:
                tag_order.append(t)

        want_tags = ",".join(sorted(set(tag_order)))
        with self._acct_sum_lock:
            self._acct_sum_buckets.clear()
        self._acct_sum_end.clear()
        rid = self._alloc_req_id()
        self._acct_sum_req_id = rid
        ok = False
        try:
            self.reqAccountSummary(rid, "All", want_tags)
            ok = bool(self._acct_sum_end.wait(timeout_sec))
        finally:
            try:
                self.cancelAccountSummary(rid)
            except Exception:
                pass
            self._acct_sum_req_id = None

        if not ok:
            return None, ""

        with self._acct_sum_lock:
            summed = {k: sum(v) for k, v in self._acct_sum_buckets.items()}
        for tag in tag_order:
            val = summed.get(tag)
            if val is not None and val > 0:
                return float(val), tag
        return None, ""

    @staticmethod
    def _merge_by_symbol(rows: List[Tuple[str, str, float, float]]) -> List[Dict[str, object]]:
        """Rows: (account, symbol, position_qty, avgCost_per_share)."""
        merged: Dict[str, Dict[str, float]] = {}
        for _acct, sym, qty, avg_cost in rows:
            bucket = merged.setdefault(sym, {"qty": 0.0, "cost_num": 0.0})
            bucket["qty"] += qty
            bucket["cost_num"] += qty * avg_cost
        out: List[Dict[str, object]] = []
        for sym in sorted(merged.keys()):
            q = merged[sym]["qty"]
            cn = merged[sym]["cost_num"]
            if abs(q) < 1e-9:
                continue
            cps = cn / q
            out.append({"symbol": sym, "shares": q, "cost_basis": float(cps)})
        return out

    @staticmethod
    def _stock_contract(symbol: str, exchange: str = "SMART", currency: str = "USD") -> Contract:
        c = Contract()
        c.symbol = symbol.upper()
        c.secType = "STK"
        c.exchange = exchange
        c.currency = currency
        return c

    def subscribe(
        self,
        symbols: List[str],
        exchange: str = "SMART",
        currency: str = "USD",
    ):
        seen: List[str] = []
        for s in symbols:
            u = s.strip().upper()
            if u and u not in seen:
                seen.append(u)

        for sym in seen:
            if sym in self._symbol_to_req_id:
                continue
            rid = self._alloc_req_id()
            self._req_id_to_symbol[rid] = sym
            self._symbol_to_req_id[sym] = rid
            with self._quotes_lock:
                self._quotes.setdefault(sym, _Quote())
            contract = self._stock_contract(sym, exchange, currency)
            self.reqMktData(rid, contract, "", False, False, [])

    def get_quotes_snapshot(self) -> Dict[str, dict]:
        with self._quotes_lock:
            return {s: q.as_dict() for s, q in self._quotes.items()}

# ---------------------------------------------------------------------------
# positions_dashboard
# ---------------------------------------------------------------------------

def ib_settings() -> tuple[str, int, int]:
    return (
        os.environ.get("IB_HOST", "127.0.0.1"),
        int(os.environ.get("IB_PORT", "7497")),
        int(os.environ.get("IB_CLIENT_ID", "47")),
    )


def account_filter() -> Optional[str]:
    v = os.environ.get("IB_ACCOUNT", "").strip()
    return v or None


def strike_resolver_client_id() -> int:
    base = int(os.environ.get("IB_CLIENT_ID", "47"))
    v = os.environ.get("IB_STRIKE_CLIENT_ID", "").strip()
    return int(v) if v else base + 1


def option_stream_client_id() -> int:
    v = os.environ.get("IB_OPTION_STREAM_CLIENT_ID", "").strip()
    if v:
        return int(v)
    return strike_resolver_client_id() + 1


def option_order_client_id() -> int:
    v = os.environ.get("IB_ORDER_CLIENT_ID", "").strip()
    if v:
        return int(v)
    return option_stream_client_id() + 1


def _same_option_contract(a: Optional[OptionCandidate], b: Optional[OptionCandidate]) -> bool:
    if a is None or b is None:
        return False
    if int(getattr(a, "con_id", 0) or 0) > 0 and int(getattr(b, "con_id", 0) or 0) > 0:
        return int(a.con_id) == int(b.con_id)
    return (
        (a.maturity or "") == (b.maturity or "")
        and abs(float(a.strike) - float(b.strike)) < 1e-6
        and (a.right or "") == (b.right or "")
    )


def _option_mid_price(c: OptionCandidate) -> Optional[float]:
    b, a = c.bid, c.ask
    if b == b and a == a and b > 0 and a > 0:
        return (float(b) + float(a)) / 2.0
    return None


def _round_option_limit(px: float) -> float:
    r = round(float(px), 2)
    return max(0.01, r)


def _fmt_qty(q: float) -> str:
    if abs(q - round(q)) < 1e-6:
        return str(int(round(q)))
    return f"{q:g}"


def _spot_value(quote: Optional[dict]) -> Optional[float]:
    if not quote:
        return None
    d = quote.get("display")
    if d is None:
        return None
    try:
        x = float(d)
    except (TypeError, ValueError):
        return None
    if math.isnan(x) or x <= 0:
        return None
    return x


def _valid_cost_basis(cost: float) -> bool:
    return math.isfinite(cost) and cost > 0


def _chart_ylim(cost: float, spot: Optional[float]) -> tuple[float, float]:
    """Fixed ±20% band around avg cost, or around spot if cost is unavailable."""
    if _valid_cost_basis(cost):
        c = float(cost)
        return c * 0.8, c * 1.2
    if spot is not None and math.isfinite(spot) and float(spot) > 0:
        s = float(spot)
        return s * 0.8, s * 1.2
    return 0.0, 1.0


def _contracts_for_metrics(share_qty: float) -> int:
    """Whole 100-share lots for option metrics (min 1 for display when long stock)."""
    n = int(abs(float(share_qty)) // 100)
    return max(1, n)


def _premium_bid_dollars(c: Optional[OptionCandidate], contracts: int) -> Optional[float]:
    """Short-option credit estimate: bid × 100 × contracts (per-share bid)."""
    if c is None:
        return None
    if c.bid != c.bid or c.bid <= 0:
        return None
    return float(c.bid) * 100.0 * float(max(1, contracts))


def _bar_colors(cost: float, spot: float) -> tuple[str, str, str]:
    """
    Return (face, edge, label) for the spot bar.
    Orange within ±0.5% of cost; green clearly above; red clearly below.
    """
    if cost <= 0 or not (cost == cost):
        return "#e8943c", "#c97d30", "#e8943c"
    band = 0.005 * abs(cost)
    if abs(spot - cost) <= band:
        return "#e8943c", "#c97d30", "#f59e0b"
    if spot > cost:
        return "#22c55e", "#15803d", "#4ade80"
    return "#ef4444", "#b91c1c", "#f87171"


def _candidate_tree_values(c: OptionCandidate) -> tuple:
    bid = f"{c.bid:.2f}" if c.bid == c.bid and c.bid > 0 else ""
    ask = f"{c.ask:.2f}" if c.ask == c.ask and c.ask > 0 else ""
    dlt = f"{c.delta:.4f}" if c.delta == c.delta and abs(c.delta) < IB_UNSET_DOUBLE else ""
    dk = f"{c.distance:.2f}" if c.distance == c.distance and c.distance != float("inf") else ""
    return (f"{c.strike:.2f}", bid, ask, dlt, dk, c.maturity)


class OptionMdHub(EWrapper, EClient):
    """Dedicated IB connection: live reqMktData for selected call/put legs."""

    def __init__(
        self,
        host: str,
        port: int,
        client_id: int,
        root: tk.Tk,
        on_refresh: Any,
    ):
        EWrapper.__init__(self)
        EClient.__init__(self, wrapper=self)
        self.host = host
        self.port = port
        self.client_id = client_id
        self._root = root
        self._on_refresh = on_refresh
        self._next_req_id: Optional[int] = None
        self._req_lock = threading.Lock()
        self._next_event = threading.Event()
        self._lock = threading.Lock()
        self._cand_by_req: Dict[int, OptionCandidate] = {}
        self._meta_by_req: Dict[int, tuple[str, str]] = {}
        self._active: Dict[tuple[str, str], int] = {}

    def nextValidId(self, orderId: int):
        with self._req_lock:
            self._next_req_id = orderId
        self._next_event.set()

    def error(self, reqId: int, errorCode: int, errorString: str, advancedOrderRejectJson: str = ""):
        pass

    def tickPrice(self, reqId: TickerId, tickType: int, price: float, attrib):
        if price is None or price <= 0:
            return
        with self._lock:
            c = self._cand_by_req.get(int(reqId))
            meta = self._meta_by_req.get(int(reqId))
        if not c or not meta:
            return
        sym, leg = meta
        if tickType in (1, 66):
            c.bid = float(price)
        elif tickType in (2, 67):
            c.ask = float(price)
        elif tickType in (4, 68):
            c.last = float(price)
        self._root.after(0, lambda s=sym, l=leg: self._on_refresh(s, l))

    def tickOptionComputation(
        self,
        reqId: TickerId,
        tickType: int,
        tickAttrib: int,
        impliedVol: float,
        delta: float,
        optPrice: float,
        pvDividend: float,
        gamma: float,
        vega: float,
        theta: float,
        undPrice: float,
    ):
        if tickType not in (
            TickTypeEnum.MODEL_OPTION,
            TickTypeEnum.BID_OPTION_COMPUTATION,
            TickTypeEnum.ASK_OPTION_COMPUTATION,
            TickTypeEnum.LAST_OPTION_COMPUTATION,
        ):
            return
        if delta is None or delta in (-2, -1) or abs(delta) >= IB_UNSET_DOUBLE:
            return
        with self._lock:
            c = self._cand_by_req.get(int(reqId))
            meta = self._meta_by_req.get(int(reqId))
        if not c or not meta:
            return
        c.delta = float(delta)
        if optPrice is not None and optPrice not in (-1, IB_UNSET_DOUBLE):
            c.model_price = float(optPrice)
        if undPrice is not None and undPrice not in (-1, IB_UNSET_DOUBLE):
            c.und_price = float(undPrice)
        sym, leg = meta
        self._root.after(0, lambda s=sym, l=leg: self._on_refresh(s, l))

    def tickSize(self, reqId: TickerId, tickType: int, size: int):
        vol_types = (
            getattr(TickTypeEnum, "VOLUME", 8),
            getattr(TickTypeEnum, "DELAYED_VOLUME", 86),
        )
        if tickType not in vol_types:
            return
        if size is None or size < 0:
            return
        with self._lock:
            c = self._cand_by_req.get(int(reqId))
            meta = self._meta_by_req.get(int(reqId))
        if not c or not meta:
            return
        c.volume = int(size)
        sym, leg = meta
        self._root.after(0, lambda s=sym, l=leg: self._on_refresh(s, l))

    def _alloc_req_id(self) -> int:
        with self._req_lock:
            if self._next_req_id is None:
                raise RuntimeError("OptionMdHub: nextValidId not received.")
            rid = self._next_req_id
            self._next_req_id += 1
            return rid

    def start(self) -> None:
        self.connect(self.host, self.port, self.client_id)
        threading.Thread(target=self.run, daemon=True).start()
        if not self._next_event.wait(15.0):
            raise TimeoutError("OptionMdHub: timeout waiting for nextValidId.")

    def stop(self) -> None:
        try:
            self.disconnect()
        except Exception:
            pass

    @staticmethod
    def _contract_from_candidate(c: OptionCandidate) -> Contract:
        ct = Contract()
        sym = (c.underlying_symbol or "").strip()
        if not sym:
            raise ValueError("OptionCandidate missing underlying_symbol for streaming.")
        ct.symbol = sym
        ct.secType = "OPT"
        ct.exchange = c.exchange or "SMART"
        ct.currency = c.currency or "USD"
        ct.lastTradeDateOrContractMonth = c.maturity
        ct.strike = float(c.strike)
        ct.right = c.right
        if c.trading_class:
            ct.tradingClass = c.trading_class
        if c.multiplier:
            ct.multiplier = str(c.multiplier)
        return ct

    def set_leg(self, sym: str, leg: str, cand: Optional[OptionCandidate]) -> None:
        if leg not in ("call", "put"):
            raise ValueError("leg must be 'call' or 'put'")
        key = (sym, leg)
        with self._lock:
            old = self._active.pop(key, None)
            if old is not None:
                try:
                    self.cancelMktData(old)
                except Exception:
                    pass
                self._cand_by_req.pop(old, None)
                self._meta_by_req.pop(old, None)
        if cand is None:
            return
        rid = self._alloc_req_id()
        with self._lock:
            self._cand_by_req[rid] = cand
            self._meta_by_req[rid] = (sym, leg)
            self._active[key] = rid
        self.reqMarketDataType(3)
        contract = self._contract_from_candidate(cand)
        self.reqMktData(rid, contract, "", False, False, [])


def _contract_for_order(c: OptionCandidate) -> Contract:
    if int(getattr(c, "con_id", 0) or 0) > 0:
        ct = Contract()
        ct.conId = int(c.con_id)
        return ct
    return OptionMdHub._contract_from_candidate(c)


# IB sends these for open orders with the order id; they are notices, not rejections.
_ORDER_INFO_ONLY_CODES = frozenset({10349})


class DashboardOrderHub(EWrapper, EClient):
    """Dedicated IB connection: transmit limit orders; reports fills on main thread."""

    def __init__(self, host: str, port: int, client_id: int, root: tk.Tk):
        EWrapper.__init__(self)
        EClient.__init__(self, wrapper=self)
        self.host = host
        self.port = port
        self.client_id = client_id
        self._root = root
        self._next_order_id: Optional[int] = None
        self._id_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending: Dict[int, Dict[str, Any]] = {}
        self._ready = threading.Event()

    def nextValidId(self, orderId: int):
        with self._id_lock:
            self._next_order_id = orderId
        self._ready.set()

    def error(self, reqId: int, errorCode: int, errorString: str, advancedOrderRejectJson: str = ""):
        if reqId <= 0:
            return
        if errorCode in _ORDER_INFO_ONLY_CODES:
            return
        with self._pending_lock:
            pend = self._pending.pop(reqId, None)
        if pend:
            cb = pend.get("on_done")
            if cb:
                msg = f"[{errorCode}] {errorString}"
                self._root.after(0, lambda m=msg: cb(False, None, m))

    def orderStatus(
        self,
        orderId: int,
        status: str,
        filled: float,
        remaining: float,
        avgFillPrice: float,
        permId: int,
        parentId: int,
        lastFillPrice: float,
        clientId: int,
        whyHeld: str,
        mktCapPrice: float,
    ):
        oid = int(orderId)
        if status == "Filled":
            with self._pending_lock:
                pend = self._pending.pop(oid, None)
            if not pend:
                return
            cb = pend.get("on_done")
            if cb:
                apx = float(avgFillPrice) if avgFillPrice == avgFillPrice else None

                def _ok():
                    cb(True, apx, None)

                self._root.after(0, _ok)
        elif status in ("Cancelled", "ApiCancelled", "Inactive"):
            with self._pending_lock:
                pend = self._pending.pop(oid, None)
            if not pend:
                return
            cb = pend.get("on_done")
            if cb:

                def _cx():
                    cb(False, None, status)

                self._root.after(0, _cx)

    def start(self):
        self.connect(self.host, self.port, self.client_id)
        threading.Thread(target=self.run, daemon=True).start()
        if not self._ready.wait(20.0):
            raise TimeoutError("Order hub: timeout waiting for nextValidId.")

    def stop(self):
        try:
            self.disconnect()
        except Exception:
            pass

    def _alloc_order_id(self) -> int:
        with self._id_lock:
            if self._next_order_id is None:
                raise RuntimeError("Order hub: nextValidId not ready.")
            oid = int(self._next_order_id)
            self._next_order_id = oid + 1
            return oid

    def _apply_account(self, order: Order) -> None:
        acct = account_filter()
        if acct:
            order.account = acct

    def place_limit(
        self,
        contract: Contract,
        action: str,
        quantity: float,
        limit_price: float,
        on_done: Any,
    ) -> None:
        oid = self._alloc_order_id()
        with self._pending_lock:
            self._pending[oid] = {"on_done": on_done}
        o = Order()
        o.action = action
        o.orderType = "LMT"
        o.totalQuantity = float(quantity)
        o.lmtPrice = float(limit_price)
        o.transmit = True
        # ibapi defaults eTradeOnly/firmQuoteOnly to True; IB rejects 10268 if unsupported.
        o.eTradeOnly = False
        o.firmQuoteOnly = False
        o.tif = "DAY"
        self._apply_account(o)
        self.placeOrder(oid, contract, o)


class _ChartState(TypedDict):
    fig: Figure
    ax: Any
    canvas: FigureCanvasTkAgg
    bar: Any
    cost_line: Any
    wait_text: Any
    spot_label: Any
    pos: Dict[str, Any]
    outer: ttk.Frame
    maturity_combo: ttk.Combobox
    btn_maturities: ttk.Button
    btn_resolve: ttk.Button
    btn_sell_call: ttk.Button
    btn_sell_put: ttk.Button
    trade_toolbar: ttk.Frame
    toolbar_lbl_stock: ttk.Label
    toolbar_lbl_call: ttk.Label
    toolbar_lbl_put: ttk.Label
    btn_close_call: ttk.Button
    btn_close_put: ttk.Button
    cand_notebook: ttk.Notebook
    calls_tree: ttk.Treeview
    puts_tree: ttk.Treeview
    strike_hlines: List[Any]
    call_rows: List[OptionCandidate]
    put_rows: List[OptionCandidate]
    selected_call: Optional[OptionCandidate]
    selected_put: Optional[OptionCandidate]
    committed_call: Optional[OptionCandidate]
    committed_put: Optional[OptionCandidate]
    metrics: Dict[str, tk.StringVar]
    metrics_value_labels: Dict[str, tk.Label]
    options_chain_locked: bool
    ib_short_call_contracts: int
    ib_short_put_contracts: int
    chain_hint_lbl: ttk.Label


def _style_axes(ax: Any) -> None:
    ax.set_facecolor("#151c24")
    ax.tick_params(axis="x", colors="#e8f0f7")
    ax.tick_params(axis="y", colors="#e8f0f7")
    ax.yaxis.label.set_color("#e8f0f7")
    for spine in ax.spines.values():
        spine.set_color("#334155")


# Metrics panel (matches chart — dark, high-contrast type)
_MET_BG = "#0f1419"
_MET_BORDER = "#1e293b"
_MET_SECTION = "#64748b"
_MET_SUB = "#94a3b8"
_MET_VALUE = "#f1f5f9"
_MET_MUTED = "#64748b"
_MET_POS = "#4ade80"
_MET_NEG = "#f87171"
_MET_ACCENT_CALL = "#86efac"
_MET_ACCENT_PUT = "#fca5a5"

_PORTFOLIO_TAB_INDEX = 0

_OPTIONS_CHAIN_HINT_DEFAULT = (
    "Resolve: 5-tick OTM anchor, then 2.50 steps → snap to chain (10/side, live on select)."
)


def _fmt_option_delta(c: Optional[OptionCandidate]) -> str:
    if c is None:
        return ""
    d = c.delta
    if d == d and abs(float(d)) < IB_UNSET_DOUBLE:
        return f"{float(d):.4f}"
    return ""


def _fmt_portfolio_premium_collected_total(
    cc: Optional[OptionCandidate],
    cp: Optional[OptionCandidate],
) -> str:
    """Sum of short-leg credits: IB |avgCost×contracts| or fill price×contracts×100 per leg."""
    total = 0.0
    any_ok = False
    for c in (cc, cp):
        if c is None:
            continue
        p = float(c.premium_collected)
        if p == p and math.isfinite(p) and p >= 0:
            total += p
            any_ok = True
    return f"{total:,.2f}" if any_ok else "—"


def _position_notional_usd(
    shares: float,
    spot: Optional[float],
    cost_per_sh: float,
) -> float:
    """Stock leg $ tied: market value when we have a spot, else cost basis."""
    if spot is not None and math.isfinite(float(spot)) and float(spot) > 0:
        return abs(float(shares)) * float(spot)
    if _valid_cost_basis(cost_per_sh):
        return abs(float(shares)) * float(cost_per_sh)
    return 0.0


def _norm_expiry_sort_key(exp: str) -> str:
    e = (exp or "").strip()
    if len(e) == 6:
        return e + "01"
    return e


def _option_candidate_from_ib_contract(
    contract: Contract,
    position_qty: float = 0.0,
    avg_cost: float = float("nan"),
) -> OptionCandidate:
    u = (contract.symbol or "").strip().upper()
    prem = float("nan")
    pq = float(position_qty)
    if abs(pq) > 1e-9 and avg_cost == avg_cost and math.isfinite(avg_cost):
        # IB option avgCost is $ per contract; position is signed contract count (short < 0).
        prem = abs(float(avg_cost) * pq)
    return OptionCandidate(
        maturity=(contract.lastTradeDateOrContractMonth or "").strip(),
        strike=float(contract.strike or 0.0),
        right=(contract.right or "C").strip(),
        delta=float("nan"),
        distance=float("inf"),
        con_id=int(getattr(contract, "conId", 0) or 0),
        local_symbol=(contract.localSymbol or ""),
        bid=float("nan"),
        ask=float("nan"),
        exchange=(contract.exchange or "SMART"),
        currency=(contract.currency or "USD"),
        trading_class=(contract.tradingClass or ""),
        multiplier=str(contract.multiplier or "100"),
        underlying_symbol=u,
        premium_collected=prem,
    )


def _pick_ib_short_leg(
    legs: List[Tuple[Contract, float, float]],
) -> Optional[Tuple[Contract, float, float]]:
    if not legs:
        return None

    def sort_key(item: Tuple[Contract, float, float]) -> Tuple[str, float]:
        c, pos, _ = item
        e = _norm_expiry_sort_key(c.lastTradeDateOrContractMonth or "")
        return (e, -abs(float(pos)))

    return sorted(legs, key=sort_key)[0]


def _group_ib_short_options_by_underlying(
    rows: List[Tuple[str, Contract, float, float]],
) -> Dict[str, Dict[str, Optional[Tuple[Contract, float, float]]]]:
    """Underlying (OPT symbol) -> {'call': ..., 'put': ...} for short legs only."""
    by: Dict[str, List[Tuple[Contract, float, float]]] = defaultdict(list)
    for _acct, ct, pos, avg in rows:
        if float(pos) >= -1e-9:
            continue
        und = (ct.symbol or "").strip().upper()
        if not und:
            continue
        r = (ct.right or "").strip().upper()
        if r not in ("C", "P"):
            continue
        by[und].append((ct, float(pos), float(avg)))
    out: Dict[str, Dict[str, Optional[Tuple[Contract, float, float]]]] = {}
    for und, legs in by.items():
        calls = [x for x in legs if (x[0].right or "").strip().upper() == "C"]
        puts = [x for x in legs if (x[0].right or "").strip().upper() == "P"]
        bc = _pick_ib_short_leg(calls)
        bp = _pick_ib_short_leg(puts)
        if bc or bp:
            out[und] = {"call": bc, "put": bp}
    return out


def _scenario_call_contracts(st: Optional[_ChartState], shares: float) -> int:
    if st is not None and int(st.get("ib_short_call_contracts") or 0) > 0:
        return max(1, int(st["ib_short_call_contracts"]))
    return _contracts_for_metrics(shares)


def _scenario_put_contracts(st: Optional[_ChartState], shares: float) -> int:
    if st is not None and int(st.get("ib_short_put_contracts") or 0) > 0:
        return max(1, int(st["ib_short_put_contracts"]))
    return _contracts_for_metrics(shares)


class HoldingsDashboardApp:
    def __init__(self):
        self.positions: List[Dict[str, Any]] = []
        self.quotes: Dict[str, dict] = {}
        self._dirty: Set[str] = set()
        self._redraw_scheduled = False
        self.ib: Optional[IBPortfolioClient] = None
        self._quote_queue: "queue.Queue[tuple[str, dict]]" = queue.Queue()
        self._running = False
        self._charts: Dict[str, _ChartState] = {}
        self._opt_hub: Optional[OptionMdHub] = None
        self._opt_hub_lock = threading.Lock()
        self._order_hub: Optional[DashboardOrderHub] = None
        self._order_hub_lock = threading.Lock()

        self.root = tk.Tk()
        self.root.title("Equity holdings · IB stream")
        self.root.minsize(820, 560)

        header = ttk.Frame(self.root, padding=(12, 10))
        header.pack(fill=tk.X)
        ttk.Label(
            header,
            text="Stock positions and average cost from IB; live spot via reqMktData. "
            "Optional: IB_ACCOUNT=... to filter one account.",
            wraplength=780,
        ).pack(anchor=tk.W)

        self.main_area = ttk.Frame(self.root)
        self.main_area.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 4))

        self._loading = ttk.Label(self.main_area, text="Loading stock positions from IB…")
        self._loading.pack(expand=True)

        self.notebook: Optional[ttk.Notebook] = None
        self._portfolio_tree: Optional[ttk.Treeview] = None
        self._portfolio_raw_cash_var: Optional[tk.StringVar] = None
        self._portfolio_cash_baseline_var: Optional[tk.StringVar] = None
        self._cash_denominator: Optional[float] = None
        self._cash_denominator_tag: str = ""
        self._portfolio_total_principal: float = 0.0
        self._portfolio_capital_base: float = 0.0

        self.status = ttk.Label(self.root, text="Connecting…", padding=(10, 6))
        self.status.pack(fill=tk.X)

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        threading.Thread(target=self._bootstrap_ib, daemon=True).start()

    def _create_metrics_panel(
        self, parent: ttk.Frame, contracts_note: str
    ) -> tuple[Dict[str, tk.StringVar], Dict[str, tk.Label]]:
        """Dark, sectioned readout: primary figure + subtitle per row; value Labels for P/L coloring."""
        outer = ttk.LabelFrame(
            parent,
            text=f"Scenario · P/L · premium · assignment · basis  ·  {contracts_note}",
            padding=(0, 2),
        )
        outer.pack(fill=tk.X, padx=2, pady=(0, 2))

        shell = tk.Frame(outer, bg=_MET_BG, highlightthickness=1, highlightbackground=_MET_BORDER)
        shell.pack(fill=tk.X, padx=1, pady=1)

        inner = tk.Frame(shell, bg=_MET_BG)
        inner.pack(fill=tk.X, padx=8, pady=(4, 5))

        mv: Dict[str, tk.StringVar] = {}
        value_labels: Dict[str, tk.Label] = {}

        def _sep(row: int) -> int:
            sep = tk.Frame(inner, bg=_MET_BORDER, height=1)
            sep.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(3, 3))
            return row + 1

        def _section(row: int, title: str) -> int:
            tk.Label(
                inner,
                text=title,
                bg=_MET_BG,
                fg=_MET_SECTION,
                font=("Segoe UI", 7, "bold"),
                anchor="w",
            ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(0, 1))
            return row + 1

        def _row(
            row: int,
            sid: str,
            label: str,
            label_fg: str,
            main0: str,
            sub0: str,
        ) -> int:
            mv[f"{sid}_main"] = tk.StringVar(value=main0)
            mv[f"{sid}_sub"] = tk.StringVar(value=sub0)
            tk.Label(
                inner,
                text=label,
                bg=_MET_BG,
                fg=label_fg,
                font=("Segoe UI", 8),
                anchor="w",
            ).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=0)
            right = tk.Frame(inner, bg=_MET_BG)
            right.grid(row=row, column=1, sticky="e", pady=0)
            line = tk.Frame(right, bg=_MET_BG)
            line.pack(anchor="e")
            # One line: bold value flush right; muted detail immediately to its left.
            vl = tk.Label(
                line,
                textvariable=mv[f"{sid}_main"],
                bg=_MET_BG,
                fg=_MET_VALUE,
                font=("Segoe UI", 10, "bold"),
                anchor="e",
            )
            vl.pack(side=tk.RIGHT)
            value_labels[sid] = vl
            tk.Label(
                line,
                textvariable=mv[f"{sid}_sub"],
                bg=_MET_BG,
                fg=_MET_SUB,
                font=("Segoe UI", 7),
                anchor="e",
            ).pack(side=tk.RIGHT, padx=(0, 6))
            return row + 1

        r = 0
        r = _section(r, "POSITION")
        r = _row(r, "stock", "Unrealized stock P/L", _MET_VALUE, "—", "Wait for spot / chain.")
        r = _sep(r)
        r = _section(r, "PREMIUM · bid×100×lots (live on row)")
        r = _row(r, "call_prem", "Covered call", _MET_ACCENT_CALL, "—", "Pick strike in Calls.")
        r = _row(r, "put_prem", "Cash-secured put", _MET_ACCENT_PUT, "—", "Pick strike in Puts.")
        r = _sep(r)
        r = _section(r, "ASSIGNMENT · saved strikes")
        r = _row(r, "call_assign", "Called away (ex prem)", _MET_ACCENT_CALL, "—", "Pick call for P/L.")
        r = _row(r, "put_basis", "Put · blended $/sh", _MET_ACCENT_PUT, "—", "Pick put for average.")
        r = _row(r, "put_cash", "Put · cash at K", _MET_ACCENT_PUT, "—", "Pick put for cash req.")

        inner.columnconfigure(0, weight=0)
        inner.columnconfigure(1, weight=1)

        return mv, value_labels

    def _root_alive(self) -> bool:
        try:
            return bool(self.root.winfo_exists())
        except tk.TclError:
            return False

    def _bootstrap_ib(self):
        def on_quote(sym: str, payload: dict):
            self._enqueue_quote(sym, payload)

        client: Optional[IBPortfolioClient] = None
        cash_denom: Optional[float] = None
        cash_tag = ""
        try:
            host, port, client_id = ib_settings()
            client = IBPortfolioClient(
                host,
                port,
                client_id,
                on_quote=on_quote,
                account_filter=account_filter(),
            )
            client.start()
            positions = client.fetch_stock_positions()
            option_pos_snap = client.get_option_positions_snapshot()
            if not positions:
                raise RuntimeError(
                    "No stock (STK) positions from IB. "
                    "Check account, API permissions, and that positionEnd is received."
                )
            cash_denom, cash_tag = client.fetch_cash_denominator_for_portfolio()
            symbols = [str(p["symbol"]) for p in positions]
            client.subscribe(symbols)
            snap = client.get_quotes_snapshot()
        except Exception as e:
            if client:
                try:
                    client.stop()
                except Exception:
                    pass
            self.root.after(0, lambda err=e: self._bootstrap_failed(err))
            return

        def finish(
            cd: Optional[float] = cash_denom,
            ct: str = cash_tag,
            opt_snap: List[Tuple[str, Contract, float, float]] = option_pos_snap,
        ):
            if not self._root_alive():
                try:
                    client.stop()
                except Exception:
                    pass
                return
            self.ib = client
            self.positions = positions
            self.quotes = snap
            self._cash_denominator = cd if cd is not None and cd > 0 else None
            self._cash_denominator_tag = ct or ""
            self._loading.destroy()
            self._build_tabs()
            self._apply_ib_option_positions(opt_snap)
            self._refresh_portfolio_table()
            for pos in self.positions:
                self._update_chart(str(pos["symbol"]))
            host, port, client_id = ib_settings()
            self._set_status(f"IB {host}:{port} · clientId {client_id} · {len(positions)} stock position(s)")

        self.root.after(0, finish)

    def _bootstrap_failed(self, err: Exception):
        messagebox.showerror("Interactive Brokers", str(err))
        self._set_status(str(err))
        ttk.Label(
            self.main_area,
            text="Fix the connection or permissions, then restart the app.",
            foreground="#666",
        ).pack(pady=12)

    def _build_portfolio_tab(self, parent: ttk.Frame) -> None:
        ttk.Label(
            parent,
            text="All stock rows from IB. Option columns use filled short legs recorded in this session "
            "(after a successful Sell call / Sell put). Stream a leg on the symbol tab for live delta.",
            wraplength=760,
        ).pack(anchor=tk.W, pady=(0, 4))
        self._portfolio_raw_cash_var = tk.StringVar(value="")
        ttk.Label(
            parent,
            textvariable=self._portfolio_raw_cash_var,
            wraplength=760,
            font=("Segoe UI", 10, "bold"),
        ).pack(anchor=tk.W, pady=(0, 2))
        self._portfolio_cash_baseline_var = tk.StringVar(value="")
        ttk.Label(
            parent,
            textvariable=self._portfolio_cash_baseline_var,
            wraplength=760,
            foreground="#475569",
        ).pack(anchor=tk.W, pady=(0, 6))

        wrap = ttk.Frame(parent)
        wrap.pack(fill=tk.BOTH, expand=True)
        cols = (
            "symbol",
            "principal",
            "position",
            "pct_cash",
            "delta",
            "prem_coll",
            "call_assign",
            "put_cash",
            "put_basis",
            "last",
            "cost_sh",
            "stock_pl",
        )
        tv = ttk.Treeview(
            wrap,
            columns=cols,
            show="headings",
            selectmode="browse",
            height=18,
        )
        tv.heading("symbol", text="Symbol")
        tv.heading("principal", text="Principal")
        tv.heading("position", text="Short option")
        tv.heading("pct_cash", text="% of capital")
        tv.heading("delta", text="Δ (C / P)")
        tv.heading("prem_coll", text="Premium collected")
        tv.heading("call_assign", text="Call assign · stock P/L")
        tv.heading("put_cash", text="Put · cash at K")
        tv.heading("put_basis", text="Put · basis if assigned")
        tv.heading("last", text="Last")
        tv.heading("cost_sh", text="Cost basis")
        tv.heading("stock_pl", text="Stock P/L")
        tv.column("symbol", width=72, anchor=tk.W)
        tv.column("principal", width=88, anchor=tk.E)
        tv.column("position", width=120, anchor=tk.W)
        tv.column("pct_cash", width=72, anchor=tk.E)
        tv.column("delta", width=110, anchor=tk.W)
        tv.column("prem_coll", width=100, anchor=tk.E)
        tv.column("call_assign", width=130, anchor=tk.E)
        tv.column("put_cash", width=108, anchor=tk.E)
        tv.column("put_basis", width=96, anchor=tk.E)
        tv.column("last", width=56, anchor=tk.E)
        tv.column("cost_sh", width=72, anchor=tk.E)
        tv.column("stock_pl", width=96, anchor=tk.E)
        vsb = ttk.Scrollbar(wrap, orient=tk.VERTICAL, command=tv.yview)
        tv.configure(yscrollcommand=vsb.set)
        tv.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._portfolio_tree = tv

    def _portfolio_row_values(self, pos: Dict[str, Any]) -> tuple:
        sym = str(pos["symbol"])
        st = self._charts.get(sym)
        cost = float(pos["cost_basis"])
        shares = float(pos["shares"])
        stock_lots = _contracts_for_metrics(shares)
        call_cx = _scenario_call_contracts(st, shares) if st else stock_lots
        put_cx = _scenario_put_contracts(st, shares) if st else stock_lots
        q = self.quotes.get(sym) or {}
        spot = _spot_value(q)
        last_s = f"{float(spot):.2f}" if spot is not None else "—"
        if _valid_cost_basis(cost):
            principal_s = f"{shares * cost:,.2f}"
            cost_s = f"{cost:.2f}"
        else:
            principal_s = "—"
            cost_s = "—"

        if spot is not None and _valid_cost_basis(cost):
            st_pl = (float(spot) - cost) * shares
            stock_pl_s = f"{st_pl:+,.2f}"
        elif _valid_cost_basis(cost):
            stock_pl_s = "—"
        else:
            stock_pl_s = "—"

        cc = st["committed_call"] if st else None
        cp = st["committed_put"] if st else None

        if cc is not None and cp is not None:
            pos_s = "Short call + put"
        elif cc is not None:
            pos_s = "Short call"
        elif cp is not None:
            pos_s = "Short put"
        else:
            pos_s = "None"

        d_parts: List[str] = []
        if cc is not None:
            fd = _fmt_option_delta(cc)
            d_parts.append(f"C {fd}" if fd else "C —")
        if cp is not None:
            fd = _fmt_option_delta(cp)
            d_parts.append(f"P {fd}" if fd else "P —")
        delta_s = " / ".join(d_parts) if d_parts else "—"
        prem_s = _fmt_portfolio_premium_collected_total(cc, cp)

        if cc is not None and _valid_cost_basis(cost):
            K = float(cc.strike)
            pl_as = (K - cost) * 100.0 * float(call_cx)
            call_s = f"{pl_as:+,.2f}"
        else:
            call_s = "—"

        if cp is not None:
            K = float(cp.strike)
            cash = K * 100.0 * float(put_cx)
            put_cash_s = f"{cash:,.2f}"
        else:
            put_cash_s = "—"

        if cp is not None and _valid_cost_basis(cost) and shares > 0:
            K = float(cp.strike)
            add_sh = 100.0 * float(put_cx)
            new_tot = shares + add_sh
            new_basis = (shares * cost + add_sh * K) / new_tot
            put_basis_s = f"{new_basis:.2f}"
        else:
            put_basis_s = "—"

        tied = _position_notional_usd(shares, spot, cost)
        if cp is not None:
            tied += float(cp.strike) * 100.0 * float(put_cx)
        base = self._portfolio_capital_base
        if base > 0 and tied >= 0:
            pct_s = f"{100.0 * tied / base:.1f}%"
        else:
            pct_s = "—"

        return (
            sym,
            principal_s,
            pos_s,
            pct_s,
            delta_s,
            prem_s,
            call_s,
            put_cash_s,
            put_basis_s,
            last_s,
            cost_s,
            stock_pl_s,
        )

    def _recompute_portfolio_capital_totals(self) -> None:
        """Capital base = IB raw cash + sum of (shares × cost/sh) for all positions."""
        total_prin = 0.0
        for p in self.positions:
            c = float(p["cost_basis"])
            sh = float(p["shares"])
            if _valid_cost_basis(c):
                total_prin += float(sh) * float(c)
        cash = (
            float(self._cash_denominator)
            if self._cash_denominator is not None and self._cash_denominator > 0
            else 0.0
        )
        self._portfolio_total_principal = total_prin
        self._portfolio_capital_base = cash + total_prin

    def _sync_portfolio_cash_labels(self) -> None:
        raw_var = self._portfolio_raw_cash_var
        hint_var = self._portfolio_cash_baseline_var
        d = self._cash_denominator
        tag = (self._cash_denominator_tag or "").strip()
        base = self._portfolio_capital_base
        tp = self._portfolio_total_principal

        if raw_var is not None:
            if d is not None and d > 0 and tag:
                raw_var.set(f"Raw cash available ({tag}): ${d:,.2f}")
            else:
                raw_var.set("Raw cash available: — (enable account summary / set IB_CASH_DENOMINATOR_TAG)")

        if hint_var is None:
            return
        cash_s = f"${d:,.2f}" if d is not None and d > 0 and tag else "—"
        if base > 0:
            hint_var.set(
                f"'% of capital' = (stock notional at last + short-put cash at K) / "
                f"(raw cash {cash_s} + total principal ${tp:,.2f} = ${base:,.2f}). "
                f"Cash tags are not NetLiquidation. Optional: IB_CASH_DENOMINATOR_TAG."
            )
        elif not self.positions:
            hint_var.set("")
        else:
            hint_var.set(
                "Cannot compute '% of capital' (capital base is zero). "
                "Need positive IB cash and/or positions with valid cost basis."
            )

    def _refresh_portfolio_table(self) -> None:
        tv = self._portfolio_tree
        if tv is None:
            return
        self._recompute_portfolio_capital_totals()
        self._sync_portfolio_cash_labels()
        want: Set[str] = set()
        for pos in self.positions:
            sym = str(pos["symbol"])
            want.add(sym)
            vals = self._portfolio_row_values(pos)
            if tv.exists(sym):
                tv.item(sym, values=vals)
            else:
                tv.insert("", tk.END, iid=sym, values=vals)
        for iid in list(tv.get_children()):
            if str(iid) not in want:
                tv.delete(iid)

    def _refresh_portfolio_row(self, sym: str) -> None:
        tv = self._portfolio_tree
        if tv is None:
            return
        self._recompute_portfolio_capital_totals()
        self._sync_portfolio_cash_labels()
        pos = next((p for p in self.positions if str(p["symbol"]) == sym), None)
        if pos is None:
            return
        vals = self._portfolio_row_values(pos)
        if tv.exists(sym):
            tv.item(sym, values=vals)
        else:
            tv.insert("", tk.END, iid=sym, values=vals)

    def _build_tabs(self):
        self.notebook = ttk.Notebook(self.main_area, padding=4)
        self.notebook.pack(fill=tk.BOTH, expand=True)
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        port_tab = ttk.Frame(self.notebook, padding=6)
        self.notebook.add(port_tab, text="Portfolio")
        self._build_portfolio_tab(port_tab)

        for pos in self.positions:
            sym = str(pos["symbol"])
            tab = ttk.Frame(self.notebook)
            sh = _fmt_qty(float(pos["shares"]))
            self.notebook.add(tab, text=f"{sym} · {sh} sh")
            self._charts[sym] = self._create_chart(tab, pos)

    def _create_chart(self, tab: ttk.Frame, pos: Dict[str, Any]) -> _ChartState:
        sym = str(pos["symbol"])
        cost = float(pos["cost_basis"])
        sh = _fmt_qty(float(pos["shares"]))

        outer = ttk.Frame(tab)
        outer.pack(fill=tk.BOTH, expand=True)

        ctrl = ttk.Frame(outer, padding=(4, 6))
        ctrl.pack(fill=tk.X)
        row1 = ttk.Frame(ctrl)
        row1.pack(fill=tk.X, pady=2)
        ttk.Label(row1, text="Maturity:").pack(side=tk.LEFT, padx=(0, 6))
        maturity_combo = ttk.Combobox(row1, width=14, state="readonly", values=())
        maturity_combo.pack(side=tk.LEFT, padx=(0, 8))
        btn_maturities = ttk.Button(
            row1,
            text="Generate maturities",
            command=lambda s=sym: self._generate_maturities(s),
        )
        btn_maturities.pack(side=tk.LEFT, padx=(0, 6))
        btn_sell_call = ttk.Button(
            row1,
            text="Sell call @ mid",
            command=lambda s=sym: self._sell_selected_option(s, "call"),
        )
        btn_sell_call.pack(side=tk.LEFT, padx=(0, 4))
        btn_sell_put = ttk.Button(
            row1,
            text="Sell put @ mid",
            command=lambda s=sym: self._sell_selected_option(s, "put"),
        )
        btn_sell_put.pack(side=tk.LEFT)

        trade_toolbar = ttk.Frame(ctrl)
        trade_toolbar.pack(fill=tk.X, pady=(6, 0))
        toolbar_lbl_stock = ttk.Label(trade_toolbar, text="Stock P/L: —")
        toolbar_lbl_stock.pack(side=tk.LEFT, padx=(0, 10))
        ttk.Separator(trade_toolbar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=4)
        toolbar_lbl_call = ttk.Label(trade_toolbar, text="Short call: —")
        toolbar_lbl_call.pack(side=tk.LEFT, padx=(6, 4))
        btn_close_call = ttk.Button(
            trade_toolbar,
            text="Close call",
            width=11,
            command=lambda s=sym: self._close_committed_option(s, "call"),
        )
        btn_close_call.pack(side=tk.LEFT, padx=(0, 10))
        ttk.Separator(trade_toolbar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=4)
        toolbar_lbl_put = ttk.Label(trade_toolbar, text="Short put: —")
        toolbar_lbl_put.pack(side=tk.LEFT, padx=(6, 4))
        btn_close_put = ttk.Button(
            trade_toolbar,
            text="Close put",
            width=11,
            command=lambda s=sym: self._close_committed_option(s, "put"),
        )
        btn_close_put.pack(side=tk.LEFT)

        chart_host = ttk.Frame(outer)
        chart_host.pack(fill=tk.BOTH, expand=True)

        fig = Figure(figsize=(7.6, 4.4), dpi=100)
        fig.patch.set_facecolor("#0f1419")
        ax = fig.add_subplot(111)
        _style_axes(ax)
        ax.set_ylabel("Price")
        ax.set_xticks([0])
        ax.set_xticklabels(["Spot"])
        ax.set_xlim(-0.6, 0.6)

        bar_patch = ax.bar(
            [0],
            [1e-6],
            width=0.42,
            bottom=0,
            color="#e8943c",
            edgecolor="#c97d30",
            linewidth=1,
            zorder=2,
        ).patches[0]
        bar_patch.set_visible(False)

        cost_line = ax.axhline(y=cost, color="#38bdf8", linewidth=2.5, zorder=1, label="Avg cost")

        wait_text = ax.text(
            0.5,
            0.55,
            "Waiting for live quote…",
            transform=ax.transAxes,
            ha="center",
            va="center",
            color="#8aa0b4",
            fontsize=12,
            zorder=3,
        )

        spot_label = ax.text(
            0,
            0,
            "",
            ha="center",
            va="bottom",
            color="#e8943c",
            fontsize=11,
            fontweight="600",
            zorder=4,
        )
        spot_label.set_visible(False)

        ax.set_title(
            f"{sym} — {sh} shares · avg cost {cost:.2f}/sh (IB)",
            color="#e8f0f7",
            fontsize=13,
            loc="left",
            pad=12,
        )

        canvas = FigureCanvasTkAgg(fig, master=chart_host)
        canvas.draw()
        canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        work_nb = ttk.Notebook(outer, padding=(0, 2))
        work_nb.pack(fill=tk.BOTH, expand=True, padx=2, pady=(0, 2))

        options_tab = ttk.Frame(work_nb, padding=4)
        scenario_tab = ttk.Frame(work_nb, padding=4)
        work_nb.add(options_tab, text="Options")
        work_nb.add(scenario_tab, text="Scenario analysis")

        row2 = ttk.Frame(options_tab)
        row2.pack(fill=tk.X, pady=(0, 4))
        chain_hint_lbl = ttk.Label(
            row2,
            text=_OPTIONS_CHAIN_HINT_DEFAULT,
            wraplength=520,
        )
        chain_hint_lbl.pack(side=tk.LEFT, padx=(0, 10))
        btn_resolve = ttk.Button(
            row2,
            text="Resolve OTM chain",
            command=lambda s=sym: self._resolve_contracts(s),
        )
        btn_resolve.pack(side=tk.LEFT)

        cand_fr = ttk.LabelFrame(
            options_tab,
            text="Candidates — click a row to save Call (green) / Put (red) on chart",
            padding=(4, 6),
        )
        cand_fr.pack(fill=tk.BOTH, expand=True)
        cand_notebook = ttk.Notebook(cand_fr)
        cand_notebook.pack(fill=tk.BOTH, expand=True)

        calls_tab = ttk.Frame(cand_notebook, padding=2)
        puts_tab = ttk.Frame(cand_notebook, padding=2)
        cand_notebook.add(calls_tab, text="Calls")
        cand_notebook.add(puts_tab, text="Puts")

        cols = ("strike", "bid", "ask", "delta", "dspot", "exp")

        def _make_tree(parent: ttk.Frame) -> ttk.Treeview:
            wrap = ttk.Frame(parent)
            wrap.pack(fill=tk.BOTH, expand=True)
            tv = ttk.Treeview(
                wrap,
                columns=cols,
                show="headings",
                height=10,
                selectmode="browse",
            )
            tv.heading("strike", text="Strike")
            tv.heading("bid", text="Bid")
            tv.heading("ask", text="Ask")
            tv.heading("delta", text="Delta")
            tv.heading("dspot", text="|K−S|")
            tv.heading("exp", text="Expiry")
            tv.column("strike", width=64, anchor=tk.E)
            tv.column("bid", width=52, anchor=tk.E)
            tv.column("ask", width=52, anchor=tk.E)
            tv.column("delta", width=56, anchor=tk.E)
            tv.column("dspot", width=56, anchor=tk.E)
            tv.column("exp", width=88, anchor=tk.CENTER)
            vsb = ttk.Scrollbar(wrap, orient=tk.VERTICAL, command=tv.yview)
            tv.configure(yscrollcommand=vsb.set)
            tv.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            vsb.pack(side=tk.RIGHT, fill=tk.Y)
            return tv

        calls_tree = _make_tree(calls_tab)
        puts_tree = _make_tree(puts_tab)
        calls_tree.bind("<<TreeviewSelect>>", lambda _e, s=sym: self._on_call_row_selected(s))
        puts_tree.bind("<<TreeviewSelect>>", lambda _e, s=sym: self._on_put_row_selected(s))

        lots = _contracts_for_metrics(float(pos["shares"]))
        mv, metrics_value_labels = self._create_metrics_panel(scenario_tab, f"{lots} lot(s) × 100 sh")

        return _ChartState(
            fig=fig,
            ax=ax,
            canvas=canvas,
            bar=bar_patch,
            cost_line=cost_line,
            wait_text=wait_text,
            spot_label=spot_label,
            pos=pos,
            outer=outer,
            maturity_combo=maturity_combo,
            btn_maturities=btn_maturities,
            btn_resolve=btn_resolve,
            btn_sell_call=btn_sell_call,
            btn_sell_put=btn_sell_put,
            trade_toolbar=trade_toolbar,
            toolbar_lbl_stock=toolbar_lbl_stock,
            toolbar_lbl_call=toolbar_lbl_call,
            toolbar_lbl_put=toolbar_lbl_put,
            btn_close_call=btn_close_call,
            btn_close_put=btn_close_put,
            cand_notebook=cand_notebook,
            calls_tree=calls_tree,
            puts_tree=puts_tree,
            strike_hlines=[],
            call_rows=[],
            put_rows=[],
            selected_call=None,
            selected_put=None,
            committed_call=None,
            committed_put=None,
            metrics=mv,
            metrics_value_labels=metrics_value_labels,
            options_chain_locked=False,
            ib_short_call_contracts=0,
            ib_short_put_contracts=0,
            chain_hint_lbl=chain_hint_lbl,
        )

    def _maybe_unlock_options_chain(self, st: _ChartState, sym: str) -> None:
        if not st.get("options_chain_locked"):
            return
        if int(st.get("ib_short_call_contracts") or 0) != 0:
            return
        if int(st.get("ib_short_put_contracts") or 0) != 0:
            return
        st["options_chain_locked"] = False
        st["btn_maturities"].configure(state=tk.NORMAL)
        st["btn_resolve"].configure(state=tk.NORMAL)
        st["maturity_combo"].configure(state="readonly", values=())
        st["maturity_combo"].set("")
        try:
            st["calls_tree"].state(())
            st["puts_tree"].state(())
        except tk.TclError:
            pass
        st["chain_hint_lbl"].configure(text=_OPTIONS_CHAIN_HINT_DEFAULT, foreground="")

    def _apply_ib_option_positions(self, raw: List[Tuple[str, Contract, float, float]]) -> None:
        grouped = _group_ib_short_options_by_underlying(raw)
        if not grouped:
            return
        try:
            hub = self._ensure_option_hub()
        except Exception:
            hub = None
        for sym, st in self._charts.items():
            legs = grouped.get(sym.upper())
            if not legs:
                continue
            call_t = legs.get("call")
            put_t = legs.get("put")
            if not call_t and not put_t:
                continue
            maturities: Set[str] = set()
            ct = st["calls_tree"]
            pt = st["puts_tree"]
            for iid in ct.get_children():
                ct.delete(iid)
            for iid in pt.get_children():
                pt.delete(iid)

            if call_t:
                c_cand = _option_candidate_from_ib_contract(call_t[0], call_t[1], call_t[2])
                st["ib_short_call_contracts"] = max(1, int(round(abs(float(call_t[1])))))
                st["committed_call"] = c_cand
                st["selected_call"] = c_cand
                st["call_rows"] = [c_cand]
                maturities.add(c_cand.maturity)
                ct.insert("", tk.END, iid="0", values=_candidate_tree_values(c_cand))
                ct.selection_set("0")
                if hub:
                    try:
                        hub.set_leg(sym, "call", c_cand)
                    except Exception:
                        pass
            else:
                st["call_rows"] = []
                st["selected_call"] = None
                st["committed_call"] = None
                st["ib_short_call_contracts"] = 0
                if hub:
                    try:
                        hub.set_leg(sym, "call", None)
                    except Exception:
                        pass

            if put_t:
                p_cand = _option_candidate_from_ib_contract(put_t[0], put_t[1], put_t[2])
                st["ib_short_put_contracts"] = max(1, int(round(abs(float(put_t[1])))))
                st["committed_put"] = p_cand
                st["selected_put"] = p_cand
                st["put_rows"] = [p_cand]
                maturities.add(p_cand.maturity)
                pt.insert("", tk.END, iid="0", values=_candidate_tree_values(p_cand))
                pt.selection_set("0")
                if hub:
                    try:
                        hub.set_leg(sym, "put", p_cand)
                    except Exception:
                        pass
            else:
                st["put_rows"] = []
                st["selected_put"] = None
                st["committed_put"] = None
                st["ib_short_put_contracts"] = 0
                if hub:
                    try:
                        hub.set_leg(sym, "put", None)
                    except Exception:
                        pass

            st["options_chain_locked"] = True
            st["btn_maturities"].configure(state=tk.DISABLED)
            st["btn_resolve"].configure(state=tk.DISABLED)
            st["btn_sell_call"].configure(state=tk.DISABLED)
            st["btn_sell_put"].configure(state=tk.DISABLED)
            ms = sorted(maturities, key=_norm_expiry_sort_key)
            if ms:
                st["maturity_combo"].configure(values=tuple(ms))
                st["maturity_combo"].set(ms[0])
            st["maturity_combo"].configure(state="disabled")
            try:
                ct.state(("disabled",))
                pt.state(("disabled",))
            except tk.TclError:
                pass
            st["chain_hint_lbl"].configure(
                text="Chain locked to IB short option leg(s); maturity matches open position.",
                foreground="#b45309",
            )
            self._redraw_saved_strikes(st)
            self._refresh_pl_metrics(sym)
            self._sync_trade_toolbar(sym)

    def _set_status(self, text: str):
        self.status.configure(text=text)

    def _clear_strike_hlines(self, st: _ChartState) -> None:
        ax = st["ax"]
        for ln in st["strike_hlines"]:
            try:
                ln.remove()
            except Exception:
                pass
        st["strike_hlines"].clear()

    def _ensure_option_hub(self) -> OptionMdHub:
        with self._opt_hub_lock:
            if self._opt_hub is not None:
                return self._opt_hub
            host, port, _ = ib_settings()
            cid = option_stream_client_id()
            hub = OptionMdHub(host, port, cid, self.root, self._on_option_stream_refresh)
            hub.start()
            self._opt_hub = hub
            return self._opt_hub

    def _ensure_order_hub(self) -> DashboardOrderHub:
        with self._order_hub_lock:
            if self._order_hub is not None:
                return self._order_hub
            host, port, _ = ib_settings()
            cid = option_order_client_id()
            hub = DashboardOrderHub(host, port, cid, self.root)
            hub.start()
            self._order_hub = hub
            return self._order_hub

    def _sell_selected_option(self, sym: str, leg: str) -> None:
        st = self._charts.get(sym)
        if not st:
            return
        cand = st["selected_call"] if leg == "call" else st["selected_put"]
        if cand is None:
            messagebox.showwarning("Order", f"Select a {'call' if leg == 'call' else 'put'} row first.")
            return
        mid = _option_mid_price(cand)
        if mid is None:
            messagebox.showerror("Order", "Need both bid and ask on the selected contract to compute mid.")
            return
        qty = float(_contracts_for_metrics(float(st["pos"]["shares"])))
        lmt = _round_option_limit(mid)
        if not messagebox.askyesno(
            "Confirm option order",
            f"SELL to open {int(qty)} {leg} contract(s) {sym} "
            f"{cand.maturity} K {cand.strike:.2f} @ {lmt:.2f} limit (mid)?",
        ):
            return
        try:
            hub = self._ensure_order_hub()
        except Exception as e:
            messagebox.showerror("Order hub", str(e))
            return
        contract = _contract_for_order(cand)

        def on_done(ok: bool, apx: Optional[float], err: Optional[str]) -> None:
            if not ok:
                messagebox.showerror("Order", err or "Order rejected or cancelled.")
                self._set_status(f"{sym}: {leg} order failed")
                return
            if leg == "call":
                st["committed_call"] = cand
                st["ib_short_call_contracts"] = max(1, int(qty))
            else:
                st["committed_put"] = cand
                st["ib_short_put_contracts"] = max(1, int(qty))
            if apx is not None and apx == apx and math.isfinite(float(apx)):
                # Match IB convention: |fill price × contracts opened × 100|.
                cand.premium_collected = abs(float(apx) * float(qty) * 100.0)
            try:
                md = self._ensure_option_hub()
                md.set_leg(sym, leg, cand)
            except Exception:
                pass
            self._redraw_saved_strikes(st)
            self._refresh_pl_metrics(sym)
            self._refresh_portfolio_row(sym)
            fill = f" @ {apx:.4f}" if apx is not None and apx == apx else ""
            self._set_status(f"{sym}: short {leg} filled{fill}")

        hub.place_limit(contract, "SELL", qty, lmt, on_done)

    def _close_committed_option(self, sym: str, leg: str) -> None:
        st = self._charts.get(sym)
        if not st:
            return
        cand = st["committed_call"] if leg == "call" else st["committed_put"]
        if cand is None:
            messagebox.showwarning("Order", f"No filled short {leg} to close in this session.")
            return
        mid = _option_mid_price(cand)
        if mid is None:
            messagebox.showerror(
                "Order",
                "Need bid/ask on the short leg for a mid limit. Re-select that strike so quotes stream.",
            )
            return
        sh = float(st["pos"]["shares"])
        qty = float(
            _scenario_call_contracts(st, sh) if leg == "call" else _scenario_put_contracts(st, sh)
        )
        lmt = _round_option_limit(mid)
        if not messagebox.askyesno(
            "Confirm close",
            f"BUY to close {int(qty)} {leg} contract(s) @ {lmt:.2f} limit (mid)?",
        ):
            return
        try:
            hub = self._ensure_order_hub()
        except Exception as e:
            messagebox.showerror("Order hub", str(e))
            return
        contract = _contract_for_order(cand)

        def on_done(ok: bool, apx: Optional[float], err: Optional[str]) -> None:
            if not ok:
                messagebox.showerror("Order", err or "Order rejected or cancelled.")
                return
            if leg == "call":
                st["committed_call"] = None
                st["selected_call"] = None
                st["call_rows"] = []
                st["ib_short_call_contracts"] = 0
                for iid in st["calls_tree"].get_children():
                    st["calls_tree"].delete(iid)
            else:
                st["committed_put"] = None
                st["selected_put"] = None
                st["put_rows"] = []
                st["ib_short_put_contracts"] = 0
                for iid in st["puts_tree"].get_children():
                    st["puts_tree"].delete(iid)
            try:
                if self._opt_hub is not None:
                    self._opt_hub.set_leg(sym, leg, None)
            except Exception:
                pass
            self._maybe_unlock_options_chain(st, sym)
            self._redraw_saved_strikes(st)
            self._refresh_pl_metrics(sym)
            self._refresh_portfolio_row(sym)
            self._set_status(f"{sym}: closed short {leg}")

        hub.place_limit(contract, "BUY", qty, lmt, on_done)

    def _sync_trade_toolbar(self, sym: str) -> None:
        st = self._charts.get(sym)
        if not st:
            return
        pos = st["pos"]
        cost = float(pos["cost_basis"])
        shares = float(pos["shares"])
        spot = _spot_value(self.quotes.get(sym))

        if spot is not None and _valid_cost_basis(cost):
            pl = (float(spot) - cost) * shares
            st["toolbar_lbl_stock"].configure(
                text=f"Stock P/L: {pl:+,.2f}  ·  {_fmt_qty(shares)} sh  ·  spot {float(spot):.2f}",
            )
        elif _valid_cost_basis(cost):
            st["toolbar_lbl_stock"].configure(
                text=f"Stock P/L: —  ·  {_fmt_qty(shares)} sh @ {cost:.2f}  ·  no spot",
            )
        else:
            st["toolbar_lbl_stock"].configure(text="Stock P/L: —")

        sc = st["selected_call"]
        sp = st["selected_put"]
        cc = st["committed_call"]
        cp = st["committed_put"]

        def leg_txt(sel: Optional[OptionCandidate], com: Optional[OptionCandidate], name: str) -> str:
            c = com if com is not None else sel
            if c is None:
                return f"{name}: —"
            m = _option_mid_price(c)
            ms = f"{m:.2f}" if m is not None else "—"
            tag = "filled" if com is not None else "scenario"
            return f"{name}: K {c.strike:.2f}  ·  mid {ms}  ·  {tag}"

        st["toolbar_lbl_call"].configure(text=leg_txt(sc, cc, "Call"))
        st["toolbar_lbl_put"].configure(text=leg_txt(sp, cp, "Put"))

        c_mid = _option_mid_price(cc) if cc is not None else None
        st["btn_close_call"].configure(
            state=tk.NORMAL if (cc is not None and c_mid is not None) else tk.DISABLED,
        )
        p_mid = _option_mid_price(cp) if cp is not None else None
        st["btn_close_put"].configure(
            state=tk.NORMAL if (cp is not None and p_mid is not None) else tk.DISABLED,
        )
        sel_cm = _option_mid_price(sc) if sc is not None else None
        sel_pm = _option_mid_price(sp) if sp is not None else None
        if st.get("options_chain_locked"):
            st["btn_sell_call"].configure(state=tk.DISABLED)
            st["btn_sell_put"].configure(state=tk.DISABLED)
        else:
            st["btn_sell_call"].configure(
                state=tk.NORMAL if (sc is not None and sel_cm is not None) else tk.DISABLED,
            )
            st["btn_sell_put"].configure(
                state=tk.NORMAL if (sp is not None and sel_pm is not None) else tk.DISABLED,
            )

    def _on_option_stream_refresh(self, sym: str, leg: str) -> None:
        self._refresh_leg_display(sym, leg)
        self._refresh_portfolio_row(sym)

    def _refresh_leg_display(self, sym: str, leg: str) -> None:
        st = self._charts.get(sym)
        if not st:
            return
        if leg == "call":
            tree, rows, sel = st["calls_tree"], st["call_rows"], st["selected_call"]
        else:
            tree, rows, sel = st["puts_tree"], st["put_rows"], st["selected_put"]
        if sel is None:
            self._refresh_pl_metrics(sym)
            return
        try:
            idx = rows.index(sel)
        except ValueError:
            self._refresh_pl_metrics(sym)
            return
        iid = str(idx)
        children = tree.get_children()
        if iid not in children:
            self._refresh_pl_metrics(sym)
            return
        tree.item(iid, values=_candidate_tree_values(sel))
        self._refresh_pl_metrics(sym)

    def _cancel_option_streams(self, sym: str) -> None:
        if self._opt_hub is None:
            return
        try:
            self._opt_hub.set_leg(sym, "call", None)
            self._opt_hub.set_leg(sym, "put", None)
        except Exception:
            pass

    def _redraw_saved_strikes(self, st: _ChartState) -> None:
        ax = st["ax"]
        canvas = st["canvas"]
        self._clear_strike_hlines(st)
        cc = st["committed_call"]
        cp = st["committed_put"]
        sc = st["selected_call"]
        sp = st["selected_put"]

        def _hline(y: float, color: str, dashed: bool) -> None:
            st["strike_hlines"].append(
                ax.axhline(
                    y=float(y),
                    color=color,
                    linewidth=2.0,
                    linestyle="--" if dashed else "-",
                    zorder=5,
                    alpha=0.95,
                )
            )

        if cc is not None:
            _hline(float(cc.strike), "#22c55e", False)
        if sc is not None and not _same_option_contract(sc, cc):
            _hline(float(sc.strike), "#22c55e", True)
        if cp is not None:
            _hline(float(cp.strike), "#ef4444", False)
        if sp is not None and not _same_option_contract(sp, cp):
            _hline(float(sp.strike), "#ef4444", True)
        canvas.draw_idle()

    def _on_call_row_selected(self, sym: str) -> None:
        st = self._charts.get(sym)
        if not st:
            return
        sel = st["calls_tree"].selection()
        if not sel:
            st["selected_call"] = None
            try:
                if self._opt_hub is not None:
                    self._opt_hub.set_leg(sym, "call", None)
            except Exception:
                pass
            self._redraw_saved_strikes(st)
            self._refresh_pl_metrics(sym)
            return
        try:
            idx = int(sel[0])
        except ValueError:
            return
        rows = st["call_rows"]
        st["selected_call"] = rows[idx] if 0 <= idx < len(rows) else None
        self._redraw_saved_strikes(st)
        self._refresh_pl_metrics(sym)
        try:
            hub = self._ensure_option_hub()
            hub.set_leg(sym, "call", st["selected_call"])
        except Exception as e:
            messagebox.showerror("Option stream", f"{sym} call leg: {e}")

    def _on_put_row_selected(self, sym: str) -> None:
        st = self._charts.get(sym)
        if not st:
            return
        sel = st["puts_tree"].selection()
        if not sel:
            st["selected_put"] = None
            try:
                if self._opt_hub is not None:
                    self._opt_hub.set_leg(sym, "put", None)
            except Exception:
                pass
            self._redraw_saved_strikes(st)
            self._refresh_pl_metrics(sym)
            return
        try:
            idx = int(sel[0])
        except ValueError:
            return
        rows = st["put_rows"]
        st["selected_put"] = rows[idx] if 0 <= idx < len(rows) else None
        self._redraw_saved_strikes(st)
        self._refresh_pl_metrics(sym)
        try:
            hub = self._ensure_option_hub()
            hub.set_leg(sym, "put", st["selected_put"])
        except Exception as e:
            messagebox.showerror("Option stream", f"{sym} put leg: {e}")

    def _refresh_pl_metrics(self, sym: str) -> None:
        st = self._charts.get(sym)
        if not st:
            return
        pos = st["pos"]
        cost = float(pos["cost_basis"])
        shares = float(pos["shares"])
        spot = _spot_value(self.quotes.get(sym))
        call_contracts = _scenario_call_contracts(st, shares)
        put_contracts = _scenario_put_contracts(st, shares)
        sc = st["selected_call"]
        sp = st["selected_put"]
        mv = st["metrics"]
        vl = st["metrics_value_labels"]

        def _set_value(sid: str, main: str, sub: str, fg: Optional[str] = None) -> None:
            mv[f"{sid}_main"].set(main)
            mv[f"{sid}_sub"].set(sub)
            lab = vl.get(sid)
            if lab is not None:
                lab.configure(fg=fg if fg is not None else _MET_VALUE)

        if spot is not None and _valid_cost_basis(cost):
            pl = (float(spot) - cost) * shares
            sign = "+" if pl > 0 else ""
            pl_fg = _MET_POS if pl > 0 else (_MET_NEG if pl < 0 else _MET_VALUE)
            _set_value(
                "stock",
                f"{sign}{pl:,.2f}",
                f"{_fmt_qty(shares)} sh · {cost:.2f} · {float(spot):.2f}",
                pl_fg,
            )
        elif _valid_cost_basis(cost):
            _set_value(
                "stock",
                "—",
                f"No spot · {_fmt_qty(shares)} sh @ {cost:.2f}",
                _MET_MUTED,
            )
        else:
            _set_value("stock", "—", "No valid average cost from IB.", _MET_MUTED)

        cp = _premium_bid_dollars(sc, call_contracts)
        if sc is None:
            _set_value("call_prem", "—", "Pick call row.", _MET_MUTED)
        elif cp is not None:
            _set_value(
                "call_prem",
                f"{cp:,.2f}",
                f"{call_contracts} lot  ·  bid×100",
                _MET_VALUE,
            )
        else:
            _set_value("call_prem", "—", "No bid — select row.", _MET_MUTED)

        pprem = _premium_bid_dollars(sp, put_contracts)
        if sp is None:
            _set_value("put_prem", "—", "Pick put row.", _MET_MUTED)
        elif pprem is not None:
            _set_value(
                "put_prem",
                f"{pprem:,.2f}",
                f"{put_contracts} lot  ·  bid×100",
                _MET_VALUE,
            )
        else:
            _set_value("put_prem", "—", "No bid — select row.", _MET_MUTED)

        if sc is None:
            _set_value("call_assign", "—", "Pick call for P/L.", _MET_MUTED)
        elif _valid_cost_basis(cost):
            K = float(sc.strike)
            pl_as = (K - cost) * 100.0 * float(call_contracts)
            sign = "+" if pl_as > 0 else ""
            as_fg = _MET_POS if pl_as > 0 else (_MET_NEG if pl_as < 0 else _MET_VALUE)
            _set_value(
                "call_assign",
                f"{sign}{pl_as:,.2f}",
                f"{call_contracts} lot · K {K:.2f} vs {cost:.2f}/sh",
                as_fg,
            )
        else:
            _set_value("call_assign", "—", "Invalid cost basis.", _MET_MUTED)

        if sp is None:
            _set_value("put_basis", "—", "Pick put for average.", _MET_MUTED)
        elif _valid_cost_basis(cost) and shares > 0:
            K = float(sp.strike)
            add_sh = 100.0 * float(put_contracts)
            new_tot = shares + add_sh
            new_basis = (shares * cost + add_sh * K) / new_tot
            _set_value(
                "put_basis",
                f"{new_basis:.2f}",
                f"{_fmt_qty(shares)} sh @ {cost:.2f} + {add_sh:g} sh @ {K:.2f}",
                _MET_VALUE,
            )
        else:
            _set_value("put_basis", "—", "Need long shares and valid cost.", _MET_MUTED)

        if sp is None:
            _set_value("put_cash", "—", "Pick put for cash.", _MET_MUTED)
        else:
            K = float(sp.strike)
            cash = K * 100.0 * float(put_contracts)
            _set_value(
                "put_cash",
                f"{cash:,.2f}",
                f"{put_contracts} lot @ K {K:.2f}",
                _MET_VALUE,
            )

        self._sync_trade_toolbar(sym)

    def _generate_maturities(self, sym: str):
        st = self._charts.get(sym)
        if not st:
            return
        if st.get("options_chain_locked"):
            messagebox.showinfo(
                "Options",
                "Maturity is fixed to your IB open option position for this symbol.",
            )
            return
        host, port, _ = ib_settings()
        cid = strike_resolver_client_id()
        st["btn_maturities"].configure(state=tk.DISABLED)
        self._set_status(f"{sym}: fetching expiries (IB {host}:{port}, client {cid})…")

        def work():
            err: Optional[BaseException] = None
            mats: List[str] = []
            try:
                resolver = StrikeResolver(host, port, cid)
                resolver.start()
                try:
                    mats = resolver.fetch_maturities(sym, count=10)
                finally:
                    resolver.stop()
            except BaseException as e:
                err = e

            def finish():
                st["btn_maturities"].configure(state=tk.NORMAL)
                if err is not None:
                    messagebox.showerror("Maturities", f"{sym}: {err}")
                    self._set_status(f"{sym}: maturity fetch failed")
                    return
                combo = st["maturity_combo"]
                combo.configure(values=mats)
                if mats:
                    combo.set(mats[0])
                else:
                    combo.set("")
                self._set_status(f"{sym}: loaded {len(mats)} maturities")

            self.root.after(0, finish)

        threading.Thread(target=work, daemon=True).start()

    def _resolve_contracts(self, sym: str):
        st = self._charts.get(sym)
        if not st:
            return
        if st.get("options_chain_locked"):
            messagebox.showinfo(
                "Options",
                "Strike grid is disabled while maturity is locked to your IB open option position.",
            )
            return
        maturity = st["maturity_combo"].get().strip()
        if not maturity:
            messagebox.showwarning("Resolve", "Generate or choose a maturity first.")
            return

        host, port, _ = ib_settings()
        cid = strike_resolver_client_id()
        st["btn_resolve"].configure(state=tk.DISABLED)
        st["btn_maturities"].configure(state=tk.DISABLED)
        self._set_status(f"{sym}: loading 5/2.5 OTM grid (10/side) for {maturity}…")

        def work():
            err: Optional[BaseException] = None
            spot: Optional[float] = None
            call_top: List[OptionCandidate] = []
            put_top: List[OptionCandidate] = []
            try:
                resolver = StrikeResolver(host, port, cid)
                resolver.start()
                try:
                    spot, call_top, put_top = resolver.fetch_closest_otm(
                        symbol=sym,
                        expiration=maturity,
                        count=10,
                        snapshot_wait_sec=4.0,
                        verbose=False,
                    )
                finally:
                    resolver.stop()
            except BaseException as e:
                err = e

            def finish():
                st["btn_resolve"].configure(state=tk.NORMAL)
                st["btn_maturities"].configure(state=tk.NORMAL)
                ct = st["calls_tree"]
                pt = st["puts_tree"]
                if err is not None:
                    messagebox.showerror("Resolve", f"{sym}: {err}")
                    self._set_status(f"{sym}: resolve failed")
                    return
                self._cancel_option_streams(sym)
                st["selected_call"] = None
                st["selected_put"] = None
                self._clear_strike_hlines(st)
                for iid in ct.get_children():
                    ct.delete(iid)
                for iid in pt.get_children():
                    pt.delete(iid)
                st["call_rows"] = call_top
                st["put_rows"] = put_top
                for i, c in enumerate(call_top):
                    ct.insert("", tk.END, iid=str(i), values=_candidate_tree_values(c))
                for i, c in enumerate(put_top):
                    pt.insert("", tk.END, iid=str(i), values=_candidate_tree_values(c))
                self._redraw_saved_strikes(st)
                self._refresh_pl_metrics(sym)
                try:
                    md = self._ensure_option_hub()
                    if st["committed_call"] is not None:
                        md.set_leg(sym, "call", st["committed_call"])
                    if st["committed_put"] is not None:
                        md.set_leg(sym, "put", st["committed_put"])
                except Exception:
                    pass
                ntot = len(call_top) + len(put_top)
                self._set_status(
                    f"{sym}: spot {spot:.2f} — {ntot} OTM row(s) · select row for live stream (client {option_stream_client_id()})"
                    if spot is not None and spot == spot
                    else f"{sym}: {ntot} OTM row(s)"
                )

            self.root.after(0, finish)

        threading.Thread(target=work, daemon=True).start()

    def _on_tab_changed(self, _event=None):
        if not self.notebook or not self.positions:
            return
        try:
            idx = self.notebook.index(self.notebook.select())
        except tk.TclError:
            return
        if idx == _PORTFOLIO_TAB_INDEX:
            self._refresh_portfolio_table()
            return
        pos_idx = idx - 1
        if 0 <= pos_idx < len(self.positions):
            self._update_chart(str(self.positions[pos_idx]["symbol"]))

    def _enqueue_quote(self, sym: str, payload: dict):
        self._quote_queue.put((sym, payload))

    def _poll_quote_queue(self):
        if not self._running:
            return
        try:
            while True:
                sym, payload = self._quote_queue.get_nowait()
                self.quotes[sym] = payload
                self._dirty.add(sym)
                self._schedule_redraw()
        except queue.Empty:
            pass
        self.root.after(40, self._poll_quote_queue)

    def _schedule_redraw(self):
        if self._redraw_scheduled:
            return
        self._redraw_scheduled = True
        self.root.after(120, self._flush_redraw)

    def _flush_redraw(self):
        self._redraw_scheduled = False
        for sym in list(self._dirty):
            self._update_chart(sym)
        self._dirty.clear()
        self._refresh_portfolio_table()
        if self.ib:
            self._set_status("Connected · streaming quotes")

    def _update_chart(self, symbol: str):
        if symbol not in self._charts:
            return
        st = self._charts[symbol]
        ax = st["ax"]
        canvas = st["canvas"]
        bar = st["bar"]
        cost_line = st["cost_line"]
        wait_text = st["wait_text"]
        spot_label = st["spot_label"]
        pos = st["pos"]

        cost = float(pos["cost_basis"])
        spot = _spot_value(self.quotes.get(symbol))
        has_spot = spot is not None

        wait_text.set_visible(not has_spot)

        y0, y1 = _chart_ylim(cost, spot if has_spot else None)

        if has_spot:
            s = float(spot)
            face, edge, lbl = _bar_colors(cost, s)
            bar.set_facecolor(face)
            bar.set_edgecolor(edge)
            bar.set_visible(True)
            bar.set_height(s)
            bar.set_y(0)
            spot_label.set_visible(True)
            spot_label.set_color(lbl)
            spot_label.set_text(f"{s:.2f}")
            spot_label.set_position((0, s + 0.02 * (y1 - y0)))
        else:
            bar.set_visible(False)
            bar.set_height(1e-6)
            spot_label.set_visible(False)
            spot_label.set_text("")

        ax.set_ylim(y0, y1)
        cost_line.set_ydata((cost, cost))

        canvas.draw_idle()
        self._refresh_pl_metrics(symbol)

    def _on_close(self):
        self._running = False
        if self._order_hub is not None:
            try:
                self._order_hub.stop()
            except Exception:
                pass
            self._order_hub = None
        if self._opt_hub is not None:
            try:
                self._opt_hub.stop()
            except Exception:
                pass
            self._opt_hub = None
        if self.ib:
            try:
                self.ib.stop()
            except Exception:
                pass
        self.root.destroy()

    def run(self):
        self._running = True
        self.root.after(40, self._poll_quote_queue)
        self.root.mainloop()


def main():
    HoldingsDashboardApp().run()


if __name__ == "__main__":
    main()
