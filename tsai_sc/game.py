"""Read-only observations for the original 1998 StarCraft shareware executable.

Addresses apply to the June 24, 1998 demo, not retail/Brood War. They were
recovered from the executable's unit-pool initialization, resource accounting,
scrolling, and trigger-action dispatch, then checked against a live Boot Camp.
No function in this module writes guest memory or awards victory.
"""
from __future__ import annotations

import struct
from collections import Counter
from collections.abc import Callable

ReadBytes = Callable[[int, int], bytes]

GAME = 0x4EDE48
LOCAL_PLAYER = 0x4E2954
CAMERA = 0x508094
UNIT_BASE = 0x59D270
UNIT_SIZE = 0x138
UNIT_COUNT = 1700
VICTORY = 0x4FC458
PENDING_VICTORY = 0x68F6C0
MAP_IDENTITY = GAME + 0xC4C
GAME_MODE = 0x61FC14
TERRAN_SUPPLY = GAME + 0x3054 + 0x90
BUILD_SIGNATURE_ADDRESS = 0x41FAC2
BUILD_SIGNATURE = bytes.fromhex("b9f805020033c0bf70d25900")

UNIT_NAMES = {
    0: "Marine", 1: "Ghost", 2: "Vulture", 3: "Goliath", 5: "Siege Tank",
    7: "SCV", 8: "Wraith", 9: "Science Vessel", 11: "Dropship",
    12: "Battlecruiser", 15: "Civilian", 32: "Firebat", 37: "Zergling",
    38: "Hydralisk", 41: "Drone", 42: "Overlord", 65: "Zealot",
    106: "Command Center", 107: "Comsat Station", 108: "Nuclear Silo",
    109: "Supply Depot", 110: "Refinery", 111: "Barracks", 112: "Academy",
    113: "Factory", 114: "Starport", 116: "Science Facility",
    122: "Engineering Bay", 123: "Armory", 124: "Missile Turret",
    125: "Bunker", 176: "Mineral Field", 177: "Mineral Field",
    178: "Mineral Field", 188: "Vespene Geyser",
}
RESOURCE_TYPES = frozenset({176, 177, 178, 188})


class GameStateError(RuntimeError):
    """The observed memory cannot safely be interpreted as this mission."""


def _read(read: ReadBytes, address: int, length: int) -> bytes:
    result = read(address, length)
    if len(result) != length:
        raise GameStateError(f"Short game-memory read at {address:#x}: {len(result)}/{length}")
    return result


def _u16(data: bytes, offset: int = 0) -> int:
    return struct.unpack_from("<H", data, offset)[0]


def _u32(data: bytes, offset: int = 0) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def _text(data: bytes) -> str:
    return data.split(b"\0", 1)[0].decode("latin1")


def game_to_screen(x: int, y: int, camera: dict, *, width=640, height=312):
    """Convert map pixels to the original 640×480 game's safe viewport.

    The irregular lower panel begins near y=316 at the sides. Keep every
    returned coordinate above that UI. None means callers must move the camera.
    """
    sx, sy = int(x) - camera["x"], int(y) - camera["y"]
    return {"x": sx, "y": sy} if 0 <= sx < width and 0 <= sy < height else None


