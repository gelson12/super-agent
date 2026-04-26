# Statistical & Probabilistic Methods for Trading Signals
*Practical reference for the 10-strategy crypto signal aggregator*

---

## 1. Wilson Score Confidence Interval

**Use:** Honest hit-rate estimation at small n. Use **lower bound** as pessimistic estimate driving adaptive weights.

```js
function wilsonInterval(wins, n, z = 1.96) {
  if (n === 0) return { lower: 0, upper: 1, center: 0.5 };
  const p = wins / n;
  const denom = 1 + (z*z)/n;
  const center = (p + (z*z)/(2*n)) / denom;
  const margin = (z/denom) * Math.sqrt((p*(1-p))/n + (z*z)/(4*n*n));
  return { lower: center - margin, upper: center + margin, center };
}
```

**When NOT:** Non-iid outcomes (overlapping signals — use triple-barrier first). Continuous PnL — use bootstrap CI on Sharpe.

*Wilson (1927) JASA 22:209–212.*

---

## 2. Brier Score

**Use:** Confidence calibration. Lower = better.

```js
function brierScore(preds) {
  if (!preds.length) return null;
  let s = 0;
  for (const { prob, outcome } of preds) s += (prob - outcome) ** 2;
  return s / preds.length;
}
```

**Thresholds:** <0.18 well-calibrated · 0.18–0.22 acceptable · 0.22–0.25 marginal · >0.25 worse than random.

*Brier (1950) Monthly Weather Review 78:1–3.*

---

## 3. Sharpe Ratio

```js
function sharpe(returns, rfPerPeriod = 0, periodsPerYear = 2190) {
  const n = returns.length;
  if (n < 30) return null;
  const m = returns.reduce((a,b)=>a+b,0)/n;
  const v = returns.reduce((s,r)=>s+(r-m)**2,0)/(n-1);
  const sd = Math.sqrt(v);
  return sd === 0 ? null : ((m - rfPerPeriod)/sd) * Math.sqrt(periodsPerYear);
}
```

For 4h crypto bars: `periodsPerYear = 365 × 6 = 2190`. Min sample ~30; SE ≈ √((1+0.5×SR²)/n).

---

## 4. Sortino Ratio

```js
function sortino(returns, target = 0, periodsPerYear = 2190) {
  const n = returns.length;
  if (n < 30) return null;
  const m = returns.reduce((a,b)=>a+b,0)/n;
  const dSq = returns.reduce((s,r)=>s+Math.min(r-target,0)**2,0)/n;
  const dd = Math.sqrt(dSq);
  return dd === 0 ? null : ((m - target)/dd) * Math.sqrt(periodsPerYear);
}
```

**Prefer over Sharpe for:** trend/breakout/momentum (positive skew). Use Sharpe for mean-rev/stat-arb.

---

## 5. Kelly Criterion

```js
function kellyBinary(p, b) {
  return Math.max(0, (p*b - (1-p)) / b);
}
function kellyContinuous(meanRet, variance) {
  return variance > 0 ? meanRet / variance : 0;
}
function fractionalKelly(fStar, fraction = 0.5, cap = 0.25) {
  return Math.min(cap, Math.max(0, fStar * fraction));
}
```

**Always use ≤ half-Kelly** — full Kelly catastrophically over-bets given parameter uncertainty. Cap at 20–25% of equity.

*Kelly (1956); Thorp (2006).*

---

## 6. Bayesian Signal Aggregation

Combine 10 strategy outputs into one posterior P(BULL | signals) via log-odds.

```js
const logit   = p => Math.log(p/(1-p));
const sigmoid = x => 1/(1+Math.exp(-x));

function bayesAggregate(scores, likelihoodTables, prior = 0.5) {
  // scores: [s1..s10] each 0..100
  // likelihoodTables[i] = { bull: [...10 deciles], bear: [...10 deciles] }
  let lo = logit(prior);
  for (let i = 0; i < scores.length; i++) {
    const dec = Math.min(9, Math.floor(scores[i] / 10));
    const lb = likelihoodTables[i].bull[dec] || 0.05;
    const lr = likelihoodTables[i].bear[dec] || 0.05;
    lo += Math.log(lb) - Math.log(lr);
  }
  return sigmoid(lo);
}
```

**When NOT:** Highly correlated strategies (trend + momentum) — independence violation → over-confident. Mitigate by shrinking each log-likelihood by 1/k where k = effective independent signals.

---

## 7. Information Coefficient (IC)

Spearman rank correlation between signal score and forward return. |IC|>0.05 = real edge in liquid markets.

