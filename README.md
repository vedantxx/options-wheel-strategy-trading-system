# Wheel Command Center

A Flask + HTML dashboard for running an options wheel workflow with Alpaca. It adapts the provided desktop strategy and dashboard template into a responsive web interface.

## Run

```bash
python3 -m pip install -r requirements.txt
python3 app.py
```

Open `http://127.0.0.1:5055`.

The app reads `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, and `ALPACA_PAPER` from `.env`. Paper mode is the default. If Alpaca cannot be reached, a clearly marked demo dataset is shown unless `ALLOW_DEMO_FALLBACK=false` is set.

To permit live-account order submission, both `ALPACA_PAPER=false` and `ALLOW_LIVE_ORDERS=true` are required. Every order also requires an in-app confirmation.

## Main flows

- Six-card global portfolio overview
- Stock and maturity selectors
- Wheel position chart and one-month equity curve
- Recent trade history
- Separate snapped OTM call and put chains
- Covered-call / cash-secured-put limit orders at midpoint
- Buy-to-close actions for existing short option legs
- Scenario panel for stock P/L, premium, assignment cash, called-away P/L, and post-assignment basis
# options-wheel-trading-system
# options-wheel-trading-system
