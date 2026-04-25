// sprites.js — Sprite-sheet loader and 8-direction frame slicer.
//
// THREE sprite formats are supported, in priority order per bot:
//
//  1. Per-bot individual frames (assets/sprites/bots/<id>/<frame>.png)
//     RECOMMENDED. The user drops one PNG per (direction, walk frame), with
//     real per-pixel alpha. Up to 4 walk frames per direction for true
//     leg-alternating motion. Filenames the loader looks for:
//       stand_left.png   stand_right.png   stand_up.png   stand_down.png
//       walk_left.png    walk_right.png    walk_up.png    walk_down.png
//     Optional extra walk frames for a smoother cycle:
//       walk_left_2.png  walk_left_3.png   walk_left_4.png   (etc per dir)
//
//  2. Per-bot horizontal strip (assets/sprites/bots/<id>.png)
//     Single row, 8 cells (1414×177 etc.). Same column order as the grid.
//     Alpha-keyed version (`<id>_alpha.png`) is preferred when available.
//
//  3. Multi-bot grid (sheet_1..5): 8×8 grid, 192×128 cells. Final fallback.
//
// Column / direction order is consistent across all three:
//   0 stand-left   1 stand-right   2 stand-up   3 stand-down
//   4 walk-left    5 walk-right    6 walk-up    7 walk-down

export const SHEET_COLS = 8;
export const SHEET_ROWS = 8;
export const CELL_W = 192;
export const CELL_H = 128;

const GRID_PATHS = {
  sheet_1: 'assets/sprites/sheet_1_alpha.png',
  sheet_2: 'assets/sprites/sheet_2_alpha.png',
  sheet_3: 'assets/sprites/sheet_3_alpha.png',
  sheet_4: 'assets/sprites/sheet_4_alpha.png',
  sheet_5: 'assets/sprites/sheet_5_alpha.png',
};
const GRID_FALLBACK = {
  sheet_1: 'assets/sprites/sheet_1.png',
  sheet_2: 'assets/sprites/sheet_2.png',
  sheet_3: 'assets/sprites/sheet_3.png',
  sheet_4: 'assets/sprites/sheet_4.png',
  sheet_5: 'assets/sprites/sheet_5.png',
};

const FRAME_COL = {
  stand_left: 0, stand_right: 1, stand_up: 2, stand_down: 3,
  walk_left:  4, walk_right:  5, walk_up:  6, walk_down:  7,
};

// Filenames the per-bot directory loader probes. dir → list of frame files
// in cycle order (stand → walk_1 → walk_2 → ...). Missing files are skipped.
const BOT_DIR_FRAMES = {
  stand_left:  ['stand_left.png'],
  stand_right: ['stand_right.png'],
  stand_up:    ['stand_up.png'],
  stand_down:  ['stand_down.png'],
  walk_left:   ['walk_left.png',  'walk_left_2.png',  'walk_left_3.png',  'walk_left_4.png'],
  walk_right:  ['walk_right.png', 'walk_right_2.png', 'walk_right_3.png', 'walk_right_4.png'],
  walk_up:     ['walk_up.png',    'walk_up_2.png',    'walk_up_3.png',    'walk_up_4.png'],
  walk_down:   ['walk_down.png',  'walk_down_2.png',  'walk_down_3.png',  'walk_down_4.png'],
};


export class SpriteCache {
  constructor() {
    this.gridImages = {};       // sheet name -> HTMLImageElement
    this.botImages = {};        // bot id   -> { img, cellW, cellH }       (strip)
    this.botFrames = {};        // bot id   -> { stand_left:[img,...], walk_left:[...], ... }
    this.placeholder = null;
  }

  async load(botList = []) {
    // 1. Multi-bot grid sheets (final fallback) — parallel.
    await Promise.all(Object.entries(GRID_PATHS).map(([n, p]) => this._loadGrid(n, p)));
    // 2. Per-bot strips when bots declare `botSprite`.
    await Promise.all(botList
      .filter(b => b.botSprite)
      .map(b => this._loadBotStrip(b.id, `assets/sprites/bots/${b.botSprite}`)));
    // 3. Per-bot individual-frame directory — probe assets/sprites/bots/<id>/<frame>.png
    //    Each bot is probed independently; missing frames just don't get cached.
    await Promise.all(botList.map(b => this._loadBotFrames(b.id)));
    this.placeholder = this._makePlaceholder();
    return this;
  }

  async _loadBotFrames(botId) {
    const base = `assets/sprites/bots/${botId}`;
    const frames = {};
    let any = false;
    for (const [dir, files] of Object.entries(BOT_DIR_FRAMES)) {
      const loaded = [];
      for (const file of files) {
        const path = `${base}/${file}`;
        try {
          const img = new Image();
          img.src = path;
          await img.decode();
          loaded.push(img);
        } catch { /* file missing — fine */ }
      }
      if (loaded.length) { frames[dir] = loaded; any = true; }
    }
    if (any) {
      this.botFrames[botId] = frames;
      console.log(`[sprites] bot ${botId}: ${Object.keys(frames).length} directions with individual frames`);
    }
  }

  _loadGrid(name, path) {
    return new Promise(resolve => {
      const img = new Image();
      img.onload = () => { this.gridImages[name] = img; resolve(); };
      img.onerror = () => {
        const fb = GRID_FALLBACK[name];
        if (fb && fb !== path) {
          const f = new Image();
          f.onload = () => { this.gridImages[name] = f; resolve(); };
          f.onerror = () => { console.warn(`[sprites] grid ${name} failed`); resolve(); };
          f.src = fb;
        } else { console.warn(`[sprites] grid ${name} failed`); resolve(); }
      };
      img.src = path;
    });
  }

