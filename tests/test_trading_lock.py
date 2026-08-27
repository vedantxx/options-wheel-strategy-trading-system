import app as app_module


def test_sell_orders_require_unlock(monkeypatch):
    monkeypatch.setenv("TRADING_UNLOCK_PASSWORD", "test-only-password")
    client = app_module.app.test_client()

    status = client.get("/api/trading-lock")
    assert status.status_code == 200
    assert status.get_json()["unlocked"] is False

    locked_order = client.post("/api/orders", json={"position_intent": "sell_to_open"})
    assert locked_order.status_code == 423

    locked_close_call = client.post("/api/orders", json={
        "position_intent": "buy_to_close",
        "symbol": "AAPL260918C00230000",
    })
    assert locked_close_call.status_code == 423

    close_put = client.post("/api/orders", json={
        "position_intent": "buy_to_close",
        "symbol": "AAPL260918P00200000",
    })
    assert close_put.status_code != 423


def test_password_unlocks_and_lock_endpoint_relocks(monkeypatch):
    monkeypatch.setenv("TRADING_UNLOCK_PASSWORD", "test-only-password")
    client = app_module.app.test_client()

    wrong = client.post("/api/trading/unlock", json={"password": "wrong"})
    assert wrong.status_code == 401

    unlocked = client.post("/api/trading/unlock", json={"password": "test-only-password"})
    assert unlocked.status_code == 200
    assert unlocked.get_json()["unlocked"] is True
    assert client.get("/api/trading-lock").get_json()["unlocked"] is True

    locked = client.post("/api/trading/lock", json={})
    assert locked.status_code == 200
    assert locked.get_json()["unlocked"] is False


def test_unconfigured_password_fails_closed(monkeypatch):
    monkeypatch.delenv("TRADING_UNLOCK_PASSWORD", raising=False)
    client = app_module.app.test_client()
    response = client.post("/api/trading/unlock", json={"password": "anything"})
    assert response.status_code == 503
    assert "TRADING_UNLOCK_PASSWORD" in response.get_json()["error"]
