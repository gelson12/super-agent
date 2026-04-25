// sprites.js — Sprite-sheet loader and 8-direction frame slicer.
//
// Each sheet is an 8x8 grid (1536x1024 source). Each row is one character;
// each column is one directional frame, in this order:
//   0 stand-left   1 stand-right   2 stand-up   3 stand-down
//   4 walk-left    5 walk-right    6 walk-up    7 walk-down

export const SHEET_COLS = 8;
export const SHEET_ROWS = 8;
export const CELL_W = 192;
export const CELL_H = 128;
// Use the alpha-keyed (white-background-removed) versions produced by
// preprocess_assets.py. Falls back to the originals if the alpha versions
// aren't present yet.
const SHEET_PATHS = {
  sheet_1: 'assets/sprites/sheet_1_alpha.png',
  sheet_2: 'assets/sprites/sheet_2_alpha.png',
  sheet_3: 'assets/sprites/sheet_3_alpha.png',
  sheet_4: 'assets/sprites/sheet_4_alpha.png',
  sheet_5: 'assets/sprites/sheet_5_alpha.png',
};
const SHEET_FALLBACK = {
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
    this.images = {};       // sheet_name -> HTMLImageElement
    this.placeholder = null;
  }

  async load() {
    const promises = Object.entries(SHEET_PATHS).map(([name, path]) => this._loadOne(name, path));
    await Promise.all(promises);
    this.placeholder = this._makePlaceholder();
    return this;
  }

  _loadOne(name, path) {
    return new Promise((resolve) => {
      const img = new Image();
      img.onload = () => { this.images[name] = img; resolve(); };
      img.onerror = () => {
        // Fall back to the original (non-alpha) sheet if the preprocessed
        // version isn't in the deploy yet.
        const fb = SHEET_FALLBACK[name];
        if (fb && fb !== path) {
          const fbImg = new Image();
          fbImg.onload = () => { this.images[name] = fbImg; resolve(); };
          fbImg.onerror = () => {
            console.warn(`[sprites] failed to load ${name} — using placeholder`);
            resolve();
          };
          fbImg.src = fb;
        } else {
          console.warn(`[sprites] failed to load ${path} — using placeholder`);
          resolve();
        }
      };
      img.src = path;
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

  // Returns the source rect for a (sheet, row, direction-name) frame.
  // Caller draws via ctx.drawImage(img, sx, sy, sw, sh, dx, dy, dw, dh).
  frame(sheet, row, dirName) {
    const img = this.images[sheet];
    const col = FRAME_COL[dirName] ?? 3;
    const sx = col * CELL_W;
    const sy = row * CELL_H;
    return img
      ? { img, sx, sy, sw: CELL_W, sh: CELL_H }
      : { img: this.placeholder, sx: 0, sy: 0, sw: CELL_W, sh: CELL_H };
  }
}

// Helper for the renderer/agent: pick the "stand-X" frame for a facing direction.
export function standFrameName(facing) {
  return ({ left:'stand_left', right:'stand_right', up:'stand_up', down:'stand_down' }[facing]) || 'stand_down';
}
export function walkFrameName(facing) {
  return ({ left:'walk_left', right:'walk_right', up:'walk_up', down:'walk_down' }[facing]) || 'walk_down';
}