def read_state(read_bytes: ReadBytes) -> dict:
    """Observe a paused game using a callback ``(address, length) -> bytes``.

    Only owned units and currently visible resources/enemies leave the decoder.
    Victory is the original engine's committed result, never inferred merely
    from resources/objectives. Run this while the emulator is paused so the
    several memory reads describe one consistent frame.
    """
    if _read(read_bytes, BUILD_SIGNATURE_ADDRESS, len(BUILD_SIGNATURE)) != BUILD_SIGNATURE:
        raise GameStateError("Unsupported StarCraft executable; expected the original 1998 demo")
    mode = _u16(_read(read_bytes, GAME_MODE, 2))
    if mode != 3:
        raise GameStateError("StarCraft is in a menu or transition, not active gameplay")
    player = _u32(_read(read_bytes, LOCAL_PLAYER, 4))
    if not 0 <= player < 8:
        raise GameStateError("No valid active StarCraft player; enter Boot Camp first")
    identity = _read(read_bytes, MAP_IDENTITY, 292)
    map_path, map_title = _text(identity[:260]), _text(identity[260:])
    if map_path.lower().replace("/", "\\") != "campaign\\terraned\\tutorial":
        raise GameStateError("Boot Camp is not loaded; menus and other missions are not observations")
    header = _read(read_bytes, GAME, 0x180)
    width, height = _u16(header, 0xE4), _u16(header, 0xE6)
    frame = _u32(header, 0x14C)
    if (width, height) != (64, 64) or frame == 0:
        raise GameStateError("Boot Camp has not entered gameplay")
    minerals, gas = _u32(header, player * 4), _u32(header, 0x30 + player * 4)
    if minerals > 100_000_000 or gas > 100_000_000:
        raise GameStateError("Invalid resource counters for this executable")
    camera_bytes = _read(read_bytes, CAMERA, 8)
    camera = {"x": _u32(camera_bytes), "y": _u32(camera_bytes, 4)}
    if camera["x"] >= width * 32 or camera["y"] >= height * 32:
        raise GameStateError("Invalid camera coordinates")
    outcomes = _read(read_bytes, VICTORY, 8)
    pending = _read(read_bytes, PENDING_VICTORY, 8)
    result = outcomes[player]
    status = {3: "victory", 2: "defeat"}.get(result, "running")
    supply_bytes = _read(read_bytes, TERRAN_SUPPLY, 0x90)
    supply = {
        "used": _u32(supply_bytes, 0x30 + player * 4) / 2,
        "available": min(_u32(supply_bytes, player * 4), _u32(supply_bytes, 0x60 + player * 4)) / 2,
    }

    # read_memory implementations may chunk this into the runtime's 64 KiB limit.
    table = _read(read_bytes, UNIT_BASE, UNIT_SIZE * UNIT_COUNT)
    units = []
    for index in range(UNIT_COUNT):
        data = table[index * UNIT_SIZE:(index + 1) * UNIT_SIZE]
        hp_raw, sprite = _u32(data, 8), _u32(data, 12)
        if hp_raw == 0 or sprite == 0:
            continue
        owner, type_id = data[0x4C], _u16(data, 0x64)
        x, y = _u16(data, 0x28), _u16(data, 0x2A)
        if owner >= 12 or type_id >= 228 or x >= width * 32 or y >= height * 32:
            raise GameStateError(f"Invalid live unit record {index}")
        # Sprites are allocated in the demo's static image range. Reject unsafe
        # pointers instead of using corrupt data as another arbitrary read.
        if not 0x4D9000 <= sprite <= 0x699000 - 0x20:
            raise GameStateError(f"Invalid sprite pointer in unit record {index}")
        sprite_data = _read(read_bytes, sprite, 0x20)
        visible = bool(sprite_data[0x0C] & (1 << player))
        if owner != player and not visible:
            continue
        if owner == 11 and type_id not in RESOURCE_TYPES:
            continue
        flags = _u32(data, 0xDC)
        queue = [_u16(data, 0x98 + n * 2) for n in range(5)]
        units.append({
            "id": index, "address": UNIT_BASE + index * UNIT_SIZE,
            "type_id": type_id, "type": UNIT_NAMES.get(type_id, f"Unit {type_id}"),
            "owner": owner, "x": x, "y": y, "hp": (hp_raw + 255) // 256,
            "completed": bool(flags & 1), "order_id": data[0x4D],
            "order_state": data[0x4E], "order_target_address": _u32(data, 0x5C),
            "order_target": {"x": _u16(data, 0x58), "y": _u16(data, 0x5A)},
            "build_queue": [unit for unit in queue if unit < 228],
            "remaining_build_time": _u16(data, 0xAC),
            "visible": visible,
            "screen": game_to_screen(x, y, camera),
        })

    own = [unit for unit in units if unit["owner"] == player]
    if not own and status == "running":
        raise GameStateError("No owned units in a running mission")
    counts = Counter(unit["type_id"] for unit in own if unit["completed"])
    progress = {
        "supply_depots": {"current": counts[109], "target": 3},
        "refineries": {"current": counts[110], "target": 1},
        "gas": {"current": gas, "target": 100},
    }
    return {
        "mission": "Boot Camp", "player_id": player, "frame": frame,
        "minerals": minerals, "gas": gas, "supply": supply, "camera": camera,
        "map": {"width_tiles": width, "height_tiles": height},
        "units": units, "status": status, "objective_progress": progress,
        "evidence": {
            "source": "original StarCraft shareware guest memory; read-only",
            "map_path": map_path, "map_title": map_title,
            "game_mode": mode, "executable_build": "StarCraft demo, June 24 1998",
            "victory_address": VICTORY + player, "victory_value": result,
            "pending_victory_value": pending[player],
            "victory_rule": "committed engine result 3; original trigger action 1",
            "visibility_rule": "owned units or sprite visibility mask for the player",
        },
    }
