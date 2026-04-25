// renderer.js — Canvas drawing, camera, y-sort, HUD update.
//
// Coordinate model:
//   - World is a 64x40 tile grid per floor.
//   - The floor PNG is drawn to fill the canvas at any zoom; tile (0,0) maps
//     to the canvas top-left corner of the visible floor.
//   - Bots are drawn at floor coords, scaled to a sprite size that matches
//     the visual scale of the floor map's furniture.

import { TILE_W, TILE_H } from './world.js';
import { standFrameName, walkCycleFrame } from './sprites.js';

const SPRITE_DRAW_W = 56;       // px on canvas — readable but not swamping furniture
const SPRITE_DRAW_H = 56;
const WALK_FRAME_MS = 280;      // calmer pace; matches the slower STEP_MS
const WALK_BOB_AMP = 2;         // px vertical bob amplitude when walking
const WALK_BOB_HZ = 3.0;        // bobs per second

export class Renderer {
  constructor(canvas, floors, sprites, scheduler, bots) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.floors = floors;
    this.sprites = sprites;
    this.scheduler = scheduler;
    this.bots = bots;
    this.activeFloor = 2;
    this.bgImages = {};
    this.bgFallback = {};
    this.focusedBotId = null;
    this.followFocused = false;     // when true, switch floors automatically to follow the focused bot
    this.debugGrid = false;         // 'g' key toggles
    this.bubbles = new Map();       // botId -> { text, until }
    this.dpr = window.devicePixelRatio || 1;
    this._resize();
    window.addEventListener('resize', () => this._resize());
  }

  toggleDebugGrid() { this.debugGrid = !this.debugGrid; }

  // Schedule a transient label above a bot (e.g., on meeting start).
  flashBubble(botId, text, durationMs = 4000) {
    this.bubbles.set(botId, { text, until: performance.now() + durationMs });
  }

  async init() {
    // Load all 3 floor backgrounds in parallel using img.decode() — sequential
    // await on the 2.7-2.8 MB PNGs sometimes fired onload before naturalWidth
    // was set, leaving floors 2/3 stuck on the fallback solid colour.
    await Promise.all([1, 2, 3].map(id => this._loadFloorBg(id)));
  }

  async _loadFloorBg(id) {
    const path = `assets/floors/level${id}.png`;
    try {
      const img = new Image();
      img.src = path;
      await img.decode();
      this.bgImages[id] = img;
    } catch (e) {
      console.warn(`[renderer] floor ${id} ${path} primary load failed; retrying via blob`);
      try {
        const resp = await fetch(path, { cache: 'reload' });
        const blob = await resp.blob();
        const img = new Image();
        img.src = URL.createObjectURL(blob);
        await img.decode();
        this.bgImages[id] = img;
      } catch (e2) {
        console.error(`[renderer] floor ${id} unrecoverable:`, e2);
        this.bgFallback[id] = ['#1a3a4a', '#3a2a4a', '#4a3a2a'][id-1] || '#222';
      }
    }
  }

  setActiveFloor(id) {
    this.activeFloor = id;
    document.getElementById('floor-label').textContent = `Level ${id}`;
    document.querySelectorAll('.floor-pill').forEach(b => b.classList.toggle('active', +b.dataset.floor === id));
  }

  setFocusedBot(id) { this.focusedBotId = id; }

  _resize() {
    const cw = this.canvas.clientWidth;
    const ch = this.canvas.clientHeight;
    this.canvas.width = Math.floor(cw * this.dpr);
    this.canvas.height = Math.floor(ch * this.dpr);
    this.ctx.imageSmoothingEnabled = true;
  }

  // World tile -> canvas px.
  tileToPx(tx, ty) {
    const w = this.canvas.width, h = this.canvas.height;
    const px = (tx + 0.5) * (w / TILE_W);
    const py = (ty + 0.5) * (h / TILE_H);
    return { px, py };
  }

  // Inverse: canvas px -> tile.
  pxToTile(px, py) {
    const w = this.canvas.width, h = this.canvas.height;
    return {
      x: Math.floor(px * this.dpr / (w / TILE_W)),
      y: Math.floor(py * this.dpr / (h / TILE_H)),
    };
  }

  draw(now) {
    const ctx = this.ctx;
    const w = this.canvas.width, h = this.canvas.height;
    ctx.clearRect(0, 0, w, h);

    // Camera follow: if a bot is focused and `followFocused` is on, switch to their floor.
    if (this.followFocused && this.focusedBotId) {
      const focusBot = this.bots.find(b => b.id === this.focusedBotId);
      if (focusBot && focusBot.floor !== this.activeFloor) {
        this.setActiveFloor(focusBot.floor);
      }
    }

    // Draw floor background.
    const bg = this.bgImages[this.activeFloor];
    if (bg) {
      ctx.drawImage(bg, 0, 0, w, h);
    } else {
      ctx.fillStyle = this.bgFallback[this.activeFloor] || '#222';
      ctx.fillRect(0, 0, w, h);
    }

    // Optional debug grid overlay (toggled by 'g').
    if (this.debugGrid) this._drawTileGrid(ctx, w, h);

    // Y-sort bots on this floor.
    const onFloor = this.bots.filter(b => b.floor === this.activeFloor);
    onFloor.sort((a, b) => a.y - b.y);

    for (const bot of onFloor) this._drawBot(ctx, bot, now);

    // Draw room reservations as soft halos (debug-ish but pretty).
    if (this.scheduler) this._drawMeetingHalos(ctx);
  }

  _drawTileGrid(ctx, w, h) {
    ctx.strokeStyle = 'rgba(255,255,255,0.04)';
    ctx.lineWidth = 1;
    const tw = w / TILE_W, th = h / TILE_H;
    for (let x = 0; x <= TILE_W; x++) {
      ctx.beginPath(); ctx.moveTo(x*tw, 0); ctx.lineTo(x*tw, h); ctx.stroke();
    }
    for (let y = 0; y <= TILE_H; y++) {
      ctx.beginPath(); ctx.moveTo(0, y*th); ctx.lineTo(w, y*th); ctx.stroke();
    }
    // Mark blocked tiles in red, doors in gold, stairs in cyan.
    const floor = this.floors[this.activeFloor];
    for (let y = 0; y < TILE_H; y++) {
      for (let x = 0; x < TILE_W; x++) {
        const c = floor.tileAt(x, y);
        if (c === '#') { ctx.fillStyle='rgba(255,80,100,0.10)'; ctx.fillRect(x*tw, y*th, tw, th); }
        else if (c === 'D') { ctx.fillStyle='rgba(201,169,110,0.25)'; ctx.fillRect(x*tw, y*th, tw, th); }
        else if (c === 'U' || c === 'N') { ctx.fillStyle='rgba(108,208,255,0.25)'; ctx.fillRect(x*tw, y*th, tw, th); }
      }
    }
  }

  _drawBot(ctx, bot, now) {
    const { px, py } = this.tileToPx(bot.x, bot.y);
    // Pick directional frame + decide if the renderer should flip horizontally.
    let dirName, mirror = false, frameIndex = 0;
    if (bot.state === 'walking') {
      const cycle = walkCycleFrame(bot.facing, now, this.sprites, bot, WALK_FRAME_MS);
      dirName = cycle.dirName;
      mirror = cycle.mirror;
      frameIndex = cycle.frameIndex || 0;
    } else {
      dirName = standFrameName(bot.facing);
    }
    const f = this.sprites.frame(bot, dirName, frameIndex);
    // Reference-scale rendering: the bot's stand frame is treated as the
    // canonical "character size". Every other frame is scaled at the same
    // pixels-per-source-px so walk frames with extended legs naturally
    // grow upward (extra canvas height = visible leg motion) without
    // making the bot's body strobe between frames.
    const ref = this.sprites.refScale?.(bot.id);
    let drawW, drawH;
    if (ref) {
      const refDrawH = SPRITE_DRAW_H * this.dpr;        // on-screen height of the stand frame
      const k = refDrawH / ref.refH;                    // pixels per source-px
      drawW = (f.sw || ref.refW) * k;
      drawH = (f.sh || ref.refH) * k;
    } else {
      const aspect = (f.sw && f.sh) ? f.sw / f.sh : 1;
      drawW = (aspect >= 1 ? SPRITE_DRAW_W : SPRITE_DRAW_W * aspect) * this.dpr;
      drawH = (aspect >= 1 ? SPRITE_DRAW_H / aspect : SPRITE_DRAW_H) * this.dpr;
    }
    const dw = drawW;
    const dh = drawH;
    // Walk bob: subtle vertical sin-wave while walking so even with a single
    // stand/walk frame pair the motion reads as actual locomotion.
    const bob = bot.state === 'walking'
      ? -Math.abs(Math.sin(now * 0.001 * Math.PI * WALK_BOB_HZ)) * WALK_BOB_AMP * this.dpr
      : 0;
    const dx = px - dw/2;
    const dy = py - dh + 4 * this.dpr + bob;     // anchor feet near tile center

    // Drop shadow stays put on the ground while the body bobs above it.
    ctx.fillStyle = 'rgba(0,0,0,0.30)';
    ctx.beginPath();
    ctx.ellipse(px, py + 3 * this.dpr, dw*0.30, dh*0.08, 0, 0, Math.PI*2);
    ctx.fill();

    // Active-workflow halo.
    if (bot.activeFlag) {
      ctx.save();
      ctx.shadowColor = '#58e7a4';
      ctx.shadowBlur = 16 * this.dpr;
      ctx.strokeStyle = 'rgba(88,231,164,0.4)';
      ctx.lineWidth = 2 * this.dpr;
      ctx.beginPath(); ctx.arc(px, py - dh*0.45, dw*0.32, 0, Math.PI*2); ctx.stroke();
      ctx.restore();
    }

    if (bot.state === 'transit') {
      ctx.globalAlpha = 0.4 + 0.6 * Math.abs(Math.sin(now / 150));
    }
    if (f.img) {
      // dx/dy already anchor the sprite's bottom-centre at the bot's tile.
      // For ref-scaled frames, dh is the FRAME's natural height × k —
      // taller walk frames will reach further upward, but the ground
      // line stays at py.
      if (mirror) {
        ctx.save();
        ctx.translate(dx + dw/2, 0);
        ctx.scale(-1, 1);
        ctx.drawImage(f.img, f.sx, f.sy, f.sw, f.sh, -dw/2, dy, dw, dh);
        ctx.restore();
      } else {
        ctx.drawImage(f.img, f.sx, f.sy, f.sw, f.sh, dx, dy, dw, dh);
      }
    }
    ctx.globalAlpha = 1;

    // Focused-bot ring.
    if (this.focusedBotId === bot.id) {
      ctx.strokeStyle = 'rgba(108,208,255,0.9)';
      ctx.lineWidth = 2 * this.dpr;
      ctx.beginPath();
      ctx.ellipse(px, py + 4 * this.dpr, dw*0.32, dh*0.10, 0, 0, Math.PI*2);
      ctx.stroke();
    }

    // Name label when focused or in meeting.
    if (this.focusedBotId === bot.id || bot.state === 'inMeeting') {
      ctx.font = `${12 * this.dpr}px ui-monospace,monospace`;
      const labelW = ctx.measureText(bot.name).width + 12 * this.dpr;
      ctx.fillStyle = '#0a081299';
      ctx.fillRect(px - labelW/2, dy - 22 * this.dpr, labelW, 18 * this.dpr);
      ctx.fillStyle = '#e8e7f0';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText(bot.name, px, dy - 13 * this.dpr);
    }

    // Speech bubble (e.g., flashed on meeting entry).
    const bub = this.bubbles.get(bot.id);
    if (bub) {
      if (now > bub.until) {
        this.bubbles.delete(bot.id);
      } else {
        ctx.font = `${11 * this.dpr}px ui-monospace,monospace`;
        const bubW = ctx.measureText(bub.text).width + 14 * this.dpr;
        const bubH = 18 * this.dpr;
        const bubY = dy - 42 * this.dpr;
        ctx.fillStyle = 'rgba(15,12,28,0.95)';
        ctx.strokeStyle = 'rgba(201,169,110,0.6)';
        ctx.lineWidth = 1 * this.dpr;
        ctx.beginPath();
        ctx.roundRect(px - bubW/2, bubY, bubW, bubH, 5 * this.dpr);
        ctx.fill();
        ctx.stroke();
        // Tail.
        ctx.beginPath();
        ctx.moveTo(px - 4 * this.dpr, bubY + bubH);
        ctx.lineTo(px,                bubY + bubH + 5 * this.dpr);
        ctx.lineTo(px + 4 * this.dpr, bubY + bubH);
        ctx.closePath();
        ctx.fill();
        ctx.stroke();
        ctx.fillStyle = '#c9a96e';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.fillText(bub.text, px, bubY + bubH/2);
      }
    }
  }

  _drawMeetingHalos(ctx) {
    const w = this.canvas.width, h = this.canvas.height;
    const tw = w / TILE_W, th = h / TILE_H;
    for (const m of this.scheduler.activeMeetings) {
      if (m.room.floor !== this.activeFloor) continue;
      const [zx, zy, zw, zh] = m.room.bounds;
      ctx.fillStyle = 'rgba(88,231,164,0.10)';
      ctx.fillRect(zx*tw, zy*th, zw*tw, zh*th);
      ctx.strokeStyle = 'rgba(88,231,164,0.4)';
      ctx.lineWidth = 2;
      ctx.strokeRect(zx*tw, zy*th, zw*tw, zh*th);
    }
  }
}

