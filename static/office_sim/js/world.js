// world.js — Floor data loader, tile lookup, zone/door/stair queries.
//
// Each floor is a 64x40 logical tile grid. Tiles are encoded as single chars
// in `tiles` (a 64*40-char string). Coordinate convention: (col, row), origin
// top-left, x grows right, y grows down. World space is the same as tile
// space scaled by TILE_SIZE px in the renderer.
//
// Tile chars:
//   . walkable          # blocked
//   D door (walkable, but flagged)
//   U stair-up portal   N stair-down portal
//   S seat anchor       (still walkable; just a marker for nav targets)
//   c coffee anchor     t TV viewer anchor
//   p phone-booth anchor   k snooker anchor   g ping-pong anchor
//   E entry (welcome mat)  L lounge anchor
//   I ideas-board anchor   F focus-zone anchor   B bench anchor

export const TILE_W = 64;
export const TILE_H = 40;

const ANCHOR_CHARS = new Set(['S','c','t','p','k','g','E','L','I','F','B']);
const WALKABLE_CHARS = new Set(['.','D','U','N','S','c','t','p','k','g','E','L','I','F','B']);

export class Floor {
  constructor(json, overlay = null) {
    this.id = json.id;
    this.name = json.name;
    this.zones = json.zones || [];
    this.doors = json.doors || [];
    this.stairs = json.stairs || [];
    this.spawn = json.spawn || [4, 35];
    this.bg = json.bg;
    // Build tile array. Default walkable; apply obstacles[] in order.
    const arr = new Array(TILE_W * TILE_H).fill('.');
    for (const [x, y, w, h, ch] of (json.obstacles || [])) {
      for (let yy = y; yy < y + h; yy++) {
        for (let xx = x; xx < x + w; xx++) {
          if (xx >= 0 && yy >= 0 && xx < TILE_W && yy < TILE_H) {
            arr[yy * TILE_W + xx] = ch;
          }
        }
      }
    }
    // Outer walls always.
    for (let x = 0; x < TILE_W; x++) { arr[x] = '#'; arr[(TILE_H-1)*TILE_W + x] = '#'; }
    for (let y = 0; y < TILE_H; y++) { arr[y*TILE_W] = '#'; arr[y*TILE_W + TILE_W-1] = '#'; }
    // PNG-derived collision overlay: connectivity-preserving extra blocks
    // sampled from the floor PNG by preprocess_assets.py. Only stamps over
    // already-walkable cells; anchors/doors/stairs are excluded at preprocess
    // time. Significantly reduces visible "bot inside table" cases.
    if (overlay && Array.isArray(overlay.blocked)) {
      for (const [tx, ty] of overlay.blocked) {
        if (tx >= 0 && ty >= 0 && tx < TILE_W && ty < TILE_H) {
          const i = ty * TILE_W + tx;
          if (arr[i] === '.') arr[i] = '#';
        }
      }
    }
    this.tiles = arr.join('');
  }

  tileAt(x, y) {
    if (x < 0 || y < 0 || x >= TILE_W || y >= TILE_H) return '#';
    return this.tiles[y * TILE_W + x];
  }

  walkable(x, y) { return WALKABLE_CHARS.has(this.tileAt(x, y)); }
  isDoor(x, y)   { return this.tileAt(x, y) === 'D'; }
  isStair(x, y)  { const c = this.tileAt(x, y); return c === 'U' || c === 'N'; }

  // Zone lookup: which zone (if any) does this tile belong to?
  zoneAt(x, y) {
    for (const z of this.zones) {
      const [zx, zy, zw, zh] = z.bounds;
      if (x >= zx && x < zx + zw && y >= zy && y < zy + zh) return z;
    }
    return null;
  }

  zonesByType(type) { return this.zones.filter(z => z.type === type); }

  // Find the stair entry on this floor that goes to floor `toFloor`.
  stairToFloor(toFloor) {
    return this.stairs.find(s => s.toFloor === toFloor) || null;
  }

  // All anchor tiles inside a given zone (for picking standing positions).
  zoneAnchors(zone) {
    const out = [];
    if (zone.anchors) return zone.anchors.map(a => ({ x: a[0], y: a[1] }));
    const [zx, zy, zw, zh] = zone.bounds;
    for (let y = zy; y < zy + zh; y++) {
      for (let x = zx; x < zx + zw; x++) {
        if (ANCHOR_CHARS.has(this.tileAt(x, y))) out.push({ x, y });
      }
    }
    return out;
  }
}

export async function loadFloors() {
  const ids = [1, 2, 3];
  const floors = {};
  for (const id of ids) {
    try {
      const r = await fetch(`data/floor${id}.json`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const json = await r.json();
      // Optional overlay; absence is fine — collision falls back to hand-mapped only.
      let overlay = null;
      try {
        const ovR = await fetch(`data/floor${id}_overlay.json`);
        if (ovR.ok) overlay = await ovR.json();
      } catch {}
      floors[id] = new Floor(json, overlay);
    } catch (e) {
      console.error(`[world] failed to load floor ${id}:`, e);
      floors[id] = new Floor({ id, name: `Level ${id}` });
    }
  }
  return floors;
}
