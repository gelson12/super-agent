"""Generate floor1/2/3.json from a high-level layout description.

Each room is a thin-walled rectangle with at least one door tile. Desk
clusters are just anchor 'S' tiles in the open. The script emits the same
JSON format that world.js reads (obstacles + zones + doors + stairs),
guaranteed to keep all anchors reachable from the spawn tile via A*.

Run:  python build_floors.py
"""

from __future__ import annotations
import json
from pathlib import Path

TILE_W, TILE_H = 64, 40
OUT = Path(__file__).parent / "data"


def make_room(name, ztype, x, y, w, h, door_at, anchors, *, anchor_char='S', no_walls=False):
    """Return (obstacles, zone). door_at is (x,y) on the room wall."""
    obs = []
    if not no_walls:
        # 4 walls: top, bottom, left, right (1-tile thick)
        for xx in range(x, x + w):
            obs.append([xx, y,         1, 1, "#"])
            obs.append([xx, y + h - 1, 1, 1, "#"])
        for yy in range(y + 1, y + h - 1):
            obs.append([x,         yy, 1, 1, "#"])
            obs.append([x + w - 1, yy, 1, 1, "#"])
        # Open the door tile
        obs.append([door_at[0], door_at[1], 1, 1, "D"])
    # Stamp anchors inside
    for ax, ay in anchors:
        obs.append([ax, ay, 1, 1, anchor_char])
    zone = {
        "id": name,
        "type": ztype,
        "bounds": [x, y, w, h],
        "anchors": anchors,
    }
    return obs, zone


def stamp_anchors(anchor_char, points):
    return [[x, y, 1, 1, anchor_char] for (x, y) in points]


# ──────────────────────────────────────────────────────────────────
#  FLOOR 1 — Entry & Operations
# ──────────────────────────────────────────────────────────────────
def floor1():
    obs = []
    zones = []
    doors = []

    # Meeting room — top-left
    o, z = make_room("meeting_l1", "meeting_room", 4, 5, 14, 12,
                     door_at=(11, 16),
                     anchors=[(7, 9), (10, 9), (13, 9), (15, 9), (7, 12), (10, 12), (13, 12), (15, 12)])
    obs += o; zones.append(z); doors.append({"tile": [11, 16], "connects": [z["id"], "open_floor"]})

    # Coffee station — top middle (counter; no walls)
    o, z = make_room("coffee_l1", "coffee", 28, 5, 16, 1,
                     door_at=(28, 5), no_walls=True,
                     anchors=[(30, 6), (33, 6), (36, 6), (39, 6), (42, 6)],
                     anchor_char="c")
    obs += o; zones.append(z)

    # TV lounge — top right
    o, z = make_room("tv_lounge_l1", "lounge", 50, 5, 12, 12,
                     door_at=(50, 12),
                     anchors=[(53, 9), (56, 9), (59, 9), (53, 13), (56, 13), (59, 13)],
                     anchor_char="L")
    obs += o; zones.append(z); doors.append({"tile": [50, 12], "connects": [z["id"], "open_floor"]})

    # Ideas board — bottom-left (open alcove, no walls; 'I' anchors only)
    obs += stamp_anchors("I", [(5, 32), (5, 33)])
    zones.append({"id": "ideas_l1", "type": "ideas", "bounds": [3, 30, 4, 5],
                  "anchors": [[5, 32], [5, 33]]})

    # Three desk clusters — bottom centre/right (anchors only, no walls)
    for i, dx in enumerate([22, 33, 44]):
        chairs = [(dx, 23), (dx + 4, 23), (dx + 8, 23),
                  (dx, 27), (dx + 4, 27), (dx + 8, 27),
                  (dx, 31), (dx + 4, 31), (dx + 8, 31)]
        # Stamp small desk surfaces between rows
        for chx in [dx + 1, dx + 5]:
            for chy in [24, 28]:
                obs.append([chx, chy, 3, 1, "#"])  # 3-wide desk strip
        for ax, ay in chairs:
            obs.append([ax, ay, 1, 1, "S"])
        zones.append({"id": f"desks_{'abc'[i]}", "type": "desk_cluster",
                      "bounds": [dx, 23, 9, 9],
                      "anchors": list(map(list, chairs))})

    # Stairs to L2 — right edge, open corridor
    for sy in range(22, 36):
        obs.append([60, sy, 2, 1, "U"])
    zones.append({"id": "stairs_l1", "type": "stairs",
                  "bounds": [60, 22, 2, 14], "anchors": [[60, 30]]})

    # Entry mat — bottom centre
    obs.append([30, 36, 4, 1, "E"])
    zones.append({"id": "entry_l1", "type": "entry",
                  "bounds": [30, 36, 4, 1], "anchors": [[32, 36]]})

    return {
        "id": 1, "name": "Level 1 — Entry & Operations",
        "bg": "assets/floors/level1.png",
        "spawn": [32, 36],
        "obstacles": obs,
        "zones": zones,
        "doors": doors,
        "stairs": [{"tile": [60, 30], "dir": "up", "toFloor": 2, "toTile": [60, 30]}],
    }


