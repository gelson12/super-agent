// sprites.js — Sprite-sheet loader and 8-direction frame slicer.
//
// Two sheet formats are supported, picked per-bot:
//
//  1. Multi-bot grid (sheet_1..5): 8x8 grid, 192x128 cells. Each row = one
//     character; columns 0-7 = stand-L/R/U/D + walk-L/R/U/D. Used as a
//     fallback when a bot has no per-bot strip.
//
//  2. Per-bot strip (assets/sprites/bots/<id>.png): single row, 8 cells
//     across, ~177x177 each. Same column order as the grid. Smaller asset,
//     better visual fidelity for the 5 bots that have a hand-picked sheet.
//
// Frame columns:
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

export class SpriteCache {
  constructor() {
    this.gridImages = {};       // sheet name -> HTMLImageElement
    this.botImages = {};        // bot id   -> { img, cellW, cellH }
    this.placeholder = null;
  }

  async load(botList = []) {
    // Load multi-bot grid sheets in parallel.
    await Promise.all(Object.entries(GRID_PATHS).map(([n, p]) => this._loadGrid(n, p)));
    // Load per-bot strips for bots that declare `botSprite`.
    await Promise.all(botList
      .filter(b => b.botSprite)
      .map(b => this._loadBotStrip(b.id, `assets/sprites/bots/${b.botSprite}`)));
    this.placeholder = this._makePlaceholder();
    return this;
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

  // Returns the source-image rect for a (bot, dirName) frame.
  // Per-bot strip is preferred; falls back to the (sheet, row) grid lookup.
  frame(bot, dirName) {
    const col = FRAME_COL[dirName] ?? 3;
    const strip = this.botImages[bot.id];
    if (strip && strip.img) {
      return {
        img: strip.img,
        sx: col * strip.cellW, sy: 0,
        sw: strip.cellW,        sh: strip.cellH,
      };
    }
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
}

// Helpers for the renderer.

export function standFrameName(facing) {
  return ({ left:'stand_left', right:'stand_right', up:'stand_up', down:'stand_down' }[facing]) || 'stand_down';
}
export function walkFrameName(facing) {
  return ({ left:'walk_left', right:'walk_right', up:'walk_up', down:'walk_down' }[facing]) || 'walk_down';
}

// 4-phase walk cycle: STAND → WALK → STAND → WALK_MIRROR.
// Returns { dirName, mirror } so the renderer can flip the walk frame
// horizontally for left/right (creating the "opposite leg forward" pose
// without extra art). Up/down don't mirror; they get a slightly faster
// stand/walk alternation plus the procedural bob in the renderer.
export function walkCycleFrame(facing, now, frameMs = 180) {
  const phase = Math.floor(now / frameMs) & 3;       // 0,1,2,3
  if (facing === 'left' || facing === 'right') {
    if (phase === 0)      return { dirName: standFrameName(facing), mirror: false };
    else if (phase === 1) return { dirName: walkFrameName(facing),  mirror: false };
    else if (phase === 2) return { dirName: standFrameName(facing), mirror: false };
    else                  return { dirName: walkFrameName(facing),  mirror: true  };
  }
  // up/down — alternate stand/walk every two phases (no mirror).
  return phase < 2
    ? { dirName: standFrameName(facing), mirror: false }
    : { dirName: walkFrameName(facing),  mirror: false };
}
