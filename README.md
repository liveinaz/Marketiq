# MarketIQ — AI-assisted stock research & paper trading

A working, mobile-responsive Streamlit web application for research and **manual Fidelity trade approval**. It never logs into Fidelity or places real orders.

## Features
- Watchlist stock scanner with explainable 0–100 heuristic research scores
- Live/provider-available stock quotes, company financial metrics, recent news
- Optional OpenAI-generated explanations, grounded in retrieved data
- Paper buy/sell order entry, cash accounting, holdings and downloadable journal
- Fidelity link for **manual** execution only
- Persistent local SQLite data; responsive Streamlit UI

## Start locally
1. Install Python 3.10+.
2. Open a terminal in this folder.
3. `python -m pip install -r requirements.txt`
4. Copy `.env.example` to `.env` and enter your Finnhub key (https://finnhub.io/). An OpenAI key is optional.
5. `streamlit run app.py`
6. Open the displayed localhost URL on your computer. For iPhone access, deploy the project to a private Streamlit-compatible host and protect it with authentication.

## Important limitations
- No brokerage connectivity, live trade execution, push notifications, automatic scheduled scanning, or real-time exchange entitlement guarantee.
- Scanner covers a selected list of tickers, not the full market. Free API tiers can limit quote/news availability and frequency.
- The scoring formula is a **heuristic**, not a machine-learning model or validated predictor. Optional LLM writes a narrative, not a predictive signal.
- Paper trading records user-entered simulated fills; it does not model real execution, spreads, slippage, dividends, fees, or taxes. Account valuation requires quotes for all positions.
- If deployed, enable login/authentication and secure API secrets. SQLite is appropriate for a single-user prototype, not a multi-user production system. Keep `.env` private.
- Do not enter Fidelity usernames, passwords, account numbers or other sensitive details.
- Before risking money, validate signals with out-of-sample backtests, walk-forward testing and long paper-trading periods, including trading costs and benchmark comparisons.

## Potential next phase
- Add authenticated private deployment, background scheduled scanner, notification provider, broader market feed, historical candles and backtesting, alerts, and hardened portfolio accounting.
