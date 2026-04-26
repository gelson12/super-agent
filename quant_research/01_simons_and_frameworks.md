# Quantitative Trading Frameworks — Practitioner Reference
*Research compiled for the crypto signal-aggregation system (BTC + ETH workflows on n8n)*

---

## 1. Jim Simons / Renaissance Technologies / Medallion Fund

**Core principle:** Find thousands of weak, statistically validated, short-horizon anomalies; size each trade so small that the law of large numbers — not any single bet — produces the edge.

### What Simons publicly said
- MIT Mathematics Lecture (2010): *"We look at anomalies that may be small in size and brief in time. We make our forecast. Then, shortly thereafter, we re-evaluate the situation and revise our forecast and our portfolio. We do this all-day-long."*
- *"Some of the signals that we have, that have been working for close to 15 years, make no sense. Otherwise someone else would have found them."*
- *"We don't override the models. If you start to override the models, you may as well not have them."*

### Concrete Renaissance practices

| Practice | Detail | Crypto application |
|---|---|---|
| **~50.75% win rate × massive trade count** | Medallion reportedly wins ~50.75% of trades — profit comes from millions × tiny edge × leverage | Don't chase 70% win rates — accept 51-53% with thousands of signals/month |
| **Hidden Markov Models (HMMs)** | Renaissance hired Leonard Baum (Baum-Welch co-inventor). Detect regime states. | 2-state HMM in JS gating `trend` vs `meanRev` weights (~150 lines) |
| **Signal-to-noise filtering** | Strip predictable seasonality (open/close, day-of-week, overnight) before fitting | Subtract hour-of-day + funding-rate-cycle (8h) means before computing momentum/meanRev |
| **Short-horizon mean reversion** | Most Medallion alpha is intraday/multi-day | Weight `meanRev`/`micro` higher on 1m–1h, `trend`/`macro` higher on 4h–1d |
| **Market microstructure** | Order-book imbalance, VPIN, bid-ask bounce | Your `micro` family already on this path |
| **No discretionary overrides** | Models run the book | Auto-execute, no kill switch except hard risk limits |

### Risk
- Net Sharpe ~2.5–4 to outside investors during open era (gross ~7+)
- Leverage 12.5:1 historically (basket-options structure, SEC-disclosed)
- ¼-Kelly cap on aggregate exposure (rumored)

**Citations:** Zuckerman (2019) *The Man Who Solved the Market*; Patterson (2010) *The Quants*; Simons MIT Lecture 2010.

---

## 2. Ernest Chan — Mean Reversion + Stat Arb

**Core:** Statistical arbitrage and mean reversion are the most reliable retail-accessible edges.

### Implementable
1. **ADF stationarity test** — gate which assets `meanRev` family trades; only trade where p < 0.05
2. **Half-life of mean reversion** (Ornstein-Uhlenbeck): `halfLife = -log(2) / log(1 + λ)` where λ = OLS coef of `Δprice ~ price_lag`. Use as auto-tuned lookback for z-score
3. **Kalman filter for dynamic hedge ratios** — feeds the `stat` family for BTC/ETH pairs trading; ~50 lines JS
4. **Bollinger z-score with regime filter:** long when z<−2 AND ADF passes

**Books:** *Quantitative Trading* (2009), *Algorithmic Trading* (2013), *Machine Trading* (2017).

---

## 3. Andreas Clenow — Trend Following

**Core:** Diversified trend-following with strict equal-risk position sizing beats clever tactics.

### Implementable
1. **Momentum ranking** = annualized exponential regression slope × R² over 60–90 day window. R² penalizes choppy uptrends
2. **ATR position sizing:** `units = (equity × riskPerTrade) / (ATR_20 × pointValue)` — every position contributes equal dollar volatility
3. **No stop-losses on trend** — exit only on momentum reversal or rank drop-out
4. **Index regime filter:** trade longs only when BTC > 200d MA (use TOTAL or TOTAL2 in crypto)