// HUD update is its own concern. Pure DOM, no canvas.
export class HUD {
  constructor(bots, scheduler, renderer) {
    this.bots = bots;
    this.scheduler = scheduler;
    this.renderer = renderer;
    this.botListEl = document.getElementById('bot-list');
    this.activityEl = document.getElementById('activity-log');
    this.scheduleEl = document.getElementById('schedule-list');
    this.statusModeEl = document.getElementById('status-mode');
    this.statusConnEl = document.getElementById('status-conn');
    this.statusFpsEl  = document.getElementById('status-fps');
    this.statusBotsEl = document.getElementById('status-bots');
    this.clockEl      = document.getElementById('clock');
    this.activityRing = [];
    this.maxActivity = 30;

    // Build bot rows once; update in-place.
    this.botRows = new Map();
    for (const bot of bots) {
      const li = document.createElement('li');
      li.className = 'bot-row';
      li.dataset.botId = bot.id;
      li.innerHTML = `
        <span class="bot-pip idle"></span>
        <span class="bot-name">${bot.name}</span>
        <span class="bot-state">idle</span>
      `;
      li.addEventListener('click', () => {
        renderer.setFocusedBot(bot.id);
        renderer.setActiveFloor(bot.floor);
        renderer.followFocused = true;
        document.querySelectorAll('.bot-row').forEach(el => el.classList.remove('focused'));
        li.classList.add('focused');
      });
      this.botListEl.appendChild(li);
      this.botRows.set(bot.id, li);
    }
  }