# ──────────────────────────────────────────────────────────────────
#  FLOOR 2 — Executive & Focus
# ──────────────────────────────────────────────────────────────────
def floor2():
    obs = []
    zones = []
    doors = []

    # Conference room — top-left big table
    o, z = make_room("conference_l2", "conference_room", 4, 4, 18, 14,
                     door_at=(13, 17),
                     anchors=[(7, 8), (10, 8), (13, 8), (16, 8),
                              (7, 13), (10, 13), (13, 13), (16, 13)])
    obs += o; zones.append(z); doors.append({"tile": [13, 17], "connects": [z["id"], "open_floor"]})

    # Top middle — meeting cluster (open, no walls — desks for visitors)
    obs += stamp_anchors("S", [(28, 6), (32, 6), (36, 6), (40, 6), (44, 6)])
    zones.append({"id": "meeting_l2_top", "type": "meeting_room",
                  "bounds": [27, 5, 20, 4],
                  "anchors": [[28, 6], [32, 6], [36, 6], [40, 6], [44, 6]]})

    # Top right — small meeting room
    o, z = make_room("meeting_l2_small", "meeting_room", 50, 4, 11, 8,
                     door_at=(55, 11),
                     anchors=[(53, 7), (55, 7), (57, 7)])
    obs += o; zones.append(z); doors.append({"tile": [55, 11], "connects": [z["id"], "open_floor"]})

    # Phone booths cluster — middle (3 mini-rooms)
    for i, bx in enumerate([22, 26, 30]):
        o, z = make_room(f"phone_l2_{i}", "phone_booths", bx, 12, 3, 5,
                         door_at=(bx + 1, 16),
                         anchors=[(bx + 1, 14)],
                         anchor_char="p")
        obs += o; zones.append(z); doors.append({"tile": [bx + 1, 16], "connects": [z["id"], "open_floor"]})

    # Focus zones (open, no walls)
    obs += stamp_anchors("F", [(38, 19), (40, 19), (42, 19), (44, 19), (46, 19)])
    zones.append({"id": "focus_l2", "type": "focus",
                  "bounds": [38, 18, 10, 4],
                  "anchors": [[38, 19], [40, 19], [42, 19], [44, 19], [46, 19]]})

    # Lounge — bottom-left
    o, z = make_room("lounge_l2", "lounge", 4, 22, 14, 11,
                     door_at=(11, 22),
                     anchors=[(7, 25), (10, 25), (13, 25), (7, 29), (10, 29), (13, 29)],
                     anchor_char="L")
    obs += o; zones.append(z); doors.append({"tile": [11, 22], "connects": [z["id"], "open_floor"]})

    # Three desk clusters
    for i, dx in enumerate([22, 33, 44]):
        chairs = [(dx, 23), (dx + 4, 23), (dx + 8, 23),
                  (dx, 27), (dx + 4, 27), (dx + 8, 27),
                  (dx, 31), (dx + 4, 31), (dx + 8, 31)]
        for chx in [dx + 1, dx + 5]:
            for chy in [24, 28]:
                obs.append([chx, chy, 3, 1, "#"])
        for ax, ay in chairs:
            obs.append([ax, ay, 1, 1, "S"])
        zones.append({"id": f"desks_{'def'[i]}", "type": "desk_cluster",
                      "bounds": [dx, 23, 9, 9],
                      "anchors": list(map(list, chairs))})

    # Restroom — top right corner
    o, z = make_room("restroom_l2", "restroom", 56, 22, 4, 8,
                     door_at=(56, 25),
                     anchors=[(58, 24), (58, 27)],
                     anchor_char="p")
    obs += o; zones.append(z); doors.append({"tile": [56, 25], "connects": [z["id"], "open_floor"]})

    # Stairs up + stairs down
    for sy in range(4, 20):
        obs.append([60, sy, 1, 1, "U"])
    for sy in range(22, 36):
        obs.append([60, sy, 1, 1, "N"])
    zones.append({"id": "stairs_l2_up", "type": "stairs",
                  "bounds": [60, 4, 1, 16], "anchors": [[60, 8]]})
    zones.append({"id": "stairs_l2_down", "type": "stairs",
                  "bounds": [60, 22, 1, 14], "anchors": [[60, 30]]})

    return {
        "id": 2, "name": "Level 2 — Executive & Focus",
        "bg": "assets/floors/level2.png",
        "spawn": [60, 30],
        "obstacles": obs,
        "zones": zones,
        "doors": doors,
        "stairs": [
            {"tile": [60, 30], "dir": "down", "toFloor": 1, "toTile": [60, 30]},
            {"tile": [60, 8],  "dir": "up",   "toFloor": 3, "toTile": [60, 8]},
        ],
    }


