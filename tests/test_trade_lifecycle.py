import pytest

from wheel_service import WheelService


def account():
    return {"cash": "1000", "equity": "1000", "last_equity": "1000"}


def empty_history():
    return {"timestamp": [], "equity": []}


def trade(payload, symbol, side="sell"):
    return next(row for row in payload["trades"] if row["symbol"] == symbol and row["side"] == side)


def test_assignment_and_expiry_activities_complete_option_trades():
    service = WheelService()
    orders = [
        {
            "symbol": "NVDA260828C00217500", "side": "sell", "qty": "1", "filled_qty": "1",
            "filled_avg_price": "4.50", "status": "filled", "position_intent": "sell_to_open",
            "filled_at": "2026-08-20T14:00:00Z",
        },
        {
            "symbol": "NVDA260904P00215000", "side": "sell", "qty": "1", "filled_qty": "1",
            "filled_avg_price": "1.46", "status": "filled", "position_intent": "sell_to_open",
            "filled_at": "2026-08-31T14:00:00Z",
        },
    ]
    activities = [
        {"activity_type": "OPASN", "symbol": "NVDA260828C00217500", "date": "2026-08-28"},
        {"activity_type": "OPEXP", "symbol": "NVDA260904P00215000", "date": "2026-09-04"},
    ]

    payload = service._portfolio_payload(
        account(), [], orders, empty_history(), {"is_open": False}, "live", activities=activities
    )

    assigned = trade(payload, "NVDA260828C00217500")
    assert (assigned["event"], assigned["event_date"], assigned["pnl"], assigned["pnl_pct"]) == (
        "Yes", "2026-08-28", 450.0, 1.0
    )
    expired = trade(payload, "NVDA260904P00215000")
    assert (expired["event"], expired["event_date"], expired["pnl"], expired["pnl_pct"]) == (
        "Expired", "2026-09-04", 146.0, 1.0
    )


def test_assignment_stock_activity_closes_stock_trade_and_calculates_pnl():
    service = WheelService()
    orders = [{
        "symbol": "NVDA", "side": "buy", "qty": "100", "filled_qty": "100",
        "filled_avg_price": "212.4002", "status": "filled", "filled_at": "2026-08-01T14:00:00Z",
    }]
    activities = [{
        "activity_type": "OPTRD", "symbol": "NVDA", "qty": "-100", "price": "217.50",
        "date": "2026-08-28",
    }]

    payload = service._portfolio_payload(
        account(), [], orders, empty_history(), {"is_open": False}, "live", activities=activities
    )

    stock = trade(payload, "NVDA", side="buy")
    assert stock["event"] == "Closed"
    assert stock["event_date"] == "2026-08-28"
    assert stock["pnl"] == pytest.approx(509.98)
    assert stock["pnl_pct"] == pytest.approx(0.02401)


def test_open_positions_show_live_state_and_unrealized_pnl():
    service = WheelService()
    stock_positions = [{
        "symbol": "AAPL", "shares": 100, "average_cost": 200, "spot": 205,
        "market_value": 20500, "cost_basis": 20000, "unrealized_pl": 500,
        "unrealized_plpc": 0.025, "short_legs": [], "held": True,
    }]
    option_positions = [{
        "symbol": "AAPL260918P00195000", "qty": "-1", "cost_basis": "-200",
        "unrealized_pl": "75", "unrealized_plpc": "0.375",
    }]
    orders = [
        {
            "symbol": "AAPL", "side": "buy", "qty": "100", "filled_qty": "100",
            "filled_avg_price": "200", "status": "filled", "filled_at": "2026-09-01T14:00:00Z",
        },
        {
            "symbol": "AAPL260918P00195000", "side": "sell", "qty": "1", "filled_qty": "1",
            "filled_avg_price": "2", "status": "filled", "position_intent": "sell_to_open",
            "filled_at": "2026-09-08T14:00:00Z",
        },
    ]

    payload = service._portfolio_payload(
        account(), stock_positions, orders, empty_history(), {"is_open": True}, "live",
        option_positions=option_positions,
    )

    stock = trade(payload, "AAPL", side="buy")
    assert (stock["event"], stock["event_date"], stock["pnl"], stock["pnl_pct"]) == (
        "Open", None, 500.0, 0.025
    )
    option = trade(payload, "AAPL260918P00195000")
    assert (option["event"], option["event_date"], option["pnl"], option["pnl_pct"]) == (
        "Live", None, 75.0, 0.375
    )
