from __future__ import annotations

import os
import random
import re
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import requests

from wheel_core import WheelScenario, number, option_mid, parse_option_symbol, snap_otm_grid


class AlpacaError(RuntimeError):
    pass


OPEN_ORDER_STATUSES = {"new", "accepted", "pending_new", "partially_filled", "held", "accepted_for_bidding", "pending_replace", "pending_cancel"}
STOCK_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


class AlpacaClient:
    def __init__(self) -> None:
        self.key = os.getenv("ALPACA_API_KEY", "")
        self.secret = os.getenv("ALPACA_SECRET_KEY", "")
        self.paper = os.getenv("ALPACA_PAPER", "true").lower() not in {"0", "false", "no"}
        self.trading_base = "https://paper-api.alpaca.markets" if self.paper else "https://api.alpaca.markets"
        self.data_base = "https://data.alpaca.markets"
        self.session = requests.Session()
        self.session.headers.update({"APCA-API-KEY-ID": self.key, "APCA-API-SECRET-KEY": self.secret})

    @property
    def configured(self) -> bool:
        return bool(self.key and self.secret)

    def request(self, method: str, base: str, path: str, **kwargs: Any) -> dict[str, Any] | list[Any]:
        if not self.configured:
            raise AlpacaError("Alpaca credentials are not configured in .env.")
        try:
            response = self.session.request(method, base + path, timeout=18, **kwargs)
        except requests.RequestException as exc:
            raise AlpacaError(f"Could not reach Alpaca: {exc}") from exc
        if not response.ok:
            try:
                detail = response.json().get("message", response.text)
            except ValueError:
                detail = response.text
            raise AlpacaError(f"Alpaca returned {response.status_code}: {detail}")
        return response.json() if response.content else {}

    def trading(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self.request("GET", self.trading_base, path, params=params)

    def data(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self.request("GET", self.data_base, path, params=params)

    def post(self, path: str, payload: dict[str, Any]) -> Any:
        return self.request("POST", self.trading_base, path, json=payload)

    def delete(self, path: str) -> Any:
        return self.request("DELETE", self.trading_base, path)


class WheelService:
    def __init__(self) -> None:
        self.client = AlpacaClient()
        self.paper = self.client.paper
        self.configured = self.client.configured
        self.allow_demo = os.getenv("ALLOW_DEMO_FALLBACK", "true").lower() not in {"0", "false", "no"}
        self._last_cost_basis: dict[str, float] = {}

    def _fallback(self, fn, *args):
        try:
            return fn(*args)
        except AlpacaError as exc:
            if not self.allow_demo:
                raise
            result = getattr(self, f"_demo_{fn.__name__}")(*args)
            result["mode"] = "demo"
            result["warning"] = str(exc)
            return result

    def portfolio(self) -> dict[str, Any]:
        return self._fallback(self._portfolio_live)

    def _portfolio_live(self) -> dict[str, Any]:
        account = self.client.trading("/v2/account")
        clock = self.client.trading("/v2/clock")
        raw_positions = self.client.trading("/v2/positions")
        orders = self.client.trading("/v2/orders", {"status": "all", "limit": 500, "direction": "desc", "nested": "true"})
        history = self.client.trading("/v2/account/portfolio/history", {"period": "1M", "timeframe": "1D"})
        stocks = [position for position in raw_positions if position.get("asset_class") == "us_equity"]
        option_positions = [position for position in raw_positions if position.get("asset_class") == "us_option"]
        symbols = [position["symbol"] for position in stocks]
        snapshots: dict[str, Any] = {}
        if symbols:
            data = self.client.data("/v2/stocks/snapshots", {"symbols": ",".join(symbols), "feed": "iex"})
            snapshots = data.get("snapshots", data)
        positions = []
        for position in stocks:
            symbol = position["symbol"]
            snap = snapshots.get(symbol, {})
            spot = number((snap.get("latestTrade") or {}).get("p"), number(position.get("current_price")))
            positions.append(self._stock_row(position, spot, option_positions))
        return self._portfolio_payload(account, positions, orders, history, clock, mode="live", option_positions=option_positions)

    @staticmethod
    def _short_legs(symbol: str, options: list[dict[str, Any]]) -> list[dict[str, Any]]:
        short_legs = []
        for option in options:
            parsed = parse_option_symbol(option.get("symbol", ""))
            if parsed and parsed["underlying"] == symbol and number(option.get("qty")) < 0:
                short_legs.append({**parsed, **option})
        return short_legs

    def _stock_row(self, position: dict[str, Any], spot: float, options: list[dict[str, Any]]) -> dict[str, Any]:
        symbol = position["symbol"]
        shares = number(position.get("qty"))
        avg = number(position.get("avg_entry_price"))
        return {
            "symbol": symbol,
            "shares": shares,
            "average_cost": avg,
            "spot": spot,
            "market_value": number(position.get("market_value"), shares * spot),
            "cost_basis": number(position.get("cost_basis"), shares * avg),
            "unrealized_pl": number(position.get("unrealized_pl"), (spot - avg) * shares),
            "unrealized_plpc": number(position.get("unrealized_plpc")),
            "short_legs": self._short_legs(symbol, options),
            "held": True,
        }

    @staticmethod
    def _historical_stock_basis(orders: list[dict[str, Any]]) -> dict[str, float]:
        ledgers: dict[str, dict[str, float]] = {}
        dated_orders = sorted(
            orders,
            key=lambda order: order.get("filled_at") or order.get("submitted_at") or "",
        )
        for order in dated_orders:
            symbol = str(order.get("symbol", "")).upper()
            if parse_option_symbol(symbol) or not STOCK_SYMBOL_RE.fullmatch(symbol):
                continue
            qty = number(order.get("filled_qty"))
            price = number(order.get("filled_avg_price"))
            if qty <= 0 or price <= 0:
                continue
            ledger = ledgers.setdefault(symbol, {"qty": 0.0, "basis": 0.0, "last_basis": 0.0})
            if order.get("side") == "buy":
                new_qty = ledger["qty"] + qty
                ledger["basis"] = ((ledger["qty"] * ledger["basis"]) + (qty * price)) / new_qty
                ledger["qty"] = new_qty
                ledger["last_basis"] = ledger["basis"]
            elif order.get("side") == "sell" and ledger["qty"] > 0:
                ledger["last_basis"] = ledger["basis"]
                ledger["qty"] = max(0.0, ledger["qty"] - qty)
        return {symbol: values["last_basis"] for symbol, values in ledgers.items() if values["last_basis"] > 0}

    def _portfolio_payload(self, account, positions, orders, history, clock, mode: str, option_positions=None) -> dict[str, Any]:
        cash = number(account.get("cash"))
        equity = number(account.get("equity"))
        last_equity = number(account.get("last_equity"), equity)
        total_pl = sum(p["unrealized_pl"] for p in positions)
        option_premium = sum(
            abs(number(leg.get("cost_basis"))) for position in positions for leg in position.get("short_legs", [])
        )
        trade_orders = [order for order in orders if order.get("symbol")]
        order_rows = [self._trade_row(order) for order in trade_orders]
        trades = order_rows[:20]
        pending_orders = [row for row in order_rows if row["status"] in OPEN_ORDER_STATUSES and row["option_type"]]
        historical_basis = self._historical_stock_basis(orders)
        for position in positions:
            position["held"] = True
            if position["average_cost"] > 0:
                self._last_cost_basis[position["symbol"]] = position["average_cost"]
        tracked_symbols = [position["symbol"] for position in positions]
        for order, row in zip(trade_orders, order_rows):
            meaningful = number(order.get("filled_qty")) > 0 or row["status"] in OPEN_ORDER_STATUSES
            symbol = row["underlying"]
            if meaningful and STOCK_SYMBOL_RE.fullmatch(symbol) and symbol not in tracked_symbols:
                tracked_symbols.append(symbol)
        positions_by_symbol = {position["symbol"]: position for position in positions}
        wheel_profiles = []
        for symbol in tracked_symbols:
            if symbol in positions_by_symbol:
                wheel_profiles.append(positions_by_symbol[symbol])
                continue
            last_basis = self._last_cost_basis.get(symbol) or historical_basis.get(symbol, 0.0)
            if last_basis > 0:
                self._last_cost_basis[symbol] = last_basis
            wheel_profiles.append({
                "symbol": symbol,
                "shares": 0,
                "average_cost": last_basis,
                "spot": 0,
                "market_value": 0,
                "cost_basis": 0,
                "unrealized_pl": 0,
                "unrealized_plpc": 0,
                "short_legs": self._short_legs(symbol, option_positions or []),
                "held": False,
            })
        return {
            "mode": mode,
            "paper": self.paper,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "market": {
                "is_open": bool(clock.get("is_open")),
                "timestamp": clock.get("timestamp"),
                "next_open": clock.get("next_open"),
                "next_close": clock.get("next_close"),
            },
            "account": {
                "equity": equity,
                "cash": cash,
                "buying_power": number(account.get("buying_power")),
                "options_buying_power": number(account.get("options_buying_power")),
                "day_pl": equity - last_equity,
                "unrealized_pl": total_pl,
                "premium_collected": option_premium,
                "options_level": account.get("options_trading_level", 0),
            },
            "positions": positions,
            "wheel_symbols": tracked_symbols,
            "wheel_profiles": wheel_profiles,
            "trades": trades,
            "pending_orders": pending_orders,
            "equity_curve": {
                "labels": [datetime.fromtimestamp(ts, timezone.utc).strftime("%b %d") for ts in history.get("timestamp", [])],
                "values": history.get("equity", []),
            },
        }

    @staticmethod
    def _trade_row(order: dict[str, Any]) -> dict[str, Any]:
        parsed = parse_option_symbol(order.get("symbol", ""))
        filled_qty = number(order.get("filled_qty"))
        return {
            "time": order.get("filled_at") or order.get("submitted_at") or "",
            "symbol": order.get("symbol", ""),
            "underlying": parsed["underlying"] if parsed else order.get("symbol", ""),
            "strategy": (parsed["type"].title() if parsed else "Stock") + (" · close" if order.get("position_intent") == "buy_to_close" else ""),
            "side": order.get("side", ""),
            "qty": filled_qty if filled_qty > 0 else number(order.get("qty")),
            "price": number(order.get("filled_avg_price"), number(order.get("limit_price"))),
            "status": order.get("status", ""),
            "option_type": parsed["type"] if parsed else None,
            "strike": parsed["strike"] if parsed else None,
            "expiration": parsed["expiration"] if parsed else None,
            "order_id": order.get("id"),
            "cancelable": order.get("status", "") in OPEN_ORDER_STATUSES,
        }

    def maturities(self, symbol: str) -> dict[str, Any]:
        return self._fallback(self._maturities_live, symbol)

    def _maturities_live(self, symbol: str) -> dict[str, Any]:
        end = date.today() + timedelta(days=180)
        data = self.client.trading("/v2/options/contracts", {
            "underlying_symbols": symbol,
            "expiration_date_gte": date.today().isoformat(),
            "expiration_date_lte": end.isoformat(),
            "status": "active",
            "limit": 10000,
        })
        contracts = data.get("option_contracts", [])
        values = sorted({contract["expiration_date"] for contract in contracts})
        return {"mode": "live", "symbol": symbol, "maturities": values}

    def wheel(self, symbol: str, expiration: str) -> dict[str, Any]:
        return self._fallback(self._wheel_live, symbol, expiration)

    def quote(self, symbol: str) -> dict[str, Any]:
        return self._fallback(self._quote_live, symbol)

    def _quote_live(self, symbol: str) -> dict[str, Any]:
        snapshot = self.client.data(f"/v2/stocks/{symbol}/snapshot", {"feed": "iex"})
        latest_trade = snapshot.get("latestTrade") or {}
        minute_bar = snapshot.get("minuteBar") or {}
        daily_bar = snapshot.get("dailyBar") or {}
        price = number(latest_trade.get("p"), number(minute_bar.get("c"), number(daily_bar.get("c"))))
        if price <= 0:
            raise AlpacaError(f"No current price is available for {symbol}.")
        timestamp = latest_trade.get("t") or minute_bar.get("t") or datetime.now(timezone.utc).isoformat()
        return {"mode": "live", "symbol": symbol, "price": price, "timestamp": timestamp}

    def _wheel_live(self, symbol: str, expiration: str) -> dict[str, Any]:
        portfolio = self._portfolio_live()
        position = next((p for p in portfolio["wheel_profiles"] if p["symbol"] == symbol), None)
        if not position:
            raise AlpacaError(f"No portfolio or wheel history found for {symbol}.")
        if not position.get("held"):
            position = {**position, "spot": self._quote_live(symbol)["price"]}
        chain_raw = self.client.data(f"/v1beta1/options/snapshots/{symbol}", {
            "feed": "indicative",
            "expiration_date": expiration,
            "limit": 1000,
        })
        snapshots = chain_raw.get("snapshots", {})
        result = self._build_wheel_payload(position, expiration, snapshots, mode="live")
        result["pending_orders"] = [order for order in portfolio.get("pending_orders", []) if order["underlying"] == symbol]
        return result

    def _build_wheel_payload(self, position, expiration, snapshots, mode):
        spot = position["spot"]
        rows = []
        for contract_symbol, snapshot in snapshots.items():
            parsed = parse_option_symbol(contract_symbol)
            if not parsed or parsed["expiration"] != expiration:
                continue
            quote = snapshot.get("latestQuote") or {}
            greeks = snapshot.get("greeks") or {}
            rows.append({
                "symbol": contract_symbol,
                "type": parsed["type"],
                "strike": parsed["strike"],
                "bid": number(quote.get("bp")),
                "ask": number(quote.get("ap")),
                "mid": option_mid(snapshot),
                "delta": number(greeks.get("delta")),
                "iv": number(snapshot.get("impliedVolatility")),
            })
        calls = self._select_chain(rows, spot, "call")
        puts = self._select_chain(rows, spot, "put")
        selected_call = calls[0] if calls else None
        selected_put = puts[0] if puts else None
        scenario = WheelScenario(
            position["shares"], position["average_cost"], spot,
            selected_call["strike"] if selected_call else None,
            selected_call["bid"] if selected_call else None,
            selected_put["strike"] if selected_put else None,
            selected_put["bid"] if selected_put else None,
        ).as_dict()
        return {"mode": mode, "position": position, "expiration": expiration, "calls": calls, "puts": puts, "scenario": scenario}

    @staticmethod
    def _select_chain(rows, spot, right):
        side = [row for row in rows if row["type"] == right and ((row["strike"] > spot) if right == "call" else (row["strike"] < spot))]
        snapped = snap_otm_grid((row["strike"] for row in side), spot, right)
        by_strike = {row["strike"]: row for row in side}
        return [by_strike[strike] for strike in snapped if strike in by_strike]

    def place_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("confirm") is not True:
            raise ValueError("Order confirmation is required.")
        if not self.paper and os.getenv("ALLOW_LIVE_ORDERS", "false").lower() not in {"1", "true", "yes"}:
            raise ValueError("Live orders are disabled. Set ALLOW_LIVE_ORDERS=true to enable them.")
        symbol = str(payload.get("symbol", "")).upper()
        intent = str(payload.get("position_intent", ""))
        qty = int(payload.get("qty", 0))
        limit_price = round(number(payload.get("limit_price")), 2)
        if not parse_option_symbol(symbol) or intent not in {"sell_to_open", "buy_to_close"}:
            raise ValueError("A valid option contract and position intent are required.")
        if qty < 1 or limit_price <= 0:
            raise ValueError("Quantity and limit price must be positive.")
        order = self.client.post("/v2/orders", {
            "symbol": symbol,
            "qty": str(qty),
            "side": "sell" if intent == "sell_to_open" else "buy",
            "type": "limit",
            "time_in_force": "day",
            "limit_price": f"{limit_price:.2f}",
            "position_intent": intent,
        })
        return {"id": order.get("id"), "status": order.get("status"), "symbol": symbol, "paper": self.paper}

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        try:
            UUID(order_id)
        except (ValueError, TypeError) as exc:
            raise ValueError("A valid Alpaca order ID is required.") from exc
        order = self.client.trading(f"/v2/orders/{order_id}")
        if order.get("status") not in OPEN_ORDER_STATUSES:
            raise ValueError(f"This order can no longer be canceled because its status is {order.get('status', 'unknown')}.")
        self.client.delete(f"/v2/orders/{order_id}")
        return {"id": order_id, "symbol": order.get("symbol"), "status": "cancel_requested", "paper": self.paper}

    def _demo__portfolio_live(self) -> dict[str, Any]:
        positions = [
            {"symbol": "AAPL", "shares": 200, "average_cost": 218.40, "spot": 224.76, "market_value": 44952, "cost_basis": 43680, "unrealized_pl": 1272, "unrealized_plpc": .0291, "short_legs": []},
            {"symbol": "NVDA", "shares": 100, "average_cost": 171.35, "spot": 176.92, "market_value": 17692, "cost_basis": 17135, "unrealized_pl": 557, "unrealized_plpc": .0325, "short_legs": []},
            {"symbol": "AMD", "shares": 300, "average_cost": 154.12, "spot": 151.84, "market_value": 45552, "cost_basis": 46236, "unrealized_pl": -684, "unrealized_plpc": -.0148, "short_legs": []},
        ]
        today = datetime.now(timezone.utc)
        history = {"timestamp": [], "equity": []}
        for day in range(30, -1, -1):
            history["timestamp"].append(int((today - timedelta(days=day)).timestamp()))
            history["equity"].append(round(123800 + (30 - day) * 142 + 900 * __import__('math').sin(day / 4), 2))
        orders = [
            {"symbol": "AAPL260918C00230000", "side": "sell", "qty": "2", "filled_qty": "2", "filled_avg_price": "3.42", "status": "filled", "position_intent": "sell_to_open", "filled_at": (today - timedelta(days=2)).isoformat()},
            {"symbol": "AMD260918P00145000", "side": "sell", "qty": "3", "filled_qty": "3", "filled_avg_price": "2.18", "status": "filled", "position_intent": "sell_to_open", "filled_at": (today - timedelta(days=5)).isoformat()},
            {"symbol": "NVDA260821C00185000", "side": "buy", "qty": "1", "filled_qty": "1", "filled_avg_price": "1.12", "status": "filled", "position_intent": "buy_to_close", "filled_at": (today - timedelta(days=8)).isoformat()},
        ]
        account = {"equity": "126420", "cash": "16240", "buying_power": "32480", "options_buying_power": "16240", "last_equity": "125918", "options_trading_level": 2}
        now_et = datetime.now(ZoneInfo("America/New_York"))
        clock = {
            "is_open": now_et.weekday() < 5 and dt_time(9, 30) <= now_et.time() < dt_time(16, 0),
            "timestamp": now_et.isoformat(),
            "next_open": None,
            "next_close": None,
        }
        return self._portfolio_payload(account, positions, orders, history, clock, "demo")

    def _demo__maturities_live(self, symbol: str) -> dict[str, Any]:
        friday = date.today() + timedelta(days=(4 - date.today().weekday()) % 7 or 7)
        values = [(friday + timedelta(days=7 * n)).isoformat() for n in (2, 4, 8, 12, 17)]
        return {"mode": "demo", "symbol": symbol, "maturities": values}

    def _demo__wheel_live(self, symbol: str, expiration: str) -> dict[str, Any]:
        portfolio = self._demo__portfolio_live()
        position = next((p for p in portfolio["wheel_profiles"] if p["symbol"] == symbol), None)
        if not position:
            raise AlpacaError(f"No portfolio or wheel history found for {symbol}.")
        spot = position["spot"]
        rng = random.Random(f"{symbol}-{expiration}")
        snapshots = {}
        root = symbol[:6]
        yymmdd = expiration[2:].replace("-", "")
        low = int((spot * .72) // 2.5) * 2.5
        high = int((spot * 1.28) // 2.5) * 2.5
        strike = low
        while strike <= high:
            for right in ("C", "P"):
                intrinsic = max(0, spot - strike) if right == "C" else max(0, strike - spot)
                time_value = max(.12, 7.5 * __import__('math').exp(-abs(strike - spot) / 18))
                mid = intrinsic + time_value
                bid = max(.01, mid - .04 - rng.random() * .05)
                ask = mid + .04 + rng.random() * .05
                delta_mag = max(.04, min(.96, .5 * __import__('math').exp(-(strike - spot) / 24))) if right == "C" else max(.04, min(.96, .5 * __import__('math').exp((strike - spot) / 24)))
                occ = f"{root}{yymmdd}{right}{int(strike * 1000):08d}"
                snapshots[occ] = {"latestQuote": {"bp": round(bid, 2), "ap": round(ask, 2)}, "greeks": {"delta": round(delta_mag if right == 'C' else -delta_mag, 4)}, "impliedVolatility": round(.28 + rng.random() * .14, 4)}
            strike += 2.5
        return self._build_wheel_payload(position, expiration, snapshots, "demo")

    def _demo__quote_live(self, symbol: str) -> dict[str, Any]:
        portfolio = self._demo__portfolio_live()
        position = next((p for p in portfolio["positions"] if p["symbol"] == symbol), portfolio["positions"][0])
        return {"mode": "demo", "symbol": symbol, "price": position["spot"], "timestamp": datetime.now(timezone.utc).isoformat()}