  _loadBotStrip(botId, path) {
    return new Promise(resolve => {
      // Prefer alpha-keyed version
      const alphaPath = path.replace(/\.png$/, '_alpha.png');
      const tryLoad = (src, isFinal) => {
        const img = new Image();
        img.onload = () => {
          // Strip is 1 row x 8 cols. Cell width = imageWidth/8, cell height = imageHeight.
          this.botImages[botId] = {
            img,
            cellW: img.naturalWidth / SHEET_COLS,
            cellH: img.naturalHeight,
          };
          resolve();
        };
        img.onerror = () => {
          if (isFinal) { console.warn(`[sprites] bot strip ${botId} failed`); resolve(); }
          else tryLoad(path, true);   // fallback to non-alpha
        };
        img.src = src;
      };
      tryLoad(alphaPath, false);
    });
  }

  _makePlaceholder() {
    const c = document.createElement('canvas');
    c.width = CELL_W; c.height = CELL_H;
    const ctx = c.getContext('2d');
    ctx.fillStyle = '#7c5cff';
    ctx.fillRect(20, 30, CELL_W-40, CELL_H-50);
    ctx.fillStyle = '#ffd166';
    ctx.fillRect(40, 50, 30, 30);
    ctx.fillRect(CELL_W-70, 50, 30, 30);
    return c;
  }

  // Returns the source-image rect for a (bot, dirName, frameIndex) frame.
  // Priority: individual-frame dir > horizontal strip > multi-bot grid.
  // frameIndex (default 0) selects within a multi-frame walk cycle when the
  // bot has individual-frame assets; otherwise it's ignored.
  frame(bot, dirName, frameIndex = 0) {
    // 1. Per-bot individual frames (cleanest path).
    const dirFrames = this.botFrames[bot.id]?.[dirName];
    if (dirFrames && dirFrames.length) {
      const img = dirFrames[frameIndex % dirFrames.length];
      return { img, sx: 0, sy: 0, sw: img.naturalWidth, sh: img.naturalHeight };
    }
    // 2. Per-bot horizontal strip.
    const col = FRAME_COL[dirName] ?? 3;
    const strip = this.botImages[bot.id];
    if (strip && strip.img) {
      return {
        img: strip.img,
        sx: col * strip.cellW, sy: 0,
        sw: strip.cellW,        sh: strip.cellH,
      };
    }
    // 3. Multi-bot grid fallback.
    const grid = this.gridImages[bot.sheet];
    if (grid) {
      return {
        img: grid,
        sx: col * CELL_W, sy: bot.row * CELL_H,
        sw: CELL_W,       sh: CELL_H,
      };
    }
    return { img: this.placeholder, sx: 0, sy: 0, sw: CELL_W, sh: CELL_H };
  }

  // Number of walk frames available for a (bot, dir) — used by walkCycleFrame
  // to pick a real next frame when individual-frame assets are present.
  walkFrameCount(bot, facing) {
    return (this.botFrames[bot.id]?.[`walk_${facing}`])?.length ?? 1;
  }
}

// Helpers for the renderer.

export function standFrameName(facing) {
  return ({ left:'stand_left', right:'stand_right', up:'stand_up', down:'stand_down' }[facing]) || 'stand_down';
}
export function walkFrameName(facing) {
  return ({ left:'walk_left', right:'walk_right', up:'walk_up', down:'walk_down' }[facing]) || 'walk_down';
}

// Walk cycle. Returns { dirName, frameIndex, mirror }.
//
// When the bot has multi-frame walk assets (walkFrames > 1), the cycle is:
//   stand → walk_1 → walk_2 → ... → walk_N  (period = N+1 phases)
// No mirror trick needed — real frames carry the leg motion.
//
// When the bot has only stand+walk per direction (single-frame walk), the
// cycle falls back to STAND → WALK → STAND → WALK_MIRROR for L/R, and
// STAND → WALK alternation for U/D. Mirror flips the sprite horizontally
// to synthesise an "opposite leg forward" pose from a single source frame.
export function walkCycleFrame(facing, now, sprites, bot, frameMs = 180) {
  const walkN = sprites?.walkFrameCount?.(bot, facing) ?? 1;
  const stand = standFrameName(facing);
  const walk = walkFrameName(facing);

  if (walkN > 1) {
    const totalPhases = 1 + walkN;                  // 1 stand + N walk frames
    const phase = Math.floor(now / frameMs) % totalPhases;
    if (phase === 0) return { dirName: stand, frameIndex: 0, mirror: false };
    return { dirName: walk, frameIndex: phase - 1, mirror: false };
  }

  // Single-frame walk — fall back to mirror-trick for L/R.
  const phase = Math.floor(now / frameMs) & 3;
  if (facing === 'left' || facing === 'right') {
    if (phase === 0) return { dirName: stand, frameIndex: 0, mirror: false };
    if (phase === 1) return { dirName: walk,  frameIndex: 0, mirror: false };
    if (phase === 2) return { dirName: stand, frameIndex: 0, mirror: false };
    return                    { dirName: walk,  frameIndex: 0, mirror: true  };
  }
  return phase < 2
    ? { dirName: stand, frameIndex: 0, mirror: false }
    : { dirName: walk,  frameIndex: 0, mirror: false };
}
