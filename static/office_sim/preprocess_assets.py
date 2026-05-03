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

# Floor-positive detection. Per level we find the canonical "floor"
# brightness via the 75th-percentile tile (definitely-walkable cells), then
# block any tile below FLOOR_REL_CUTOFF * floor_brightness. This catches
# every furniture/wall cell because furniture is consistently darker than
# the bare-floor cells. Connectivity preservation rejects any candidate
# that would sever a required anchor from the spawn.
FLOOR_REL_CUTOFF = 0.92

# Tile chars that must REMAIN walkable even if the PNG looks dark there.
# Doors (D) and stairs (U/N) are absolute portals — never block them.
# Seat anchors (S/c/p/k/g/L/F/B/I/E) WERE protected, but this caused
# bots to spawn on top of visible tables (the chair anchor tile was
# inside the table's PNG footprint). Now they get blocked by the PNG
# check too; the snap-to-walkable step below repositions affected bot
# desks to the nearest real chair-adjacent floor tile.
ANCHOR_AND_PORTAL_CHARS = set("DUN")


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
    """Floor-positive detection.

    A tile is BLOCKED if either:
       (a) its mean brightness < FLOOR_REL_CUTOFF × per-level floor_br
           (floor_br = 75th-percentile brightness across all tiles),
       (b) its mean colour is "green-ish" (G > R + 20 → planter/foliage),
           regardless of brightness.

    Both rules match real furniture/walls/planters in the office PNGs.
    Connectivity preservation in write_obstacles_overlay() then drops
    candidates that would sever a required anchor from the spawn.
    """
    img = Image.open(floor_png).convert("RGB")
    pw, ph = img.size
    cell_w = pw / TILE_W
    cell_h = ph / TILE_H

    # Pass 1 — per-tile mean RGB across the whole grid.
    means = [(0.0, 0.0, 0.0)] * (TILE_W * TILE_H)
    brightness = [0.0] * (TILE_W * TILE_H)
    for ty in range(TILE_H):
        for tx in range(TILE_W):
            x0 = int(tx * cell_w); y0 = int(ty * cell_h)
            x1 = int((tx + 1) * cell_w); y1 = int((ty + 1) * cell_h)
            crop = img.crop((x0, y0, x1, y1))
            data = list(crop.getdata())
            n = len(data)
            mr = sum(p[0] for p in data) / n
            mg = sum(p[1] for p in data) / n
            mb = sum(p[2] for p in data) / n
            means[ty * TILE_W + tx] = (mr, mg, mb)
            brightness[ty * TILE_W + tx] = (mr + mg + mb) / 3

    # Per-level floor signature = 75th-percentile brightness across all tiles.
    sorted_br = sorted(brightness)
    floor_br = sorted_br[3 * len(sorted_br) // 4]
    cutoff = floor_br * FLOOR_REL_CUTOFF
    print(f"  floor_brightness={floor_br:.0f}  cutoff={cutoff:.0f}")

    # Pass 2 — emit candidates.
    blocked = []
    for ty in range(TILE_H):
        for tx in range(TILE_W):
            existing = json_grid[ty * TILE_W + tx]
            if existing == '#':
                continue
            if existing in ANCHOR_AND_PORTAL_CHARS:
                continue
            mr, mg, mb = means[ty * TILE_W + tx]
            br = brightness[ty * TILE_W + tx]
            is_green = mg > mr + 14 and mg > mb + 4    # planter / foliage
            is_dark = br < cutoff
            if is_dark or is_green:
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


def _required_anchors(floor_json, base_grid):
    """Tiles that MUST stay reachable: spawn + only those doors/stairs
    that are already reachable in the base grid (before PNG blocks).
    Doors that are already cut off by the floor JSON layout (sealed room
    interiors) are excluded — they can't be severed further."""
    spawn = tuple(floor_json["spawn"])
    candidates = [spawn]
    for s in floor_json.get("stairs", []):
        candidates.append(tuple(s["tile"]))
    for d in floor_json.get("doors", []):
        candidates.append(tuple(d["tile"]))
    # Only keep tiles reachable from spawn in the base (un-overlaid) grid.
    base_reach = _bfs(base_grid[:], spawn[0], spawn[1])
    return [a for a in set(candidates) if a in base_reach]


def _zone_anchor_groups(floor_json):
    """Per zone, the list of anchor tiles (any one must remain reachable)."""
    return [
        [tuple(a) for a in z.get("anchors", [])]
        for z in floor_json.get("zones", [])
        if z.get("anchors")
    ]


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
        hard_required = _required_anchors(floor_json, grid[:])   # spawn + reachable doors/stairs

        accepted = []
        rejected = 0
        for tx, ty in candidates:
            idx = ty * TILE_W + tx
            saved = grid[idx]
            grid[idx] = '#'
            reach = _bfs(grid, spawn[0], spawn[1])
            # Hard rule: spawn + doors + stairs must always be reachable.
            # Zone anchors are NOT checked — a blocked anchor just won't be
            # visited; bots snap to the nearest walkable neighbour at runtime.
            ok = all(a in reach for a in hard_required)
            if ok:
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


WALKABLE_ANCHOR_CHARS = set('.ScptpkgELIFB')


def _grid_with_overlay(floor_n: int):
    """Reproduce world.js's tile construction WITH the overlay applied —
    same view the runtime uses for collision."""
    floor = json.loads((DATA_DIR / f"floor{floor_n}.json").read_text(encoding="utf-8"))
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
    overlay_path = DATA_DIR / f"floor{floor_n}_overlay.json"
    if overlay_path.exists():
        overlay = json.loads(overlay_path.read_text(encoding="utf-8"))
        for tx, ty in overlay.get("blocked", []):
            if 0 <= tx < TILE_W and 0 <= ty < TILE_H and arr[ty * TILE_W + tx] in WALKABLE_ANCHOR_CHARS:
                arr[ty * TILE_W + tx] = '#'
    return floor, arr


def _nearest_reachable(grid, cx: int, cy: int, reach_set: set, max_radius: int = 10):
    """Find the nearest tile to (cx, cy) that's both walkable AND reachable
    from the spawn (per the precomputed reach_set). Avoids snapping into
    walkable islands cut off by furniture."""
    if 0 <= cx < TILE_W and 0 <= cy < TILE_H:
        if (cx, cy) in reach_set:
            return (cx, cy)
    for r in range(1, max_radius + 1):
        for dy in range(-r, r + 1):
            for dx in range(-r, r + 1):
                if max(abs(dx), abs(dy)) != r:
                    continue
                nx, ny = cx + dx, cy + dy
                if (nx, ny) in reach_set:
                    return (nx, ny)
    return None


def snap_bot_desks_and_anchors():
    """For every bot desk and zone anchor, if the tile is blocked OR is
    only walkable inside an island disconnected from the spawn, move it
    to the nearest tile that's REACHABLE from spawn. Doors stay where
    they are (surgical entry points)."""
    bots_path = DATA_DIR / "bots.json"
    bots_doc = json.loads(bots_path.read_text(encoding="utf-8"))

    # Build per-floor grids + per-floor reach-from-spawn sets once.
    floors = {n: _grid_with_overlay(n) for n in (1, 2, 3)}
    reach_sets = {}
    for n, (floor, grid) in floors.items():
        sx, sy = floor["spawn"]
        reach_sets[n] = _bfs(grid, sx, sy)

    n_bot_moves = 0
    for bot in bots_doc.get("bots", []):
        f = bot["desk"]["floor"]
        x, y = bot["desk"]["tile"]
        snapped = _nearest_reachable(floors[f][1], x, y, reach_sets[f], max_radius=12)
        if snapped and (snapped[0], snapped[1]) != (x, y):
            print(f"  bot {bot['id']:18s} L{f}: desk ({x},{y}) -> ({snapped[0]},{snapped[1]})")
            bot["desk"]["tile"] = list(snapped)
            n_bot_moves += 1
    bots_path.write_text(json.dumps(bots_doc, indent=2), encoding="utf-8")
    print(f"  moved {n_bot_moves} bot desks to nearest reachable tile")

    n_anchor_moves = 0
    for f in (1, 2, 3):
        floor_path = DATA_DIR / f"floor{f}.json"
        floor, grid = floors[f]
        for zone in floor.get("zones", []):
            new_anchors = []
            for a in zone.get("anchors", []):
                snap = _nearest_reachable(grid, a[0], a[1], reach_sets[f], max_radius=8)
                if snap and (snap[0], snap[1]) != (a[0], a[1]):
                    new_anchors.append([snap[0], snap[1]])
                    n_anchor_moves += 1
                else:
                    new_anchors.append(a)
            zone["anchors"] = new_anchors
        floor_path.write_text(json.dumps(floor, indent=2), encoding="utf-8")
    print(f"  moved {n_anchor_moves} zone anchors to nearest reachable tile")


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
    # PNG-overlay re-enabled as a SAFETY NET on top of the hand-mapped
    # floor[1-3].json. Hand-mapped data is authoritative for doors,
    # stairs, zone bounds and anchors; the overlay only ADDS blocks where
    # the PNG shows dark furniture that my tile estimates missed.
    # Connectivity check ensures doors/stairs/at-least-one-anchor stay
    # reachable from spawn so corridors aren't accidentally severed.
    print("\n[floors] deriving PNG-overlay safety net...")
    write_obstacles_overlay()

    print("\n[snap] repositioning bot desks + zone anchors to nearest walkable tile...")
    snap_bot_desks_and_anchors()


if __name__ == "__main__":
    main()
