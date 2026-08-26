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

## Deploy with Vercel

Connect this Git repository to a new Vercel project. Vercel detects the Flask `app` exported by `app.py`; `vercel.json` configures the Python function and keeps desktop/reference files out of the deployment bundle.

Add these environment variables in **Vercel → Project Settings → Environment Variables**:

- `ALPACA_API_KEY`
- `ALPACA_SECRET_KEY`
- `ALPACA_PAPER=true`
- `ALLOW_DEMO_FALLBACK=false` (recommended for production)
- `TRADING_UNLOCK_PASSWORD` (required to enable Sell call / Sell put)
- `FLASK_SECRET_KEY` (a long random value used to sign unlock sessions)

Do not add `PORT`, `HOST`, or `FLASK_DEBUG` in Vercel. Git pushes to the connected production branch will deploy automatically, and pull requests will receive preview deployments.

The site and market data load without a password. Sell call and Sell put remain disabled until `TRADING_UNLOCK_PASSWORD` is entered in the trading-lock dialog. Unlocks expire after 30 minutes by default; override this with `TRADING_UNLOCK_TTL_SECONDS`.

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
