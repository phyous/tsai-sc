"""Compact memory of observed combat-unit disappearances, not inferred deaths.

Only owned units and currently visible hostile actors enter a snapshot. A unit
missing from a later snapshot near previously visible hostiles is an observed
loss of that identity; these facts do not establish its cause or enemy locations
after visibility ends. No game commands, target orders, or map routes are stored.
"""
from __future__ import annotations

from collections import Counter
import math

from .game import BASE_COMBAT_STATS, UNIT_NAMES


_WORKERS = frozenset({7, 41, 64})
_NEAR_HOSTILES = 512
_MERGE_RADIUS = 384
_MERGE_FRAMES = 600
_HISTORY_LIMIT = 64
_ENCOUNTER_LIMIT = 4
_FIELDS = ("id", "generation", "type_id", "type", "hp", "x", "y")


def _identity(unit):
    return unit["id"], unit["generation"]


def _distance(a, b):
    return math.hypot(a["x"] - b["x"], a["y"] - b["y"])


def _valid_actor(unit):
    return (isinstance(unit, dict)
            and all(type(unit.get(key)) is int for key in ("id", "generation", "type_id", "hp", "x", "y"))
            and 0 <= unit["id"] < 1700 and 0 <= unit["generation"] <= 31
            and 0 <= unit["type_id"] < 228 and 0 < unit["hp"] <= 100000
            and 0 <= unit["x"] < 8192 and 0 <= unit["y"] < 8192
            and isinstance(unit.get("type"), str) and 0 < len(unit["type"]) <= 80)


def history_snapshot(state, combat_types):
    """Return small, detached observations suitable for history['combat_snapshot'].

    ``combat_types`` identifies the player's military units. Visible hostile
    workers and other ground attackers with verified base stats are included
    independently, so a Terran-only player list does not omit enemy Zerglings.
    Missing generation counters are not guessed; unfinished units are excluded.
    """
    frame, player = state.get("frame"), state.get("player_id")
    if type(frame) is not int or frame < 0 or type(player) is not int:
        raise ValueError("Battle snapshots require a frame and local player")
    own_types = frozenset(combat_types)
    hostile_types = own_types | frozenset(
        kind for kind, stats in BASE_COMBAT_STATS.items() if stats.get("base_ground_damage", 0) > 0)
    enemy_players = state.get("enemy_players")
    own, hostiles = [], []
    for unit in state.get("units", []):
        if not isinstance(unit, dict):
            continue
        owner = unit.get("owner")
        # Filter visibility before examining or copying a non-owned location.
        if owner != player and unit.get("visible") is not True:
            continue
        hostile = (owner in enemy_players if isinstance(enemy_players, (list, tuple, set, frozenset))
                   else unit.get("relationship") == "enemy")
        if not ((owner == player and unit.get("type_id") in own_types)
                or (owner != player and hostile and unit.get("type_id") in hostile_types)):
            continue
        if unit.get("completed") is not True:
            continue
        actor = {key: unit.get(key) for key in _FIELDS}
        actor["type"] = UNIT_NAMES.get(unit.get("type_id"), unit.get("type", "Unknown unit"))
        if _valid_actor(actor):
            (own if owner == player else hostiles).append(actor)
    return {"frame": frame, "own_combat": sorted(own, key=_identity),
            "visible_hostile_combat": sorted(hostiles, key=_identity)}


def _read_snapshot(value, current_frame):
    """Ignore malformed/future history as a whole, never manufacture missing units."""
    if (not isinstance(value, dict) or type(value.get("frame")) is not int
            or not 0 <= value["frame"] <= current_frame):
        return None
    result = {"frame": value["frame"]}
    for key in ("own_combat", "visible_hostile_combat"):
        actors = value.get(key)
        if (not isinstance(actors, list) or len(actors) > 1700
                or any(not _valid_actor(actor) for actor in actors)
                or len({actor["id"] for actor in actors}) != len(actors)):
            return None
        result[key] = [{field: actor[field] for field in _FIELDS} for actor in actors]
    return result


