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
    this.onInteraction = () => {};   // wired in main.js → scheduler.triggerInteraction
    this.matchers = this._buildMatchers();
    // pending[agentId] = count of open proposals addressed to that bot
    this.pendingCounts = {};
    this._seenMemoIds = new Set();   // memo_ids already turned into interactions
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

  // Find a second bot in text, excluding one already matched.
  _matchBotExcluding(text, exclude) {
    if (!text) return null;
    const lower = text.toLowerCase();
    for (const bot of this.bots) {
      if (bot === exclude) continue;
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

      // Cache full proposal list for the modal
      this._pendingProposals = proposals;

      // Count proposals per to_agent
      const counts = {};
      for (const p of proposals) {
        if (p.to_agent) counts[p.to_agent] = (counts[p.to_agent] || 0) + 1;
      }
      this.pendingCounts = counts;

      // Fire office-sim interactions for each unseen pending proposal.
      const activeIds = new Set(proposals.map(p => p.memo_id).filter(Boolean));
      // Remove expired memo_ids
      for (const id of this._seenMemoIds) {
        if (!activeIds.has(id)) this._seenMemoIds.delete(id);
      }
      for (const p of proposals) {
        if (!p.memo_id || this._seenMemoIds.has(p.memo_id)) continue;
        if (!p.from_agent || !p.to_agent) continue;
        const botA = this.bots.find(b => b.id === p.from_agent);
        const botB = this.bots.find(b => b.id === p.to_agent);
        if (botA && botB) {
          this._seenMemoIds.add(p.memo_id);
          this.onInteraction(botA, botB, {
            type: p.memo_type || 'proposal',
            priority: p.priority,
            subject: p.subject,
          });
        }
      }

      // Update badge dots on HUD bot-list rows — clicking opens approval modal.
      for (const bot of this.bots) {
        const li = document.querySelector(`[data-bot-id="${bot.id}"]`);
        if (!li) continue;
        const count = counts[bot.id] || 0;
        let badge = li.querySelector('.pending-badge');
        if (count > 0) {
          if (!badge) {
            badge = document.createElement('span');
            badge.className = 'pending-badge';
            li.appendChild(badge);
          }
          badge.textContent = count;
          badge.title = `${count} pending proposal${count > 1 ? 's' : ''} — click to review`;
          badge.style.cssText =
            'display:inline-flex;align-items:center;justify-content:center;cursor:pointer;' +
            'min-width:16px;height:16px;border-radius:8px;font-size:9px;font-weight:700;' +
            'background:#ff2d55;color:#fff;margin-left:6px;padding:0 4px;' +
            'animation:pendingPulse 1.5s infinite;flex-shrink:0';
          badge.onclick = (e) => { e.stopPropagation(); this._openApprovalModal(bot.id); };
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

  _openApprovalModal(botId) {
    const modal   = document.getElementById('approval-modal');
    const content = document.getElementById('modal-proposals');
    const closeBtn = document.getElementById('modal-close');
    if (!modal || !content) return;

    const proposals = (this._pendingProposals || []).filter(p => !botId || p.to_agent === botId);

    const PRIORITY_COLOUR = { urgent: '#ef4444', high: '#f59e0b', medium: '#3b82f6', low: '#64748b' };
    const TYPE_ICON = {
      approval_request:       '📋',
      cro_review_request:     '💰',
      cto_review_request:     '🖥️',
      bot_improvement_proposal:'💡',
    };

    if (!proposals.length) {
      content.innerHTML = '<p style="color:#64748b;font-size:.85rem;text-align:center;padding:1rem">No pending proposals for this agent.</p>';
    } else {
      content.innerHTML = proposals.map((p, idx) => {
        const typeIcon  = TYPE_ICON[p.memo_type] || '📝';
        const priColor  = PRIORITY_COLOUR[p.priority] || '#64748b';
        const ageText   = p.hours_old < 1 ? `${Math.round(p.hours_old * 60)}m ago`
                        : p.hours_old < 24 ? `${p.hours_old}h ago`
                        : `${(p.hours_old/24).toFixed(1)}d ago`;

        // Extract meaningful body preview
        const body = p.body || {};
        const bodyLines = [];
        if (body.description)  bodyLines.push(body.description);
        if (body.proposed_fix) bodyLines.push(`💡 Fix: ${body.proposed_fix}`);
        if (body.cro_score)    bodyLines.push(`📈 CRO score: ${body.cro_score}`);
        if (body.risk_level)   bodyLines.push(`⚠️ Risk: ${body.risk_level}`);
        const bodyHtml = bodyLines.slice(0,3).map(l =>
          `<p style="color:#94a3b8;font-size:.78rem;margin:.25rem 0">${l.slice(0,200)}</p>`
        ).join('');

        return `
        <div id="proposal-card-${p.memo_id}" style="border:1px solid #1e3048;border-radius:8px;
          padding:.85rem 1rem;margin-bottom:.75rem;background:#0a1628">
          <div style="display:flex;align-items:flex-start;gap:.5rem;margin-bottom:.4rem">
            <span style="font-size:1.1rem;flex-shrink:0">${typeIcon}</span>
            <div style="flex:1;min-width:0">
              <div style="font-weight:600;font-size:.9rem;color:#e0e8f0;line-height:1.3">${p.subject}</div>
              <div style="display:flex;gap:.5rem;margin-top:.3rem;flex-wrap:wrap">
                <span style="background:${priColor}22;color:${priColor};border:1px solid ${priColor}44;
                  padding:1px 7px;border-radius:10px;font-size:.7rem;font-weight:700">${p.priority.toUpperCase()}</span>
                <span style="color:#475569;font-size:.7rem">from ${p.from_agent || '?'} → ${p.to_agent}</span>
                <span style="color:#334155;font-size:.7rem">${ageText}</span>
              </div>
            </div>
          </div>
          ${bodyHtml}
          <div style="display:flex;gap:.5rem;margin-top:.7rem">
            <button onclick="window._approveProposal('${p.memo_id}')"
              style="flex:1;padding:.45rem;background:#10b981;color:#fff;border:none;border-radius:6px;
                cursor:pointer;font-weight:700;font-size:.8rem">✅ Approve</button>
            <button onclick="window._rejectProposal('${p.memo_id}')"
              style="flex:1;padding:.45rem;background:#ef4444;color:#fff;border:none;border-radius:6px;
                cursor:pointer;font-weight:700;font-size:.8rem">❌ Reject</button>
          </div>
          <div id="proposal-feedback-${p.memo_id}" style="margin-top:.4rem;font-size:.75rem;color:#64748b;min-height:1rem"></div>
        </div>`;
      }).join('');
    }

    // Wire up global approve/reject handlers
    const self = this;
    window._approveProposal = async (memoId) => {
      const fb = document.getElementById(`proposal-feedback-${memoId}`);
      if (fb) fb.textContent = 'Approving…';
      try {
        const res = await fetch(`/dashboard/bridge/proposals/${memoId}/approve`, { method: 'POST' });
        const data = await res.json();
        if (data.ok) {
          const card = document.getElementById(`proposal-card-${memoId}`);
          if (card) { card.style.opacity = '.4'; card.style.pointerEvents = 'none'; }
          if (fb) { fb.textContent = '✅ Approved!'; fb.style.color = '#10b981'; }
          // Refresh pending after short delay
          setTimeout(() => self._pollPending(), 1500);
        } else {
          if (fb) { fb.textContent = `Error: ${data.error || 'failed'}`; fb.style.color = '#ef4444'; }
        }
      } catch (e) {
        if (fb) { fb.textContent = 'Network error'; fb.style.color = '#ef4444'; }
      }
    };

    window._rejectProposal = async (memoId) => {
      const fb = document.getElementById(`proposal-feedback-${memoId}`);
      if (fb) fb.textContent = 'Rejecting…';
      try {
        const res = await fetch(`/dashboard/bridge/proposals/${memoId}/reject`, { method: 'POST' });
        const data = await res.json();
        if (data.ok) {
          const card = document.getElementById(`proposal-card-${memoId}`);
          if (card) { card.style.opacity = '.4'; card.style.pointerEvents = 'none'; }
          if (fb) { fb.textContent = '❌ Rejected.'; fb.style.color = '#ef4444'; }
          setTimeout(() => self._pollPending(), 1500);
        } else {
          if (fb) { fb.textContent = `Error: ${data.error || 'failed'}`; fb.style.color = '#ef4444'; }
        }
      } catch (e) {
        if (fb) { fb.textContent = 'Network error'; fb.style.color = '#ef4444'; }
      }
    };

    // Show modal
    modal.style.display = 'flex';
    closeBtn.onclick = () => { modal.style.display = 'none'; };
    modal.onclick = (e) => { if (e.target === modal) modal.style.display = 'none'; };
  }

  _connectStream() {
    try {
      this.es = new EventSource('/activity/stream');
      this.es.onmessage = (ev) => {
        let data = ev.data;
        try { const j = JSON.parse(ev.data); data = j.text || j.message || j.line || ev.data; } catch {}
        if (!data) return;
        this.hud.pushActivity(String(data).slice(0, 220));
        const botA = this._matchBot(String(data));
        if (botA) {
          botA.taskLabel = String(data).split('\n')[0].slice(0, 60);
          // If a second bot is mentioned in the same message, trigger an interaction.
          const botB = this._matchBotExcluding(String(data), botA);
          if (botB) {
            this.onInteraction(botA, botB, { type: 'activity', subject: String(data).slice(0, 80) });
          }
        }
      };
      this.es.onopen = () => this._setConn(true);
      this.es.onerror = () => this._setConn(false);
    } catch (e) {
      this._setConn(false);
    }
  }
}
