// live.js — Live super-agent integration.
//
// Polls /n8n/workflows + /dashboard/agents/status; subscribes to /activity/stream SSE.
// Every backend signal is mapped onto a bot via a name-match heuristic.
// All endpoints fail-soft: if super-agent is unreachable, simulation falls
// back to deterministic demo cadence.

const N8N_POLL_MS     = 30_000;
const DASH_POLL_MS    = 5_000;
const PENDING_POLL_MS = 30_000;

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
    // pending[agentId] = count of open proposals addressed to that bot
    this.pendingCounts = {};
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
    this._pollPending();
    this._connectStream();
    this.timers.push(setInterval(() => this._pollWorkflows(), N8N_POLL_MS));
    this.timers.push(setInterval(() => this._pollAgentsStatus(), DASH_POLL_MS));
    this.timers.push(setInterval(() => this._pollPending(), PENDING_POLL_MS));
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

  async _pollPending() {
    try {
      const r = await fetch('/dashboard/bridge/pending', { headers: { 'Accept': 'application/json' } });
      if (!r.ok) return;
      const json = await r.json();
      const proposals = json.proposals || [];

      // Count proposals per to_agent
      const counts = {};
      for (const p of proposals) {
        if (p.to_agent) counts[p.to_agent] = (counts[p.to_agent] || 0) + 1;
      }
      this.pendingCounts = counts;

      // Update badge dots on HUD bot-list rows.
      // Each <li> has data-bot-id matching bots.json id (e.g. "ceo", "cto").
      // We add/remove a <span class="pending-badge"> showing the count.
      for (const bot of this.bots) {
        const li = document.querySelector(`[data-bot-id="${bot.id}"]`);
        if (!li) continue;
        const count = counts[bot.id] || 0;
        let badge = li.querySelector('.pending-badge');
        if (count > 0) {
          if (!badge) {
            badge = document.createElement('span');
            badge.className = 'pending-badge';
            badge.title = `${count} pending proposal${count > 1 ? 's' : ''}`;
            li.appendChild(badge);
          }
          badge.textContent = count;
          badge.style.cssText =
            'display:inline-flex;align-items:center;justify-content:center;' +
            'min-width:16px;height:16px;border-radius:8px;font-size:9px;font-weight:700;' +
            'background:#ff2d55;color:#fff;margin-left:6px;padding:0 4px;' +
            'animation:pendingPulse 1.5s infinite;flex-shrink:0';
        } else if (badge) {
          badge.remove();
        }
      }

      // Inject keyframe if not already present
      if (!document.getElementById('pending-pulse-style')) {
        const s = document.createElement('style');
        s.id = 'pending-pulse-style';
        s.textContent = '@keyframes pendingPulse{0%,100%{opacity:1}50%{opacity:0.5}}';
        document.head.appendChild(s);
      }
    } catch (e) { /* fail-soft */ }
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