# ──────────────────────────────────────────────────────────────────
#  FLOOR 3 — Social & Relax
# ──────────────────────────────────────────────────────────────────
def floor3():
    obs = []
    zones = []
    doors = []

    # Conference room — top-left
    o, z = make_room("conference_l3", "conference_room", 4, 4, 14, 12,
                     door_at=(11, 15),
                     anchors=[(7, 8), (10, 8), (13, 8), (15, 8),
                              (7, 12), (10, 12), (13, 12), (15, 12)])
    obs += o; zones.append(z); doors.append({"tile": [11, 15], "connects": [z["id"], "open_floor"]})

    # Small meeting / good-vibes lounge — open with sofas
    obs += stamp_anchors("L", [(20, 6), (22, 6), (24, 6), (26, 6), (28, 6)])
    zones.append({"id": "meeting_l3", "type": "meeting_room",
                  "bounds": [20, 5, 10, 4],
                  "anchors": [[20, 6], [22, 6], [24, 6], [26, 6], [28, 6]]})

    # Coffee bar — top centre
    obs += stamp_anchors("c", [(33, 6), (36, 6), (39, 6), (42, 6)])
    zones.append({"id": "coffee_l3", "type": "coffee",
                  "bounds": [32, 5, 12, 2],
                  "anchors": [[33, 6], [36, 6], [39, 6], [42, 6]]})

    # Phone booths
    for i, bx in enumerate([46, 49]):
        o, z = make_room(f"phone_l3_{i}", "phone_booths", bx, 4, 2, 5,
                         door_at=(bx, 8),
                         anchors=[(bx + 1, 6)],
                         anchor_char="p")
        obs += o; zones.append(z); doors.append({"tile": [bx, 8], "connects": [z["id"], "open_floor"]})

    # Restroom
    o, z = make_room("restroom_l3", "restroom", 54, 4, 6, 5,
                     door_at=(54, 7),
                     anchors=[(56, 6), (58, 6)],
                     anchor_char="p")
    obs += o; zones.append(z); doors.append({"tile": [54, 7], "connects": [z["id"], "open_floor"]})

    # Ping-pong table (open, just anchors)
    obs += stamp_anchors("g", [(22, 13), (26, 13)])
    zones.append({"id": "ping_pong_l3", "type": "ping_pong",
                  "bounds": [22, 12, 6, 4],
                  "anchors": [[22, 13], [26, 13]]})

    # Snooker / pool table
    obs += stamp_anchors("k", [(30, 13), (34, 13)])
    zones.append({"id": "snooker_l3", "type": "snooker",
                  "bounds": [30, 12, 6, 4],
                  "anchors": [[30, 13], [34, 13]]})

    # TV lounge / chill area — middle right
    obs += stamp_anchors("L", [(40, 14), (43, 14), (46, 14)])
    zones.append({"id": "tv_lounge_l3", "type": "lounge",
                  "bounds": [38, 13, 12, 6],
                  "anchors": [[40, 14], [43, 14], [46, 14]]})

    # Focus / quiet meeting (left)
    o, z = make_room("focus_l3", "focus", 4, 21, 14, 10,
                     door_at=(13, 21),
                     anchors=[(7, 25), (10, 25), (13, 25)],
                     anchor_char="F")
    obs += o; zones.append(z); doors.append({"tile": [13, 21], "connects": [z["id"], "open_floor"]})

    # Three desk clusters (bottom)
    for i, dx in enumerate([22, 33, 44]):
        chairs = [(dx, 22), (dx + 4, 22), (dx + 8, 22),
                  (dx, 26), (dx + 4, 26), (dx + 8, 26),
                  (dx, 30), (dx + 4, 30), (dx + 8, 30)]
        for chx in [dx + 1, dx + 5]:
            for chy in [23, 27]:
                obs.append([chx, chy, 3, 1, "#"])
        for ax, ay in chairs:
            obs.append([ax, ay, 1, 1, "S"])
        zones.append({"id": f"desks_{'ghi'[i]}", "type": "desk_cluster",
                      "bounds": [dx, 22, 9, 9],
                      "anchors": list(map(list, chairs))})

    # Balcony — bottom strip (open)
    obs += stamp_anchors("B", [(10, 36), (24, 36), (40, 36), (52, 36)])
    zones.append({"id": "balcony_l3", "type": "balcony",
                  "bounds": [4, 36, 56, 2],
                  "anchors": [[10, 36], [24, 36], [40, 36], [52, 36]]})

    # Stairs down
    for sy in range(4, 20):
        obs.append([60, sy, 1, 1, "N"])
    zones.append({"id": "stairs_l3", "type": "stairs",
                  "bounds": [60, 4, 1, 16], "anchors": [[60, 8]]})

    return {
        "id": 3, "name": "Level 3 — Social & Relax",
        "bg": "assets/floors/level3.png",
        "spawn": [60, 8],
        "obstacles": obs,
        "zones": zones,
        "doors": doors,
        "stairs": [{"tile": [60, 8], "dir": "down", "toFloor": 2, "toTile": [60, 8]}],
    }


def main():
    OUT.mkdir(exist_ok=True)
    for fn, builder in [("floor1.json", floor1), ("floor2.json", floor2), ("floor3.json", floor3)]:
        path = OUT / fn
        path.write_text(json.dumps(builder(), indent=2), encoding="utf-8")
        print(f"  wrote {path}")


if __name__ == "__main__":
    main()
