# Crypto n8n Workflows — Canonical Source

This directory holds the canonical JSON definitions for the 8 crypto-trading n8n workflows running on `outstanding-blessing-production-1d4b.up.railway.app`.

## Workflows

| File | n8n ID | Schedule | Purpose |
|---|---|---|---|
| `btc_main.json` | `7onbBjeUwHkSsuyc` | Every 5 min | BTC signal generation (40 nodes) |
| `eth_main.json` | `GgUjF0EQw1wQa2G1` | Every 5 min | ETH signal generation (40 nodes) |
| `btc_outcome_tracker.json` | `Dz9P19r0h2P5Om77` | Every 4h | BTC signal grading + adaptive params |
| `eth_outcome_tracker.json` | `yK4VWyJitJVmjW9J` | Every 4h | ETH signal grading + adaptive params |
| `weight_tuner.json` | `7oZw8yaR5Buu8YEK` | Daily 06:30 UTC | Strategy family weight optimization |
| `dashboard.json` | `1dj3uv3G6acSniJC` | Webhook | Tabbed BTC/ETH dashboard |
| `watchdog.json` | `dBrfEjCiJY7wgImW` | Every 1 min | Volatility/liquidity sentinel |
| `perf_report.json` | `rdjetGa1aegwIB4t` | Daily 08:00 UTC | Daily performance report |

## Quant Methods Implemented (2026-04-25/26)

### Outcome Trackers v6
- **Triple-barrier grading** (Lopez de Prado): TP=1.5×ATR, SL=1.0×ATR, TIME=4h
- **Wilson lower bound** on hit rate (z=1.96) — pessimistic estimate, kills weight oscillation
- **Brier score** for calibration: <0.18 EXCELLENT, 0.18–0.22 GOOD, 0.22–0.25 MARGINAL, >0.25 POOR
- **Per-direction Wilson HR** (buyHRWilson, sellHRWilson)
- Adaptive minConf driven by Wilson lower (not raw hitRate)

### Risk Engine n22 (BTC + ETH)
- **¼-Kelly hard cap at 5% equity**
- **Signal-based Kelly override** when OT has ≥10 graded signals (uses Wilson lower)
- **Drawdown deleveraging**: 50% size at –15% DD, 100% halt at –25% DD

### Strategy Scoring n21 — Carver FDM combiner (BTC + ETH)
- Replaces ad-hoc `Σ score × weight / Σ weight` with **Forecast Diversification Multiplier**
- `q = w' Σ w` (weighted sum of pairwise correlations)
- `FDM = 1/√q` capped at 2.5
- Hardcoded semantic correlation matrix between 10 strategy families (trend/momentum/multiTF cluster, meanRev/fibonacci cluster, etc.)
- Reports `fdm`, `fdmMult`, `avgCorr`, `qDiv` for monitoring

### Weight Tuner — Deflated Sharpe gate (Bailey & Lopez de Prado 2014)
- Computes Sharpe on realized return series with skew/kurtosis adjustment
- Critical Sharpe `SR0 = √(2·ln(N)) − ln(ln(N))` for N=10 trials
- Promotes new weights only if `DSR > 1.96` (95% confidence) — otherwise falls back to defaults
- Prevents random fluctuations from changing strategy weights

## Vault Namespace Isolation

| Coin | source | adaptive key | tags |
|---|---|---|---|
| BTC | `crypto_specialist_v3` | `ADAPTIVE_PARAMS` | `crypto_signal`, `btc` |
| ETH | `crypto_eth_v1` | `ETH_ADAPT_PARAMS` | `eth_signal`, `eth` |

`'ADAPTIVE_PARAMS'.includes('ETH_ADAPT_PARAMS')` = false (verified). Zero cross-contamination.

## Deployment

```bash
N8N_KEY=$(grep N8N_API_KEY .env | cut -d= -f2)
BASE="https://outstanding-blessing-production-1d4b.up.railway.app/api/v1/workflows"
ID="7onbBjeUwHkSsuyc"  # workflow ID
curl -X PUT -H "X-N8N-API-KEY: $N8N_KEY" -H "Content-Type: application/json" \
  -d @n8n_workflows/btc_main.json "$BASE/$ID"
# IMPORTANT: PUT silently sets active=false. Always re-activate:
curl -X POST -H "X-N8N-API-KEY: $N8N_KEY" "$BASE/$ID/activate"
```

## Reference

- Quant research docs: `../quant_research/`
- Skills: `~/.claude/skills/crypto-{signal-stats,quant-frameworks,quant-audit}/`
