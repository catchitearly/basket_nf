# Fyers Basket P&L Dashboard

Compute the historical P&L-vs-time for a multi-leg Nifty options basket
(any mix of BUY/SELL, any strikes, any expiries) using the **Fyers
historical data API**, run it on demand via **GitHub Actions**, and view
the result as a chart on **GitHub Pages**.

## How it works

1. You describe your basket in `config/basket.yaml` (one entry per leg:
   Fyers symbol, BUY/SELL, lots, lot size, optional entry price).
2. You trigger the **"Compute Basket P&L"** GitHub Action manually
   (Actions tab → Run workflow).
3. The action calls the Fyers `/data/history` endpoint for each leg over
   the date range in your config, combines the legs into a single P&L
   timeseries, and commits the result to `docs/pnl_timeseries.json`.
4. GitHub Pages serves `docs/index.html`, which reads that JSON and
   renders: total P&L vs time, per-leg P&L vs time, and a leg summary
   table.

## One-time setup

### 1. Push this repo to GitHub
Create a new repo and push all these files (keep the folder structure as-is).

### 2. Add your Fyers token as a secret
Go to **Settings → Secrets and variables → Actions → New repository
secret**.

- Name: `FYERS_TOKEN`
- Value: `<APP_ID>:<ACCESS_TOKEN>`

  `APP_ID` is the Fyers app id you generated the token with (looks like
  `XXXXXX-100`). `ACCESS_TOKEN` is your daily access token. Since Fyers
  tokens expire daily, **update this secret each day** before running the
  workflow (or paste a fresh token directly in the workflow's manual
  trigger if you adapt it to take a token input — not recommended since
  inputs aren't secret).

### 3. Enable GitHub Pages
**Settings → Pages → Build and deployment → Source: "Deploy from a
branch"**, branch `main`, folder `/docs`. Save. Your dashboard will be at
`https://<your-username>.github.io/<repo-name>/`.

### 4. Edit your basket
Open `config/basket.yaml` and fill in:
- `date_range.from` / `date_range.to` — the window to analyze
- `resolution` — candle size in minutes (`"1"`, `"5"`, `"15"`, `"60"`, or `"D"`)
- Each leg's exact **Fyers symbol** (get this from the Fyers symbol
  master / scrip search — option symbols look like
  `NSE:NIFTY25D2623500CE`), side, lots, and lot size.

If you leave `entry_price: null`, the script uses the close of the first
available candle in the range as the entry price (i.e. "what if I
entered at the start of this window"). Set an explicit `entry_price` if
you know your actual fill price.

### 5. Run it
**Actions tab → "Compute Basket P&L" → Run workflow.** After it finishes
(~10-30s depending on number of legs), refresh your GitHub Pages URL.

## Adding/changing legs
A basket can have any number of legs (2+), same or different expiries,
any mix of CE/PE and BUY/SELL. Just add more entries under `legs:` in
`basket.yaml` — no code changes needed.

## Notes & limitations
- Fyers access tokens are valid for the trading day they were issued; the
  workflow will fail with an auth error if your token has expired —
  update the `FYERS_TOKEN` secret and re-run.
- Historical intraday candles from Fyers are typically available for a
  limited recent lookback window (varies by resolution); very old dates
  may return no data.
- All timestamps in the dashboard are rendered in your browser's local
  time.
- This computes **mark-to-market P&L from candle close prices**, not
  actual fills/slippage — useful for "what if" and backtest-style
  analysis, not exact broker P&L.

## File map
```
config/basket.yaml          ← edit this: your basket
scripts/compute_pnl.py      ← fetches Fyers data, computes P&L
.github/workflows/pnl.yml   ← manual-trigger GitHub Action
docs/index.html             ← the dashboard (GitHub Pages serves this)
docs/pnl_timeseries.json    ← generated output (overwritten each run)
```
