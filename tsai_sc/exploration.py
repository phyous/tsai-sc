"""Bounded memory of sampled owned combat positions, never inferred map knowledge.

Historical input is the runner's ``combat_snapshot.own_combat`` observations.
Enemy lists, orders, destinations, squad names, and inferred paths are ignored.
This module neither ranks candidates nor changes the available action menu.
"""
from __future__ import annotations

CELL_PIXELS = 256
MAX_HISTORY = 2000
MAX_UNITS = 1700
INTERPRETATION = (
    "Cells are256px; rows run south and columns east from(0,0). "
    "prior_last_seen is the latest earlier frame with a sampled owned combat unit; null means no such sample. "
    "current_presence X marks a current owned combat sample. This is not fog coverage, cleared territory, "
    "enemy absence, or passability; unmarked cells may have been visible. No path between samples is inferred."
)


def _integer(value, low, high):
    return type(value) is int and low <= value <= high


def _geometry(state):
    if not isinstance(state, dict):
        raise ValueError("Visitation requires a current game observation")
    map_info = state.get("map")
    if (not isinstance(map_info, dict)
            or not _integer(map_info.get("width_tiles"), 1, 96)
            or not _integer(map_info.get("height_tiles"), 1, 64)
            or not _integer(state.get("frame"), 0, 0xffffffff)
            or not _integer(state.get("player_id"), 0, 11)):
        raise ValueError("Visitation requires a supported map, frame, and local player")
    return map_info["width_tiles"] * 32, map_info["height_tiles"] * 32


def _points(actors, combat_types, width, height, player, *, current):
    """Read only valid owned rows; reject conflicting duplicate identities."""
    if not isinstance(actors, list) or len(actors) > MAX_UNITS:
        return []
    valid, duplicate_ids = {}, set()
    for actor in actors:
        if not isinstance(actor, dict):
            continue
        # Current units have explicit ownership. Historical own_combat rows
        # inherit ownership from that recorded container; contradictory explicit
        # ownership is rejected before examining any location.
        if current:
            if type(actor.get("owner")) is not int or actor["owner"] != player or actor.get("completed") is not True:
                continue
        elif "owner" in actor and (type(actor["owner"]) is not int or actor["owner"] != player):
            continue
        if (not _integer(actor.get("id"), 0, MAX_UNITS - 1)
                or not _integer(actor.get("generation"), 0, 31)
                or type(actor.get("type_id")) is not int or actor["type_id"] not in combat_types
                or not _integer(actor.get("hp"), 1, 100000)
                or not _integer(actor.get("x"), 0, width - 1)
                or not _integer(actor.get("y"), 0, height - 1)):
            continue
        uid, point = actor["id"], (actor["x"], actor["y"])
        if uid in valid:
            duplicate_ids.add(uid)
        valid[uid] = point
    return [point for uid, point in valid.items() if uid not in duplicate_ids]


def visitation_summary(state, history=None, combat_types=(0, 1, 32)):
    """Summarize all earlier samples plus separate current presence.

    The supported demo maps fit at most12×8 cells. Inputs exceeding2000 history
    events are rejected rather than silently forgetting old visits. Malformed,
    future, same-frame, and out-of-map historical samples contribute no visits.
    Current presence does not turn a first arrival into a historical revisit.
    """
    width, height = _geometry(state)
    if history is None:
        history = []
    if not isinstance(history, list) or len(history) > MAX_HISTORY:
        raise ValueError("Visitation history must contain at most2000 events")
    if (not isinstance(combat_types, (list, tuple, set, frozenset)) or len(combat_types) > 228
            or any(not _integer(kind, 0, 227) for kind in combat_types)):
        raise ValueError("Visitation combat types must be bounded unit type IDs")
    kinds = frozenset(combat_types)
    columns, rows = (width + 255) // 256, (height + 255) // 256
    prior = [[None for _ in range(columns)] for _ in range(rows)]
    current = [["." for _ in range(columns)] for _ in range(rows)]
    frame, player = state["frame"], state["player_id"]
    for event in history:
        snapshot = event.get("combat_snapshot") if isinstance(event, dict) else None
        if not isinstance(snapshot, dict) or not _integer(snapshot.get("frame"), 0, frame - 1):
            continue
        when = snapshot["frame"]
        for x, y in _points(snapshot.get("own_combat"), kinds, width, height, player, current=False):
            row, column = y // CELL_PIXELS, x // CELL_PIXELS
            previous = prior[row][column]
            prior[row][column] = when if previous is None else max(when, previous)
    for x, y in _points(state.get("units"), kinds, width, height, player, current=True):
        current[y // CELL_PIXELS][x // CELL_PIXELS] = "X"
    return {"cell_size_pixels": CELL_PIXELS, "map_pixels": [width, height],
            "observed_frame": frame, "prior_last_seen": prior,
            "current_presence": ["".join(row) for row in current], "interpretation": INTERPRETATION}


def endpoint_fact(summary, point):
    """Describe one candidate endpoint cell without scoring or choosing it."""
    if not isinstance(summary, dict) or not isinstance(point, dict):
        raise ValueError("Endpoint facts require a visitation summary and point")
    dimensions = summary.get("map_pixels")
    if (not isinstance(dimensions, list) or len(dimensions) != 2
            or not _integer(dimensions[0], 1, 3072) or not _integer(dimensions[1], 1, 2048)
            or type(summary.get("cell_size_pixels")) is not int or summary["cell_size_pixels"] != CELL_PIXELS
            or not _integer(point.get("x"), 0, dimensions[0] - 1)
            or not _integer(point.get("y"), 0, dimensions[1] - 1)):
        raise ValueError("Endpoint is outside the supported visitation map")
    columns, rows = (dimensions[0] + 255) // 256, (dimensions[1] + 255) // 256
    prior, current, frame = summary.get("prior_last_seen"), summary.get("current_presence"), summary.get("observed_frame")
    if (not _integer(frame, 0, 0xffffffff) or not isinstance(prior, list) or len(prior) != rows
            or not isinstance(current, list) or len(current) != rows
            or any(not isinstance(row, list) or len(row) != columns
                   or any(value is not None and not _integer(value, 0, frame - 1) for value in row) for row in prior)
            or any(not isinstance(row, str) or len(row) != columns or set(row) - {".", "X"} for row in current)):
        raise ValueError("Malformed visitation summary")
    column, row = point["x"] // CELL_PIXELS, point["y"] // CELL_PIXELS
    last_seen = prior[row][column]
    return {"cell": [column, row], "previously_occupied": last_seen is not None,
            "last_prior_observed_frame": last_seen, "currently_occupied": current[row][column] == "X"}