**Books:** *Following the Trend* (2013), *Stocks on the Move* (2015).

---

## 4. Robert Carver — DIRECTLY APPLICABLE TO YOUR SYSTEM

**Core:** Combine many uncorrelated low-Sharpe signals into one volatility-targeted forecast; complexity is the enemy.

This is the most useful author for the user's 10-family combiner.

### Implementable
1. **Forecast scaling to ±20:** each family's `forecastScalar = 10 / mean(|rawSignal|)` measured over backtest. Makes families directly comparable
2. **Forecast Diversification Multiplier (FDM):** `FDM = 1 / sqrt(w' Σ w)` (capped ≈ 2.5) — restores unit vol after combining correlated forecasts. The mathematically correct combiner.
3. **Volatility targeting:** `positionSize = (capital × volTarget × combinedForecast/10) / (instrumentVol × price)`, volTarget = 0.20
4. **Handcrafted weights** instead of mean-variance optimization (which overfits): cluster the 10 families {trend cluster, mean-rev cluster, structure cluster, macro cluster}, equal-weight within and across clusters
5. **Buffering:** don't rebalance unless new position differs from current by > 10% of average — kills crypto fee bleed

**Books:** *Systematic Trading* (2015), *Leveraged Trading* (2019), *Advanced Futures Trading Strategies* (2023). Free blog: qoppac.blogspot.com. GitHub: pysystemtrade.

---

## 5. AQR / Cliff Asness — Factor Investing

**Core:** Four factors — value, momentum, carry, defensive — work across every asset class.

### Implementable
1. **Time-series momentum:** sign of past 30/60/90-day return as long-only trend filter (Moskowitz/Ooi/Pedersen 2012)
2. **Carry as a universal signal:** `carry = -fundingRate × annualizationFactor` — negative funding = long carry. Add to `macro` family
3. **Crypto value:** MVRV ratio, NVT, network value/transactions. Cross-sectional rank top vs bottom quintile
4. **Risk parity weighting:** weight each factor inversely to realized vol, not equally by capital

**Papers (free at aqr.com):**
- "Value and Momentum Everywhere" (2013) JF
- "Time Series Momentum" (2012) JFE
- "Carry" (2018) JFE

---

## 6. Marcos López de Prado — Avoid Overfitting

**Core:** Most published backtests are false discoveries; financial ML needs specialized CV, labeling, features.

### Implementable
1. **Triple-barrier method** (AFML ch. 3): for every signal define TP / SL / time barriers. Label = whichever hits first. Replaces fixed-horizon outcome tracking
2. **Meta-labeling:** primary model says side, secondary classifier says whether to take it. Logistic regression on (side + features) → significant Sharpe lift. ~30 lines JS
3. **Fractional differentiation** with d ≈ 0.4 — keeps memory while making series stationary (vs differencing which destroys memory)
4. **Purged k-fold CV with embargo:** remove training samples whose label window overlaps test set + skip a buffer. Critical for any in-sample tuning
5. **Deflated Sharpe Ratio:** when picking best of N strategies, headline Sharpe is inflated. DSR corrects for # of trials. Use before promoting weight changes

**Books:** *Advances in Financial Machine Learning* (2018), *Machine Learning for Asset Managers* (2020). Papers free at ssrn.com/author=434076.

---

## 7. Bridgewater / Ray Dalio — Risk Parity

**Core:** Diversify across uncorrelated streams sized to equal risk contribution.

### Implementable
1. **Risk parity weighting** across 10 families: `weight_i ∝ 1/vol_i` then normalize
2. **Equal risk contribution** (better): iterative — `weight_i × (Σw)_i` constant, ~20 lines JS
3. **4-quadrant macro regime:** rising/falling DXY × rising/falling BTC dominance. Per-regime weight overlay
4. **Holy Grail:** 15+ uncorrelated streams cut vol by ~80% at same expected return. Average pairwise correlation among the 10 families should be < 0.3 — measure and drop clustering ones

