"""Hand-mapped floor data generator for the office simulation.

Replaces the brightness-heuristic PNG-overlay with ground-truth tile data
authored from the user's annotated screenshots (red=solid, blue=door,
yellow/purple/brown=stairs, pink=phone booths, lime=snooker, dark-red=
ping-pong, black=relax zone, green=monitors on desks, white=scheduled
meeting room).

Run:  python build_floors_v2.py
Outputs: data/floor1.json, floor2.json, floor3.json (overwrites).

Design notes:
- 64x40 logical tile grid, ~24x25 px per tile against the 1536x1024 PNGs.
- Every solid object the user circled in red becomes a '#' rectangle.
- Doors are single 'D' tiles inside the room wall — bots route through
  them surgically (the runtime hard-requires every door to remain
  reachable from spawn).
- Stair tiles are 'U' (up) or 'N' (down) with a paired entry/exit pair
  in `stairs[]` so bots can teleport between floors after walking onto
  the tile.
- Zone anchors are placed AT chair positions next to tables, NOT on the
  table itself — this is how bots avoid spawning inside furniture.
"""

from __future__ import annotations
import json
from pathlib import Path

REPO = Path(__file__).resolve().parent
OUT = REPO / "data"

TILE_W, TILE_H = 64, 40


# ─── Helpers ──────────────────────────────────────────────────────

def rect(x, y, w, h, char="#"):
    return [x, y, w, h, char]


def stamp_anchors(char, points):
    return [[p[0], p[1], 1, 1, char] for p in points]


def make_room(name, ztype, x, y, w, h, *, doors, anchors, anchor_char='S', extra_walls=None):
    """A walled room with one or more doors. `doors` is a list of (dx, dy)
    tiles (must lie on the wall perimeter). Anchors are listed inside the
    room and stamped as the given char (S/c/p/L/F/etc.)."""
    obs = []
    door_set = set((d[0], d[1]) for d in doors)
    # Walls: top, bottom, left, right
    for xx in range(x, x + w):
        if (xx, y) not in door_set: obs.append([xx, y, 1, 1, "#"])
        if (xx, y + h - 1) not in door_set: obs.append([xx, y + h - 1, 1, 1, "#"])
    for yy in range(y + 1, y + h - 1):
        if (x, yy) not in door_set: obs.append([x, yy, 1, 1, "#"])
        if (x + w - 1, yy) not in door_set: obs.append([x + w - 1, yy, 1, 1, "#"])
    # Doors stamped explicitly
    for d in doors:
        obs.append([d[0], d[1], 1, 1, "D"])
    # Anchors inside
    for a in anchors:
        obs.append([a[0], a[1], 1, 1, anchor_char])
    # Extra walls inside the room (e.g., partitions, table blocks)
    if extra_walls:
        obs.extend(extra_walls)
    zone = {
        "id": name, "type": ztype,
        "bounds": [x, y, w, h],
        "anchors": [list(a) for a in anchors],
    }
    return obs, zone, [{"tile": [d[0], d[1]], "connects": [name, "open_floor"]} for d in doors]


