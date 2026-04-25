"""Pre-process the 5 sprite sheets and 3 floor maps so the runtime gets:

1. Alpha-keyed sprites (`sheet_N_alpha.png`): near-white pixels become fully
   transparent. This kills the visible white box around each bot.

2. PNG-derived obstacle map for each floor (`data/floor*.json` is rewritten by
   `build_floors.py` ahead of time, but here we *augment* it with extra blocked
   tiles where the floor PNG shows dark furniture/walls in cells my hand-mapped
   layout missed).

Run:
    python preprocess_assets.py
"""
from __future__ import annotations
import json
from pathlib import Path
from PIL import Image

ROOT = Path(__file__).parent
SPRITES_DIR = ROOT / "assets" / "sprites"
FLOORS_DIR = ROOT / "assets" / "floors"
DATA_DIR = ROOT / "data"

# ─── Sprite sheets — alpha-key near-white pixels ─────────────────────────
WHITE_THRESHOLD = 235        # any pixel where R, G, B all >= this → transparent
EDGE_FEATHER = 6             # pixels gradient transparency below the threshold


def alpha_key_sheet(src: Path, dst: Path) -> None:
    img = Image.open(src).convert("RGBA")
    px = img.load()
    w, h = img.size
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            if r >= WHITE_THRESHOLD and g >= WHITE_THRESHOLD and b >= WHITE_THRESHOLD:
                # Soften the edge: pixels just below threshold get partial alpha
                # so the silhouette doesn't look razor-edged.
                px[x, y] = (r, g, b, 0)
            else:
                # Anti-alias near-white edges by reducing alpha proportionally.
                m = min(r, g, b)
                if m >= WHITE_THRESHOLD - EDGE_FEATHER:
                    falloff = (WHITE_THRESHOLD - m) / EDGE_FEATHER
                    px[x, y] = (r, g, b, int(a * falloff))
                else:
                    pass  # opaque interior pixels untouched
    img.save(dst, optimize=True)
    print(f"  alpha-keyed {src.name} -> {dst.name}")


# ─── Floor PNG → blocked-tile overlay ────────────────────────────────────
TILE_W, TILE_H = 64, 40

# A tile is flagged FURNITURE only if BOTH conditions:
#   (a) its mean brightness < FURNITURE_MEAN_MAX (256-scale, RGB sum / 3)
#   (b) ≥ FURNITURE_DARK_FRAC of its pixels are below 95 brightness
# Tightened from (95, 0.62) to (130, 0.45) so we catch lighter wood
# desktops too. Connectivity check still drops any block that would
# sever a required anchor from the spawn — corridors stay open.
FURNITURE_MEAN_MAX = 130
FURNITURE_DARK_FRAC = 0.45

# Tile chars that must REMAIN walkable even if the PNG looks dark there:
ANCHOR_AND_PORTAL_CHARS = set("DUNScptpkgELIFB")


def _build_grid_from_floor_json(json_path: Path):
    """Reproduce world.js's tile-grid construction so we know which tiles
    are anchors/doors/stairs and shouldn't be blocked."""
    floor = json.loads(json_path.read_text(encoding="utf-8"))
    arr = ['.'] * (TILE_W * TILE_H)
    for ob in floor.get("obstacles", []):
        x, y, w, h, ch = ob
        for yy in range(y, y + h):
            for xx in range(x, x + w):
                if 0 <= xx < TILE_W and 0 <= yy < TILE_H:
                    arr[yy * TILE_W + xx] = ch
    for x in range(TILE_W):
        arr[x] = '#'; arr[(TILE_H - 1) * TILE_W + x] = '#'
    for y in range(TILE_H):
        arr[y * TILE_W] = '#'; arr[y * TILE_W + TILE_W - 1] = '#'
    return arr


def derive_obstacles(floor_png: Path, json_grid):
    img = Image.open(floor_png).convert("RGB")
    pw, ph = img.size
    cell_w = pw / TILE_W
    cell_h = ph / TILE_H
    blocked = []
    for ty in range(TILE_H):
        for tx in range(TILE_W):
            existing = json_grid[ty * TILE_W + tx]
            if existing == '#':
                continue   # already blocked
            if existing in ANCHOR_AND_PORTAL_CHARS:
                continue   # protect anchors/doors/stairs
            x0 = int(tx * cell_w); y0 = int(ty * cell_h)
            x1 = int((tx + 1) * cell_w); y1 = int((ty + 1) * cell_h)
            crop = img.crop((x0, y0, x1, y1))
            data = list(crop.getdata())
            n = len(data)
            sum_b = 0; dark = 0
            for r, g, b in data:
                br = r + g + b
                sum_b += br
                if br < 285:    # 95*3 = 285 (per-pixel sum threshold)
                    dark += 1
            mean = sum_b / (n * 3)
            if mean < FURNITURE_MEAN_MAX and dark / n > FURNITURE_DARK_FRAC:
                blocked.append([tx, ty])
    return blocked


WALKABLE = set('.DUNScptpkgELIFB')


