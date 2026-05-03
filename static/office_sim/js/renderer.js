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
import { STEP_MS } from './agents.js';

const SPRITE_DRAW_W = 56;
const SPRITE_DRAW_H = 56;
const WALK_FRAME_MS = 175;      // frame cycle matched to 350 ms/tile — smooth walk
const WALK_BOB_AMP  = 3;        // px — subtle vertical lift
// Bob is step-phase-synced (not time-based), so no Hz constant needed.

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
    this.ctx.imageSmoothingQuality = 'high';
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
    ctx.imageSmoothingEnabled  = true;
    ctx.imageSmoothingQuality  = 'high';
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

    // Animated TV screens — drawn on top of floor, behind bots.
    this._drawTVScreens(ctx, w, h, now);

    // Projector screens — animated stats when meeting room is reserved.
    if (this.scheduler) this._drawProjectorScreens(ctx, w, h, now);

    // Y-sort bots on this floor. Exclude bots in 'transit' — they are between
    // floors (physically on the staircase) and must not pop-in on the new floor
    // until they finish crossing and start walking.
    const onFloor = this.bots.filter(b => b.floor === this.activeFloor && b.state !== 'transit');
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

  // TV screens: floor → [tile, ...] (one entry per TV on that floor).
  // Tile coords come from the 'tv' zone anchors in floorN.json.
  static get TV_TILES() {
    return { 1: [[55, 6]], 3: [[56, 12]] };
  }

  // Projector screens: floor → [{ tile, zones[] }]
  // tile = where the screen is drawn; zones = reservedRooms keys that trigger it.
  static get PROJECTOR_CONFIG() {
    return {
      1: [{ tile: [12, 2],  zones: ['1:meeting_l1'] }],
      2: [{ tile: [4,  1],  zones: ['2:conference_l2', '2:meeting_l2_round'] }],
      3: [
        { tile: [8,  2],  zones: ['3:conference_l3'] },
        { tile: [7,  25], zones: ['3:meeting_l3_round'] },
      ],
    };
  }

  _drawTVScreens(ctx, w, h, now) {
    const tvTiles = Renderer.TV_TILES[this.activeFloor];
    if (!tvTiles) return;

    const tw = w / TILE_W;
    const th = h / TILE_H;

    for (const [tx, ty] of tvTiles) {
      // Screen occupies roughly 1.6 × 0.7 tiles, centred on the TV tile.
      const cx = (tx + 0.5) * tw;
      const cy = (ty + 0.5) * th;
      const sw = tw * 1.6;
      const sh = th * 0.72;

      const t = now / 1000;  // seconds

      // Bezel (dark rounded frame)
      ctx.save();
      ctx.shadowColor = 'rgba(80,180,255,0.55)';
      ctx.shadowBlur  = 14;
      ctx.fillStyle = '#111';
      ctx.beginPath();
      const r = 5;
      ctx.roundRect(cx - sw/2 - 3, cy - sh/2 - 3, sw + 6, sh + 6, r);
      ctx.fill();
      ctx.shadowBlur = 0;

      // Animated screen content — slow hue rotation + scanlines
      const hue = (t * 18) % 360;  // drifts through colours
      const bright = 0.55 + 0.1 * Math.sin(t * 0.7);
      const grad = ctx.createLinearGradient(cx - sw/2, cy - sh/2, cx + sw/2, cy + sh/2);
      grad.addColorStop(0,   `hsla(${hue},        70%, ${bright*100}%, 0.95)`);
      grad.addColorStop(0.5, `hsla(${(hue+40)%360},60%, ${bright*80}%, 0.95)`);
      grad.addColorStop(1,   `hsla(${(hue+80)%360},70%, ${bright*100}%, 0.95)`);
      ctx.fillStyle = grad;
      ctx.beginPath();
      ctx.roundRect(cx - sw/2, cy - sh/2, sw, sh, r - 2);
      ctx.fill();

      // Scanlines — subtle horizontal dark bands
      ctx.globalAlpha = 0.13;
      ctx.fillStyle = '#000';
      for (let line = 0; line < sh; line += 4) {
        ctx.fillRect(cx - sw/2, cy - sh/2 + line, sw, 2);
      }
      ctx.globalAlpha = 1;

      // Soft glow spilling onto the floor below
      const glow = ctx.createRadialGradient(cx, cy + sh/2, 0, cx, cy + sh/2, tw * 2.5);
      glow.addColorStop(0,   `hsla(${hue},80%,70%,0.18)`);
      glow.addColorStop(1,   'transparent');
      ctx.fillStyle = glow;
      ctx.fillRect(cx - tw*2.5, cy, tw*5, th*2);

      ctx.restore();
    }
  }

  _drawProjectorScreens(ctx, w, h, now) {
    const configs = Renderer.PROJECTOR_CONFIG[this.activeFloor];
    if (!configs) return;
    const reserved = this.scheduler.reservedRooms;
    const tw = w / TILE_W, th = h / TILE_H;
    const t = now / 1000;

    for (const { tile: [tx, ty], zones } of configs) {
      const active = zones.some(z => reserved.has(z));
      if (!active) continue;

      const cx = (tx + 0.5) * tw;
      const cy = (ty + 0.5) * th;
      const sw = tw * 4.2;   // screen width in px
      const sh = th * 2.2;   // screen height in px
      const r  = 4;

      ctx.save();

      // Projector beam — wide cone from screen downward
      const beamGrad = ctx.createLinearGradient(cx, cy + sh/2, cx, cy + sh/2 + th * 4);
      beamGrad.addColorStop(0, 'rgba(180,210,255,0.10)');
      beamGrad.addColorStop(1, 'transparent');
      ctx.fillStyle = beamGrad;
      ctx.beginPath();
      ctx.moveTo(cx - sw/2, cy + sh/2);
      ctx.lineTo(cx + sw/2, cy + sh/2);
      ctx.lineTo(cx + sw/2 + tw, cy + sh/2 + th*4);
      ctx.lineTo(cx - sw/2 - tw, cy + sh/2 + th*4);
      ctx.closePath();
      ctx.fill();

      // Screen bezel
      ctx.shadowColor = 'rgba(100,160,255,0.7)';
      ctx.shadowBlur  = 16;
      ctx.fillStyle   = '#0b1a2e';
      ctx.beginPath();
      ctx.roundRect(cx - sw/2 - 3, cy - sh/2 - 3, sw + 6, sh + 6, r + 1);
      ctx.fill();
      ctx.shadowBlur = 0;

      // Screen background
      ctx.fillStyle = '#071220';
      ctx.beginPath();
      ctx.roundRect(cx - sw/2, cy - sh/2, sw, sh, r);
      ctx.fill();

      // ── Clip all content to the screen area ──
      ctx.beginPath();
      ctx.roundRect(cx - sw/2, cy - sh/2, sw, sh, r);
      ctx.clip();

      const sx = cx - sw/2, sy = cy - sh/2;   // screen top-left
      const pad = sw * 0.04;

      // ── Heading strip ──
      ctx.fillStyle = 'rgba(30,80,160,0.85)';
      ctx.fillRect(sx, sy, sw, sh * 0.18);
      ctx.fillStyle = '#a8d4ff';
      ctx.font = `bold ${Math.round(sh * 0.14)}px monospace`;
      ctx.textAlign = 'center';
      ctx.fillText('MEETING STATS', cx, sy + sh * 0.145);

      // ── Bar chart (left 55% of screen) ──
      const bars = 6;
      const barZone = { x: sx + pad, y: sy + sh*0.22, w: sw*0.52, h: sh*0.55 };
      const barW = (barZone.w / bars) * 0.55;
      const barGap = barZone.w / bars;
      const COLORS = ['#3b82f6','#06b6d4','#8b5cf6','#10b981','#f59e0b','#ef4444'];

      for (let i = 0; i < bars; i++) {
        // Each bar oscillates with its own phase and frequency
        const phase  = i * 1.1 + 0.3;
        const freq   = 0.4 + i * 0.07;
        const height = (0.45 + 0.4 * Math.abs(Math.sin(t * freq + phase))) * barZone.h;
        const bx = barZone.x + i * barGap + (barGap - barW) / 2;
        const by = barZone.y + barZone.h - height;

        // Bar gradient — brighter at top
        const bg = ctx.createLinearGradient(bx, by, bx, by + height);
        bg.addColorStop(0, COLORS[i]);
        bg.addColorStop(1, COLORS[i] + '55');
        ctx.fillStyle = bg;
        ctx.fillRect(bx, by, barW, height);

        // Value label above bar
        const val = Math.round(40 + 55 * Math.abs(Math.sin(t * freq + phase)));
        ctx.fillStyle = '#c8e4ff';
        ctx.font = `${Math.round(sh * 0.1)}px monospace`;
        ctx.textAlign = 'center';
        ctx.fillText(val + '%', bx + barW/2, by - 2);
      }

      // Bar axis line
      ctx.strokeStyle = 'rgba(100,160,255,0.3)';
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(barZone.x, barZone.y + barZone.h);
      ctx.lineTo(barZone.x + barZone.w, barZone.y + barZone.h);
      ctx.stroke();

      // ── Line graph (right 40% of screen) ──
      const lgZone = { x: sx + sw*0.58, y: sy + sh*0.22, w: sw*0.38, h: sh*0.55 };
      const points = 20;

      ctx.strokeStyle = '#34d399';
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      for (let i = 0; i < points; i++) {
        const px2 = lgZone.x + (i / (points-1)) * lgZone.w;
        const v   = 0.5 + 0.38 * Math.sin(t * 0.8 + i * 0.7) + 0.12 * Math.sin(t * 2.1 + i * 1.3);
        const py2 = lgZone.y + lgZone.h * (1 - v);
        i === 0 ? ctx.moveTo(px2, py2) : ctx.lineTo(px2, py2);
      }
      ctx.stroke();

      // Moving dot on line (latest data point)
      const dotV  = 0.5 + 0.38 * Math.sin(t * 0.8 + (points-1)*0.7) + 0.12 * Math.sin(t*2.1 + (points-1)*1.3);
      const dotPx = lgZone.x + lgZone.w;
      const dotPy = lgZone.y + lgZone.h * (1 - dotV);
      ctx.beginPath();
      ctx.arc(dotPx, dotPy, 3, 0, Math.PI*2);
      ctx.fillStyle = '#34d399';
      ctx.fill();

      // Line graph border
      ctx.strokeStyle = 'rgba(52,211,153,0.2)';
      ctx.lineWidth = 1;
      ctx.strokeRect(lgZone.x, lgZone.y, lgZone.w, lgZone.h);

      // ── Bottom KPI strip ──
      const kpis = [
        { label: 'ROI',  val: () => (2.1 + 0.4*Math.sin(t*0.3)).toFixed(1)+'x' },
        { label: 'Q/H',  val: () => Math.round(87 + 8*Math.sin(t*0.5))+'%' },
        { label: 'NPS',  val: () => Math.round(72 + 5*Math.sin(t*0.4)) },
      ];
      const kpiY  = sy + sh*0.82;
      const kpiW  = sw / kpis.length;
      ctx.font      = `bold ${Math.round(sh * 0.13)}px monospace`;
      ctx.textAlign = 'center';
      for (let i = 0; i < kpis.length; i++) {
        const kx = sx + kpiW * i + kpiW/2;
        ctx.fillStyle = '#3b82f680';
        ctx.fillRect(sx + kpiW*i + pad, kpiY - sh*0.12, kpiW - pad*2, sh*0.16);
        ctx.fillStyle = '#fbbf24';
        ctx.fillText(kpis[i].val(), kx, kpiY);
        ctx.fillStyle = '#64748b';
        ctx.font = `${Math.round(sh * 0.09)}px monospace`;
        ctx.fillText(kpis[i].label, kx, kpiY + sh*0.11);
        ctx.font = `bold ${Math.round(sh * 0.13)}px monospace`;
      }

      ctx.restore();
    }
  }

  _drawBot(ctx, bot, now) {
    const { px, py } = this.tileToPx(bot.x, bot.y);

    // ── Frame selection ──────────────────────────────────────────
    let dirName, mirror = false, frameIndex = 0;
    if (bot.state === 'walking') {
      const cycle = walkCycleFrame(bot.facing, now, this.sprites, bot, WALK_FRAME_MS);
      dirName    = cycle.dirName;
      mirror     = cycle.mirror;
      frameIndex = cycle.frameIndex || 0;
    } else {
      dirName = standFrameName(bot.facing);
    }
    const f = this.sprites.frame(bot, dirName, frameIndex);

    // ── Sprite dimensions (reference-scale) ──────────────────────
    const ref = this.sprites.refScale?.(bot.id);
    let dw, dh;
    if (ref) {
      const k = (SPRITE_DRAW_H * this.dpr) / ref.refH;
      dw = (f.sw || ref.refW) * k;
      dh = (f.sh || ref.refH) * k;
    } else {
      const aspect = (f.sw && f.sh) ? f.sw / f.sh : 1;
      dw = (aspect >= 1 ? SPRITE_DRAW_W : SPRITE_DRAW_W * aspect) * this.dpr;
      dh = (aspect >= 1 ? SPRITE_DRAW_H / aspect : SPRITE_DRAW_H) * this.dpr;
    }

    // ── Animation values ─────────────────────────────────────────
    // Step phase 0→1 within the current tile crossing (synced to bot.stepStart).
    const stepT = bot.state === 'walking'
      ? Math.min(1, (now - bot.stepStart) / STEP_MS) : 0;

    // Vertical bob: peaks at mid-stride (feet off ground), zero at foot-plant.
    const bob = bot.state === 'walking'
      ? -Math.abs(Math.sin(stepT * Math.PI)) * WALK_BOB_AMP * this.dpr
      : 0;

    // Idle / at-desk breathing: subtle scaleY oscillation.
    const breatheAmt = (bot.state === 'idle' || bot.state === 'atDesk')
      ? Math.sin(now * 0.00045 + (bot._uid || 0) * 0.7) * 0.012
      : 0;

    // Squash & stretch: taller at peak stride (mid-step), returns to neutral.
    const stretchY = bot.state === 'walking'
      ? 1 + 0.07 * Math.sin(stepT * Math.PI)
      : 1 + breatheAmt;
    // Width conserved — compress X slightly when stretched, expand when squashed.
    const squashX = 1 / Math.max(0.9, stretchY);

    // Directional lean: sprite tilts forward slightly during walk.
    const leanX = bot.state === 'walking'
      ? (bot.facing === 'right' ? 1.8 : bot.facing === 'left' ? -1.8 : 0) * this.dpr : 0;
    const leanY = bot.state === 'walking'
      ? (bot.facing === 'down'  ? 1.2 : bot.facing === 'up'   ? -1.2 : 0) * this.dpr : 0;

    // ── Drop shadow (stays on ground — never bobs) ────────────────
    // Shadow squashes when the bot is mid-air (mid-stride = smaller contact).
    const airFrac  = Math.abs(Math.sin(stepT * Math.PI)); // 0 at plant, 1 at peak
    const shadowRx = dw * (0.30 - 0.07 * airFrac);
    const shadowRy = dh * (0.07 - 0.02 * airFrac);
    ctx.fillStyle = `rgba(0,0,0,${0.32 - 0.08 * airFrac})`;
    ctx.beginPath();
    ctx.ellipse(px, py + 3 * this.dpr, shadowRx, shadowRy, 0, 0, Math.PI * 2);
    ctx.fill();

    // ── Active-workflow halo (breathing pulse) ────────────────────
    if (bot.activeFlag) {
      const pulse = 0.60 + 0.40 * Math.sin(now * 0.002 * Math.PI);
      ctx.save();
      ctx.shadowColor = '#58e7a4';
      ctx.shadowBlur  = (12 + 6 * pulse) * this.dpr;
      ctx.strokeStyle = `rgba(88,231,164,${0.30 + 0.25 * pulse})`;
      ctx.lineWidth   = 2 * this.dpr;
      ctx.beginPath();
      ctx.arc(px, py - dh * 0.45, dw * 0.32, 0, Math.PI * 2);
      ctx.stroke();
      ctx.restore();
    }

    // ── Transit ghost fade (slow, cinematic) ─────────────────────
    if (bot.state === 'transit') {
      ctx.globalAlpha = 0.40 + 0.55 * Math.abs(Math.sin(now * 0.0007 * Math.PI));
    }

    // ── Sprite draw with squash/stretch + lean ────────────────────
    if (f.img) {
      ctx.save();
      // Anchor transform at the foot point so scaling grows upward.
      ctx.translate(px + leanX, py + bob + leanY);
      ctx.scale(squashX, stretchY);
      const drawDy = -dh + 4 * this.dpr;   // feet at transform origin
      if (mirror) {
        ctx.scale(-1, 1);
        ctx.drawImage(f.img, f.sx, f.sy, f.sw, f.sh, -dw / 2, drawDy, dw, dh);
      } else {
        ctx.drawImage(f.img, f.sx, f.sy, f.sw, f.sh, -dw / 2, drawDy, dw, dh);
      }
      ctx.restore();
    }
    ctx.globalAlpha = 1;

    // ── Focused-bot selection ring ────────────────────────────────
    if (this.focusedBotId === bot.id) {
      const ringPulse = 0.70 + 0.30 * Math.sin(now * 0.003 * Math.PI);
      ctx.save();
      ctx.shadowColor = 'rgba(108,208,255,0.6)';
      ctx.shadowBlur  = 8 * this.dpr;
      ctx.strokeStyle = `rgba(108,208,255,${ringPulse})`;
      ctx.lineWidth   = 2 * this.dpr;
      ctx.beginPath();
      ctx.ellipse(px, py + 4 * this.dpr, dw * 0.32, dh * 0.10, 0, 0, Math.PI * 2);
      ctx.stroke();
      ctx.restore();
    }

    // Sprite top-y for label placement (accounts for bob and stretch).
    const labelTopY = py + bob - dh * stretchY + 4 * this.dpr;

    // ── Name label ────────────────────────────────────────────────
    if (this.focusedBotId === bot.id || bot.state === 'inMeeting') {
      ctx.font = `bold ${11 * this.dpr}px "Segoe UI",ui-sans-serif,system-ui,sans-serif`;
      const labelW = ctx.measureText(bot.name).width + 14 * this.dpr;
      const labelH = 17 * this.dpr;
      const lx = px - labelW / 2;
      const ly = labelTopY - 20 * this.dpr;
      // Pill background with subtle glow
      ctx.save();
      ctx.shadowColor = 'rgba(108,208,255,0.4)';
      ctx.shadowBlur  = 6 * this.dpr;
      ctx.fillStyle   = 'rgba(10,8,18,0.88)';
      ctx.beginPath();
      ctx.roundRect(lx, ly, labelW, labelH, 5 * this.dpr);
      ctx.fill();
      ctx.restore();
      ctx.fillStyle    = '#e8e7f0';
      ctx.textAlign    = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText(bot.name, px, ly + labelH / 2);
    }

    // ── Speech bubble ─────────────────────────────────────────────
    const bub = this.bubbles.get(bot.id);
    if (bub) {
      if (now > bub.until) {
        this.bubbles.delete(bot.id);
      } else {
        const fadeT  = Math.min(1, (bub.until - now) / 600);
        ctx.globalAlpha = fadeT;
        ctx.font = `${10 * this.dpr}px "Segoe UI",ui-sans-serif,system-ui,sans-serif`;
        const bubW = ctx.measureText(bub.text).width + 16 * this.dpr;
        const bubH = 18 * this.dpr;
        const bubY = labelTopY - 46 * this.dpr;
        ctx.save();
        ctx.shadowColor = 'rgba(201,169,110,0.5)';
        ctx.shadowBlur  = 8 * this.dpr;
        ctx.fillStyle   = 'rgba(15,12,28,0.96)';
        ctx.strokeStyle = 'rgba(201,169,110,0.7)';
        ctx.lineWidth   = 1 * this.dpr;
        ctx.beginPath();
        ctx.roundRect(px - bubW / 2, bubY, bubW, bubH, 5 * this.dpr);
        ctx.fill();
        ctx.stroke();
        ctx.restore();
        // Tail
        ctx.fillStyle = 'rgba(15,12,28,0.96)';
        ctx.strokeStyle = 'rgba(201,169,110,0.7)';
        ctx.lineWidth = 1 * this.dpr;
        ctx.beginPath();
        ctx.moveTo(px - 4 * this.dpr, bubY + bubH);
        ctx.lineTo(px,                bubY + bubH + 5 * this.dpr);
        ctx.lineTo(px + 4 * this.dpr, bubY + bubH);
        ctx.closePath();
        ctx.fill();
        ctx.stroke();
        ctx.fillStyle    = '#c9a96e';
        ctx.textAlign    = 'center';
        ctx.textBaseline = 'middle';
        ctx.fillText(bub.text, px, bubY + bubH / 2);
        ctx.globalAlpha = 1;
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

    // Pluggable: main.js wires this to open the sprite-editor modal.
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
      // Row click — focus + camera-follow.
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
