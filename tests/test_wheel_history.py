from wheel_service import WheelService


def empty_account():
    return {"cash": "1000", "equity": "1000", "last_equity": "1000"}


def empty_history():
    return {"timestamp": [], "equity": []}


def test_assigned_away_symbol_remains_in_wheel_from_trade_history():
    service = WheelService()
    orders = [
        {
            "symbol": "NVDA",
            "side": "buy",
            "qty": "100",
            "filled_qty": "100",
            "filled_avg_price": "212.40",
            "status": "filled",
            "filled_at": "2026-08-01T14:00:00Z",
        },
        {
            "symbol": "NVDA260828C00215000",
            "side": "sell",
            "qty": "1",
            "filled_qty": "1",
            "filled_avg_price": "1.50",
            "status": "filled",
            "position_intent": "sell_to_open",
            "filled_at": "2026-08-20T14:00:00Z",
        },
    ]

    payload = service._portfolio_payload(
        empty_account(), [], orders, empty_history(), {"is_open": False}, "live"
    )

    assert payload["positions"] == []
    assert payload["wheel_symbols"] == ["NVDA"]
    profile = payload["wheel_profiles"][0]
    assert profile["held"] is False
    assert profile["shares"] == 0
    assert profile["average_cost"] == 212.40


def test_empty_portfolio_and_empty_history_are_safe():
    service = WheelService()

    payload = service._portfolio_payload(
        empty_account(), [], [], empty_history(), {"is_open": False}, "live"
    )

    assert payload["positions"] == []
    assert payload["wheel_symbols"] == []
    assert payload["wheel_profiles"] == []


def test_short_put_premium_counts_without_an_underlying_stock_position():
    service = WheelService()
    option_positions = [
        {
            "symbol": "NVDA260904P00215000",
            "qty": "-1",
            "cost_basis": "-146",
        },
        {
            "symbol": "AAPL260904P00310000",
            "qty": "-1",
            "cost_basis": "-135",
        },
    ]

    payload = service._portfolio_payload(
        empty_account(),
        [],
        [],
        empty_history(),
        {"is_open": True},
        "live",
        option_positions=option_positions,
    )

    assert payload["positions"] == []
    assert payload["account"]["premium_collected"] == 281


def test_inactive_wheel_uses_live_quote_and_still_builds_put_chain(monkeypatch):
    service = WheelService()
    inactive = {
        "symbol": "NVDA",
        "shares": 0,
        "average_cost": 212.40,
        "spot": 0,
        "market_value": 0,
        "cost_basis": 0,
        "unrealized_pl": 0,
        "unrealized_plpc": 0,
        "short_legs": [],
        "held": False,
    }
    monkeypatch.setattr(
        service,
        "_portfolio_live",
        lambda: {"wheel_profiles": [inactive], "pending_orders": []},
    )
    monkeypatch.setattr(
        service,
        "_quote_live",
        lambda symbol: {"symbol": symbol, "price": 210.25},
    )

    class ChainClient:
        def data(self, path, params):
            return {
                "snapshots": {
                    "NVDA260918C00215000": {
                        "latestQuote": {"bp": 1.0, "ap": 1.2},
                        "greeks": {"delta": 0.3},
                        "impliedVolatility": 0.35,
                    },
                    "NVDA260918P00205000": {
                        "latestQuote": {"bp": 1.1, "ap": 1.3},
                        "greeks": {"delta": -0.3},
                        "impliedVolatility": 0.36,
                    },
                }
            }

    service.client = ChainClient()
    result = service._wheel_live("NVDA", "2026-09-18")

    assert result["position"]["held"] is False
    assert result["position"]["spot"] == 210.25
    assert result["position"]["average_cost"] == 212.40
    assert result["puts"][0]["symbol"] == "NVDA260918P00205000"
