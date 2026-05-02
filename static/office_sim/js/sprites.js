// sprites.js — Sprite-sheet loader and 8-direction frame slicer.
//
// THREE sprite formats are supported, in priority order per bot:
//
//  1. Per-bot individual frames (assets/sprites/bots/<folder>/<frame>.png)
//     RECOMMENDED. Drop one PNG per (direction, walk frame). Folder name
//     can be the bot id (`ceo/`, `crypto/`) OR an alias the loader knows
//     about (`crypto_alpha/`, `chief_security_officer/`, `project_manager/`,
//     `website_designer/`, `accountant/` for finance, etc. — see
//     FOLDER_ALIASES below).
//
//     Per direction the loader probes `walk_<dir>.png` plus `walk_<dir>_2..N.png`
//     for as many extra frames as you provide. Any 1–N frames per direction
//     work; missing files are skipped silently. Required-ish per direction:
//       stand_<dir>.png   walk_<dir>.png   (extras are optional)
//
//  2. Per-bot horizontal strip (assets/sprites/bots/<id>.png)
//     Single row, 8 cells. Same column order as the grid.
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
// in cycle order (stand → walk_1 → walk_2 → ...). Probes up to _8 so any
// number of frames you have works. Missing files are skipped silently.
const BOT_DIR_FRAMES = {
  stand_left:  ['stand_left.png'],
  stand_right: ['stand_right.png'],
  stand_up:    ['stand_up.png'],
  stand_down:  ['stand_down.png'],
  walk_left:   ['walk_left.png',  'walk_left_2.png',  'walk_left_3.png',  'walk_left_4.png',  'walk_left_5.png',  'walk_left_6.png',  'walk_left_7.png',  'walk_left_8.png'],
  walk_right:  ['walk_right.png', 'walk_right_2.png', 'walk_right_3.png', 'walk_right_4.png', 'walk_right_5.png', 'walk_right_6.png', 'walk_right_7.png', 'walk_right_8.png'],
  walk_up:     ['walk_up.png',    'walk_up_2.png',    'walk_up_3.png',    'walk_up_4.png',    'walk_up_5.png',    'walk_up_6.png',    'walk_up_7.png',    'walk_up_8.png'],
  walk_down:   ['walk_down.png',  'walk_down_2.png',  'walk_down_3.png',  'walk_down_4.png',  'walk_down_5.png',  'walk_down_6.png',  'walk_down_7.png',  'walk_down_8.png'],
};

