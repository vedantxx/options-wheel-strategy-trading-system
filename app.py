from __future__ import annotations

import hmac
import os
import time
from pathlib import Path

from flask import Flask, jsonify, render_template, request, session

from wheel_core import parse_option_symbol
from wheel_service import AlpacaError, WheelService, load_env


ROOT = Path(__file__).resolve().parent
load_env(ROOT / ".env")

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY") or os.urandom(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Strict",
    SESSION_COOKIE_SECURE=os.getenv("VERCEL") == "1",
)
service = WheelService()


def _unlock_password() -> str:
    return os.getenv("TRADING_UNLOCK_PASSWORD", "")


def _unlock_ttl_seconds() -> int:
    try:
        return max(60, int(os.getenv("TRADING_UNLOCK_TTL_SECONDS", "1800")))
    except ValueError:
        return 1800


def _sell_unlocked() -> bool:
    expires_at = float(session.get("sell_unlocked_until", 0) or 0)
    if expires_at <= time.time():
        session.pop("sell_unlocked_until", None)
        return False
    return True


def _requires_trading_unlock(payload: dict) -> bool:
    intent = payload.get("position_intent")
    if intent == "sell_to_open":
        return True
    contract = parse_option_symbol(str(payload.get("symbol", "")))
    return intent == "buy_to_close" and bool(contract and contract["type"] == "call")


@app.get("/")
def index():
    return render_template("index.html", paper=service.paper)


@app.get("/api/portfolio")
def portfolio():
    return jsonify(service.portfolio())


@app.get("/api/maturities")
def maturities():
    symbol = request.args.get("symbol", "").strip().upper()
    if not symbol:
        return jsonify({"error": "A stock symbol is required."}), 400
    return jsonify(service.maturities(symbol))


@app.get("/api/wheel")
def wheel():
    symbol = request.args.get("symbol", "").strip().upper()
    expiration = request.args.get("expiration", "").strip()
    if not symbol or not expiration:
        return jsonify({"error": "Stock and maturity are required."}), 400
    return jsonify(service.wheel(symbol, expiration))


@app.get("/api/quote")
def quote():
    symbol = request.args.get("symbol", "").strip().upper()
    if not symbol:
        return jsonify({"error": "A stock symbol is required."}), 400
    return jsonify(service.quote(symbol))


@app.get("/api/trading-lock")
def trading_lock_status():
    unlocked = _sell_unlocked()
    return jsonify({
        "configured": bool(_unlock_password()),
        "unlocked": unlocked,
        "expires_at": session.get("sell_unlocked_until") if unlocked else None,
    })


@app.post("/api/trading/unlock")
def unlock_trading():
    configured_password = _unlock_password()
    supplied_password = str((request.get_json(silent=True) or {}).get("password", ""))
    if not configured_password:
        return jsonify({"error": "Set TRADING_UNLOCK_PASSWORD before unlocking sell orders."}), 503
    if not hmac.compare_digest(supplied_password, configured_password):
        return jsonify({"error": "Incorrect trading password."}), 401
    expires_at = time.time() + _unlock_ttl_seconds()
    session["sell_unlocked_until"] = expires_at
    return jsonify({"configured": True, "unlocked": True, "expires_at": expires_at})


@app.post("/api/trading/lock")
def lock_trading():
    session.pop("sell_unlocked_until", None)
    return jsonify({"configured": bool(_unlock_password()), "unlocked": False, "expires_at": None})


@app.post("/api/orders")
def place_order():
    payload = request.get_json(silent=True) or {}
    if _requires_trading_unlock(payload) and not _sell_unlocked():
        return jsonify({"error": "Trading is locked. Unlock it with your trading password first."}), 423
    try:
        return jsonify(service.place_order(payload)), 201
    except (AlpacaError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400


@app.delete("/api/orders/<order_id>")
def cancel_order(order_id: str):
    try:
        return jsonify(service.cancel_order(order_id))
    except (AlpacaError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400


@app.get("/api/health")
def health():
    return jsonify({"ok": True, "paper": service.paper, "configured": service.configured})


@app.errorhandler(AlpacaError)
def alpaca_error(exc: AlpacaError):
    return jsonify({"error": str(exc)}), 502


if __name__ == "__main__":
    app.run(
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "5055")),
        debug=os.getenv("FLASK_DEBUG", "false").lower() == "true",
    )
