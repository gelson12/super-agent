const https = require('https');
const crypto = require('crypto');

// ═══ CONFIG ══════════════════════════════════════════════════
const liveTrading = $env.ENABLE_LIVE_TRADING || 'false';
const apiKey      = $env.CRYPT_API_KEY || '';
const apiSecret   = $env.CRYPT_PRIVATE_KEY || '';
const minConf     = parseFloat($env.TRADE_MIN_CONFIDENCE || '75');
const basePct     = parseFloat($env.TRADE_SIZE_PCT || '10') / 100;

// ═══ INPUTS ══════════════════════════════════════════════════
const signal     = $('Format Alert').first().json.signal || 'HOLD';
const confidence = parseFloat($('Format Alert').first().json.confidence || 0);
const risk       = $('Risk Engine').first().json;
const price      = $('MTF Analysis').first().json.price || 80000;
const slPrice    = parseFloat(risk.stopLoss || 0);

if (liveTrading !== 'true') {
  return [{ json: { status: 'PAPER_ONLY', signal, confidence, minConf } }];
}
if (!['BUY', 'SELL'].includes(signal) || confidence < minConf) {
  return [{ json: { status: 'SKIPPED', reason: `Signal=${signal} conf=${confidence}%<${minConf}%` } }];
}

// ═══ BALANCE ═════════════════════════════════════════════════
const bal    = $('Fetch Balance').first().json;
const xbtBal = parseFloat((bal && bal.result && bal.result.XXBT) || 0);
const usdBal = parseFloat((bal && bal.result && bal.result.ZUSD) || 0);

// ═══ POSITION STATE ══════════════════════════════════════════
const pastRaw = $('Read Past Signals').first().json;
const mems    = (pastRaw && pastRaw.memories) || [];
const posMem  = mems.find(m => (m.content || '').includes('POSITION_STATE_BTC:'));
const posStr  = posMem ? (posMem.content || '') : '';
const rawPos  = posStr.includes('LONG') ? 'LONG' : 'FLAT';

// Reconcile: if vault says LONG but balance is gone, stop-loss fired externally
const currentPosition = (rawPos === 'LONG' && xbtBal < 0.0001) ? 'FLAT' : rawPos;

// Extract stored SL order ID for cancellation on SELL
const slMatch    = posStr.match(/slOrder=([\w-]+)/);
const storedSlId = (slMatch && slMatch[1] !== 'none') ? slMatch[1] : null;

if (signal === 'BUY' && currentPosition === 'LONG') {
  return [{ json: { status: 'SKIPPED', reason: 'Already LONG — await SELL signal or SL/TP', currentPosition, xbtBal } }];
}
if (signal === 'SELL' && currentPosition === 'FLAT') {
  return [{ json: { status: 'SKIPPED', reason: 'Already FLAT — no BTC to sell', currentPosition, xbtBal } }];
}

// ═══ POSITION SIZING (2x at ≥85% confidence) ═════════════════
const sizePct = confidence >= 85 ? Math.min(basePct * 2, 0.20) : basePct;
const volume  = signal === 'BUY'
  ? ((usdBal * sizePct) / price).toFixed(8)
  : (xbtBal * sizePct).toFixed(8);

if (parseFloat(volume) < 0.0001) {
  return [{ json: { status: 'SKIPPED', reason: `Vol ${volume} < 0.0001 BTC min`, usdBal, xbtBal, sizePct } }];
}