def desk_cluster(name, x, y, *, w=8, h=4, anchors=None):
    """A desk cluster — central desk surface (blocked) with chair anchors
    around the perimeter. Anchors are work positions where bots `atDesk`."""
    obs = []
    # Desk surface: inner rectangle of '#' (blocks pass-through)
    obs.append([x + 1, y + 1, w - 2, h - 2, "#"])
    # Default chair anchors at perimeter corners + mid-edges if not provided
    if anchors is None:
        anchors = [
            (x, y + h // 2),                # west
            (x + w - 1, y + h // 2),        # east
            (x + w // 2, y),                # north
            (x + w // 2, y + h - 1),        # south
        ]
    for a in anchors:
        obs.append([a[0], a[1], 1, 1, "S"])
    return obs, {
        "id": name, "type": "desk_cluster",
        "bounds": [x, y, w, h],
        "anchors": [list(a) for a in anchors],
    }


# ─── LEVEL 1 ──────────────────────────────────────────────────────

def floor1():
    """L1: Entry, meeting room (NW), coffee (N centre), TV lounge (NE),
    ideas board (W mid), 6 desk clusters (S half), entry door (S centre),
    stairs to L2 (E)."""
    obs, zones, doors = [], [], []

    # Conference / Meeting room — top-left (white-circle reference)
    o, z, d = make_room("meeting_l1", "meeting_room", x=2, y=2, w=22, h=16,
                        doors=[(11, 17)],
                        anchors=[
                            (5, 6), (8, 6), (11, 6), (14, 6), (17, 6), (20, 6),
                            (5, 13), (8, 13), (11, 13), (14, 13), (17, 13), (20, 13),
                        ],
                        # Conference table block in the centre
                        extra_walls=[[5, 8, 16, 4, "#"]])
    obs += o; zones.append(z); doors += d

    # Coffee station — top middle (open counter, no walls)
    obs += stamp_anchors("c", [(28, 5), (32, 5), (36, 5), (40, 5), (44, 5)])
    # Counter block (the dark-wood coffee bar)
    obs.append([28, 3, 18, 2, "#"])
    zones.append({"id": "coffee_l1", "type": "coffee", "bounds": [28, 3, 18, 4],
                  "anchors": [[28, 5], [32, 5], [36, 5], [40, 5], [44, 5]]})

    # TV lounge — top-right
    o, z, d = make_room("tv_lounge_l1", "lounge", x=49, y=2, w=14, h=16,
                        doors=[(49, 12)],
                        anchors=[(53, 9), (56, 9), (59, 9), (53, 14), (56, 14), (59, 14)],
                        anchor_char="L",
                        # TV unit on north wall + sofa block
                        extra_walls=[
                            [50, 3, 12, 2, "#"],   # TV unit
                            [53, 11, 6, 3, "#"],   # central coffee table
                        ])
    obs += o; zones.append(z); doors += d
    # Mark TV (grey) zone + viewer anchor
    zones.append({"id": "tv_l1", "type": "tv",
                  "bounds": [50, 3, 12, 2],
                  "anchors": [[55, 6]]})
    obs += stamp_anchors("t", [(55, 6)])

    # Ideas alcove — left mid
    obs.append([0, 28, 7, 7, "#"])
    obs.append([4, 31, 1, 1, "I"])
    obs.append([4, 32, 1, 1, "I"])
    # Carve an opening on the east side of this alcove
    obs.append([6, 32, 1, 1, "."])
    zones.append({"id": "ideas_l1", "type": "ideas",
                  "bounds": [0, 28, 7, 7], "anchors": [[4, 31], [4, 32]]})

    # Six desk clusters in 2 rows of 3 (bottom half)
    for i, (cx, cy) in enumerate([(11, 22), (24, 22), (37, 22), (50, 22),
                                   (11, 30), (24, 30)]):
        o, z = desk_cluster(f"desks_{'abcdef'[i]}", cx, cy, w=8, h=4)
        obs += o; zones.append(z)

    # Entry / Welcome (south centre)
    obs.append([30, 38, 5, 1, "E"])
    zones.append({"id": "entry_l1", "type": "entry",
                  "bounds": [30, 38, 5, 1], "anchors": [[32, 38]]})
    # Entry door tile (in the south wall)
    obs.append([32, 39, 1, 1, "D"])
    doors.append({"tile": [32, 39], "connects": ["entry_l1", "outside"]})

    # Stairs to L2 — east strip
    for sy in range(22, 38):
        obs.append([60, sy, 1, 1, "U"])
    zones.append({"id": "stairs_l1", "type": "stairs",
                  "bounds": [60, 22, 1, 16], "anchors": [[60, 30]]})

    return {
        "id": 1, "name": "Level 1 — Entry & Operations",
        "bg": "assets/floors/level1.png",
        "spawn": [32, 38],
        "obstacles": obs,
        "zones": zones,
        "doors": doors,
        "stairs": [{"tile": [60, 30], "dir": "up", "toFloor": 2, "toTile": [60, 30]}],
    }


# ─── LEVEL 2 ──────────────────────────────────────────────────────

def floor2():
    """L2: Conference room (NW big), workstations + windows (N centre),
    small meeting room (NE round table), restrooms (NE corner), phone
    booths (centre-left), focus + coffee zone (centre-right), lounge
    (SW), 6 desk clusters (S), stairs (E up + E down)."""
    obs, zones, doors = [], [], []

    # Conference room — top-left
    o, z, d = make_room("conference_l2", "conference_room", x=2, y=2, w=22, h=18,
                        doors=[(13, 19)],
                        anchors=[(6, 6), (10, 6), (14, 6), (18, 6), (22, 6),
                                  (6, 16), (10, 16), (14, 16), (18, 16), (22, 16)],
                        extra_walls=[[6, 8, 16, 7, "#"]])  # long table
    obs += o; zones.append(z); doors += d
    # Projector marker on the north wall
    zones.append({"id": "projector_l2", "type": "projector",
                  "bounds": [3, 2, 4, 1], "anchors": [[5, 2]]})

    # Top middle — workstation row (open desks against the windows)
    for i, dx in enumerate([26, 32, 38, 44]):
        o, z = desk_cluster(f"desks_window_{i}", dx, 4, w=4, h=4,
                            anchors=[(dx, 7), (dx + 3, 7)])
        obs += o; zones.append(z)

    # Top right — small round meeting room (round table, white-circle reference)
    o, z, d = make_room("meeting_l2_round", "meeting_room", x=49, y=2, w=12, h=10,
                        doors=[(53, 11)],
                        anchors=[(52, 5), (54, 5), (56, 5), (58, 5),
                                  (52, 8), (58, 8)],
                        extra_walls=[[53, 6, 6, 2, "#"]])  # round table
    obs += o; zones.append(z); doors += d

    # Restrooms — top-right corner
    o, z, d = make_room("restroom_men_l2", "restroom", x=53, y=12, w=4, h=6,
                        doors=[(54, 17)], anchors=[(55, 14)], anchor_char="p")
    obs += o; zones.append(z); doors += d
    o, z, d = make_room("restroom_women_l2", "restroom", x=58, y=12, w=4, h=6,
                        doors=[(59, 17)], anchors=[(60, 14)], anchor_char="p")
    obs += o; zones.append(z); doors += d

    # Phone booths — centre-left.
    # User feedback: bots were walking through the booth area. Tighten:
    # widen the booths to 4 tiles each and add solid wall segments
    # between/around them so bots can't slip past.
    for i, bx in enumerate([24, 29]):
        o, z, d = make_room(f"phone_l2_{i}", "phone_booths", x=bx, y=10, w=4, h=6,
                            doors=[(bx + 1, 15)], anchors=[(bx + 1, 13)],
                            anchor_char="p")
        obs += o; zones.append(z); doors += d
    # Solid wall connecting the two booths so the gap between is blocked
    obs.append([28, 10, 1, 6, "#"])
    # Buffer wall on the south face of the booth pair — bots walk around
    obs.append([24, 16, 9, 1, "#"])
    obs.append([30, 15, 1, 1, "."])      # carve a passage tile

    # Focus + coffee — centre-right (the dark counter with kitchenware)
    obs.append([40, 14, 16, 2, "#"])    # counter
    obs += stamp_anchors("c", [(42, 16), (45, 16), (48, 16), (51, 16)])
    zones.append({"id": "coffee_l2", "type": "coffee",
                  "bounds": [40, 14, 16, 3],
                  "anchors": [[42, 16], [45, 16], [48, 16], [51, 16]]})
    # Focus / Plan / Execute / Succeed sign
    zones.append({"id": "focus_l2", "type": "focus",
                  "bounds": [37, 14, 3, 3], "anchors": [[37, 17]]})
    obs += stamp_anchors("F", [(37, 17)])

    # Bench in the centre (small island)
    obs.append([34, 16, 4, 1, "#"])

    # Lounge — SW
    o, z, d = make_room("lounge_l2", "lounge", x=2, y=22, w=14, h=12,
                        doors=[(11, 22)],
                        anchors=[(5, 25), (8, 25), (11, 25),
                                  (5, 30), (8, 30), (11, 30)],
                        anchor_char="L",
                        extra_walls=[[4, 25, 6, 4, "#"]])     # sofa block
    obs += o; zones.append(z); doors += d

    # 6 desk clusters — bottom centre/right (2 rows of 3)
    for i, (cx, cy) in enumerate([(20, 23), (33, 23), (46, 23),
                                   (20, 31), (33, 31), (46, 31)]):
        o, z = desk_cluster(f"desks_{'ghijkl'[i]}", cx, cy, w=10, h=4,
                            anchors=[(cx, cy + 1), (cx, cy + 2),
                                     (cx + 9, cy + 1), (cx + 9, cy + 2)])
        obs += o; zones.append(z)

    # Stairs: up to L3 (east, top half) + down to L1 (east, bottom half)
    # User feedback: bots were walking through the stair-landing area near
    # the restrooms (east edge). Add a solid wall column at x=61 between
    # the restroom block and the stairs so bots only enter via the proper
    # stair tiles. The opening is at the stair entry point only.
    for sy in range(2, 22):
        obs.append([62, sy, 1, 1, "U"])
        # Buffer wall west of the up-stairs except at the entry tile
        if sy != 10:
            obs.append([61, sy, 1, 1, "#"])
    for sy in range(22, 38):
        obs.append([62, sy, 1, 1, "N"])
        if sy != 30:
            obs.append([61, sy, 1, 1, "#"])
    zones.append({"id": "stairs_l2_up", "type": "stairs",
                  "bounds": [62, 2, 1, 20], "anchors": [[62, 10]]})
    zones.append({"id": "stairs_l2_down", "type": "stairs",
                  "bounds": [62, 22, 1, 16], "anchors": [[62, 30]]})

    return {
        "id": 2, "name": "Level 2 — Executive & Focus",
        "bg": "assets/floors/level2.png",
        "spawn": [62, 30],
        "obstacles": obs,
        "zones": zones,
        "doors": doors,
        "stairs": [
            {"tile": [62, 30], "dir": "down", "toFloor": 1, "toTile": [60, 30]},
            {"tile": [62, 10], "dir": "up",   "toFloor": 3, "toTile": [62, 10]},
        ],
    }


# ─── LEVEL 3 ──────────────────────────────────────────────────────

def floor3():
    """L3: Conference room (NW), 'Good Vibes' chill area + Fuel Up bar
    (N centre), phone booths (NE centre), restrooms (NE corner), TV
    lounge (NE big), relax/gaming zone (mid-W), snooker + ping-pong
    (centre), bean-bag area (mid-E), centre fountain seating (centre),
    meeting room (SW round table), 6 desk clusters (S), phone booths
    (SE), balcony (S strip), stairs (E down to L2)."""
    obs, zones, doors = [], [], []

    # Conference room — top-left
    o, z, d = make_room("conference_l3", "conference_room", x=2, y=2, w=14, h=14,
                        doors=[(8, 15)],
                        anchors=[(5, 6), (8, 6), (11, 6), (5, 12), (8, 12), (11, 12)],
                        extra_walls=[[5, 8, 8, 3, "#"]])     # long table
    obs += o; zones.append(z); doors += d

    # 'Good Vibes Only' lounge — top centre-left (open seating)
    obs.append([18, 4, 6, 4, "#"])      # sofa block
    obs += stamp_anchors("L", [(20, 8), (23, 8)])
    zones.append({"id": "good_vibes_l3", "type": "lounge",
                  "bounds": [18, 2, 7, 7], "anchors": [[20, 8], [23, 8]]})

    # Fuel Up bar — top centre
    obs.append([26, 3, 12, 2, "#"])     # counter
    obs += stamp_anchors("c", [(27, 6), (30, 6), (33, 6), (36, 6)])
    zones.append({"id": "fuel_up_l3", "type": "coffee",
                  "bounds": [26, 3, 12, 4],
                  "anchors": [[27, 6], [30, 6], [33, 6], [36, 6]]})

    # Phone booths cluster — top centre-right
    for i, bx in enumerate([40, 43, 46]):
        o, z, d = make_room(f"phone_l3_{i}", "phone_booths", x=bx, y=2, w=3, h=6,
                            doors=[(bx + 1, 7)], anchors=[(bx + 1, 4)],
                            anchor_char="p")
        obs += o; zones.append(z); doors += d

    # Restrooms — top-right
    o, z, d = make_room("restroom_l3", "restroom", x=50, y=2, w=4, h=6,
                        doors=[(51, 7)], anchors=[(52, 4)], anchor_char="p")
    obs += o; zones.append(z); doors += d
    o, z, d = make_room("restroom_men_l3", "restroom", x=55, y=2, w=3, h=6,
                        doors=[(56, 7)], anchors=[(56, 4)], anchor_char="p")
    obs += o; zones.append(z); doors += d
    o, z, d = make_room("restroom_women_l3", "restroom", x=59, y=2, w=3, h=6,
                        doors=[(60, 7)], anchors=[(60, 4)], anchor_char="p")
    obs += o; zones.append(z); doors += d

    # TV lounge — middle right (with TV unit + sofas)
    obs.append([50, 10, 12, 2, "#"])    # TV unit (grey)
    obs.append([52, 14, 8, 3, "#"])     # central coffee table
    obs += stamp_anchors("L", [(51, 13), (55, 13), (59, 13)])
    obs += stamp_anchors("t", [(56, 12)])
    zones.append({"id": "tv_lounge_l3", "type": "lounge",
                  "bounds": [50, 10, 12, 8],
                  "anchors": [[51, 13], [55, 13], [59, 13]]})
    zones.append({"id": "tv_l3", "type": "tv",
                  "bounds": [50, 10, 12, 2], "anchors": [[56, 12]]})

    # Relax / gaming zone — mid-W (the black-circle reference)
    obs.append([2, 17, 12, 6, "#"])     # relax zone outline
    obs.append([4, 19, 8, 3, "."])      # carve walkable interior
    obs += stamp_anchors("F", [(6, 20), (9, 20)])
    zones.append({"id": "relax_l3", "type": "relax",
                  "bounds": [2, 17, 12, 6], "anchors": [[6, 20], [9, 20]]})

    # Snooker table — mid centre-left (lime-green annotation)
    obs.append([18, 17, 6, 4, "#"])
    obs += stamp_anchors("k", [(17, 19), (24, 19)])
    zones.append({"id": "snooker_l3", "type": "snooker",
                  "bounds": [17, 17, 8, 4], "anchors": [[17, 19], [24, 19]]})

    # Ping-pong table — mid centre (dark-red annotation)
    obs.append([26, 17, 6, 3, "#"])
    obs += stamp_anchors("g", [(25, 18), (32, 18)])
    zones.append({"id": "ping_pong_l3", "type": "ping_pong",
                  "bounds": [25, 17, 8, 3], "anchors": [[25, 18], [32, 18]]})

    # Centre fountain / circular seating
    obs.append([35, 17, 4, 4, "#"])
    obs += stamp_anchors("L", [(34, 19), (39, 19)])
    zones.append({"id": "fountain_l3", "type": "lounge",
                  "bounds": [34, 17, 6, 4], "anchors": [[34, 19], [39, 19]]})

    # Bean-bag area — mid-east
    obs += stamp_anchors("L", [(43, 18), (45, 18), (47, 19)])
    zones.append({"id": "beanbag_l3", "type": "relax",
                  "bounds": [42, 17, 7, 4],
                  "anchors": [[43, 18], [45, 18], [47, 19]]})

    # Meeting room — SW (round table, white-circle reference)
    o, z, d = make_room("meeting_l3_round", "meeting_room", x=2, y=24, w=10, h=10,
                        doors=[(7, 33)],
                        anchors=[(4, 27), (7, 27), (10, 27),
                                  (4, 31), (7, 31), (10, 31)],
                        extra_walls=[[5, 28, 5, 3, "#"]])
    obs += o; zones.append(z); doors += d

    # 6 desk clusters — bottom (2 rows of 3)
    for i, (cx, cy) in enumerate([(15, 25), (28, 25), (41, 25),
                                   (15, 32), (28, 32), (41, 32)]):
        o, z = desk_cluster(f"desks_{'mnopqr'[i]}", cx, cy, w=10, h=4,
                            anchors=[(cx, cy + 1), (cx, cy + 2),
                                     (cx + 9, cy + 1), (cx + 9, cy + 2)])
        obs += o; zones.append(z)

    # Phone booths — SE
    for i, bx in enumerate([54, 57]):
        o, z, d = make_room(f"phone_l3_se_{i}", "phone_booths", x=bx, y=24, w=3, h=5,
                            doors=[(bx + 1, 28)], anchors=[(bx + 1, 26)],
                            anchor_char="p")
        obs += o; zones.append(z); doors += d

    # Balcony — south strip.
    # User feedback: cyan arrows showed bots cutting across the bottom
    # row through plants/balcony rail. Add a solid wall along y=37 to
    # separate the desk-cluster band from the balcony, with anchor-only
    # entry tiles at the four anchor positions.
    for x in range(2, 62):
        if x not in (10, 24, 38, 52):           # anchor tiles stay walkable
            obs.append([x, 37, 1, 1, "#"])
    obs += stamp_anchors("B", [(10, 38), (24, 38), (38, 38), (52, 38)])
    zones.append({"id": "balcony_l3", "type": "balcony",
                  "bounds": [2, 38, 60, 2],
                  "anchors": [[10, 38], [24, 38], [38, 38], [52, 38]]})

    # Stairs down to L2 — east edge.
    # User feedback: cyan arrows showed bots clipping through the stair
    # railing both above and below the stair landing. Wall off x=61 for
    # the full vertical span except at the entry tile.
    for sy in range(2, 38):
        if 10 <= sy < 26:
            obs.append([62, sy, 1, 1, "N"])
            if sy != 18:
                obs.append([61, sy, 1, 1, "#"])
        else:
            obs.append([62, sy, 1, 1, "#"])
            obs.append([61, sy, 1, 1, "#"])
    zones.append({"id": "stairs_l3", "type": "stairs",
                  "bounds": [62, 10, 1, 16], "anchors": [[62, 18]]})

    return {
        "id": 3, "name": "Level 3 — Social & Relax",
        "bg": "assets/floors/level3.png",
        "spawn": [62, 18],
        "obstacles": obs,
        "zones": zones,
        "doors": doors,
        "stairs": [{"tile": [62, 18], "dir": "down", "toFloor": 2, "toTile": [62, 10]}],
    }


def main():
    OUT.mkdir(exist_ok=True)
    for fn, builder in [("floor1.json", floor1), ("floor2.json", floor2), ("floor3.json", floor3)]:
        path = OUT / fn
        path.write_text(json.dumps(builder(), indent=2), encoding="utf-8")
        print(f"  wrote {path}")


if __name__ == "__main__":
    main()