```js
function rank(arr) {
  const sorted = arr.map((v,i)=>[v,i]).sort((a,b)=>a[0]-b[0]);
  const r = new Array(arr.length);
  sorted.forEach(([_,idx], k) => r[idx] = k + 1);
  return r;
}
function spearman(x, y) {
  if (x.length !== y.length || x.length < 20) return null;
  const rx = rank(x), ry = rank(y), n = x.length;
  const m = (n+1)/2;
  let num=0, dx=0, dy=0;
  for (let i=0;i<n;i++){
    const a=rx[i]-m, b=ry[i]-m;
    num+=a*b; dx+=a*a; dy+=b*b;
  }
  return num / Math.sqrt(dx*dy);
}
```

Track **IC IR** = mean(IC) / stdev(IC) across rolling windows.

*Grinold & Kahn (1999) ch. 6 — Fundamental Law of Active Management.*

---

## 8. Signal Half-Life / Decay

Fit exponential decay to IC vs horizon:

```js
function halfLife(horizons, ics) {
  const pts = horizons.map((h,i)=>[h, Math.log(Math.max(ics[i], 1e-4))]);
  const n = pts.length;
  const mx = pts.reduce((s,p)=>s+p[0],0)/n;
  const my = pts.reduce((s,p)=>s+p[1],0)/n;
  let num=0, den=0;
  for (const [x,y] of pts){ num+=(x-mx)*(y-my); den+=(x-mx)**2; }
  return -Math.LN2 / (num/den); // bars
}
```

Use to set outcome-tracker horizon AND decay weights of old observations: `0.5^(k/halfLife)`.

---

## 9. Walk-Forward / Purged K-Fold CV

```js
function* purgedKFold(n, k, labelHorizon, embargo = 0) {
  const foldSize = Math.floor(n / k);
  for (let f = 0; f < k; f++) {
    const testStart = f * foldSize;
    const testEnd   = (f === k-1) ? n : testStart + foldSize;
    const purgeFrom = Math.max(0, testStart - labelHorizon);
    const embargoTo = Math.min(n, testEnd + embargo);
    const train = [];
    for (let i = 0; i < n; i++) {
      if (i >= purgeFrom && i < embargoTo) continue;
      train.push(i);
    }
    const test = [];
    for (let i = testStart; i < testEnd; i++) test.push(i);
    yield { train, test };
  }
}
```

**Never** shuffle time series. Anchored walk-forward in non-stationary regimes.

*López de Prado (2018) ch. 7.*

---

## 10. Triple-Barrier Method

Replaces fixed-horizon grading. Label = first barrier hit (TP, SL, or time).

```js
function tripleBarrier(prices, t, side, tp, sl, H) {
  const p0 = prices[t];
  const up = p0 * (1 + tp), dn = p0 * (1 - sl);
  const end = Math.min(prices.length - 1, t + H);
  for (let i = t + 1; i <= end; i++) {
    if (prices[i] >= up) return { label: side===1 ? 1 : -1, hit: 'TP', bars: i-t };
    if (prices[i] <= dn) return { label: side===1 ? -1 : 1, hit: 'SL', bars: i-t };
  }
  const ret = (prices[end] - p0) / p0 * side;
  return { label: Math.sign(ret) || 0, hit: 'TIME', bars: end-t };
}
```

**Tip:** Set tp/sl from rolling realized vol (e.g., 2σ), not constants — adapts to regime.

*López de Prado (2018) ch. 3.*

---

## 11. Meta-Labeling

Layer 1 (your existing 10-family aggregator): says **side**. Layer 2: predicts **whether to take it** using logistic regression. Sized by predicted probability.

```js
function metaPredict(features, w, b) {
  let z = b;
  for (let i = 0; i < features.length; i++) z += w[i] * features[i];
  return 1 / (1 + Math.exp(-z));
}
function metaUpdate(features, label, w, b, lr = 0.01) {
  const p = metaPredict(features, w, b);
  const err = p - label;
  for (let i = 0; i < features.length; i++) w[i] -= lr * err * features[i];
  return { w, b: b - lr * err };
}
```

`features = [confidence, regime_flag, vol_pct, hour_of_day, ...]`. Need ≥100 primary signals to train.

---

## 12. Fractional Differentiation

```js
function fracDiffWeights(d, threshold = 1e-4) {
  const w = [1];
  for (let k = 1; k < 5000; k++) {
    const wk = w[k-1] * (-(d - k + 1) / k);
    if (Math.abs(wk) < threshold) break;
    w.push(wk);
  }
  return w;
}
function fracDiff(series, d, threshold = 1e-4) {
  const w = fracDiffWeights(d, threshold);
  const out = new Array(series.length).fill(NaN);
  for (let t = w.length - 1; t < series.length; t++) {
    let s = 0;
    for (let k = 0; k < w.length; k++) s += w[k] * series[t - k];
    out[t] = s;
  }
  return out;
}
```

