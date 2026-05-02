// nav.js — Tile-grid A* pathfinding + multi-floor routing.
//
// Per-floor 4-neighbour A*. For inter-floor moves we compose:
//   path = [A* on src floor to src stair] + ['transition'] + [A* on dst floor from dst stair]

import { TILE_W, TILE_H } from './world.js';

function key(x, y) { return y * TILE_W + x; }

// Manhattan distance heuristic.
function h(ax, ay, bx, by) { return Math.abs(ax-bx) + Math.abs(ay-by); }

// Navigable = walkable but NOT a stair-visual tile ('U'/'N').
// Stair tiles span the entire visual staircase column; only the single
// teleport tile at stairs[].tile should be reachable as a destination —
// bots must not use the staircase column as a general-purpose corridor.
const NAVIGABLE = new Set(['.','D','S','c','t','p','k','g','E','L','I','F','B']);
function navigable(floor, x, y) { return NAVIGABLE.has(floor.tileAt(x, y)); }

// 4-direction A* on a single floor. Returns array of {x,y} or null.
export function findPath(floor, sx, sy, gx, gy, opts = {}) {
  const isWalkable = opts.isWalkable || ((x,y) => navigable(floor, x, y));
  const blockedBy = opts.blockedBy || (() => false);
  if (!isWalkable(sx, sy) || !isWalkable(gx, gy)) return null;
  if (sx === gx && sy === gy) return [{ x: sx, y: sy }];

  const open = new Map();          // key -> {x,y,g,f,parent}
  const closed = new Set();
  const start = { x: sx, y: sy, g: 0, f: h(sx,sy,gx,gy), parent: null };
  open.set(key(sx, sy), start);

  while (open.size) {
    // Pick lowest-f node from open (linear scan is fine for our grid size).
    let curK = null, cur = null;
    for (const [k, n] of open) if (!cur || n.f < cur.f) { cur = n; curK = k; }
    open.delete(curK);
    closed.add(curK);

    if (cur.x === gx && cur.y === gy) {
      const out = [];
      let n = cur;
      while (n) { out.push({ x: n.x, y: n.y }); n = n.parent; }
      return out.reverse();
    }
    const neighbours = [[1,0],[-1,0],[0,1],[0,-1]];
    for (const [dx, dy] of neighbours) {
      const nx = cur.x + dx, ny = cur.y + dy;
      const nK = key(nx, ny);
      if (closed.has(nK)) continue;
      if (!isWalkable(nx, ny)) continue;
      if (blockedBy(nx, ny)) continue;
      const tentG = cur.g + 1;
      const existing = open.get(nK);
      if (existing && tentG >= existing.g) continue;
      open.set(nK, { x: nx, y: ny, g: tentG, f: tentG + h(nx, ny, gx, gy), parent: cur });
    }
  }
  return null;
}

// Multi-floor route. Returns an array of "segments":
//   { kind: 'walk', floor: N, path: [{x,y}, ...] }
//   { kind: 'transition', fromFloor, toFloor, fromTile, toTile }
//
// For now this only handles |srcFloor - dstFloor| <= 2 by hopping through
// each intermediate floor's matching stair node.
export function findRoute(floors, srcFloor, sx, sy, dstFloor, gx, gy) {
  if (srcFloor === dstFloor) {
    const p = findPath(floors[srcFloor], sx, sy, gx, gy);
    return p ? [{ kind:'walk', floor: srcFloor, path: p }] : null;
  }
  const segments = [];
  const step = dstFloor > srcFloor ? 1 : -1;
  let curFloor = srcFloor;
  let curX = sx, curY = sy;

  while (curFloor !== dstFloor) {
    const nextFloor = curFloor + step;
    const stair = floors[curFloor].stairToFloor(nextFloor);
    if (!stair) return null;
    // Allow the specific stair tile as destination even though it's a 'U'/'N' tile.
    const stairX = stair.tile[0], stairY = stair.tile[1];
    const toStair = (x, y) => (x === stairX && y === stairY) || navigable(floors[curFloor], x, y);
    const walk = findPath(floors[curFloor], curX, curY, stairX, stairY, { isWalkable: toStair });
    if (!walk) return null;
    segments.push({ kind:'walk', floor: curFloor, path: walk });
    segments.push({ kind:'transition', fromFloor: curFloor, toFloor: nextFloor,
                     fromTile: stair.tile, toTile: stair.toTile });
    curX = stair.toTile[0];
    curY = stair.toTile[1];
    curFloor = nextFloor;
  }

  const finalWalk = findPath(floors[curFloor], curX, curY, gx, gy);
  if (!finalWalk) return null;
  segments.push({ kind:'walk', floor: curFloor, path: finalWalk });
  return segments;
}

// Choose the dominant direction from a delta vector.
export function dirFromDelta(dx, dy) {
  if (Math.abs(dx) >= Math.abs(dy)) return dx >= 0 ? 'right' : 'left';
  return dy >= 0 ? 'down' : 'up';
}

// Find nearest walkable tile to (x, y) within a max search radius.
// Used as a fall-back when a desired anchor is occupied / blocked.
export function nearestWalkable(floor, x, y, radius = 5, blockedBy = () => false) {
  if (navigable(floor, x, y) && !blockedBy(x, y)) return { x, y };
  for (let r = 1; r <= radius; r++) {
    for (let dy = -r; dy <= r; dy++) {
      for (let dx = -r; dx <= r; dx++) {
        if (Math.abs(dx) !== r && Math.abs(dy) !== r) continue;
        const nx = x + dx, ny = y + dy;
        if (navigable(floor, nx, ny) && !blockedBy(nx, ny)) return { x: nx, y: ny };
      }
    }
  }
  return null;
}
