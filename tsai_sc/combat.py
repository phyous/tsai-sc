"""Combat action menus for Jev, with no game input or hidden-state access.

Every Choice option names an executable squad command. The deterministic code
forms nearby squads of at most twelve, offers targets and geometric movement
candidates, and describes observed facts. It never chooses an action for Jev,
uses an unseen enemy position, imposes a route, or declares victory.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from enum import Enum
import math
from typing import Literal, NotRequired, TypedDict

from .controller import BUILDING_ORDERS, GAS_ORDERS, MINERAL_ORDERS, building_sites, depot_sites, supply
from .battle import recent_battle_outcomes
from .game import BASE_COMBAT_STATS


class CombatKind(str, Enum):
    CONTINUE = "continue"
    ATTACK_MOVE = "attack_move"
    ATTACK_TARGET = "attack_target"
    RETREAT = "retreat"
    REGROUP = "regroup"
    EXPLORE = "explore"
    GATHER = "gather"
    TRAIN = "train"
    BUILD = "build"
    UPGRADE = "upgrade"


class Point(TypedDict):
    x: int
    y: int


class CombatAction(TypedDict):
    kind: str
    label: str
    units: list[int]
    squad: NotRequired[str]
    point: NotRequired[Point]
    target: NotRequired[int]
    target_label: NotRequired[str]
    objective: NotRequired[str]
    unit: NotRequired[int]
    train_type: NotRequired[int]
    building: NotRequired[int]
    mineral_cost: NotRequired[int]
    gas_cost: NotRequired[int]
    upgrade: NotRequired[str]


@dataclass(frozen=True)
class KnownRegion:
    """A location learned legitimately, never extracted from hidden enemy units."""
    label: str
    x: int
    y: int
    source: Literal["mission_briefing", "previously_observed", "initial_friendly_position"]


@dataclass(frozen=True)
class MissionConfig:
    name: str
    objectives: tuple[str, ...]
    enemy_players: tuple[int, ...]
    allied_players: tuple[int, ...] = ()
    known_regions: tuple[KnownRegion, ...] = ()
    # Standard mobile Terran combat units plus the Marine/Vulture heroes. The
    # adapter currently exposes movement/attack, not spells, transport or siege.
    combat_types: tuple[int, ...] = (0, 1, 2, 3, 5, 8, 12, 16, 20, 32)


STRONGARM = MissionConfig(
    name="Strongarm",
    objectives=("Destroy the rebel base.", "Preserve a combat force and respond to visible hostile attacks."),
    enemy_players=(0, 3),
    allied_players=(2, 6),
)

MISSION_PLAYBOOK = (
    "Establish mineral income early by assigning idle SCVs to visible mineral fields; idle workers earn nothing, and training more workers does not assign existing ones.",
    "Maintain replacements: when affordable, keep idle Barracks producing Marines, maintain supply, and rebuild or expand production capacity when needed. Mining and production continue concurrently with army orders; avoid stockpiling minerals while production is idle.",
    "Assemble roughly 8–12 Marines together before an unsupported exploration or assault. A squad with at least 8 combat units within 192 pixels of an observed member has a coherent core: assembly is already achieved there, even away from base. This formation measurement does not guarantee combat strength or passability; consider composition, health and visible threats. After losses, rebuild and bring small reinforcements together rather than feeding isolated units forward. Immediate defense can take priority.",
    "Advance a formed force toward observed objectives or unexplored terrain while income and replacements continue. New reinforcements or another distant squad do not invalidate its assembly. Preserve productive attack-move orders; pathfinding can temporarily move away from the destination or stretch a formation, so distance or spread alone does not prove failure. A return to base is a withdrawal, not a prerequisite to every advance. Regroup when the measured formation needs it, or withdraw for a tactical reason such as danger or losses. Squad names can change: compare actual members, core strength and orders. No fixed route is supplied.",
)

COMBAT_GUIDANCE = (
    "A core of eight or more measures formation, not superiority over the observed defense. Compare local visible armed units, HP, composition and known base ranges, including attack-capable workers and uncertain defensive structures. "
    "Deal with immediate armed threats before committing Marines to durable unarmed buildings such as an Academy; focusing a building lets surviving defenders continue firing. "
    "Marines have base range 128px versus Firebats' 32px; preserve range and avoid crowding into close-range Firebat splash. Retreat or reinforce when current local strength is inadequate. "
    "Use recent_battle_outcomes to reconsider an approach associated with observed losses before repeating it with comparable strength. Past hostile sightings do not establish present enemy strength or the cause of unit disappearance. All commands remain model choices."
)

_FAILED_BUILD_COOLDOWN_FRAMES = 720
_UNARMED_TERRAN_STRUCTURES = frozenset({106, 107, 108, 109, 110, 111, 112, 113, 114, 115, 116, 117, 118, 120, 122, 123})
_GROUND_ATTACKERS = frozenset({0, 1, 2, 3, 5, 7, 8, 12, 16, 20, 32, 37, 38, 41, 64, 65})
_ATTACK_CAPABLE_WORKERS = frozenset({7, 41, 64})


class CombatStateError(ValueError):
    pass


def _point(unit: dict) -> Point:
    return {"x": int(unit["x"]), "y": int(unit["y"])}


def _distance(a: dict, b: dict) -> float:
    return math.hypot(a["x"] - b["x"], a["y"] - b["y"])


def _center(units: list[dict]) -> Point:
    return {"x": round(sum(unit["x"] for unit in units) / len(units)),
            "y": round(sum(unit["y"] for unit in units) / len(units))}


def _direction(origin: dict, target: dict) -> str:
    dx, dy = target["x"] - origin["x"], target["y"] - origin["y"]
    if abs(dx) < 48 and abs(dy) < 48:
        return "nearby"
    horizontal = "east" if dx > 0 else "west"
    vertical = "south" if dy > 0 else "north"
    if abs(dx) > abs(dy) * 2:
        return horizontal
    if abs(dy) > abs(dx) * 2:
        return vertical
    return vertical + horizontal


def _bounds(state: dict) -> tuple[int, int]:
    try:
        width, height = state["map"]["width_tiles"] * 32, state["map"]["height_tiles"] * 32
    except (KeyError, TypeError):
        raise CombatStateError("Combat observations require map dimensions.") from None
    if type(width) is not int or type(height) is not int or width < 128 or height < 128:
        raise CombatStateError("Combat map dimensions are invalid.")
    return width, height


def _clamp(point: dict, bounds: tuple[int, int]) -> Point:
    return {"x": int(min(max(point["x"], 32), bounds[0] - 33)),
            "y": int(min(max(point["y"], 32), bounds[1] - 33))}


def _observed(state: dict, mission: MissionConfig) -> tuple[list[dict], list[dict], list[dict]]:
    if not isinstance(mission, MissionConfig) or not mission.name or not mission.objectives:
        raise CombatStateError("Provide a named mission and its explicit objectives.")
    if set(mission.enemy_players) & set(mission.allied_players):
        raise CombatStateError("A player cannot be both hostile and allied.")
    player = state.get("player_id")
    if type(player) is not int or player in mission.enemy_players:
        raise CombatStateError("The local player must not be an enemy.")
    bounds = _bounds(state)
    own, enemies, allies = [], [], []
    ids = set()
    for unit in state.get("units", []):
        if not isinstance(unit, dict):
            raise CombatStateError("Invalid unit observation.")
        # Discard hidden non-owned entities before examining even their location.
        owner = unit.get("owner")
        if owner != player and unit.get("visible") is not True:
            continue
        if owner != player and owner not in mission.enemy_players and owner not in mission.allied_players:
            continue
        if unit.get("hp", 0) <= 0:
            continue
        try:
            valid = (type(unit["id"]) is int and unit["id"] >= 0 and unit["id"] not in ids
                     and type(unit["type_id"]) is int
                     and ("generation" not in unit or type(unit["generation"]) is int and 0 <= unit["generation"] <= 31)
                     and type(unit["x"]) is int and 0 <= unit["x"] < bounds[0]
                     and type(unit["y"]) is int and 0 <= unit["y"] < bounds[1])
        except KeyError:
            valid = False
        if not valid:
            raise CombatStateError("Invalid owned or visible unit observation.")
        ids.add(unit["id"])
        (own if owner == player else enemies if owner in mission.enemy_players else allies).append(unit)
    return own, enemies, allies


def _squads(own: list[dict], mission: MissionConfig) -> list[tuple[str, list[dict]]]:
    # Nearby groups avoid trying to select thirteen units or silently
    # joining a distant reinforcement to a squad on the other side of the map.
    remaining = sorted((unit for unit in own if unit["type_id"] in mission.combat_types
                        and unit.get("completed") is True and unit.get("visible") is True), key=lambda unit: unit["id"])
    squads = []
    names = ("Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot", "Golf", "Hotel")
    while remaining and len(squads) < len(names):
        anchor = remaining[0]
        nearby = sorted((unit for unit in remaining if _distance(unit, anchor) <= 384),
                        key=lambda unit: (_distance(unit, anchor), unit["id"]))[:12]
        chosen = {unit["id"] for unit in nearby}
        remaining = [unit for unit in remaining if unit["id"] not in chosen]
        squads.append((names[len(squads)], nearby))
    return squads


def _formation(squad: list[dict]) -> dict:
    """Measure a member-centered core, without assuming terrain or combat success."""
    cores = [(anchor, [unit for unit in squad if _distance(unit, anchor) <= 192]) for anchor in squad]
    anchor, core = min(cores, key=lambda item: (-len(item[1]),
                       sum(_distance(unit, item[0]) for unit in item[1]), item[0]["id"]))
    formed = len(core) >= 8
    return {
        "member_count": len(squad), "marine_count": sum(unit["type_id"] == 0 for unit in squad),
        "core_radius_pixels": 192, "core_anchor_member_id": anchor["id"],
        "core_anchor_position": _point(anchor), "core_member_ids": sorted(unit["id"] for unit in core),
        "core_count": len(core), "core_marine_count": sum(unit["type_id"] == 0 for unit in core),
        "has_coherent_8plus_core": formed,
        "assembly_status": "Coherent core already assembled here." if formed else "Fewer than 8 combat units in this measured core.",
    }


def _combat_role(unit: dict) -> dict:
    """Known unit-type mechanics, never inferred from hidden orders or occupants."""
    kind = unit["type_id"]
    if kind in _UNARMED_TERRAN_STRUCTURES:
        return {"role": "unarmed production/support structure", "can_attack_ground": False,
                "note": "No direct weapon; nearby defenders can still attack while this building is targeted."}
    if kind == 124:
        return {"role": "anti-air defensive structure", "can_attack_ground": False,
                "note": "Missile Turrets attack air targets, not ground Marines."}
    if kind == 125:
        return {"role": "potentially armed garrison structure", "can_attack_ground": None,
                "note": "Bunker occupants may attack; occupancy is not inferred from seeing the structure."}
    if kind in _GROUND_ATTACKERS:
        result = {"role": "attack-capable worker" if kind in _ATTACK_CAPABLE_WORKERS else "armed combat unit",
                  "can_attack_ground": True}
        if kind in BASE_COMBAT_STATS:
            result["base_ground_range_pixels"] = BASE_COMBAT_STATS[kind]["base_ground_range_pixels"]
            result["base_ground_damage"] = BASE_COMBAT_STATS[kind]["base_ground_damage"]
        result["note"] = ("Short-range splash threatens clustered Marines; base weapon values do not include upgrades." if kind == 32 else
                          "Worker can attack at close range; its current economic order does not remove that capability." if kind in _ATTACK_CAPABLE_WORKERS else
                          "Base weapon values, when known, do not include upgrades or predict damage per second.")
        return result
    return {"role": "combat capability not classified", "can_attack_ground": None,
            "note": "Unknown capability is not evidence that this unit is harmless."}


def _local_threats(squad: list[dict], own: list[dict], enemies: list[dict], mission: MissionConfig) -> dict:
    local = [unit for unit in enemies if min(_distance(unit, member) for member in squad) <= 512]
    armed = [unit for unit in local if _combat_role(unit)["can_attack_ground"] is True and unit["type_id"] not in _ATTACK_CAPABLE_WORKERS]
    workers = [unit for unit in local if unit["type_id"] in _ATTACK_CAPABLE_WORKERS]
    friendly = [unit for unit in own if unit["type_id"] in mission.combat_types and unit.get("completed") is True
                and min(_distance(unit, member) for member in squad) <= 512]
    return {
        "neighborhood_radius_pixels": 512, "distance_basis": "Within 512px of any current squad member.",
        "squad_count": len(squad), "squad_hp": sum(unit["hp"] for unit in squad),
        "nearby_owned_combat_count_including_squad": len(friendly), "nearby_owned_combat_hp": sum(unit["hp"] for unit in friendly),
        "visible_armed_combat_count": len(armed), "visible_armed_combat_hp": sum(unit["hp"] for unit in armed),
        "visible_armed_composition": dict(Counter(unit.get("type", str(unit["type_id"])) for unit in armed)),
        "visible_attack_capable_worker_count": len(workers), "visible_attack_capable_worker_hp": sum(unit["hp"] for unit in workers),
        "visible_unarmed_structure_count": sum(unit["type_id"] in _UNARMED_TERRAN_STRUCTURES for unit in local),
        "visible_unknown_ground_capability_count": sum(_combat_role(unit)["can_attack_ground"] is None for unit in local),
        "known_enemy_base_ground_ranges": {unit.get("type", str(unit["type_id"])): BASE_COMBAT_STATS[unit["type_id"]]["base_ground_range_pixels"]
                                             for unit in armed + workers if unit["type_id"] in BASE_COMBAT_STATS},
        "interpretation": "Observed geometry and current HP, not simultaneous firing range, DPS or predicted victory. Unseen defenders and upgrades are unknown; coherence alone is not sufficient combat strength.",
    }


def _failed_build_sites(state: dict, history: list[dict] | None) -> list[dict]:
    now = state.get("frame")
    if type(now) is not int:
        return []
    width, height = _bounds(state)
    failed = {}
    for event in history or []:
        if not isinstance(event, dict) or event.get("issued") is not True or event.get("accepted") is not False:
            continue
        action = event.get("action", event)
        frame = event.get("frame")
        if not isinstance(action, dict) or action.get("kind") != "build" or action.get("building") not in {109, 111}:
            continue
        point = action.get("point")
        if (type(frame) is not int or not 0 <= now - frame < _FAILED_BUILD_COOLDOWN_FRAMES or not isinstance(point, dict)
                or type(point.get("x")) is not int or type(point.get("y")) is not int
                or not 0 <= point["x"] < width or not 0 <= point["y"] < height):
            continue
        key = (action["building"], point["x"], point["y"])
        if key not in failed or frame > failed[key]["failed_on_frame"]:
            failed[key] = {"building": action["building"], "point": {"x": point["x"], "y": point["y"]}, "failed_on_frame": frame,
                           "retry_after_frame": frame + _FAILED_BUILD_COOLDOWN_FRAMES}
    return sorted(failed.values(), key=lambda event: (-event["failed_on_frame"], event["building"], event["point"]["x"], event["point"]["y"]))


def _weapons_upgrade_ordered(history: list[dict] | None) -> bool:
    for event in history or []:
        if not isinstance(event, dict) or event.get("issued") is not True or event.get("accepted") is not True:
            continue
        action = event.get("action", event)
        if isinstance(action, dict) and action.get("kind") == "upgrade" and action.get("upgrade") == "infantry_weapons":
            return True
    return False


def _put(actions: dict[str, CombatAction], title: str, action: CombatAction) -> None:
    key = title
    number = 2
    while key in actions:
        key = f"{title} ({number})"
        number += 1
    actions[key] = action


def _known_enemy_structures(state: dict, enemies: list[dict], history: list[dict] | None) -> list[dict]:
    """Retain only explicit prior visible sightings; absence is not destruction."""
    width, height = _bounds(state)
    frame = state.get("frame")
    if type(frame) is not int or frame < 0:
        raise CombatStateError("Structure memory requires the current observed frame.")
    known = {}
    for event in history or []:
        sightings = event.get("observed_enemy_structures", []) if isinstance(event, dict) else []
        if not isinstance(sightings, list):
            continue
        for sighting in sightings:
            if (not isinstance(sighting, dict) or type(sighting.get("id")) is not int or sighting["id"] < 0
                    or type(sighting.get("type_id")) is not int or not 106 <= sighting["type_id"] <= 173
                    or not isinstance(sighting.get("type"), str) or not sighting["type"]
                    or type(sighting.get("x")) is not int or not 0 <= sighting["x"] < width
                    or type(sighting.get("y")) is not int or not 0 <= sighting["y"] < height
                    or type(sighting.get("last_seen_frame")) is not int or not 0 <= sighting["last_seen_frame"] <= frame
                    or ("generation" in sighting and (type(sighting["generation"]) is not int or not 0 <= sighting["generation"] <= 31))
                    or sighting.get("visible") is False):
                continue
            identity = (sighting["id"], sighting.get("generation"))
            previous = known.get(identity)
            if previous is None or sighting["last_seen_frame"] >= previous["last_seen_frame"]:
                known[identity] = {key: sighting[key] for key in ("id", "type", "type_id", "x", "y", "last_seen_frame", "generation") if key in sighting}
    visible_identities = set()
    for unit in enemies:
        if 106 <= unit["type_id"] <= 173:
            identity = (unit["id"], unit.get("generation"))
            visible_identities.add(identity)
            known[identity] = {key: unit[key] for key in ("id", "type", "type_id", "x", "y", "generation") if key in unit}
            known[identity]["last_seen_frame"] = frame
    memories = sorted(known.values(), key=lambda item: (-item["last_seen_frame"], item["id"]))[:32]
    for memory in memories:
        memory["currently_visible"] = (memory["id"], memory.get("generation")) in visible_identities
        memory["source"] = "visible enemy observation" if memory["currently_visible"] else "recorded prior visible enemy observation"
        memory["uncertainty"] = "Currently visible." if memory["currently_visible"] else "Not currently visible; presence and current position are unconfirmed."
    return memories


def _economy_candidates(state: dict, own: list[dict], actions: dict[str, CombatAction], history: list[dict] | None) -> None:
    workers = [unit for unit in own if unit["type_id"] == 7 and unit.get("completed") is True and unit.get("visible") is True]
    minerals = [unit for unit in state["units"] if unit.get("type_id") in {176, 177, 178}
                and unit.get("visible") is True and unit.get("hp", 0) > 0]
    bases = [unit for unit in own if unit["type_id"] == 106 and unit.get("completed") is True]
    available_workers = [unit for unit in workers if unit.get("order_id") not in BUILDING_ORDERS]
    # Equivalent mineral assignments use the closest available worker. This only
    # resolves an actor for the offered command; Jev still decides whether to mine.
    # ResetCollision (151) and other transient orders do not establish idleness.
    unassigned = [worker for worker in available_workers if worker.get("order_id") in ({1, 2, 3} | GAS_ORDERS)]
    unassigned.sort(key=lambda worker: (min((_distance(worker, mineral) for mineral in minerals), default=float("inf")), worker["id"]))
    existing_miner_ids = sorted(worker["id"] for worker in own if worker["type_id"] == 7 and worker.get("order_id") in MINERAL_ORDERS)
    for worker in unassigned[:1]:
        if not minerals or worker.get("order_id") in MINERAL_ORDERS:
            continue
        target = min(minerals, key=lambda unit: _distance(unit, min(bases, key=lambda base: _distance(base, worker)) if bases else worker))
        current_job = "gathering gas" if worker.get("order_id") in GAS_ORDERS else "idle" if worker.get("order_id") in {1, 2, 3} else "doing another non-mining task"
        title = "Reassign a gas SCV to minerals" if current_job == "gathering gas" else "Assign an idle SCV to minerals" if current_job == "idle" else "Assign an available SCV to minerals"
        _put(actions, title, {
            "kind": CombatKind.GATHER.value,
            "label": f"Assign currently {current_job} SCV {worker['id']} to minerals. Current engine order {worker.get('order_id')}; this actor is not one of the existing mineral workers {existing_miner_ids}. Adds an assigned mineral worker without changing army orders.",
            "units": [worker["id"]], "unit": worker["id"], "target": target["id"], "point": _point(target),
        })
    used, capacity = supply(state)
    amount = state.get("minerals", 0)
    scvs = sum(unit["type_id"] == 7 for unit in own) + sum(unit.get("build_queue", []).count(7) for unit in own if unit["type_id"] == 106)
    marines = sum(unit["type_id"] == 0 for unit in own) + sum(unit.get("build_queue", []).count(0) for unit in own if unit["type_id"] == 111)
    offered_train_types = set()
    if amount >= 50 and used < capacity:
        for number, producer in enumerate(sorted(own, key=lambda unit: unit["id"]), 1):
            if producer.get("completed") is not True or producer.get("visible") is not True or producer.get("build_queue"):
                continue
            if producer["type_id"] == 111 and marines < 72 and 0 not in offered_train_types:
                _put(actions, "Train Marine", {"kind": CombatKind.TRAIN.value, "label": f"Train one Marine at an idle Barracks (50 minerals, 1 supply)",
                                               "units": [producer["id"]], "unit": producer["id"], "train_type": 0, "mineral_cost": 50})
                offered_train_types.add(0)
            elif producer["type_id"] == 106 and scvs < 12 and 7 not in offered_train_types:
                _put(actions, "Train SCV", {"kind": CombatKind.TRAIN.value, "label": "Train one SCV at an idle Command Center (50 minerals, 1 supply)",
                                            "units": [producer["id"]], "unit": producer["id"], "train_type": 7, "mineral_cost": 50})
                offered_train_types.add(7)
    if amount >= 100 and state.get("gas", 0) >= 100 and not _weapons_upgrade_ordered(history):
        engineering_bays = sorted((unit for unit in own if unit["type_id"] == 122 and unit.get("completed") is True
                                  and unit.get("visible") is True and unit.get("order_id") != 76), key=lambda unit: unit["id"])
        if engineering_bays:
            bay = engineering_bays[0]
            _put(actions, "Upgrade infantry weapons", {
                "kind": CombatKind.UPGRADE.value,
                "label": "Start infantry weapons level 1 at the observed Engineering Bay (100 minerals, 100 gas). Improves infantry weapon damage when research completes; does not immediately finish the upgrade or change army orders.",
                "units": [bay["id"]], "unit": bay["id"], "upgrade": "infantry_weapons", "mineral_cost": 100, "gas_cost": 100,
            })
    depot_underway = any(unit["type_id"] == 109 and unit.get("completed") is not True for unit in own)
    failed = {(event["building"], event["point"]["x"], event["point"]["y"]) for event in _failed_build_sites(state, history)}
    if amount >= 100 and capacity - used <= 3 and not depot_underway and available_workers:
        available_sites = [point for point in depot_sites(state) if (109, point["x"], point["y"]) not in failed]
        for index, point in enumerate(available_sites[:2], 1):
            worker = min(available_workers, key=lambda unit: _distance(unit, point))
            _put(actions, f"Build Supply Depot: site {index}", {
                "kind": CombatKind.BUILD.value, "label": f"Build a Supply Depot at open candidate site {index} near the friendly base (100 minerals, adds 8 supply)",
                "units": [worker["id"]], "unit": worker["id"], "building": 109, "point": dict(point), "mineral_cost": 100,
            })
    barracks = [unit for unit in own if unit["type_id"] == 111]
    if (amount >= 150 and len(barracks) < 3 and not any(unit.get("completed") is not True for unit in barracks)
            and available_workers):
        available_sites = [point for point in building_sites(state, 111) if (111, point["x"], point["y"]) not in failed]
        for index, point in enumerate(available_sites[:2], 1):
            worker = min(available_workers, key=lambda unit: _distance(unit, point))
            _put(actions, f"Build Barracks: site {index}", {
                "kind": CombatKind.BUILD.value,
                "label": f"Build a Barracks at open candidate site {index} near the observed friendly base (150 minerals; adds Marine production capacity; {len(barracks)} Barracks currently owned)",
                "units": [worker["id"]], "unit": worker["id"], "building": 111, "point": dict(point), "mineral_cost": 150,
            })


def candidates(state: dict, mission: MissionConfig = STRONGARM, history: list[dict] | None = None) -> dict[str, CombatAction]:
    """Offer actual commands; the returned dict is the model's probability space.

    Selection/camera handling belongs to the input adapter. ``units`` lists the
    intended squad (at most twelve). attack_move/explore use A-click at ``point``;
    attack_target uses A-click on the visible ``target``; retreat/regroup use move.
    Continue issues no input. The engine remains the authority on pathfinding,
    attack success, damage, and mission outcome.
    """
    own, enemies, _ = _observed(state, mission)
    bounds = _bounds(state)
    actions: dict[str, CombatAction] = {
        "Continue current orders": {"kind": CombatKind.CONTINUE.value, "label": "Continue current orders without issuing a new command", "units": []}
    }
    squads = _squads(own, mission)
    bases = [unit for unit in own if unit["type_id"] in {106, 111, 113} and unit.get("completed") is True]
    assembly_bases = [unit for unit in bases if unit.get("visible") is True and unit["type_id"] == 106] or [unit for unit in bases if unit.get("visible") is True]
    known_structures = _known_enemy_structures(state, enemies, history)
    army_center = _center([unit for _, squad in squads for unit in squad]) if squads else None
    for name, squad in squads:
        center = _center(squad)
        formation = _formation(squad)
        unit_ids = sorted(unit["id"] for unit in squad)
        nearby_enemies = sorted(enemies, key=lambda unit: (_distance(unit, center), unit["id"]))
        # Each target is currently visible. A bounded nearest-target shortlist is
        # disclosed in model state; it does not choose the model's focus target.
        for enemy in nearby_enemies[:4]:
            enemy_name = str(enemy.get("type", "enemy unit"))
            direction = _direction(center, enemy)
            title = f"{name}: focus {enemy_name} {direction}"
            role = _combat_role(enemy)
            role_description = role["role"] + (f"; base ground range {role['base_ground_range_pixels']}px" if "base_ground_range_pixels" in role else "")
            if enemy["type_id"] == 32:
                role_description += "; close-range splash"
            elif enemy["type_id"] in _UNARMED_TERRAN_STRUCTURES:
                role_description += "; focusing this does not suppress nearby defenders"
            _put(actions, title, {"kind": CombatKind.ATTACK_TARGET.value, "label": f"{name}: focus fire on visible enemy {enemy_name} {direction} ({enemy['hp']} HP; {role_description})",
                                 "units": unit_ids.copy(), "squad": name, "target": enemy["id"], "target_label": enemy_name, "point": _point(enemy)})
        if nearby_enemies:
            enemy = nearby_enemies[0]
            direction = _direction(center, enemy)
            _put(actions, f"{name}: attack toward enemies {direction}", {
                "kind": CombatKind.ATTACK_MOVE.value, "label": f"{name}: attack-move toward the visible enemy group {direction}; engage enemies along the path",
                "units": unit_ids.copy(), "squad": name, "point": _point(enemy),
            })
            dx, dy = center["x"] - enemy["x"], center["y"] - enemy["y"]
            length = math.hypot(dx, dy)
            if length > 0:
                retreat = _clamp({"x": center["x"] + 256 * dx / length, "y": center["y"] + 256 * dy / length}, bounds)
                if _distance(retreat, center) >= 64:
                    _put(actions, f"{name}: retreat {_direction(center, retreat)}", {
                        "kind": CombatKind.RETREAT.value, "label": f"{name}: move away from the nearest visible enemy toward {_direction(center, retreat)}; movement can interrupt firing",
                        "units": unit_ids.copy(), "squad": name, "point": retreat,
                    })
        if assembly_bases:
            home = min(assembly_bases, key=lambda unit: _distance(unit, center))
            if _distance(home, center) >= 192:
                _put(actions, f"{name}: regroup at friendly base", {
                    "kind": CombatKind.REGROUP.value,
                    "label": (f"{name}: withdraw to the observed friendly {home.get('type', 'base')}; this squad already has a coherent core here, so this returns a formed force toward base" if formation["has_coherent_8plus_core"] else
                              f"{name}: regroup near the observed friendly {home.get('type', 'base')} to assemble with replacements") + "; ordinary movement replaces current attack or advance orders",
                    "units": unit_ids.copy(), "squad": name, "point": _point(home),
                })
        spread = max(_distance(unit, center) for unit in squad)
        if army_center is not None and (_distance(center, army_center) >= 96 or spread >= 96):
            _put(actions, f"{name}: regroup with friendly force", {
                "kind": CombatKind.REGROUP.value, "label": f"{name}: move toward the current friendly combat-force center ({round(_distance(center, army_center))} pixels from this squad's center) to regroup; " +
                    ("a coherent core is already assembled here; " if formation["has_coherent_8plus_core"] else "") +
                    "ordinary movement replaces current attack or advance orders",
                "units": unit_ids.copy(), "squad": name, "point": dict(army_center),
            })
        for region in mission.known_regions[:4]:
            if region.source not in {"mission_briefing", "previously_observed", "initial_friendly_position"}:
                raise CombatStateError("Known regions need a legitimate observation or briefing source.")
            if not (0 <= region.x < bounds[0] and 0 <= region.y < bounds[1]):
                raise CombatStateError("A known region is outside the map.")
            destination = {"x": region.x, "y": region.y}
            if _distance(center, destination) >= 96:
                _put(actions, f"{name}: advance to {region.label}", {
                    "kind": CombatKind.ATTACK_MOVE.value, "label": f"{name}: attack-move toward known region {region.label} ({region.source.replace('_', ' ')})",
                    "units": unit_ids.copy(), "squad": name, "point": destination, "objective": region.label,
                })
        for memory in known_structures[:4]:
            if _distance(center, memory) < 96:
                continue
            visibility = "currently visible" if memory["currently_visible"] else "not currently visible; presence and current position unconfirmed"
            _put(actions, f"{name}: advance to last-seen {memory['type']} {_direction(center, memory)}", {
                "kind": CombatKind.ATTACK_MOVE.value,
                "label": f"{name}: attack-move toward enemy {memory['type']} last seen at ({memory['x']}, {memory['y']}) on frame {memory['last_seen_frame']}; {visibility}",
                "units": unit_ids.copy(), "squad": name, "point": _point(memory), "objective": "last-seen enemy structure",
            })
        destinations = set()
        for direction, dx, dy in (("north", 0, -384), ("east", 384, 0), ("south", 0, 384), ("west", -384, 0)):
            point = _clamp({"x": center["x"] + dx, "y": center["y"] + dy}, bounds)
            coords = (point["x"], point["y"])
            if coords in destinations or _distance(point, center) < 96:
                continue
            destinations.add(coords)
            _put(actions, f"{name}: scout {direction}", {
                "kind": CombatKind.EXPLORE.value, "label": f"{name}: cautiously advance {direction} with attack-move to reveal terrain and engage encountered enemies",
                "units": unit_ids.copy(), "squad": name, "point": point,
            })
        # Names can change as pool slots are reused or reinforcements arrive.
        # Attach the actual current formation to every concrete squad command.
        description = f"{name} ({len(squad)} units; {formation['core_count']} within 192px of one member)"
        for action in actions.values():
            if action.get("squad") == name:
                action["label"] = action["label"].replace(name + ":", description + ":", 1)
    if len(actions) > 255:
        raise CombatStateError("The combat action menu exceeds the model's choice limit.")
    _economy_candidates(state, own, actions, history)
    if len(actions) > 255:
        raise CombatStateError("The combined combat/economy menu exceeds the model's choice limit.")
    return actions


def _unit_summary(unit: dict, origin: dict | None = None, *, owned: bool) -> dict:
    result = {"id": unit["id"], "type": unit.get("type", "Unknown unit"), "hp": unit["hp"],
              "position": _point(unit), "completed": unit.get("completed", False),
              "engine_order_id": unit.get("order_id"), "visible": unit.get("visible", False)}
    result["current_activity"] = {3: "standing guard", 6: "moving", 10: "attacking a target", 14: "attack-moving", 49: "following a unit"}.get(unit.get("order_id"), "other engine order")
    result["combat_role"] = _combat_role(unit)
    if origin is not None:
        result.update(direction=_direction(origin, unit), distance_pixels=round(_distance(origin, unit)))
    for key in ("generation", "max_hp", "shields", "ground_weapon_cooldown", "air_weapon_cooldown", "weapon_cooldown", "combat_stats", "kills"):
        if key in unit:
            result[key] = unit[key]
    # Seeing a unit does not reveal its internal future orders or waypoints.
    if owned and "order_target" in unit:
        result["order_target"] = unit["order_target"]
    return result


def request_for(state: dict, actions: dict[str, CombatAction], history: list[dict] | None = None,
                mission: MissionConfig = STRONGARM) -> tuple[dict, dict]:
    """Create compact tactical state and one exact-action Choice question."""
    own, enemies, allies = _observed(state, mission)
    squads = _squads(own, mission)
    own_by_id, enemy_by_id = {unit["id"]: unit for unit in own}, {unit["id"]: unit for unit in enemies}
    visible_minerals = {unit["id"] for unit in state["units"] if unit.get("type_id") in {176, 177, 178} and unit.get("visible") is True}
    tactical_kinds = {kind.value for kind in (CombatKind.ATTACK_MOVE, CombatKind.ATTACK_TARGET, CombatKind.RETREAT, CombatKind.REGROUP, CombatKind.EXPLORE)}
    legal_kinds = {kind.value for kind in CombatKind}
    if not 2 <= len(actions) <= 255:
        raise CombatStateError("Ask Jev only when at least two real commands are available.")
    for action in actions.values():
        if action.get("kind") not in legal_kinds or not isinstance(action.get("label"), str):
            raise CombatStateError("Unsupported combat action.")
        members = action.get("units")
        if not isinstance(members, list) or len(members) > 12 or len(set(members)) != len(members):
            raise CombatStateError("A squad command must select at most twelve distinct units.")
        if action["kind"] != CombatKind.CONTINUE.value and not members:
            raise CombatStateError("An executable squad command requires combat units.")
        if any(uid not in own_by_id or own_by_id[uid].get("visible") is not True or own_by_id[uid].get("completed") is not True for uid in members):
            raise CombatStateError("A command references an unavailable friendly unit.")
        if action["kind"] in tactical_kinds and any(own_by_id[uid]["type_id"] not in mission.combat_types for uid in members):
            raise CombatStateError("A squad command references a non-combat unit.")
        if action["kind"] == CombatKind.ATTACK_TARGET.value and action.get("target") not in enemy_by_id:
            raise CombatStateError("Focus fire requires a currently visible hostile target.")
        if action["kind"] in {CombatKind.GATHER.value, CombatKind.BUILD.value, CombatKind.TRAIN.value, CombatKind.UPGRADE.value}:
            if len(members) != 1 or action.get("unit") != members[0]:
                raise CombatStateError("An economic command requires one explicit actor.")
            actor = own_by_id[members[0]]
            if action["kind"] in {CombatKind.GATHER.value, CombatKind.BUILD.value} and actor["type_id"] != 7:
                raise CombatStateError("Gathering or construction requires an SCV.")
            if action["kind"] == CombatKind.GATHER.value and action.get("target") not in visible_minerals:
                raise CombatStateError("Gathering requires a currently observed mineral field.")
            if action["kind"] == CombatKind.TRAIN.value and (actor["type_id"], action.get("train_type")) not in {(106, 7), (111, 0)}:
                raise CombatStateError("Unsupported producer or training target.")
            if action["kind"] == CombatKind.BUILD.value and action.get("building") not in {109, 111}:
                raise CombatStateError("Unsupported combat construction target.")
            if action["kind"] == CombatKind.UPGRADE.value and (actor["type_id"] != 122 or actor.get("order_id") == 76
                    or action.get("upgrade") != "infantry_weapons" or action.get("mineral_cost") != 100 or action.get("gas_cost") != 100
                    or state.get("minerals", 0) < 100 or state.get("gas", 0) < 100 or _weapons_upgrade_ordered(history)):
                raise CombatStateError("Unsupported or unavailable infantry weapons upgrade.")
        if action["kind"] not in {CombatKind.CONTINUE.value, CombatKind.TRAIN.value, CombatKind.UPGRADE.value}:
            point = action.get("point")
            width, height = _bounds(state)
            if not isinstance(point, dict) or type(point.get("x")) is not int or type(point.get("y")) is not int or not (0 <= point["x"] < width and 0 <= point["y"] < height):
                raise CombatStateError("The command destination is outside the observed map bounds.")
    squad_state = []
    for name, squad in squads:
        center = _center(squad)
        squad_state.append({
            "name": name, "composition": dict(Counter(unit.get("type", "unit") for unit in squad)),
            "center": center, "total_hp": sum(unit["hp"] for unit in squad),
            "formation": _formation(squad),
            "local_visible_ground_threats": _local_threats(squad, own, enemies, mission),
            "spread_pixels": round(max(_distance(unit, center) for unit in squad)),
            "members": [{key: value for key, value in _unit_summary(unit, owned=True).items()
                         if key in {"id", "generation", "type", "hp", "position", "current_activity", "order_target", "combat_role"}} for unit in squad],
            "current_order_counts": dict(Counter(_unit_summary(unit, owned=True)["current_activity"] for unit in squad)),
            "visible_enemies_nearest_first": [_unit_summary(unit, center, owned=False) for unit in sorted(enemies, key=lambda unit: _distance(unit, center))[:8]],
        })
    recent = []
    observed_positions = {}
    for event in (history or [])[-64:]:
        if not isinstance(event, dict):
            continue
        action = event.get("action", event)
        if not isinstance(action, dict):
            continue
        entry = {key: action[key] for key in ("kind", "label", "squad", "point", "target", "building", "upgrade") if key in action}
        member_ids = action.get("units", event.get("units"))
        if isinstance(member_ids, list) and member_ids and all(type(uid) is int and uid >= 0 for uid in member_ids):
            entry["units"] = sorted(set(member_ids))
            generations = action.get("unit_generations", event.get("unit_generations"))
            if isinstance(generations, dict):
                valid_generations = {str(uid): generations[str(uid)] for uid in entry["units"]
                                     if type(generations.get(str(uid))) is int and 0 <= generations[str(uid)] <= 31}
                if valid_generations:
                    entry["unit_generations"] = valid_generations
            if entry.get("kind") in tactical_kinds and "unit_generations" not in entry:
                entry["identity_note"] = "Legacy order lacks generations; ID-only matching applies only to current units whose generation is unknown."
        elif entry.get("kind") in tactical_kinds:
            entry["identity_note"] = "Legacy order has no member IDs; not applied to current squads by name."
        for key in ("frame", "accepted", "issued"):
            if key in event:
                entry[key] = event[key]
        if "command" in event:
            entry["label"] = event["command"]
        if isinstance(event.get("squad_centers"), list):
            for position in event["squad_centers"]:
                if isinstance(position, dict) and type(position.get("x")) is int and type(position.get("y")) is int:
                    key = (position["x"] // 128, position["y"] // 128)
                    observed_positions[key] = {"x": position["x"], "y": position["y"], "observed_frame": event.get("frame")}
        if entry and entry.get("kind") != CombatKind.CONTINUE.value:
            recent.append(entry)
    exploration_orders = [event for event in recent if event.get("kind") in {"explore", "attack_move"} and isinstance(event.get("point"), dict)][-12:]
    for squad in squad_state:
        def matching_members(event):
            return [unit for unit in squad["members"] if unit["id"] in event.get("units", [])
                    and ("generation" not in unit or event.get("unit_generations", {}).get(str(unit["id"])) == unit["generation"])]
        matching = [event for event in exploration_orders if event.get("accepted") is not False and matching_members(event)]
        if matching:
            latest = matching[-1]
            matched = matching_members(latest)
            legacy_note = " Legacy ID-only match: current generation is unknown for some matched members." if any("generation" not in unit for unit in matched) else ""
            squad["last_advance_order"] = {"destination": latest["point"], "distance_from_current_center": round(_distance(squad["center"], latest["point"])),
                                           "matched_member_ids": sorted(unit["id"] for unit in matched),
                                           "matched_member_generations": {str(unit["id"]): unit["generation"] for unit in matched if "generation" in unit},
                                           "accepted": latest.get("accepted"), "note": "Applies only to matched members, irrespective of current squad name. Acceptance confirms input, not arrival; current orders remain authoritative." + legacy_note}
            squad["last_advance_order"]["current_observation"] = {
                "matched_members_with_attack_move_order": sorted(unit["id"] for unit in matched if own_by_id[unit["id"]].get("order_id") == 14),
                "matched_members_standing_guard": sorted(unit["id"] for unit in matched if own_by_id[unit["id"]].get("order_id") == 3),
                "matched_members_within96px_of_destination": sorted(unit["id"] for unit in matched if _distance(unit["position"], latest["point"]) <= 96),
                "interpretation": "Attack-move orders can continue while another squad or production receives a command. Guard is not a march; a past accepted advance is not proof of a currently active order, arrival, or measured movement.",
            }
    economic_workers = []
    for unit in own:
        if unit["type_id"] != 7:
            continue
        job = ("being trained" if unit.get("completed") is not True else "constructing" if unit.get("order_id") in BUILDING_ORDERS else
               "gathering minerals" if unit.get("order_id") in MINERAL_ORDERS else "gathering gas" if unit.get("order_id") in GAS_ORDERS else "idle/other")
        economic_workers.append({"id": unit["id"], "position": _point(unit), "current_job": job, "visible": unit.get("visible", False)})
    combat_units = [unit for unit in own if unit["type_id"] in mission.combat_types and unit.get("completed") is True]
    current_activity = {
        "living_combat_units": len(combat_units),
        "largest_selectable_squad": max((len(squad) for _, squad in squads), default=0),
        "largest_coherent_core": max((squad["formation"]["core_count"] for squad in squad_state), default=0),
        "squads_with_coherent_8plus_core": sum(squad["formation"]["has_coherent_8plus_core"] for squad in squad_state),
        "combat_order_counts": dict(Counter(_unit_summary(unit, owned=True)["current_activity"] for unit in combat_units)),
        "combat_units_moving_or_attacking": sum(unit.get("order_id") in {6, 10, 14, 49} for unit in combat_units),
        "movement_count_note": "Counts Move, Follow, AttackUnit and AttackMove engine orders, not measured physical movement. Follow can remain active near its target.",
        "worker_count": len(economic_workers),
        "mineral_workers": sum(worker["current_job"] == "gathering minerals" for worker in economic_workers),
        "idle_workers": sum(worker["current_job"] == "idle/other" for worker in economic_workers),
        "queued_production": sum(len(unit.get("build_queue", [])) for unit in own if unit["type_id"] in {106, 111}),
        "unfinished_owned_buildings": sum(unit.get("completed") is not True for unit in own if 106 <= unit["type_id"] <= 173),
        "visible_enemies": len(enemies),
    }
    model_state = {
        "game": "Original StarCraft shareware combat mission", "mission": mission.name,
        "objectives": list(mission.objectives), "observed_frame": state.get("frame"),
        "mission_playbook": list(MISSION_PLAYBOOK),
        "combat_guidance": COMBAT_GUIDANCE,
        "recent_battle_outcomes": recent_battle_outcomes(state, history, mission.combat_types),
        "map_pixels": {"width": _bounds(state)[0], "height": _bounds(state)[1]},
        "coordinates": "x increases east; y increases south. Map boundaries are known; unseen enemy locations are not.",
        "resources": {key: state[key] for key in ("minerals", "gas", "supply") if key in state},
        "current_activity": current_activity,
        "unit_type_base_stats": {unit.get("type", str(unit["type_id"])): unit["combat_stats"] for unit in own + enemies if "combat_stats" in unit},
        "economy": {"workers": economic_workers,
                    "mineral_workers": sum(worker["current_job"] == "gathering minerals" for worker in economic_workers),
                    "production": [{"id": unit["id"], "type": unit.get("type"), "completed": unit.get("completed"), "queued_unit_types": unit.get("build_queue", [])} for unit in own if unit["type_id"] in {106, 111}],
                    "available_supply": supply(state)[1] - supply(state)[0],
                    "infantry_weapons_order_previously_accepted": _weapons_upgrade_ordered(history),
                    "visible_engineering_bays": [{"id": unit["id"], "completed": unit.get("completed", False),
                                                  "upgrading_order_active": unit.get("order_id") == 76}
                                                 for unit in own if unit["type_id"] == 122 and unit.get("visible") is True],
                    "recent_failed_build_sites": _failed_build_sites(state, history),
                    "failed_placement_rule": "An issued building command that failed verification suppresses only the same building type and exact point for 720 game frames. Other candidate sites remain available; this does not prove terrain is permanently invalid.",
                    "limits": "SCVs and Marines each cost50 minerals and1 supply. SCV cap12; Marine cap72; one queued unit per producer. Supply Depots cost100 and add8 supply; offered near supply limit with one underway. Barracks cost150; at most3 owned or underway, with only one Barracks under construction. Marines need no gas. Infantry weapons level1 costs100 minerals and100 gas at a completed Engineering Bay; offered until one issued order is accepted, never while that Bay has upgrade order76. Acceptance is not proof of completed research.",
                    "menu_actor_selection": "Equivalent worker assignments use the closest available worker to visible minerals; equivalent producers use one idle compatible building. Jev chooses whether to issue that concrete command.",
                    "parallel_orders": "A worker command does not stop army orders. Production runs concurrently with army movement and fighting."},
        "squads": squad_state,
        "owned_combat_units_not_in_selectable_squads": sum(unit["type_id"] in mission.combat_types for unit in own) - sum(len(squad) for _, squad in squads),
        "visible_enemy_count": len(enemies),
        "visible_allies": [_unit_summary(unit, owned=False) for unit in allies[:12]],
        "known_regions": [{"label": region.label, "position": {"x": region.x, "y": region.y}, "source": region.source} for region in mission.known_regions],
        "known_enemy_structures": _known_enemy_structures(state, enemies, history),
        "observed_friendly_bases": [{"id": unit["id"], "type": unit.get("type"), "position": _point(unit)} for unit in own
                                    if unit["type_id"] in {106, 111, 113} and unit.get("completed") is True and unit.get("visible") is True],
        "recent_model_orders": recent[-8:],
        "previously_ordered_exploration_destinations": exploration_orders,
        "previously_observed_friendly_positions": list(observed_positions.values())[-24:],
        "current_orders_are_authoritative": "Standing guard means idle until an enemy enters range; it is not an ongoing march. Follow 49 is a unit-target movement order and can remain active after getting close. Engine orders describe intent, not measured velocity. Previously accepted movement may have ended. Continue preserves current activities, including idle units; a production or other-squad command also preserves a formed force's current advance.",
        "observation_limits": "Only owned units and currently visible hostiles/allies, plus up to32 recorded prior visible enemy structures with explicitly uncertain current presence. Attack-move options may revisit the four most recent structure sightings; focus fire still requires current visibility. Focus options use the four nearest visible enemies per squad; squad summaries show the eight nearest. Up to eight nearby squads of twelve are offered. Candidate geometry does not reveal terrain passability; the original engine resolves movement. No never-observed base coordinates or winning route are provided.",
    }
    assignment = next((action for action in actions.values() if action["kind"] == CombatKind.GATHER.value), None)
    if assignment is not None:
        actor = next(worker for worker in economic_workers if worker["id"] == assignment["unit"])
        model_state["next_mineral_assignment"] = {
            "actor_id": actor["id"], "actor_current_job": actor["current_job"],
            "actor_is_already_a_miner": actor["current_job"] == "gathering minerals",
            "currently_assigned_mineral_workers": current_activity["mineral_workers"],
            "military_orders_unchanged": True,
        }
    questions = {"action": {
        "type": "choice",
        "instructions": "Choose the available gameplay command that makes the most useful progress toward completing the mission from the CURRENT observed state. Use combat_guidance: compare local armed threats, composition, HP and range; a coherent formation does not guarantee enough strength. Handle immediate armed defenders before durable unarmed buildings and reconsider approaches associated with recent observed losses. Compare income, production, and exploration too. Continue means issue no command: judge its effect from current orders, not historical acceptance. A worker or production command can run concurrently with existing military orders. Avoid reversing a productive advance without a tactical reason. Unknown enemy locations remain unknown. All options are real gameplay actions; choose one.",
        "criteria": {key: action["label"] for key, action in actions.items()},
    }}
    for key, action in actions.items():
        if action["kind"] == CombatKind.CONTINUE.value:
            questions["action"]["criteria"][key] = (
                f"Issue no command; preserve current activities: {current_activity['combat_units_moving_or_attacking']} combat units with move/follow/attack orders (not measured movement), "
                f"{current_activity['mineral_workers']} mineral workers, {current_activity['queued_production']} queued units. "
                "Idle/guard units remain idle; no new mining, production or scouting begins."
            )
    return model_state, questions


_INTENT_KINDS = {
    "Economy": {"gather", "train", "build", "upgrade"},
    "Engage": {"attack_target", "attack_move"},
    "Explore": {"explore"},
    "Reposition": {"retreat", "regroup"},
    "Continue": {"continue"},
}
_GRAPH_SEMANTICS = "Independent parallel judgments; intent selects a branch, then its selected concrete command. No joint probability is inferred."


def graph_routing(actions: dict[str, CombatAction]) -> dict:
    """Reconstruct the complete route map from exact candidate IDs and kinds."""
    if (not isinstance(actions, dict) or not 2 <= len(actions) <= 255
            or any(not isinstance(key, str) or not key or not isinstance(action, dict)
                   or action.get("kind") not in set().union(*_INTENT_KINDS.values()) for key, action in actions.items())):
        raise CombatStateError("Invalid candidates for intent graph routing.")
    routing = {"version": 1, "root_question": "intent", "branches": {}, "semantics": _GRAPH_SEMANTICS}
    for category, kinds in _INTENT_KINDS.items():
        candidate_ids = [key for key, action in actions.items() if action["kind"] in kinds]
        if candidate_ids:
            question_id = "action_" + category.lower() if len(candidate_ids) >= 2 else None
            routing["branches"][category] = {"question": question_id, "candidate_ids": candidate_ids}
    if len(routing["branches"]) < 2:
        raise CombatStateError("An intent graph requires at least two available command categories.")
    return routing


def graph_request_for(state: dict, actions: dict[str, CombatAction], history: list[dict] | None = None,
                      mission: MissionConfig = STRONGARM) -> tuple[dict, dict, dict]:
    """Batch an intent Choice and speculative per-intent command Choices.

    All questions see the same validated state, independently. Routing happens in
    the harness after the response; child questions never see the intent answer.
    A singleton branch needs no child inference. Its only command is selected
    through Jev's intent answer, without inventing another probability.
    """
    model_state, flat_questions = request_for(state, actions, history, mission)
    descriptions = {
        "Economy": "Issue a worker, production, supply, or infantry weapons upgrade command. Existing military orders continue independently; research takes time.",
        "Engage": "Issue a squad attack against a currently visible hostile target or attack-move toward a known objective. Compare immediate armed threats with unarmed buildings; stale sightings remain uncertain.",
        "Explore": "Issue a squad attack-move to reveal terrain and look for remaining hostile forces or the rebel base. A coherent core is already assembled wherever it currently stands; maintaining replacements does not require returning that force to base.",
        "Reposition": "Move a squad to regroup or retreat; ordinary movement replaces firing or an advance. Consider formation.core_count: regrouping a coherent core is an optional reposition or withdrawal, not unfinished assembly. Small reinforcements and threatened forces can still benefit.",
        "Continue": "Issue no new command; preserve only the currently observed activities.",
    }
    questions = {"intent": {
        "type": "choice",
        "instructions": "Choose which kind of gameplay command would make the most useful progress toward completing the mission from the CURRENT observed state. Use mission_playbook and combat_guidance: establish income, maintain Marine production, and compare local armed defenders, HP, composition, range and recent_battle_outcomes before committing a force. Handle immediate armed threats before durable unarmed buildings; Marines should preserve range against Firebat splash. Check each squad's formation, not its name or total scattered army. At least 8 combat units in a measured 192px core means already assembled, not sufficient strength against the observed defense. When strength permits, preserve its advance while replacements are produced or smaller groups regroup. A distant reinforcement does not require reversing it. After costly observed losses, reconsider the approach, rebuild or reinforce; current danger can justify retreat. Compare each category's best available command regardless of option count. Continue starts no new activity; production and commands to other squads preserve an existing advance. Select a category; independent companion questions recommend its concrete commands.",
        "criteria": {},
    }}
    routing = graph_routing(actions)
    flat_criteria = flat_questions["action"]["criteria"]
    for category, branch in routing["branches"].items():
        candidate_ids, question_id = branch["candidate_ids"], branch["question"]
        description = descriptions[category]
        if category == "Continue":
            description = flat_criteria[candidate_ids[0]]
        questions["intent"]["criteria"][category] = description + " Available commands: " + "; ".join(candidate_ids)
        if question_id is not None:
            questions[question_id] = {
                "type": "choice",
                "instructions": f"Assuming a {category.lower()} command is to be issued, choose the available concrete command that best advances the mission from current observations, mission_playbook and combat_guidance. Compare local armed threats and range before durable unarmed buildings; avoid close-range Firebat splash and repeating approaches associated with recent observed losses at comparable strength. Maintain income and production. A measured core of at least 8 is already assembled but is not automatically strong enough for the defense. A return to base is a withdrawal, not required assembly. After losses, reinforce; health and visible danger can justify retreat. This is an independent recommendation, used only if the separate intent decision selects {category}. Compare actual members, formation, targets, health, current orders, resources, and observed positions. Preserve a productive supported advance unless there is a tactical reason to replace it; other-squad commands and production run concurrently. Last-seen structures remain uncertain and never-observed locations unknown. Choose one actual gameplay command.",
                "criteria": {key: flat_criteria[key] for key in candidate_ids},
            }
    return model_state, questions, routing


def resolve_graph_choice(response: dict, routing: dict) -> tuple[str, str | None]:
    """Route a client-validated response without changing any probabilities.

    This verifies graph identity and exact answer/candidate sets. The API client
    remains responsible for numerical probability and response validation.
    Audit tools should reconstruct the routing from the recorded candidate map.
    """
    if (not isinstance(routing, dict) or set(routing) != {"version", "root_question", "branches", "semantics"}
            or type(routing["version"]) is not int or routing["version"] != 1
            or routing["root_question"] != "intent" or routing["semantics"] != _GRAPH_SEMANTICS):
        raise CombatStateError("Invalid intent graph routing metadata.")
    branches = routing["branches"]
    if not isinstance(branches, dict) or not 2 <= len(branches) <= len(_INTENT_KINDS) or not set(branches) <= set(_INTENT_KINDS):
        raise CombatStateError("Invalid intent graph branches.")
    expected_answers = {"intent": set(branches)}
    seen_candidates = set()
    for category, branch in branches.items():
        if not isinstance(branch, dict) or set(branch) != {"question", "candidate_ids"}:
            raise CombatStateError("Invalid intent graph branch metadata.")
        ids = branch["candidate_ids"]
        if (not isinstance(ids, list) or not ids or any(not isinstance(key, str) or not key for key in ids)
                or len(ids) != len(set(ids)) or seen_candidates.intersection(ids)):
            raise CombatStateError("Graph branches require distinct concrete candidate IDs.")
        seen_candidates.update(ids)
        expected_question = "action_" + category.lower() if len(ids) >= 2 else None
        if branch["question"] != expected_question:
            raise CombatStateError("Graph child question does not match its singleton or multi-command branch.")
        if expected_question is not None:
            expected_answers[expected_question] = set(ids)
    answers = response.get("answers") if isinstance(response, dict) else None
    if not isinstance(answers, dict) or set(answers) != set(expected_answers):
        raise CombatStateError("Graph response question IDs do not match the routing.")
    for question_id, expected_ids in expected_answers.items():
        answer = answers[question_id]
        if (not isinstance(answer, dict) or answer.get("type") != "choice"
                or not isinstance(answer.get("choice"), str) or answer["choice"] not in expected_ids
                or not isinstance(answer.get("probabilities"), dict) or set(answer["probabilities"]) != expected_ids):
            raise CombatStateError("Graph answer choices do not match their exact candidate IDs.")
    branch = branches[answers["intent"]["choice"]]
    child_question = branch["question"]
    return (answers[child_question]["choice"] if child_question is not None else branch["candidate_ids"][0]), child_question
