// live.js — Live super-agent integration.
//
// Polls /n8n/workflows + /dashboard/agents/status; subscribes to /activity/stream SSE.
// Every backend signal is mapped onto a bot via a name-match heuristic.
// All endpoints fail-soft: if super-agent is unreachable, simulation falls
// back to deterministic demo cadence.

const N8N_POLL_MS  = 30_000;
const DASH_POLL_MS = 5_000;

export class LiveFeed {
  constructor(bots, hud) {
    this.bots = bots;
    this.hud = hud;
    this.mode = 'live';
    this.es = null;
    this.timers = [];
    this.connected = false;
    this.onConnChange = () => {};
    this.matchers = this._buildMatchers();
  }

  setMode(mode) {
    this.mode = mode;
    if (mode === 'demo') this.stop();
    else this.start();
  }

  start() {
    if (this.mode !== 'live') return;
    this._pollWorkflows();
    this._pollAgentsStatus();
    this._connectStream();
    this.timers.push(setInterval(() => this._pollWorkflows(), N8N_POLL_MS));
    this.timers.push(setInterval(() => this._pollAgentsStatus(), DASH_POLL_MS));
  }

  stop() {
    this.timers.forEach(t => clearInterval(t));
    this.timers = [];
    if (this.es) { try { this.es.close(); } catch {} this.es = null; }
    this._setConn(false);
  }

  _setConn(ok) {
    if (ok === this.connected) return;
    this.connected = ok;
    this.onConnChange(ok);
  }

  _buildMatchers() {
    // Map a backend identifier (workflow name, agent id, activity-line snippet)
    // to a bot id. Heuristic: lower-case substring match against bot.id, name,
    // or role.
    const m = {};
    for (const bot of this.bots) {
      const tags = [
        bot.id.toLowerCase(),
        bot.name.toLowerCase(),
        (bot.role || '').toLowerCase(),
      ].filter(Boolean);
      m[bot.id] = tags;
    }
    return m;
  }

  _matchBot(text) {
    if (!text) return null;
    const lower = text.toLowerCase();
    for (const bot of this.bots) {
      for (const tag of this.matchers[bot.id]) {
        if (tag && lower.includes(tag)) return bot;
      }
    }
    return null;
  }

  async _pollWorkflows() {
    try {
      const r = await fetch('/n8n/workflows', { headers: { 'Accept': 'application/json' } });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const json = await r.json();
      const workflows = json.workflows || json.data || (Array.isArray(json) ? json : []);
      const activeBots = new Set();
      for (const wf of workflows) {
        if (!wf.active) continue;
        const bot = this._matchBot(wf.name);
        if (bot) activeBots.add(bot.id);
      }
      for (const bot of this.bots) bot.activeFlag = activeBots.has(bot.id);
      this._setConn(true);
    } catch (e) {
      this._setConn(false);
    }
  }

  async _pollAgentsStatus() {
    try {
      const r = await fetch('/dashboard/agents/status');
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      // Just touching it confirms connectivity — actual data isn't strictly
      // needed for the visual layer, since /n8n/workflows already gates the
      // halo. (Future: per-bot health pip from worker rows.)
      this._setConn(true);
    } catch (e) { /* fall-soft */ }
  }

  _connectStream() {
    try {
      this.es = new EventSource('/activity/stream');
      this.es.onmessage = (ev) => {
        let data = ev.data;
        try { const j = JSON.parse(ev.data); data = j.text || j.message || j.line || ev.data; } catch {}
        if (!data) return;
        this.hud.pushActivity(String(data).slice(0, 220));
        const bot = this._matchBot(String(data));
        if (bot) bot.taskLabel = String(data).split('\n')[0].slice(0, 60);
      };
      this.es.onopen = () => this._setConn(true);
      this.es.onerror = () => this._setConn(false);
    } catch (e) {
      this._setConn(false);
    }
  }
}
