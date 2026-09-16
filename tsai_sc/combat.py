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

from .controller import BUILDING_ORDERS, GAS_ORDERS, MINERAL_ORDERS, depot_sites, supply


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


def _put(actions: dict[str, CombatAction], title: str, action: CombatAction) -> None:
    key = title
    number = 2
    while key in actions:
        key = f"{title} ({number})"
        number += 1
    actions[key] = action


def _economy_candidates(state: dict, own: list[dict], actions: dict[str, CombatAction]) -> None:
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
    depot_underway = any(unit["type_id"] == 109 and unit.get("completed") is not True for unit in own)
    if amount >= 100 and capacity - used <= 3 and not depot_underway and available_workers:
        for index, point in enumerate(depot_sites(state)[:2], 1):
            worker = min(available_workers, key=lambda unit: _distance(unit, point))
            _put(actions, f"Build Supply Depot: site {index}", {
                "kind": CombatKind.BUILD.value, "label": f"Build a Supply Depot at open candidate site {index} near the friendly base (100 minerals, adds 8 supply)",
                "units": [worker["id"]], "unit": worker["id"], "building": 109, "point": dict(point), "mineral_cost": 100,
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
    army_center = _center([unit for _, squad in squads for unit in squad]) if squads else None
    for name, squad in squads:
        center = _center(squad)
        unit_ids = sorted(unit["id"] for unit in squad)
        nearby_enemies = sorted(enemies, key=lambda unit: (_distance(unit, center), unit["id"]))
        # Each target is currently visible. A bounded nearest-target shortlist is
        # disclosed in model state; it does not choose the model's focus target.
        for enemy in nearby_enemies[:4]:
            enemy_name = str(enemy.get("type", "enemy unit"))
            direction = _direction(center, enemy)
            title = f"{name}: focus {enemy_name} {direction}"
            _put(actions, title, {"kind": CombatKind.ATTACK_TARGET.value, "label": f"{name}: focus fire on visible enemy {enemy_name} {direction} ({enemy['hp']} HP)",
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
            if bases:
                home = min(bases, key=lambda unit: _distance(unit, center))
                if _distance(home, center) >= 192:
                    _put(actions, f"{name}: fall back to friendly base", {
                        "kind": CombatKind.RETREAT.value, "label": f"{name}: fall back toward the observed friendly {home.get('type', 'base')}",
                        "units": unit_ids.copy(), "squad": name, "point": _point(home),
                    })
        spread = max(_distance(unit, center) for unit in squad)
        if army_center is not None and (_distance(center, army_center) >= 96 or spread >= 96):
            _put(actions, f"{name}: regroup with friendly force", {
                "kind": CombatKind.REGROUP.value, "label": f"{name}: move toward the current friendly combat-force center to regroup",
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
    if len(actions) > 255:
        raise CombatStateError("The combat action menu exceeds the model's choice limit.")
    _economy_candidates(state, own, actions)
    if len(actions) > 255:
        raise CombatStateError("The combined combat/economy menu exceeds the model's choice limit.")
    return actions


def _unit_summary(unit: dict, origin: dict | None = None, *, owned: bool) -> dict:
    result = {"id": unit["id"], "type": unit.get("type", "Unknown unit"), "hp": unit["hp"],
              "position": _point(unit), "completed": unit.get("completed", False),
              "engine_order_id": unit.get("order_id"), "visible": unit.get("visible", False)}
    result["current_activity"] = {3: "standing guard", 6: "moving", 10: "attacking a target", 14: "attack-moving"}.get(unit.get("order_id"), "other engine order")
    if origin is not None:
        result.update(direction=_direction(origin, unit), distance_pixels=round(_distance(origin, unit)))
    for key in ("max_hp", "shields", "ground_weapon_cooldown", "air_weapon_cooldown", "weapon_cooldown", "combat_stats", "kills"):
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
        if action["kind"] in {CombatKind.GATHER.value, CombatKind.BUILD.value, CombatKind.TRAIN.value}:
            if len(members) != 1 or action.get("unit") != members[0]:
                raise CombatStateError("An economic command requires one explicit actor.")
            actor = own_by_id[members[0]]
            if action["kind"] in {CombatKind.GATHER.value, CombatKind.BUILD.value} and actor["type_id"] != 7:
                raise CombatStateError("Gathering or construction requires an SCV.")
            if action["kind"] == CombatKind.GATHER.value and action.get("target") not in visible_minerals:
                raise CombatStateError("Gathering requires a currently observed mineral field.")
            if action["kind"] == CombatKind.TRAIN.value and (actor["type_id"], action.get("train_type")) not in {(106, 7), (111, 0)}:
                raise CombatStateError("Unsupported producer or training target.")
        if action["kind"] not in {CombatKind.CONTINUE.value, CombatKind.TRAIN.value}:
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
            "spread_pixels": round(max(_distance(unit, center) for unit in squad)),
            "members": [{key: value for key, value in _unit_summary(unit, owned=True).items()
                         if key in {"id", "type", "hp", "position", "current_activity", "order_target"}} for unit in squad],
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
        entry = {key: action[key] for key in ("kind", "label", "squad", "point", "target") if key in action}
        member_ids = action.get("units", event.get("units"))
        if isinstance(member_ids, list) and member_ids and all(type(uid) is int and uid >= 0 for uid in member_ids):
            entry["units"] = sorted(set(member_ids))
        elif entry.get("kind") in tactical_kinds:
            entry["identity_note"] = "Legacy order has no member IDs; not applied to current squads by name."
        for key in ("frame", "accepted"):
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
        member_ids = {unit["id"] for unit in squad["members"]}
        matching = [event for event in exploration_orders
                    if event.get("accepted") is not False and member_ids.intersection(event.get("units", []))]
        if matching:
            latest = matching[-1]
            squad["last_advance_order"] = {"destination": latest["point"], "distance_from_current_center": round(_distance(squad["center"], latest["point"])),
                                           "matched_member_ids": sorted(member_ids.intersection(latest["units"])),
                                           "accepted": latest.get("accepted"), "note": "Applies only to matched members, irrespective of current squad name. Acceptance confirms input, not arrival; current orders remain authoritative."}
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
        "combat_order_counts": dict(Counter(_unit_summary(unit, owned=True)["current_activity"] for unit in combat_units)),
        "combat_units_moving_or_attacking": sum(unit.get("order_id") in {6, 10, 14} for unit in combat_units),
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
        "map_pixels": {"width": _bounds(state)[0], "height": _bounds(state)[1]},
        "coordinates": "x increases east; y increases south. Map boundaries are known; unseen enemy locations are not.",
        "resources": {key: state[key] for key in ("minerals", "gas", "supply") if key in state},
        "current_activity": current_activity,
        "unit_type_base_stats": {unit.get("type", str(unit["type_id"])): unit["combat_stats"] for unit in own + enemies if "combat_stats" in unit},
        "economy": {"workers": economic_workers,
                    "mineral_workers": sum(worker["current_job"] == "gathering minerals" for worker in economic_workers),
                    "production": [{"id": unit["id"], "type": unit.get("type"), "completed": unit.get("completed"), "queued_unit_types": unit.get("build_queue", [])} for unit in own if unit["type_id"] in {106, 111}],
                    "available_supply": supply(state)[1] - supply(state)[0],
                    "limits": "SCVs and Marines each cost50 minerals and1 supply. SCV cap12; Marine cap72; one queued unit per producer. Supply Depots cost100 and add8 supply; offered near supply limit with one underway. Marines need no gas.",
                    "menu_actor_selection": "Equivalent worker assignments use the closest available worker to visible minerals; equivalent producers use one idle compatible building. Jev chooses whether to issue that concrete command.",
                    "parallel_orders": "A worker command does not stop army orders. Production runs concurrently with army movement and fighting."},
        "squads": squad_state,
        "owned_combat_units_not_in_selectable_squads": sum(unit["type_id"] in mission.combat_types for unit in own) - sum(len(squad) for _, squad in squads),
        "visible_enemy_count": len(enemies),
        "visible_allies": [_unit_summary(unit, owned=False) for unit in allies[:12]],
        "known_regions": [{"label": region.label, "position": {"x": region.x, "y": region.y}, "source": region.source} for region in mission.known_regions],
        "recent_model_orders": recent[-8:],
        "previously_ordered_exploration_destinations": exploration_orders,
        "previously_observed_friendly_positions": list(observed_positions.values())[-24:],
        "current_orders_are_authoritative": "Standing guard means idle until an enemy enters range; it is not an ongoing march. Previously accepted movement may have ended. Continue issues no new command and preserves the current observed activities, including any idle units.",
        "observation_limits": "Only owned units and currently visible hostiles/allies. Focus options use the four nearest visible enemies per squad; squad summaries show the eight nearest. Up to eight nearby squads of twelve are offered. Candidate geometry does not reveal terrain passability; the original engine resolves movement. No unseen base coordinates or winning route are provided.",
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
        "instructions": "Choose the available gameplay command that makes the most useful progress toward completing the mission from the CURRENT observed state. Compare present combat activity, health, visible targets, mineral income, production, and exploration. Continue means issue no command: judge its effect from current orders, not historical acceptance. A worker or production command can run concurrently with existing military orders. Avoid reversing a productive advance without a tactical reason. Unknown enemy locations remain unknown. All options are real gameplay actions; choose one.",
        "criteria": {key: action["label"] for key, action in actions.items()},
    }}
    for key, action in actions.items():
        if action["kind"] == CombatKind.CONTINUE.value:
            questions["action"]["criteria"][key] = (
                f"Issue no command; preserve current activities: {current_activity['combat_units_moving_or_attacking']} combat units moving/attacking, "
                f"{current_activity['mineral_workers']} mineral workers, {current_activity['queued_production']} queued units. "
                "Idle/guard units remain idle; no new mining, production or scouting begins."
            )
    return model_state, questions