def _bfs(grid, sx, sy):
    if grid[sy * TILE_W + sx] not in WALKABLE:
        return set()
    seen = {(sx, sy)}
    stack = [(sx, sy)]
    while stack:
        x, y = stack.pop()
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, ny = x + dx, y + dy
            if 0 <= nx < TILE_W and 0 <= ny < TILE_H and (nx, ny) not in seen:
                if grid[ny * TILE_W + nx] in WALKABLE:
                    seen.add((nx, ny)); stack.append((nx, ny))
    return seen


def _required_anchors(floor_json):
    """Tiles that MUST stay reachable: spawn + every zone anchor + stair tiles."""
    out = []
    out.append(tuple(floor_json["spawn"]))
    for z in floor_json.get("zones", []):
        for a in z.get("anchors", []):
            out.append((a[0], a[1]))
    for s in floor_json.get("stairs", []):
        out.append(tuple(s["tile"]))
    return list(set(out))


def write_obstacles_overlay():
    """Connectivity-preserving overlay: try to add each PNG-derived blocked
    tile, but skip any that would sever a required anchor from the spawn.
    Score candidates by "darkness intensity" so the worst offenders get
    proposed first."""
    for n in (1, 2, 3):
        floor_png = FLOORS_DIR / f"level{n}.png"
        floor_json_path = DATA_DIR / f"floor{n}.json"
        if not floor_png.exists() or not floor_json_path.exists():
            print(f"  skip floor {n} — missing input")
            continue
        floor_json = json.loads(floor_json_path.read_text(encoding="utf-8"))
        grid = _build_grid_from_floor_json(floor_json_path)
        candidates = derive_obstacles(floor_png, grid)
        spawn = tuple(floor_json["spawn"])
        anchors = _required_anchors(floor_json)

        # Sort candidates by distance from corridors first (try blocking
        # cells far from spawn first; saves a lot of breakage).
        # Simple heuristic: keep input order — they're scanned row-by-row.
        accepted = []
        rejected = 0
        for tx, ty in candidates:
            idx = ty * TILE_W + tx
            saved = grid[idx]
            grid[idx] = '#'
            reach = _bfs(grid, spawn[0], spawn[1])
            if all(a in reach for a in anchors):
                accepted.append([tx, ty])
            else:
                grid[idx] = saved
                rejected += 1
        out = DATA_DIR / f"floor{n}_overlay.json"
        out.write_text(json.dumps({"blocked": accepted}, separators=(',', ':')), encoding="utf-8")
        print(f"  floor {n}: accepted {len(accepted)} blocks, rejected {rejected} (would sever connectivity)")


def alpha_key_inplace(path: Path) -> bool:
    """Alpha-key a PNG in place. No-op if it already has meaningful alpha."""
    img = Image.open(path).convert("RGBA")
    px = img.load()
    w, h = img.size
    # Sample to detect existing transparency
    sample = []
    for sy in range(0, h, max(1, h // 20)):
        for sx in range(0, w, max(1, w // 20)):
            sample.append(px[sx, sy][3])
    if sum(1 for a in sample if a < 32) / max(1, len(sample)) > 0.05:
        return False     # already alpha — leave alone
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            if r >= WHITE_THRESHOLD and g >= WHITE_THRESHOLD and b >= WHITE_THRESHOLD:
                px[x, y] = (r, g, b, 0)
            else:
                m = min(r, g, b)
                if m >= WHITE_THRESHOLD - EDGE_FEATHER:
                    falloff = (WHITE_THRESHOLD - m) / EDGE_FEATHER
                    px[x, y] = (r, g, b, int(a * falloff))
    img.save(path, optimize=True)
    return True


def alpha_key_individual_frames():
    """Walk assets/sprites/bots/<folder>/*.png and alpha-key any white-bg
    frames in place. Skips frames that already have transparency."""
    bots_dir = SPRITES_DIR / "bots"
    if not bots_dir.is_dir():
        print("  no bots dir")
        return
    for sub in sorted(bots_dir.iterdir()):
        if not sub.is_dir():
            continue
        for f in sorted(sub.glob("*.png")):
            try:
                if alpha_key_inplace(f):
                    print(f"  alpha-keyed {sub.name}/{f.name}")
            except Exception as e:
                print(f"  ERR {sub.name}/{f.name}: {e}")


def main():
    print("[sprites] alpha-keying multi-bot grid sheets...")
    for n in (1, 2, 3, 4, 5):
        src = SPRITES_DIR / f"sheet_{n}.png"
        dst = SPRITES_DIR / f"sheet_{n}_alpha.png"
        if src.exists():
            alpha_key_sheet(src, dst)
        else:
            print(f"  missing {src}")
    print("\n[sprites] alpha-keying per-bot strips (assets/sprites/bots/<id>.png)...")
    bots_dir = SPRITES_DIR / "bots"
    if bots_dir.is_dir():
        for src in sorted(bots_dir.glob("*.png")):
            if src.stem.endswith("_alpha"):
                continue
            dst = src.parent / f"{src.stem}_alpha.png"
            alpha_key_sheet(src, dst)
    else:
        print("  no per-bot strips dir — skipping")
    print("\n[sprites] alpha-keying individual frame PNGs in bots/<folder>/...")
    alpha_key_individual_frames()
    print("\n[floors] deriving obstacle overlays from PNGs...")
    write_obstacles_overlay()


if __name__ == "__main__":
    main()