// Folder-name aliases. The user named some folders with the verbose backend
// identity (chief_security_officer/) and others with the bot id (cso/) and
// others with an _alpha suffix (crypto_alpha/). The loader probes ALL these
// candidates per bot — first one that yields any frames wins.
const FOLDER_ALIASES = {
  ceo:            ['ceo', 'ceo_alpha'],
  cto:            ['cto', 'cto_alpha'],
  coo:            ['coo', 'coo_alpha'],
  chief_of_staff: ['chief_of_staff', 'chief_of_staff_alpha', 'Chief of Staff'],
  cso:            ['cso', 'cso_alpha', 'chief_security_officer', 'chief_security_officer_alpha', 'Chief Security Officer'],
  researcher:     ['researcher', 'researcher_alpha', 'Researcher'],
  pm:             ['pm', 'pm_alpha', 'project_manager', 'project_manager_alpha', 'project manager'],
  marketing:      ['marketing', 'marketing_alpha'],
  finance:        ['finance', 'finance_alpha', 'accountant', 'accountant_alpha', 'Finance_accounting'],
  website:        ['website', 'website_alpha', 'website_designer', 'website_designer_alpha', 'Programmer'],
  cleaner:        ['cleaner', 'cleaner_alpha'],
  crypto:         ['crypto', 'crypto_alpha'],
  scholar:        ['scholar', 'scholar_alpha'],
  nova:           ['nova', 'nova_alpha'],
  writer:         ['writer', 'writer_alpha', 'ai_writer'],
  cro:            ['cro', 'Chief Revenue Optimizer'],
  // Reference identities (extra characters from animated_office_bots/)
  programmer:     ['programmer', 'programmer_alpha'],
  hacker:         ['hacker', 'hacker_alpha'],
  chairman:       ['chairman', 'chairman_alpha', 'cob', 'cob_alpha'],
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
    // Walk the alias list — first folder that yields any frames wins.
    const candidates = FOLDER_ALIASES[botId] || [botId];
    for (const folder of candidates) {
      const base = `assets/sprites/bots/${folder}`;
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
            loaded.push(this._stripWhiteBg(img));
          } catch { /* missing — fine */ }
        }
        if (loaded.length) { frames[dir] = loaded; any = true; }
      }
      if (any) {
        this.botFrames[botId] = frames;
        const ref = frames.stand_down?.[0]
                 || frames.stand_left?.[0]
                 || frames.stand_right?.[0]
                 || frames.stand_up?.[0]
                 || Object.values(frames)[0]?.[0];
        const refH = ref ? (ref.naturalHeight ?? ref.height) : 170;
        const refW = ref ? (ref.naturalWidth  ?? ref.width)  : 120;
        this.botFrames[botId]._refScale = { refW, refH };
        const dirCount = Object.keys(frames).filter(k => !k.startsWith('_')).length;
        console.log(`[sprites] bot ${botId} from "${folder}": ${dirCount} directions, ref ${refW}x${refH}`);
        return;
      }
    }
  }

  // Flood-fill white/near-white background from the 4 corners and replace with
  // transparent pixels. Returns an OffscreenCanvas-like HTMLCanvasElement.
  // Falls back silently if getImageData is blocked (tainted canvas, old browser).
  _stripWhiteBg(img) {
    const srcW = img.naturalWidth || img.width || 0;
    const srcH = img.naturalHeight || img.height || 0;
    const c = document.createElement('canvas');
    c.width = srcW; c.height = srcH;
    if (!srcW || !srcH) { c.naturalWidth = 0; c.naturalHeight = 0; return c; }
    const ctx = c.getContext('2d', { willReadFrequently: true });
    ctx.drawImage(img, 0, 0);

    let data, imgData;
    try {
      imgData = ctx.getImageData(0, 0, srcW, srcH);
      data = imgData.data;
    } catch {
      c.naturalWidth = srcW; c.naturalHeight = srcH;
      return c; // CORS / security block — use raw canvas
    }

    // Detect background colour from corners (must be opaque and bright).
    let bgR = 255, bgG = 255, bgB = 255, hasBg = false;
    for (const [cx, cy] of [[0,0],[srcW-1,0],[0,srcH-1],[srcW-1,srcH-1]]) {
      const p = (cy * srcW + cx) * 4;
      const [r, g, b, a] = [data[p], data[p+1], data[p+2], data[p+3]];
      if (a > 200 && r > 180 && g > 180 && b > 180) {
        bgR = r; bgG = g; bgB = b; hasBg = true; break;
      }
    }
    if (!hasBg) { // already transparent or dark background
      ctx.putImageData(imgData, 0, 0);
      c.naturalWidth = srcW; c.naturalHeight = srcH;
      return c;
    }

    // BFS flood-fill from all 4 corners — erase connected background region.
    const visited = new Uint8Array(srcW * srcH);
    const queue = [];
    const tol = 38;
    for (const [cx, cy] of [[0,0],[srcW-1,0],[0,srcH-1],[srcW-1,srcH-1]]) {
      const idx = cy * srcW + cx;
      if (!visited[idx]) { visited[idx] = 1; queue.push(idx); }
    }
    let qi = 0;
    while (qi < queue.length) {
      const idx = queue[qi++];
      data[idx * 4 + 3] = 0;
      const x = idx % srcW, y = (idx / srcW) | 0;
      for (const n of [
        x > 0      ? idx - 1    : -1,
        x < srcW-1 ? idx + 1    : -1,
        y > 0      ? idx - srcW : -1,
        y < srcH-1 ? idx + srcW : -1,
      ]) {
        if (n < 0 || visited[n]) continue;
        const np = n * 4;
        if (data[np+3] < 10) { visited[n] = 1; queue.push(n); continue; }
        if (Math.abs(data[np]-bgR) < tol && Math.abs(data[np+1]-bgG) < tol && Math.abs(data[np+2]-bgB) < tol) {
          visited[n] = 1; queue.push(n);
        }
      }
    }
    ctx.putImageData(imgData, 0, 0);
    c.naturalWidth = srcW; c.naturalHeight = srcH;
    return c;
  }

  // Reference scale for a bot — { refW, refH } pulled from the stand frame
  // when individual frames are loaded. Renderer multiplies frame.canvas size
  // by (target_height / refH) so all frames render at a consistent
  // pixels-per-canvas-px scale. Returns null when the bot doesn't use
  // individual frames.
  refScale(botId) {
    return this.botFrames[botId]?._refScale || null;
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
      const sw = img.naturalWidth  ?? img.width  ?? 120;
      const sh = img.naturalHeight ?? img.height ?? 170;
      return { img, sx: 0, sy: 0, sw, sh };
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
    const dir = this.botFrames[bot.id]?.[`walk_${facing}`];
    return Array.isArray(dir) ? dir.length : 1;
  }

  // Does this bot use individual-frame assets? When true the renderer
  // skips the mirror trick (real frames carry the leg motion).
  hasIndividualFrames(botId) {
    return !!this.botFrames[botId];
  }

  // Re-fetch this bot's individual-frame assets after a sprite-editor
  // upload. Cache-busts via ?v=<ts> so the browser picks up fresh files.
  async reloadBot(botId) {
    delete this.botFrames[botId];
    const v = `?v=${Date.now()}`;
    const candidates = FOLDER_ALIASES[botId] || [botId];
    for (const folder of candidates) {
      const base = `assets/sprites/bots/${folder}`;
      const frames = {};
      let any = false;
      for (const [dir, files] of Object.entries(BOT_DIR_FRAMES)) {
        const loaded = [];
        for (const file of files) {
          try {
            const img = new Image();
            img.src = `${base}/${file}${v}`;
            await img.decode();
            loaded.push(this._stripWhiteBg(img));
          } catch { /* missing — fine */ }
        }
        if (loaded.length) { frames[dir] = loaded; any = true; }
      }
      if (any) {
        this.botFrames[botId] = frames;
        const ref = frames.stand_down?.[0]
                 || frames.stand_left?.[0]
                 || frames.stand_right?.[0]
                 || frames.stand_up?.[0]
                 || Object.values(frames)[0]?.[0];
        const refH = ref ? (ref.naturalHeight ?? ref.height) : 170;
        const refW = ref ? (ref.naturalWidth  ?? ref.width)  : 120;
        this.botFrames[botId]._refScale = { refW, refH };
        return true;
      }
    }
    return false;
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
// Behaviour by what the bot has loaded:
//   walkN ≥ 2 real frames → cycle stand → walk_1 → walk_2 → ... → walk_N.
//                            No mirror (real frames carry leg motion).
//   walkN === 1 (single)  → only when the bot has a folder with just
//                            walk_<dir>.png. Cycle stand → walk → stand → walk.
//                            Still no mirror — feels less like a flicker.
//   No individual frames  → strip/grid path. Mirror trick used for L/R.
export function walkCycleFrame(facing, now, sprites, bot, frameMs = 220) {
  const stand = standFrameName(facing);
  const walk = walkFrameName(facing);
  const usingIndividual = sprites?.hasIndividualFrames?.(bot.id);

  if (usingIndividual) {
    const walkN = sprites.walkFrameCount(bot, facing);
    const totalPhases = 1 + walkN;
    const phase = Math.floor(now / frameMs) % totalPhases;
    if (phase === 0) return { dirName: stand, frameIndex: 0, mirror: false };
    return { dirName: walk, frameIndex: phase - 1, mirror: false };
  }

  // Strip/grid path — keep the mirror synthesis for L/R single-frame walks.
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