---

## 8. Two Sigma / DE Shaw — Process Discipline

**Core:** Treat the market as a high-dimensional pattern recognition problem; control turnover, decay-adjust signals, kill underperformers.

### Implementable
1. **Signal decay analysis:** plot IC vs forward horizon k. The k where IC peaks is the natural holding period. Trade each family at *its own* horizon
2. **IC-weighted alpha combination:** `weight_i = IC_i / Σ|IC_j|`, shrunk toward equal weights (Bayesian)
3. **Transaction-cost-aware optimization:** `expected_alpha_gain > 2 × cost` before trading
4. **No-overrides + sunset signals:** monthly Sharpe check on each family; weight → 0 if 6m rolling Sharpe < 0

**Free:** twosigma.com/insights, deshawresearch.com/publications.html

---

## Position Sizing — Author Synthesis

| Method | Formula | When |
|---|---|---|
| **Binary Kelly** | `f* = (p×b - (1-p)) / b` | Single signal, known edge |
| **¼-Kelly** | `0.25 × f*` | Realistic — full Kelly over-bets given parameter uncertainty |
| **Vol targeting (Carver)** | `size = capital × σ_target / σ_realized` | Portfolio-level default |
| **ATR sizing (Clenow)** | `units = capital × risk% / ATR` | Per-trade for trend/breakout |
| **Risk parity (Bridgewater)** | `w ∝ 1/σ` | Across 10 families |
| **Equal risk contribution** | iterative | Better than 1/σ when correlations differ |

### Recommended stack for your system
1. Each of 10 families outputs a raw forecast
2. Carver-scale to ±20
3. Combine via handcrafted cluster weights × FDM
4. Convert to position via vol targeting at 20% annualized
5. Apply 10% buffer before rebalancing
6. ¼-Kelly cap on aggregate exposure
7. Monthly: re-measure each family's IC, decay weights of negative-Sharpe families

---

## Risk Management Consensus

- **Sharpe target:** 1.0–2.0 net is excellent for retail crypto; 3+ usually means overfitting
- **Drawdown rule:** cut leverage in half at 15% DD; cut to zero at 25%
- **Vol targeting:** 15–25% annualized portfolio vol — higher kills you in crashes
- **Correlation crash rule:** if avg pairwise correlation among the 10 families exceeds 0.5, reduce gross exposure 50% (the "all correlations go to 1 in a crisis" rule)
- **Out-of-sample:** Deflated Sharpe before any weight change goes live

---

## Implementation Priority for Your n8n Stack

Ranked by ROI:

1. **Carver forecast scaling + FDM combiner** — replaces ad-hoc weights. ~100 lines JS
2. **Volatility targeting at portfolio level** — replaces per-family sizing. ~30 lines
3. **EWMA volatility estimator** (36-day half-life) — feeds #2. ~10 lines
4. **Cross-family correlation matrix + handcrafted clusters** — robust weights. ~50 lines
5. **Buffering / no-trade zone** — kills fee bleed. ~10 lines
6. **Triple-barrier labeling** — replaces fixed 4h grading. ~40 lines
7. **HMM 2-state regime detector** — gates trend vs meanRev. ~150 lines
8. **Half-life-tuned mean reversion** (Chan) — auto-tunes meanRev lookback. ~50 lines
9. **¼-Kelly aggregate cap** — survival rule. ~5 lines
10. **Deflated Sharpe gate** before promoting weight change. ~30 lines

Items 1–5 alone capture ~70% of institutional sophistication and need no ML libraries.

---

## Reading Order

1. Carver, *Systematic Trading* — operational manual
2. Chan, *Algorithmic Trading* — for `meanRev` and `stat`
3. López de Prado, *Advances in Financial Machine Learning* ch. 3, 5, 7 — to avoid overfitting
4. Zuckerman, *The Man Who Solved the Market* — philosophy/discipline
5. AQR papers (free) — for `macro` and factor additions
