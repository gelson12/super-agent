// agents.js — Bot entity + finite state machine.
//
// Bot states:
//   idle        — stationary at current tile, doing nothing
//   walking     — following a path on the current floor
//   transit     — taking the stairs between floors
//   atDesk      — sitting/standing at assigned desk (work)
//   inMeeting   — at a meeting anchor
//   social      — at an informal-activity anchor (coffee, ping-pong, etc.)
//
// Per tick the bot consumes its `path` queue (one tile step every `stepMs`).

import { findPath, findRoute, dirFromDelta, nearestWalkable } from './nav.js';

let _seq = 0;
export const STEP_MS = 350;   // natural office walk pace — ~2.9 tiles/sec
const TRANSITION_MS = 900;    // stair crossing pause — long enough to read as "going upstairs"

// Cubic ease-in-out — crisp start/stop matching Unity's Animator default curve.
function easeInOut(t) {
  return t < 0.5 ? 4*t*t*t : 1 - Math.pow(-2*t + 2, 3) / 2;
}

export class Bot {
  constructor(spec) {
    this.id = spec.id;
    this.name = spec.name;
    this.role = spec.role || '';
    this.sheet = spec.sheet || 'sheet_1';
    this.row = spec.row ?? 0;
    this.tint = spec.tint || '#ffd166';
    this.affinity = spec.affinity || ['desk_cluster','coffee','lounge'];
    this.deskFloor = spec.desk?.floor || 1;
    this.deskTile = spec.desk?.tile || [10, 25];

    this.floor = this.deskFloor;
    this.x = this.deskTile[0];
    this.y = this.deskTile[1];
    this.tx = this.x; this.ty = this.y;
    // Random initial facing so the office isn't a row of bots all looking down.
    this.facing = ['left','right','up','down'][Math.floor(Math.random() * 4)];
    this.state = 'idle';
    this.stepStart = 0;
    this.stepFromX = this.x; this.stepFromY = this.y;

    this.path = [];                // remaining tile waypoints on current floor
    this.route = null;             // multi-segment route across floors
    this.routeIdx = 0;
    this.transitionEnd = 0;

    this.activeFlag = false;       // mirrors live n8n workflow active=true
    this.taskLabel = '';           // human-readable current task
    this.socialEnd = 0;            // when social activity expires (ms)
    this.pendingActivity = null;   // { label, durationMs } — set by goTo for leisure zones

    this._uid = ++_seq;
  }

  // Compute a navigation route to (floor, x, y) and start walking.
  // Returns true on success, false if unreachable.
  goTo(floors, dstFloor, dx, dy, ctx = {}) {
    // Never interrupt a staircase crossing — bot.floor and bot.x/y are already
    // set to the *destination* stair landing, so building a route from here
    // would place the bot on the wrong floor without any visible transition.
    if (this.state === 'transit') return false;

    // Steer to nearest walkable if dest is blocked (e.g., furniture tile).
    const target = nearestWalkable(floors[dstFloor], dx, dy, 4) || { x: dx, y: dy };
    const route = findRoute(floors, this.floor, Math.round(this.x), Math.round(this.y),
                            dstFloor, target.x, target.y);
    if (!route) return false;
    this.route = route;
    this.routeIdx = 0;
    this.taskLabel = ctx.label || '';
    this.pendingActivity = ctx.durationMs ? { label: ctx.label || '', durationMs: ctx.durationMs } : null;
    this._enterNextSegment();
    return true;
  }

  _enterNextSegment() {
    if (!this.route || this.routeIdx >= this.route.length) {
      this.route = null;
      this.path = [];
      this.state = 'idle';
      return;
    }
    const seg = this.route[this.routeIdx];
    if (seg.kind === 'walk') {
      // Skip the first node (current pos)
      this.path = seg.path.slice(1);
      this.state = this.path.length ? 'walking' : 'idle';
    } else if (seg.kind === 'transition') {
      this.state = 'transit';
      this.floor = seg.toFloor;
      this.x = seg.toTile[0]; this.y = seg.toTile[1];
      this.tx = this.x; this.ty = this.y;
      this.transitionEnd = performance.now() + TRANSITION_MS;
    }
  }

  // Called from main loop. now = high-res timestamp in ms.
  update(now) {
    if (this.state === 'transit') {
      if (now >= this.transitionEnd) {
        this.routeIdx++;
        this._enterNextSegment();
      }
      return;
    }
    if (this.state === 'social') {
      if (now >= this.socialEnd) {
        this.state = 'idle';
        this.taskLabel = '';
      }
      return;
    }
    if (this.state !== 'walking') return;

    if (this.tx === Math.round(this.x) && this.ty === Math.round(this.y)) {
      // Snap and pull next waypoint.
      if (!this.path.length) {
        // End of segment.
        this.routeIdx++;
        if (this.route && this.routeIdx < this.route.length) {
          this._enterNextSegment();
        } else {
          this.route = null;
          if (this.pendingActivity) {
            this.state = 'social';
            this.socialEnd = performance.now() + this.pendingActivity.durationMs;
            this.taskLabel = this.pendingActivity.label;
            this.pendingActivity = null;
          } else {
            this.state = 'idle';
          }
        }
        return;
      }
      const next = this.path.shift();
      this.stepFromX = this.x; this.stepFromY = this.y;
      this.tx = next.x; this.ty = next.y;
      this.stepStart = now;
      const dx = this.tx - this.stepFromX, dy = this.ty - this.stepFromY;
      this.facing = dirFromDelta(dx, dy);
    } else {
      // Eased interpolation between adjacent tiles — steps glide rather
      // than snap, smoothing out 90° corners visually.
      const t = Math.min(1, (now - this.stepStart) / STEP_MS);
      const e = easeInOut(t);
      this.x = this.stepFromX + (this.tx - this.stepFromX) * e;
      this.y = this.stepFromY + (this.ty - this.stepFromY) * e;
      if (t >= 1) { this.x = this.tx; this.y = this.ty; }
    }
  }

  // Convert current state to a UI label.
  stateLabel() {
    if (this.state === 'walking')   return 'walking';
    if (this.state === 'transit')   return 'on stairs';
    if (this.state === 'inMeeting') return 'in meeting';
    if (this.state === 'social')    return this.taskLabel || 'social';
    if (this.state === 'atDesk')    return 'at desk';
    return 'idle';
  }
}

export async function loadBots() {
  try {
    const r = await fetch('data/bots.json');
    const json = await r.json();
    return json.bots.map(spec => new Bot(spec));
  } catch (e) {
    console.error('[agents] failed to load bots.json:', e);
    return [];
  }
}