  pushActivity(text, fresh = true) {
    this.activityRing.unshift({ text, fresh, at: Date.now() });
    while (this.activityRing.length > this.maxActivity) this.activityRing.pop();
  }

  updateClock(simTime) {
    const d = new Date(simTime);
    const hh = String(d.getUTCHours()).padStart(2,'0');
    const mm = String(d.getUTCMinutes()).padStart(2,'0');
    this.clockEl.textContent = `${hh}:${mm} UTC`;
  }

  updateBotList() {
    for (const bot of this.bots) {
      const li = this.botRows.get(bot.id);
      if (!li) continue;
      const pip = li.querySelector('.bot-pip');
      const state = li.querySelector('.bot-state');
      const cls = bot.state === 'walking' ? 'walking'
                : bot.state === 'inMeeting' ? 'inMeeting'
                : bot.state === 'transit' ? 'transit'
                : bot.state === 'social' ? 'social'
                : bot.state === 'atDesk' ? 'atDesk' : 'idle';
      pip.className = `bot-pip ${cls}`;
      const taskBit = bot.taskLabel ? ` · ${bot.taskLabel}` : '';
      state.textContent = `L${bot.floor} ${bot.stateLabel()}${taskBit}`;
    }
  }

  updateActivity() {
    this.activityEl.innerHTML = '';
    for (const e of this.activityRing) {
      const li = document.createElement('li');
      if (e.fresh) li.classList.add('fresh');
      const ago = Math.floor((Date.now() - e.at) / 1000);
      li.textContent = `${ago}s · ${e.text}`;
      this.activityEl.appendChild(li);
    }
  }

  updateSchedule() {
    this.scheduleEl.innerHTML = '';
    const ups = this.scheduler.upcoming(5);
    for (let i = 0; i < ups.length; i++) {
      const u = ups[i];
      const li = document.createElement('li');
      if (i === 0 && u.ms <= 5*60*1000) li.classList.add('next');
      const mins = Math.round(u.ms / 60000);
      const when = mins <= 0 ? 'NOW' : `+${mins}m`;
      li.textContent = `${u.ev.time}  ${when}  ${u.ev.label || u.ev.id}`;
      this.scheduleEl.appendChild(li);
    }
  }

  updateStatus(mode, conn, fps, botCount) {
    this.statusModeEl.textContent = `mode: ${mode}`;
    this.statusConnEl.textContent = `conn: ${conn}`;
    this.statusFpsEl.textContent = `fps: ${fps}`;
    this.statusBotsEl.textContent = `bots: ${botCount}`;
  }
}
