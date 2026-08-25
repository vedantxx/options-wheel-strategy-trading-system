from __future__ import annotations

import os
from pathlib import Path

from flask import Flask, jsonify, render_template, request

from wheel_service import AlpacaError, WheelService, load_env


ROOT = Path(__file__).resolve().parent
load_env(ROOT / ".env")

app = Flask(__name__)
service = WheelService()


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


@app.post("/api/orders")
def place_order():
    payload = request.get_json(silent=True) or {}
    try:
        return jsonify(service.place_order(payload)), 201
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
