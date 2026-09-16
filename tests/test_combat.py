"""Actual-command policy menus, fog-of-war boundaries, and tactical observations."""

import copy
import json
import math
import unittest

from tsai_sc.combat import (
    CombatKind, CombatStateError, KnownRegion, MissionConfig, STRONGARM,
    candidates, graph_request_for, graph_routing, request_for, resolve_graph_choice,
)


def unit(uid, kind=0, owner=6, x=400, y=400, *, visible=True, completed=True, hp=40, name="Marine"):
    return {"id": uid, "type_id": kind, "type": name, "owner": owner, "x": x, "y": y,
            "hp": hp, "visible": visible, "completed": completed, "order_id": 3, "build_queue": []}


def state():
    return {"player_id": 6, "frame": 300, "mission": "Strongarm", "minerals": 150, "gas": 0,
            "map": {"width_tiles": 96, "height_tiles": 64},
            "units": [unit(1), unit(2, x=424), unit(3, 106, x=280, y=320, hp=1500, name="Command Center"),
                      unit(4, 7, x=320, y=360, hp=60, name="SCV")]}


class CombatTests(unittest.TestCase):
    def test_unseen_enemy_locations_never_enter_actions_or_model_state(self):
        observed = state()
        baseline = candidates(observed)
        model, _ = request_for(observed, baseline)
        observed["units"].append(unit(900, 38, owner=3, x=2880, y=1800, visible=False, name="Hidden Hydralisk"))
        self.assertEqual(candidates(observed), baseline)
        self.assertEqual(request_for(observed, baseline)[0], model)
        self.assertFalse(any(action["kind"] == "attack_target" for action in baseline.values()))

    def test_visible_hostile_focus_and_attack_map_to_exact_game_targets(self):
        observed = state()
        observed["units"].append(unit(20, owner=0, x=560, y=420, hp=13))
        actions = candidates(observed)
        focus = next(action for action in actions.values() if action["kind"] == "attack_target")
        self.assertEqual(focus["units"], [1, 2])
        self.assertEqual(focus["target"], 20)
        self.assertEqual(focus["point"], {"x": 560, "y": 420})
        self.assertIn("13 HP", focus["label"])
        self.assertTrue(any(action["kind"] == "attack_move" for action in actions.values()))
        self.assertTrue(any(action["kind"] == "retreat" for action in actions.values()))
        _, questions = request_for(observed, actions)
        self.assertEqual(set(questions["action"]["criteria"]), set(actions))
        for key, action in actions.items():
            if action["kind"] != "continue":
                self.assertEqual(questions["action"]["criteria"][key], action["label"])

    def test_allied_and_neutral_units_are_never_hostile_targets(self):
        observed = state()
        observed["units"].extend([unit(20, owner=2), unit(21, owner=11, kind=176, name="Mineral Field")])
        actions = candidates(observed)
        self.assertFalse(any(action["kind"] == "attack_target" for action in actions.values()))
        model, _ = request_for(observed, actions)
        self.assertEqual(model["visible_enemy_count"], 0)
        self.assertEqual([unit["id"] for unit in model["visible_allies"]], [20])

    def test_visible_non_owned_internal_waypoints_never_enter_model_state(self):
        observed = state()
        observed["units"].extend([unit(20, owner=0, x=560, y=420), unit(21, owner=2, x=590, y=450)])
        observed["units"][0]["order_target"] = {"x": 800, "y": 400}
        baseline, _ = request_for(observed, candidates(observed))
        for non_owned in observed["units"][-2:]:
            non_owned["order_target"] = {"x": 2789, "y": 1837, "unit_id": 999, "secret": "unseen rendezvous"}
        model, _ = request_for(observed, candidates(observed))
        self.assertEqual(model, baseline)
        serialized = json.dumps(model)
        for hidden_value in ("2789", "1837", "unseen rendezvous"):
            self.assertNotIn(hidden_value, serialized)
        self.assertNotIn("order_target", model["squads"][0]["visible_enemies_nearest_first"][0])
        self.assertNotIn("order_target", model["visible_allies"][0])
        self.assertEqual(model["squads"][0]["members"][0]["order_target"], {"x": 800, "y": 400})

    def test_only_visible_completed_owned_combat_units_are_selected(self):
        observed = state()
        observed["units"].extend([unit(10, visible=False), unit(11, completed=False), unit(12, hp=0), unit(13, owner=2)])
        for action in candidates(observed).values():
            if action["kind"] not in {"gather", "train", "build"}:
                self.assertLessEqual(set(action["units"]), {1, 2})

    def test_squads_respect_original_game_twelve_unit_selection_limit(self):
        observed = state()
        observed["units"] = [unit(index, x=400 + index * 3) for index in range(30)]
        actions = candidates(observed)
        groups = {action["squad"]: action["units"] for action in actions.values() if "squad" in action}
        self.assertEqual(sorted(len(group) for group in groups.values()), [6, 12, 12])
        self.assertEqual(set().union(*(set(group) for group in groups.values())), set(range(30)))

    def test_generic_exploration_has_no_fixed_winning_route_and_stays_in_bounds(self):
        observed = state()
        observed["units"] = [unit(1, x=40, y=40)]
        actions = candidates(observed)
        exploration = [action for action in actions.values() if action["kind"] == "explore"]
        self.assertEqual(len(exploration), 2)
        self.assertTrue(any("east" in action["label"] for action in exploration))
        self.assertTrue(any("south" in action["label"] for action in exploration))
        for action in exploration:
            self.assertTrue(0 <= action["point"]["x"] < 3072)
            self.assertTrue(0 <= action["point"]["y"] < 2048)
        observed["units"][0].update(x=2000, y=1000)
        moved = candidates(observed)
        self.assertEqual(len([action for action in moved.values() if action["kind"] == "explore"]), 4)

    def test_briefing_or_previously_observed_regions_are_explicitly_sourced(self):
        mission = MissionConfig("Rescue", ("Reach the previously seen friendly outpost.",), (0,), (2, 6),
                                (KnownRegion("friendly outpost", 1200, 700, "previously_observed"),))
        actions = candidates(state(), mission)
        region = next(action for action in actions.values() if action.get("objective"))
        self.assertEqual(region["point"], {"x": 1200, "y": 700})
        self.assertIn("previously observed", region["label"])
        model, _ = request_for(state(), actions, mission=mission)
        self.assertEqual(model["known_regions"][0]["source"], "previously_observed")

    def test_unsafe_forged_target_or_selection_fails_before_model_request(self):
        observed = state()
        actions = candidates(observed)
        key = next(key for key, action in actions.items() if action["kind"] == "explore")
        forged = copy.deepcopy(actions)
        forged[key].update(kind="attack_target", target=999)
        with self.assertRaises(CombatStateError):
            request_for(observed, forged)
        forged = copy.deepcopy(actions)
        forged[key]["units"] = [4]  # SCV is not part of a combat squad.
        with self.assertRaises(CombatStateError):
            request_for(observed, forged)

    def test_model_state_contains_only_current_tactical_facts_and_selected_history(self):
        observed = state()
        observed["units"].append(unit(20, owner=3, x=560, y=420, hp=9, name="Zergling"))
        history = [{"frame": 240, "accepted": False, "action": {"kind": "attack_target", "label": "Alpha: focus Zergling", "target": 20}, "debug_secret": "never copy this"}]
        model, question = request_for(observed, candidates(observed), history)
        self.assertEqual(model["squads"][0]["total_hp"], 80)
        self.assertEqual(model["visible_enemy_count"], 1)
        self.assertEqual(model["recent_model_orders"][0]["accepted"], False)
        self.assertNotIn("debug_secret", repr(model))
        self.assertEqual(set(question), {"action"})

    def test_no_combat_force_is_not_fake_victory_or_fallback(self):
        observed = state()
        observed["units"] = [unit(4, 7, name="SCV")]
        actions = candidates(observed)
        self.assertEqual(len(actions), 1)
        self.assertEqual(next(iter(actions.values()))["kind"], CombatKind.CONTINUE.value)
        with self.assertRaises(CombatStateError):
            request_for(observed, actions)

    def test_economic_options_have_real_actors_costs_and_training_types(self):
        observed = state()
        observed["units"].extend([unit(30, 111, x=700, y=320, hp=1000, name="Barracks"),
                                   unit(31, 176, owner=11, x=250, y=250, hp=100000, name="Mineral Field")])
        actions = candidates(observed)
        trained = {action["train_type"]: action for action in actions.values() if action["kind"] == "train"}
        self.assertEqual(set(trained), {0, 7})
        self.assertEqual(trained[0]["unit"], 30)
        self.assertEqual(trained[7]["unit"], 3)
        self.assertEqual(trained[0]["mineral_cost"], 50)
        gathering = next(action for action in actions.values() if action["kind"] == "gather")
        self.assertEqual((gathering["unit"], gathering["target"]), (4, 31))
        model, _ = request_for(observed, actions)
        self.assertIn("economy", model)
        observed["minerals"] = 49
        self.assertFalse(any(action["kind"] == "train" for action in candidates(observed).values()))

    def test_supply_construction_and_existing_orders_gate_redundant_economy(self):
        observed = state()
        observed["supply"] = {"used": 10, "available": 10}
        observed["units"].append(unit(31, 176, owner=11, x=250, y=250, hp=100000, name="Mineral Field"))
        observed["units"][3]["order_id"] = 87
        actions = candidates(observed)
        self.assertFalse(any(action["kind"] == "train" for action in actions.values()))
        self.assertFalse(any(action["kind"] == "gather" for action in actions.values()))
        self.assertTrue(any(action["kind"] == "build" for action in actions.values()))
        observed["units"].append(unit(40, 109, completed=False, name="Supply Depot"))
        self.assertFalse(any(action.get("building") == 109 for action in candidates(observed).values()))

    def test_exploration_memory_distinguishes_ordered_destination_from_observed_position(self):
        observed = state()
        history = [{"frame": 200, "kind": "explore", "point": {"x": 800, "y": 400}, "squad": "Alpha",
                    "command": "Alpha: scout east", "accepted": True, "units": [1, 2],
                    "squad_centers": [{"name": "Alpha", "x": 380, "y": 400}]}]
        model, _ = request_for(observed, candidates(observed), history)
        self.assertEqual(model["previously_ordered_exploration_destinations"][0]["point"], {"x": 800, "y": 400})
        self.assertEqual(model["previously_observed_friendly_positions"], [{"x": 380, "y": 400, "observed_frame": 200}])
        self.assertGreater(model["squads"][0]["last_advance_order"]["distance_from_current_center"], 300)
        self.assertIn("not arrival", model["squads"][0]["last_advance_order"]["note"])
        self.assertEqual(model["recent_model_orders"][0]["units"], [1, 2])

    def test_advance_memory_follows_members_when_reinforcement_changes_squad_names(self):
        observed = state()
        observed["units"][0].update(x=2000, y=500)
        observed["units"][1].update(x=2024, y=500)
        observed["units"].append(unit(0))  # New lower ID becomes the current Alpha.
        history = [{"kind": "explore", "squad": "Alpha", "units": [1, 2],
                    "point": {"x": 2400, "y": 500}, "accepted": True}]
        model, _ = request_for(observed, candidates(observed), history)
        squads = {squad["name"]: squad for squad in model["squads"]}
        self.assertNotIn("last_advance_order", squads["Alpha"])
        self.assertEqual(squads["Bravo"]["last_advance_order"]["matched_member_ids"], [1, 2])
        self.assertEqual(squads["Bravo"]["last_advance_order"]["destination"], {"x": 2400, "y": 500})

    def test_legacy_and_rejected_advances_do_not_apply_to_current_squads(self):
        observed = state()
        history = [{"kind": "explore", "squad": "Alpha", "point": {"x": 800, "y": 400}, "accepted": True},
                   {"kind": "explore", "squad": "Alpha", "units": [1, 2], "point": {"x": 900, "y": 400}, "accepted": False}]
        model, _ = request_for(observed, candidates(observed), history)
        self.assertNotIn("last_advance_order", model["squads"][0])
        self.assertIn("not applied", model["recent_model_orders"][0]["identity_note"])

    def test_partial_advance_match_is_explicit_and_nested_history_preserves_ids(self):
        observed = state()
        history = [{"accepted": True, "action": {"kind": "attack_move", "squad": "Bravo", "units": [2, 9],
                                                   "point": {"x": 800, "y": 400}}}]
        model, _ = request_for(observed, candidates(observed), history)
        self.assertEqual(model["recent_model_orders"][0]["units"], [2, 9])
        self.assertEqual(model["squads"][0]["last_advance_order"]["matched_member_ids"], [2])
        self.assertIn("only to matched members", model["squads"][0]["last_advance_order"]["note"])

    def test_recycled_pool_id_does_not_inherit_a_dead_units_advance(self):
        observed = state()
        observed["units"][0]["generation"] = 3  # New Marine in former pool slot.
        observed["units"][1]["generation"] = 4
        history = [{"kind": "explore", "squad": "Alpha", "units": [1, 2],
                    "unit_generations": {"1": 2, "2": 4}, "point": {"x": 2400, "y": 1500}, "accepted": True}]
        model, _ = request_for(observed, candidates(observed), history)
        squad = model["squads"][0]
        self.assertEqual({member["id"]: member["generation"] for member in squad["members"]}, {1: 3, 2: 4})
        self.assertEqual(squad["last_advance_order"]["matched_member_ids"], [2])
        self.assertEqual(squad["last_advance_order"]["matched_member_generations"], {"2": 4})
        self.assertEqual(model["recent_model_orders"][0]["unit_generations"], {"1": 2, "2": 4})
        history[0]["units"] = [1]
        model, _ = request_for(observed, candidates(observed), history)
        self.assertNotIn("last_advance_order", model["squads"][0])

    def test_generation_known_units_reject_legacy_or_invalid_generation_history(self):
        observed = state()
        observed["units"][0]["generation"] = 0
        observed["units"][1]["generation"] = 1
        history = [{"kind": "explore", "units": [1, 2], "point": {"x": 800, "y": 400}, "accepted": True}]
        for generations in (None, {"1": 32, "2": False}, {"1": -1, "2": "1"}):
            with self.subTest(generations=generations):
                history[0]["unit_generations"] = generations
                model, _ = request_for(observed, candidates(observed), history)
                self.assertNotIn("last_advance_order", model["squads"][0])
                self.assertNotIn("unit_generations", model["recent_model_orders"][0])

    def test_legacy_generation_unknown_identity_matching_is_explicit(self):
        observed = state()
        history = [{"kind": "explore", "units": [1, 2], "point": {"x": 800, "y": 400}, "accepted": True}]
        model, _ = request_for(observed, candidates(observed), history)
        self.assertEqual(model["squads"][0]["last_advance_order"]["matched_member_ids"], [1, 2])
        self.assertIn("Legacy ID-only match", model["squads"][0]["last_advance_order"]["note"])
        self.assertIn("lacks generations", model["recent_model_orders"][0]["identity_note"])

    def test_idle_progress_summary_and_history_do_not_anchor_repeated_noops(self):
        observed = state()
        history = [{"kind": "continue", "command": "Continue current orders", "accepted": True, "frame": index} for index in range(10)]
        model, question = request_for(observed, candidates(observed), history)
        self.assertEqual(model["recent_model_orders"], [])
        self.assertEqual(model["current_activity"]["combat_units_moving_or_attacking"], 0)
        self.assertEqual(model["current_activity"]["mineral_workers"], 0)
        self.assertEqual(model["current_activity"]["queued_production"], 0)
        self.assertIn("Idle/guard units remain idle", question["action"]["criteria"]["Continue current orders"])
        self.assertEqual(model["squads"][0]["current_order_counts"], {"standing guard": 2})

    def test_equivalent_economy_choices_use_one_observed_representative_actor(self):
        observed = state()
        observed["units"].extend([unit(30, 111, x=700, y=320, name="Barracks"),
                                   unit(31, 176, owner=11, x=250, y=250, hp=100000, name="Mineral Field"),
                                   unit(32, 111, x=800, y=320, name="Barracks"),
                                   unit(50, 7, x=260, y=260, name="SCV")])
        actions = candidates(observed)
        gathering = [action for action in actions.values() if action["kind"] == "gather"]
        self.assertEqual(len(gathering), 1)
        self.assertEqual(gathering[0]["unit"], 50)
        self.assertEqual(len([action for action in actions.values() if action["kind"] == "train" and action["train_type"] == 0]), 1)

    def test_mining_choice_names_current_actor_instead_of_aliasing_previous_worker(self):
        observed = state()
        observed["units"][3]["order_id"] = 87
        observed["units"].extend([unit(31, 176, owner=11, x=250, y=250, hp=100000, name="Mineral Field"),
                                   unit(50, 7, x=260, y=260, name="SCV")])
        actions = candidates(observed)
        assignment = actions["Assign an idle SCV to minerals"]
        self.assertEqual(assignment["unit"], 50)
        self.assertIn("currently idle SCV 50", assignment["label"])
        self.assertIn("existing mineral workers [4]", assignment["label"])
        model, questions = request_for(observed, actions)
        self.assertEqual(model["next_mineral_assignment"], {
            "actor_id": 50, "actor_current_job": "idle/other", "actor_is_already_a_miner": False,
            "currently_assigned_mineral_workers": 1, "military_orders_unchanged": True,
        })
        self.assertEqual(questions["action"]["criteria"]["Assign an idle SCV to minerals"], assignment["label"])
        observed["units"][-1]["order_id"] = 84
        self.assertIn("Reassign a gas SCV to minerals", candidates(observed))

    def test_only_idle_or_gas_workers_are_offered_mineral_reassignment(self):
        observed = state()
        observed["units"].append(unit(31, 176, owner=11, x=250, y=250, hp=100000, name="Mineral Field"))
        for order in (151, 6, 49, 87, 0):
            with self.subTest(order=order):
                observed["units"][3]["order_id"] = order
                self.assertFalse(any(action["kind"] == "gather" for action in candidates(observed).values()))
        for order in (1, 2, 3, 84):
            with self.subTest(order=order):
                observed["units"][3]["order_id"] = order
                self.assertTrue(any(action["kind"] == "gather" for action in candidates(observed).values()))

    def test_regroup_at_observed_base_is_available_without_visible_enemies(self):
        observed = state()
        observed["units"][0].update(x=1000, y=500)
        observed["units"][1].update(x=1024, y=500)
        actions = candidates(observed)
        regroup = actions["Alpha: regroup at friendly base"]
        self.assertEqual(regroup["kind"], "regroup")
        self.assertEqual(regroup["point"], {"x": 280, "y": 320})
        self.assertEqual(regroup["units"], [1, 2])
        self.assertFalse(any(action["kind"] == "attack_target" for action in actions.values()))
        observed["units"][2]["visible"] = False
        self.assertNotIn("Alpha: regroup at friendly base", candidates(observed))

    def test_regroup_at_base_is_omitted_within_192_pixels(self):
        observed = state()
        observed["units"] = [unit(1, x=471, y=320), unit(3, 106, x=280, y=320, name="Command Center")]
        self.assertNotIn("Alpha: regroup at friendly base", candidates(observed))
        observed["units"][0]["x"] = 472
        self.assertIn("Alpha: regroup at friendly base", candidates(observed))
        observed["units"][0]["x"] = 280
        self.assertNotIn("Alpha: regroup at friendly base", candidates(observed))

    def test_largest_squad_distinguishes_scattered_army_from_assembled_force(self):
        observed = state()
        observed["units"].extend(unit(i, x=1900 + i) for i in range(50, 54))
        model, _ = request_for(observed, candidates(observed))
        self.assertEqual(model["current_activity"]["living_combat_units"], 6)
        self.assertEqual(model["current_activity"]["largest_selectable_squad"], 4)
        self.assertIn("8–12 Marines together", model["mission_playbook"][2])
        self.assertIn("Immediate defense", model["mission_playbook"][2])

    def test_twelve_member_formed_force_keeps_all_actions_and_labels_base_return_as_withdrawal(self):
        observed = state()
        observed["units"] = [unit(i, x=1000 + (i % 4) * 20, y=800 + (i // 4) * 20) for i in range(12)]
        observed["units"].extend([unit(30, 106, x=280, y=320, name="Command Center"),
                                   unit(40, x=2400, y=800), unit(50, owner=0, x=1300, y=800)])
        actions = candidates(observed)
        model, questions, _ = graph_request_for(observed, actions)
        formation = model["squads"][0]["formation"]
        self.assertEqual(formation["member_count"], 12)
        self.assertEqual(formation["core_count"], 12)
        self.assertTrue(formation["has_coherent_8plus_core"])
        self.assertEqual(set(formation["core_member_ids"]), set(range(12)))
        self.assertEqual(model["current_activity"]["squads_with_coherent_8plus_core"], 1)
        self.assertEqual(model["current_activity"]["largest_coherent_core"], 12)
        base = actions["Alpha: regroup at friendly base"]
        self.assertEqual(base["units"], list(range(12)))
        self.assertEqual(base["point"], {"x": 280, "y": 320})
        self.assertIn("withdraw", base["label"])
        self.assertIn("already has a coherent core", base["label"])
        self.assertIn("12 units", base["label"])
        self.assertIn("Alpha: regroup with friendly force", actions)
        self.assertTrue({"attack_target", "attack_move", "retreat", "regroup", "explore"}.issubset(
            {action["kind"] for action in actions.values() if action.get("squad") == "Alpha"}))
        self.assertIn("already assembled", questions["intent"]["instructions"])
        self.assertIn("replacements", questions["intent"]["instructions"])

    def test_twelve_selectable_but_scattered_units_do_not_claim_completed_assembly(self):
        observed = state()
        # Every member is selectable with the center anchor, but no member has
        # eight neighbors within192px. Total/selection count alone is insufficient.
        observed["units"] = [unit(0, x=1000, y=800)] + [
            unit(i + 1, x=round(1000 + 350 * math.cos(i * 2 * math.pi / 11)),
                 y=round(800 + 350 * math.sin(i * 2 * math.pi / 11))) for i in range(11)]
        model, _ = request_for(observed, candidates(observed))
        self.assertEqual(model["current_activity"]["largest_selectable_squad"], 12)
        formation = model["squads"][0]["formation"]
        self.assertLess(formation["core_count"], 8)
        self.assertFalse(formation["has_coherent_8plus_core"])
        self.assertEqual(model["current_activity"]["squads_with_coherent_8plus_core"], 0)
        anchor = formation["core_anchor_position"]
        actual_core = {u["id"] for u in observed["units"] if math.hypot(u["x"] - anchor["x"], u["y"] - anchor["y"]) <= 192}
        self.assertEqual(set(formation["core_member_ids"]), actual_core)

    def test_follow_orders_are_described_without_claiming_measured_motion_or_arrival(self):
        observed = state()
        observed["units"][0].update(order_id=49, order_target={"x": 424, "y": 400})
        model, questions = request_for(observed, candidates(observed))
        self.assertEqual(model["squads"][0]["current_order_counts"], {"following a unit": 1, "standing guard": 1})
        self.assertEqual(model["current_activity"]["combat_units_moving_or_attacking"], 1)
        self.assertIn("not measured", model["current_activity"]["movement_count_note"])
        self.assertIn("not measured movement", questions["action"]["criteria"]["Continue current orders"])

    def test_advance_observation_distinguishes_current_orders_proximity_and_past_acceptance(self):
        observed = state()
        observed["units"][0].update(order_id=14, x=650)
        observed["units"][1].update(order_id=3, x=795)
        history = [{"kind": "explore", "units": [1, 2], "point": {"x": 800, "y": 400}, "accepted": True}]
        model, _ = request_for(observed, candidates(observed), history)
        current = model["squads"][0]["last_advance_order"]["current_observation"]
        self.assertEqual(current["matched_members_with_attack_move_order"], [1])
        self.assertEqual(current["matched_members_standing_guard"], [2])
        self.assertEqual(current["matched_members_within96px_of_destination"], [2])
        self.assertIn("not proof", current["interpretation"])

    def test_barracks_building_options_enforce_cost_count_and_construction_limits(self):
        observed = state()
        buildings = lambda: [action for action in candidates(observed).values() if action.get("building") == 111]
        options = buildings()
        self.assertTrue(options)
        self.assertLessEqual(len(options), 2)
        for option in options:
            self.assertEqual(option["mineral_cost"], 150)
            self.assertEqual(option["units"], [4])
            self.assertEqual(option["point"]["x"] % 32, 0)
            self.assertEqual(option["point"]["y"] % 32, 16)
        request_for(observed, candidates(observed))
        observed["minerals"] = 149
        self.assertFalse(buildings())
        observed["minerals"] = 1000
        observed["units"].append(unit(30, 111, x=2000, y=1500, completed=False, name="Barracks"))
        self.assertFalse(buildings())
        observed["units"][-1]["completed"] = True
        self.assertTrue(buildings())
        observed["units"].extend([unit(31, 111, x=2200, y=1500, name="Barracks"), unit(32, 111, x=2400, y=1500, name="Barracks")])
        self.assertFalse(buildings())
        observed["units"] = [u for u in observed["units"] if u["type_id"] != 111]
        observed["units"][3]["completed"] = False
        self.assertFalse(buildings())

    def test_structure_memory_offers_uncertain_ground_advance_not_hidden_focus_fire(self):
        observed = state()
        sighting = {"id": 70, "type": "Barracks", "type_id": 111, "x": 1200, "y": 900, "last_seen_frame": 100}
        history = [{"observed_enemy_structures": [sighting]}]
        actions = candidates(observed, history=history)
        advance = next(action for action in actions.values() if action.get("objective") == "last-seen enemy structure")
        self.assertEqual(advance["kind"], "attack_move")
        self.assertNotIn("target", advance)
        self.assertEqual(advance["point"], {"x": 1200, "y": 900})
        self.assertIn("not currently visible", advance["label"])
        self.assertFalse(any(action["kind"] == "attack_target" for action in actions.values()))
        model, _ = request_for(observed, actions, history)
        memory = model["known_enemy_structures"][0]
        self.assertFalse(memory["currently_visible"])
        self.assertEqual(memory["last_seen_frame"], 100)
        # An injected hidden position cannot update a legitimate last sighting.
        observed["units"].append(unit(70, 111, owner=0, x=2789, y=1837, visible=False, name="Barracks"))
        self.assertEqual(candidates(observed, history=history), actions)
        self.assertEqual(request_for(observed, actions, history)[0], model)

    def test_current_structure_sightings_refresh_memory_and_close_targets_need_no_advance(self):
        observed = state()
        history = [{"observed_enemy_structures": [{"id": 70, "type": "Barracks", "type_id": 111, "x": 1200, "y": 900, "last_seen_frame": 100}]}]
        observed["units"].append(unit(70, 111, owner=0, x=430, y=420, name="Barracks"))
        actions = candidates(observed, history=history)
        self.assertFalse(any(action.get("objective") == "last-seen enemy structure" for action in actions.values()))
        self.assertTrue(any(action.get("target") == 70 for action in actions.values()))
        model, _ = request_for(observed, actions, history)
        self.assertEqual(len(model["known_enemy_structures"]), 1)
        memory = model["known_enemy_structures"][0]
        self.assertEqual((memory["x"], memory["y"], memory["last_seen_frame"]), (430, 420, 300))
        self.assertTrue(memory["currently_visible"])

    def test_structure_memory_is_bounded_and_rejects_invalid_or_future_sightings(self):
        observed = state()
        sightings = [{"id": 70 + i, "type": "Barracks", "type_id": 111, "x": 1200 + i, "y": 900, "last_seen_frame": i} for i in range(40)]
        invalid = [{**sightings[0], "id": 500, "last_seen_frame": 301},
                   {**sightings[0], "id": 501, "x": 4000},
                   {**sightings[0], "id": 502, "type_id": 0},
                   {**sightings[0], "id": 503, "visible": False},
                   {**sightings[0], "id": 504, "type_id": 174}]
        # Sightings persist beyond the short action-history window.
        history = [{"observed_enemy_structures": sightings + invalid}] + [{"kind": "continue"} for _ in range(70)]
        actions = candidates(observed, history=history)
        memories = request_for(observed, actions, history)[0]["known_enemy_structures"]
        self.assertEqual(len(memories), 32)
        self.assertEqual([memory["last_seen_frame"] for memory in memories], list(range(39, 7, -1)))
        self.assertEqual(len([action for action in actions.values() if action.get("objective") == "last-seen enemy structure"]), 4)

    def test_structure_memory_does_not_mark_a_reused_visible_marine_as_the_structure(self):
        observed = state()
        sighting = {"id": 70, "generation": 2, "type": "Barracks", "type_id": 111,
                    "x": 1200, "y": 900, "last_seen_frame": 100}
        history = [{"observed_enemy_structures": [sighting]}]
        observed["units"].append({**unit(70, owner=0, x=560, y=420), "generation": 3})
        model, _ = request_for(observed, candidates(observed, history=history), history)
        self.assertEqual(len(model["known_enemy_structures"]), 1)
        memory = model["known_enemy_structures"][0]
        self.assertEqual((memory["id"], memory["generation"], memory["x"], memory["y"]), (70, 2, 1200, 900))
        self.assertFalse(memory["currently_visible"])
        self.assertIn("unconfirmed", memory["uncertainty"])

    def test_structure_incarnations_have_separate_visibility_and_keep_stale_positions(self):
        observed = state()
        history = [{"observed_enemy_structures": [{"id": 70, "generation": 2, "type": "Barracks", "type_id": 111,
                                                  "x": 1200, "y": 900, "last_seen_frame": 100}]}]
        observed["units"].append({**unit(70, 109, owner=0, x=1800, y=1000, name="Supply Depot"), "generation": 3})
        model, _ = request_for(observed, candidates(observed, history=history), history)
        memories = {memory["generation"]: memory for memory in model["known_enemy_structures"]}
        self.assertEqual(set(memories), {2, 3})
        self.assertFalse(memories[2]["currently_visible"])
        self.assertTrue(memories[3]["currently_visible"])
        self.assertEqual(memories[2]["x"], 1200)
        self.assertEqual(memories[3]["x"], 1800)

    def test_generation_known_structure_does_not_promote_legacy_sighting_to_visible(self):
        observed = state()
        history = [{"observed_enemy_structures": [{"id": 70, "type": "Barracks", "type_id": 111,
                                                  "x": 1200, "y": 900, "last_seen_frame": 100}]}]
        observed["units"].append({**unit(70, 111, owner=0, x=1800, y=1000, name="Barracks"), "generation": 3})
        model, _ = request_for(observed, candidates(observed, history=history), history)
        old = next(memory for memory in model["known_enemy_structures"] if "generation" not in memory)
        self.assertFalse(old["currently_visible"])
        self.assertEqual(old["last_seen_frame"], 100)


class GraphTests(unittest.TestCase):
    def setUp(self):
        self.observed = state()
        self.observed["units"].extend([unit(20, owner=0, x=560, y=420),
                                       unit(30, 111, x=700, y=320, name="Barracks"),
                                       unit(31, 176, owner=11, x=250, y=250, hp=100000, name="Mineral Field"),
                                       unit(5, x=1100, y=500)])
        self.actions = candidates(self.observed)
        self.model, self.questions, self.routing = graph_request_for(self.observed, self.actions)

    def response(self, intent):
        answers = {}
        for question_id, question in self.questions.items():
            ids = list(question["criteria"])
            answers[question_id] = {"type": "choice", "choice": intent if question_id == "intent" else ids[-1],
                                    "probabilities": {key: 1 / len(ids) for key in ids}, "confidence": 0}
        return {"answers": answers}

    def test_graph_preserves_flat_validated_state_and_partitions_every_actual_command_once(self):
        self.assertEqual(self.model, request_for(self.observed, self.actions)[0])
        self.assertEqual(set(self.routing["branches"]), {"Economy", "Engage", "Explore", "Reposition", "Continue"})
        self.assertEqual(self.routing, graph_routing(self.actions))
        ids = [key for branch in self.routing["branches"].values() for key in branch["candidate_ids"]]
        self.assertEqual(set(ids), set(self.actions))
        self.assertEqual(len(ids), len(self.actions))
        self.assertEqual(set(self.questions["intent"]["criteria"]), set(self.routing["branches"]))
        for category, branch in self.routing["branches"].items():
            if branch["question"] is not None:
                question = self.questions[branch["question"]]
                self.assertEqual(set(question["criteria"]), set(branch["candidate_ids"]))
                self.assertIn("independent recommendation", question["instructions"])
                for key, description in question["criteria"].items():
                    self.assertEqual(description, self.actions[key]["label"])
        self.assertIn("No joint probability", self.routing["semantics"])

    def test_playbook_guidance_does_not_remove_competing_model_commands(self):
        self.assertEqual(len(self.model["mission_playbook"]), 4)
        self.assertIn("mission_playbook", self.questions["intent"]["instructions"])
        self.assertEqual(set(self.questions["intent"]["criteria"]), {"Economy", "Engage", "Explore", "Reposition", "Continue"})
        for branch in self.routing["branches"].values():
            if branch["question"] is not None:
                self.assertIn("mission_playbook", self.questions[branch["question"]]["instructions"])
        # A model-selected alternative still routes exactly; guidance is no override.
        response = self.response("Explore")
        selected, _ = resolve_graph_choice(response, self.routing)
        self.assertEqual(selected, response["answers"]["action_explore"]["choice"])

    def test_empty_categories_are_absent_and_singletons_have_no_child_question(self):
        observed = state()
        observed["minerals"] = 50
        _, questions, routing = graph_request_for(observed, candidates(observed))
        self.assertNotIn("Engage", routing["branches"])
        self.assertNotIn("Reposition", routing["branches"])
        self.assertEqual(routing["branches"]["Economy"], {"question": None, "candidate_ids": ["Train SCV"]})
        self.assertNotIn("action_economy", questions)
        self.assertNotIn("action_continue", questions)
        self.assertIn("Idle/guard units remain idle", questions["intent"]["criteria"]["Continue"])

    def test_route_uses_selected_intent_then_exact_child_and_preserves_probabilities(self):
        response = self.response("Economy")
        before = copy.deepcopy(response)
        selected, child = resolve_graph_choice(response, self.routing)
        self.assertEqual(child, "action_economy")
        self.assertEqual(selected, response["answers"]["action_economy"]["choice"])
        self.assertIn(selected, self.routing["branches"]["Economy"]["candidate_ids"])
        self.assertEqual(response, before)

    def test_singleton_route_uses_only_model_intent_without_synthetic_probability(self):
        response = self.response("Continue")
        before = copy.deepcopy(response)
        self.assertEqual(resolve_graph_choice(response, self.routing), ("Continue current orders", None))
        self.assertEqual(response, before)

    def test_wrong_root_child_or_answer_option_sets_fail_closed(self):
        invalid = []
        response = self.response("Economy")
        response["answers"]["intent"]["choice"] = "Win"
        invalid.append(response)
        response = self.response("Economy")
        response["answers"]["action_economy"]["choice"] = "Continue current orders"
        invalid.append(response)
        response = self.response("Economy")
        response["answers"]["action_economy"]["probabilities"]["Continue current orders"] = 0
        invalid.append(response)
        response = self.response("Economy")
        del response["answers"]["action_explore"]
        invalid.append(response)
        response = self.response("Economy")
        response["answers"]["intent"]["probabilities"].pop("Engage")
        invalid.append(response)
        for response in invalid:
            with self.subTest(response=response), self.assertRaises(CombatStateError):
                resolve_graph_choice(response, self.routing)

    def test_malformed_duplicate_and_singleton_routing_fail_closed(self):
        invalid = []
        routing = copy.deepcopy(self.routing)
        routing["version"] = True
        invalid.append(routing)
        routing = copy.deepcopy(self.routing)
        routing["branches"]["Continue"]["question"] = "action_continue"
        invalid.append(routing)
        routing = copy.deepcopy(self.routing)
        routing["branches"]["Economy"]["question"] = None
        invalid.append(routing)
        routing = copy.deepcopy(self.routing)
        routing["branches"]["Economy"]["candidate_ids"].append("Continue current orders")
        invalid.append(routing)
        routing = copy.deepcopy(self.routing)
        routing["branches"]["Economy"]["candidate_ids"] = []
        invalid.append(routing)
        for routing in invalid:
            with self.subTest(routing=routing), self.assertRaises(CombatStateError):
                resolve_graph_choice(self.response("Economy"), routing)

    def test_routing_rejects_unknown_action_kinds_and_missing_intent_choice(self):
        actions = copy.deepcopy(self.actions)
        actions["Injected"] = {"kind": "declare_victory"}
        with self.assertRaises(CombatStateError):
            graph_routing(actions)
        only_economy = {key: value for key, value in self.actions.items() if value["kind"] in {"gather", "train", "build"}}
        with self.assertRaises(CombatStateError):
            graph_routing(only_economy)

    def test_graph_keeps_hidden_enemy_locations_out_of_every_question_and_route(self):
        baseline = (self.model, self.questions, self.routing)
        self.observed["units"].append(unit(900, owner=0, x=2789, y=1837, visible=False))
        self.assertEqual(graph_request_for(self.observed, candidates(self.observed)), baseline)


if __name__ == "__main__":
    unittest.main()