def recent_battle_outcomes(state, history, combat_types):
    """Summarize at most four recent encounters from the last 64 snapshots.

    Adjacent observations establish disappearances of exact pool ID/generation
    pairs. Only missing actors within 512px of a then-visible hostile qualify.
    Nearby loss observations up to 600 game frames apart coalesce; the recorded
    hostile composition comes from one actual snapshot, never a union presented
    as a simultaneous force. Currently re-observed identities are not losses.
    """
    current = history_snapshot(state, combat_types)
    snapshots = []
    for event in (history or [])[-_HISTORY_LIMIT:]:
        snapshot = _read_snapshot(event.get("combat_snapshot"), current["frame"]) if isinstance(event, dict) else None
        if snapshot is None or (snapshots and snapshot["frame"] < snapshots[-1]["frame"]):
            continue
        if snapshots and snapshot["frame"] == snapshots[-1]["frame"]:
            snapshots[-1] = snapshot
        else:
            snapshots.append(snapshot)
    if snapshots and snapshots[-1]["frame"] == current["frame"]:
        snapshots[-1] = current
    else:
        snapshots.append(current)
    presently_owned = {_identity(unit) for unit in current["own_combat"]}
    encounters = []
    for previous, following in zip(snapshots, snapshots[1:]):
        following_ids = {_identity(unit) for unit in following["own_combat"]}
        missing = [unit for unit in previous["own_combat"]
                   if _identity(unit) not in following_ids | presently_owned
                   and any(_distance(unit, enemy) <= _NEAR_HOSTILES for enemy in previous["visible_hostile_combat"])]
        while missing:
            anchor = missing[0]
            group = [unit for unit in missing if _distance(unit, anchor) <= _MERGE_RADIUS]
            group_ids = {_identity(unit) for unit in group}
            missing = [unit for unit in missing if _identity(unit) not in group_ids]
            location = {axis: round(sum(unit[axis] for unit in group) / len(group)) for axis in ("x", "y")}
            enemies = [enemy for enemy in previous["visible_hostile_combat"]
                       if any(_distance(unit, enemy) <= _NEAR_HOSTILES for unit in group)]
            encounter = next((item for item in reversed(encounters)
                              if following["frame"] - item["last_observed_frame"] <= _MERGE_FRAMES
                              and _distance(item["observed_location"], location) <= _MERGE_RADIUS), None)
            if encounter is None:
                encounter = {"first_observed_frame": previous["frame"],
                             "last_observed_frame": following["frame"], "observed_location": location,
                             "missing": {}, "hostiles": [], "armed_force_observed_frame": previous["frame"]}
                encounters.append(encounter)
            encounter["last_observed_frame"] = following["frame"]
            encounter["missing"].update({_identity(unit): unit for unit in group})
            losses = list(encounter["missing"].values())
            encounter["observed_location"] = {
                axis: round(sum(unit[axis] for unit in losses) / len(losses)) for axis in ("x", "y")}
            strength = lambda actors: (sum(actor["type_id"] not in _WORKERS for actor in actors),
                                       sum(actor["hp"] for actor in actors if actor["type_id"] not in _WORKERS), len(actors))
            if strength(enemies) > strength(encounter["hostiles"]):
                encounter["hostiles"] = enemies
                encounter["armed_force_observed_frame"] = previous["frame"]
    result = []
    for encounter in sorted(encounters, key=lambda item: item["last_observed_frame"])[-_ENCOUNTER_LIMIT:]:
        losses = list(encounter["missing"].values())
        military = [unit for unit in encounter["hostiles"] if unit["type_id"] not in _WORKERS]
        workers = [unit for unit in encounter["hostiles"] if unit["type_id"] in _WORKERS]
        result.append({
            "first_observed_frame": encounter["first_observed_frame"],
            "last_observed_frame": encounter["last_observed_frame"],
            "observed_location": encounter["observed_location"],
            "own_units_no_longer_observed": len(losses),
            "own_types_no_longer_observed": dict(sorted(Counter(unit["type"] for unit in losses).items())),
            "previously_visible_armed_composition": dict(sorted(Counter(unit["type"] for unit in military).items())),
            "observed_hostile_hp": sum(unit["hp"] for unit in military),
            "previously_visible_attack_capable_workers": dict(sorted(Counter(unit["type"] for unit in workers).items())),
            "observed_worker_hp": sum(unit["hp"] for unit in workers),
            "armed_force_observed_frame": encounter["armed_force_observed_frame"],
            "observation_note": "Owned combat identities disappeared between observations near then-visible hostiles; this does not establish death or its cause. Location summarizes their last observed positions. Hostile counts and HP are from one recorded observation, not a predicted force.",
            "current_enemy_uncertainty": "These past sightings do not establish current enemy positions, survival, or strength. Use current visible observations for those facts.",
        })
    return result
