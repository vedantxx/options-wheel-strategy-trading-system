import pytest

import app as app_module
from wheel_service import WheelService


def test_pending_option_order_keeps_submitted_quantity_and_contract_details():
    row = WheelService._trade_row({
        "symbol": "AAPL260828C00315000",
        "side": "sell",
        "qty": "1",
        "filled_qty": "0",
        "limit_price": "1.63",
        "status": "new",
        "position_intent": "sell_to_open",
    })

    assert row["qty"] == 1
    assert row["price"] == 1.63
    assert row["option_type"] == "call"
    assert row["strike"] == 315
    assert row["expiration"] == "2026-08-28"


def test_portfolio_exposes_only_open_option_orders_as_pending():
    service = WheelService()
    orders = [
        {"symbol": "AAPL260828C00315000", "side": "sell", "qty": "1", "filled_qty": "0", "limit_price": "1.63", "status": "new"},
        {"symbol": "NVDA260828C00217500", "side": "sell", "qty": "1", "filled_qty": "1", "filled_avg_price": "4.50", "status": "filled"},
    ]
    payload = service._portfolio_payload(
        {"cash": "1000", "equity": "1000", "last_equity": "1000"},
        [],
        orders,
        {"timestamp": [], "equity": []},
        {"is_open": True},
        "live",
    )

    assert [order["symbol"] for order in payload["pending_orders"]] == ["AAPL260828C00315000"]


def test_cancel_order_only_accepts_open_alpaca_order():
    service = WheelService()

    class FakeClient:
        deleted = None

        def trading(self, path):
            return {"id": "5f5dbf40-14ca-4f82-8948-1b5baa7289ac", "symbol": "AAPL260828C00315000", "status": "new"}

        def delete(self, path):
            self.deleted = path

    service.client = FakeClient()
    result = service.cancel_order("5f5dbf40-14ca-4f82-8948-1b5baa7289ac")

    assert result["status"] == "cancel_requested"
    assert service.client.deleted == "/v2/orders/5f5dbf40-14ca-4f82-8948-1b5baa7289ac"


def test_cancel_endpoint_delegates_to_service(monkeypatch):
    order_id = "5f5dbf40-14ca-4f82-8948-1b5baa7289ac"
    monkeypatch.setattr(app_module.service, "cancel_order", lambda value: {"id": value, "symbol": "AAPL260828C00315000", "status": "cancel_requested"})

    response = app_module.app.test_client().delete(f"/api/orders/{order_id}")

    assert response.status_code == 200
    assert response.get_json()["status"] == "cancel_requested"


def test_filled_order_is_not_cancelable():
    service = WheelService()

    class FilledClient:
        def trading(self, path):
            return {"status": "filled"}

    service.client = FilledClient()

    with pytest.raises(ValueError, match="can no longer be canceled"):
        service.cancel_order("5f5dbf40-14ca-4f82-8948-1b5baa7289ac")