// ═══ KRAKEN HELPERS ══════════════════════════════════════════
function sign(path, body, nonce) {
  const h = crypto.createHash('sha256').update(nonce + body).digest();
  const m = crypto.createHmac('sha512', Buffer.from(apiSecret, 'base64'));
  m.update(Buffer.from(path)); m.update(h);
  return m.digest('base64');
}
async function kPost(path, params) {
  const nonce = Date.now().toString();
  const body  = `nonce=${nonce}&${params}`;
  const sig   = sign(path, body, nonce);
  return new Promise(res => {
    const req = https.request({
      hostname: 'api.kraken.com', path, method: 'POST',
      headers: { 'API-Key': apiKey, 'API-Sign': sig, 'Content-Type': 'application/x-www-form-urlencoded', 'Content-Length': Buffer.byteLength(body) }
    }, r => { let d = ''; r.on('data', c => d += c); r.on('end', () => { try { res(JSON.parse(d)); } catch(e) { res({ error: [d.slice(0,80)] }); } }); });
    req.on('error', e => res({ error: [e.message] }));
    req.setTimeout(10000, () => { req.destroy(); res({ error: ['timeout'] }); });
    req.write(body); req.end();
  });
}
async function vaultWrite(content, tags) {
  const payload = JSON.stringify({ memories: [{ content, memory_type: 'episodic', importance: 5, source: 'crypto_specialist_v3', session_id: `pos_${Date.now()}`, tags }] });
  return new Promise(res => {
    const req = https.request({
      hostname: 'inspiring-cat-production.up.railway.app', path: '/memory/ingest', method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Memory-Secret': '2dc6c69574c14d615ce146e54640aa13030dade7aa86697c', 'Content-Length': Buffer.byteLength(payload) }
    }, r => { r.on('data', () => {}); r.on('end', res); });
    req.on('error', res); req.setTimeout(5000, () => { req.destroy(); res(); });
    req.write(payload); req.end();
  });
}

const out = { entryOrderId: null, slOrderId: null, cancelledSl: false, errors: [] };

// ═══ CANCEL OPEN SL WHEN CLOSING LONG ════════════════════════
if (signal === 'SELL' && storedSlId) {
  const cancelRes = await kPost('/0/private/CancelOrder', `txid=${storedSlId}`);
  out.cancelledSl = !(cancelRes.error && cancelRes.error.length > 0);
}

// ═══ ENTRY ORDER (market) ════════════════════════════════════
const entryRes = await kPost('/0/private/AddOrder',
  `pair=XBTUSD&type=${signal === 'BUY' ? 'buy' : 'sell'}&ordertype=market&volume=${volume}`
);
if (entryRes.error && entryRes.error.length > 0) {
  return [{ json: { status: 'ERROR', error: entryRes.error[0], signal, volume, price, out } }];
}
out.entryOrderId = entryRes.result && entryRes.result.txid && entryRes.result.txid[0];

// ═══ STOP-LOSS ORDER (BUY entry only) ════════════════════════
// Only one open order at a time — TP handled by next SELL signal
if (signal === 'BUY' && slPrice > 0) {
  const slLimit = (slPrice * 0.9985).toFixed(1); // limit 0.15% below trigger
  const slRes   = await kPost('/0/private/AddOrder',
    `pair=XBTUSD&type=sell&ordertype=stop-loss-limit&price=${slPrice.toFixed(1)}&price2=${slLimit}&volume=${volume}`
  );
  if (slRes.error && slRes.error.length > 0) { out.errors.push(`SL: ${slRes.error[0]}`); }
  else { out.slOrderId = slRes.result && slRes.result.txid && slRes.result.txid[0]; }
}

// ═══ WRITE POSITION STATE TO VAULT ═══════════════════════════
const newPos = signal === 'BUY' ? 'LONG' : 'FLAT';
await vaultWrite(
  `POSITION_STATE_BTC: ${newPos} | entry=${price.toFixed(1)} | vol=${volume} | conf=${confidence}% | slOrder=${out.slOrderId || 'none'} | ts=${new Date().toISOString()}`,
  ['crypto_signal', 'btc', 'position_state']
);

return [{ json: {
  status: 'EXECUTED', signal,
  volume: parseFloat(volume), price, confidence,
  sizePct: (sizePct * 100).toFixed(1) + '%',
  positionBefore: currentPosition, positionAfter: newPos,
  ...out, timestamp: new Date().toISOString()
}}];