Pick smallest d that passes ADF test on log-prices; usually d ≈ 0.3–0.5.

---

## 13. CUSUM Change-Point Detection

Fires on regime shifts.

```js
function cusumDetector(threshold) {
  let sP = 0, sN = 0;
  return function(x, mu = 0, k = 0) {
    sP = Math.max(0, sP + (x - mu - k));
    sN = Math.min(0, sN + (x - mu + k));
    if (sP > threshold)  { sP = sN = 0; return 'UP_SHIFT'; }
    if (sN < -threshold) { sP = sN = 0; return 'DOWN_SHIFT'; }
    return null;
  };
}
```

Apply to log-returns; threshold ≈ 5σ. **Don't trade for N bars after a fire** (regime in flux).

---

## 14. 2-State HMM (Trend vs Mean-Revert)

```js
function gaussPdf(x, mu, sd) {
  const z = (x - mu) / sd;
  return Math.exp(-0.5 * z * z) / (sd * Math.sqrt(2 * Math.PI));
}
function hmmStep(alphaPrev, r, params) {
  const { A, mu, sd } = params;
  const alpha = [0, 0];
  for (let s = 0; s < 2; s++) {
    let prior = 0;
    for (let p = 0; p < 2; p++) prior += alphaPrev[p] * A[p][s];
    alpha[s] = gaussPdf(r, mu[s], sd[s]) * prior;
  }
  const z = alpha[0] + alpha[1] || 1;
  return [alpha[0]/z, alpha[1]/z];
}
// A = [[0.95,0.05],[0.05,0.95]], mu = [0,0], sd = [smallVol, largeVol]
// Use posterior P(state=trend) to gate trend-vs-meanRev family weights
```

*Rabiner (1989) Proc IEEE 77:257–286; Hamilton (1989) Econometrica 57:357–384.*

---

## 15. Vol-of-Vol Regime Classification

```js
function rollingStd(arr, w) {
  const out = new Array(arr.length).fill(null);
  for (let i = w-1; i < arr.length; i++) {
    const sl = arr.slice(i-w+1, i+1);
    const m = sl.reduce((a,b)=>a+b,0)/w;
    const v = sl.reduce((s,x)=>s+(x-m)**2,0)/(w-1);
    out[i] = Math.sqrt(v);
  }
  return out;
}
function volRegime(returns, w = 30, w2 = 30) {
  const sigma = rollingStd(returns, w);
  const sClean = sigma.filter(x => x !== null);
  const vov = rollingStd(sClean, w2);
  const s = sigma[sigma.length-1], v = vov[vov.length-1];
  const pct = (a, x) => a.filter(z => z!==null && z<=x).length / a.filter(z=>z!==null).length;
  const sP = pct(sigma.slice(-500), s), vP = pct(vov.slice(-500), v);
  if (sP > 0.7 && vP > 0.7) return 'CHAOS';      // suppress all
  if (sP > 0.7 && vP < 0.3) return 'TRENDING';   // boost trend, momentum
  if (sP < 0.3 && vP < 0.3) return 'GRIND';      // boost meanRev
  return 'NORMAL';
}
```

Need ≥500 bars cold start. Pair with CUSUM.

---

## Integration Order for the Crypto Stack

| Phase | Method | Effect |
|---|---|---|
| **Today** | Wilson lower bound on hit rates | Honest adaptive weighting |
| **Today** | Brier score on dashboard | Calibration visibility |
| **Week 1** | Triple-barrier OT (TP/SL/time) | Real outcome labels |
| **Week 1** | Sortino per family | Skew-aware ranking |
| **Week 2** | IC + half-life per family | Auto-tuned decay |
| **Week 3** | Vol-of-vol + CUSUM regime gate | Don't trade in chaos |
| **Week 4** | Naive Bayes posterior (vs linear sum) | Probabilistic combiner |
| **Month 2** | Meta-labeling | Filter primary signals |
| **Month 2** | Purged k-fold CV | Trust the weight tuning |
| **Month 3+** | 2-state HMM | Trend/MR gating |
| **Month 3+** | Frac-diff features for meta | Stationarity + memory |

¼-Kelly tied to meta-label probability is the natural endgame.

---

## Primary references
- López de Prado, M. (2018). *Advances in Financial Machine Learning*. Wiley.
- Grinold, R. & Kahn, R. (1999). *Active Portfolio Management*. McGraw-Hill.
- Bailey, D. & López de Prado, M. (2014). "The Deflated Sharpe Ratio" *J. Portfolio Mgmt.* 40(5).
- Thorp, E. (2006). "The Kelly Criterion in Blackjack, Sports Betting, and the Stock Market".
- Rabiner, L. (1989). "A Tutorial on HMMs" *Proc. IEEE* 77(2).
