"""Observed loss memory with visibility, identity, and bounded prompt output."""
import copy
import json
import unittest

from tsai_sc.battle import history_snapshot, recent_battle_outcomes


COMBAT = (0, 1, 32)


def actor(uid, *, owner=6, kind=0, generation=1, x=800, y=800, hp=40,
          visible=True, completed=True):
    return {"id": uid, "generation": generation, "type_id": kind,
            "type": "TEST actor", "owner": owner, "x": x, "y": y, "hp": hp,
            "visible": visible, "completed": completed}


def state(frame, own, enemies=()):
    return {"frame": frame, "player_id": 6, "enemy_players": [0, 3],
            "units": list(own) + list(enemies)}


def history(*states):
    return [{"combat_snapshot": history_snapshot(snapshot, COMBAT)} for snapshot in states]


class BattleMemoryTests(unittest.TestCase):
    def test_snapshot_strips_orders_and_filters_hidden_allied_neutral_unfinished_units(self):
        own = actor(1, visible=False)
        enemy = actor(10, owner=0, kind=32, hp=50, x=900)
        enemy.update(order_target={"x": 7891, "y": 7654}, address=0x123456)
        hidden = actor(11, owner=0, x=7777, y=6666, visible=False)
        observed = state(10, [own, actor(2, completed=False)],
                         [enemy, hidden, actor(12, owner=2), actor(13, owner=11),
                          actor(14, owner=0, kind=112, hp=600), actor(15, owner=0, hp=0)])
        snapshot = history_snapshot(observed, COMBAT)
        self.assertEqual([unit["id"] for unit in snapshot["own_combat"]], [1])
        self.assertEqual([unit["id"] for unit in snapshot["visible_hostile_combat"]], [10])
        self.assertEqual(set(snapshot["visible_hostile_combat"][0]),
                         {"id", "generation", "type_id", "type", "hp", "x", "y"})
        for secret in ("7891", "7654", "7777", "6666", "order_target", "address"):
            self.assertNotIn(secret, json.dumps(snapshot))

    def test_enemy_ground_attackers_are_not_limited_to_own_terran_types(self):
        enemies = [actor(10, owner=0, kind=32, hp=50),
                   actor(11, owner=3, kind=37, hp=35),
                   actor(12, owner=0, kind=7, hp=60)]
        initial = state(10, [actor(1)], enemies)
        self.assertEqual({unit["type_id"] for unit in history_snapshot(initial, (0,))["visible_hostile_combat"]},
                         {7, 32, 37})
        result = recent_battle_outcomes(state(20, []), history(initial), (0,))[0]
        self.assertEqual(result["previously_visible_armed_composition"], {"Firebat": 1, "Zergling": 1})
        self.assertEqual(result["previously_visible_attack_capable_workers"], {"SCV": 1})
        self.assertEqual((result["observed_hostile_hp"], result["observed_worker_hp"]), (85, 60))

    def test_twelve_to_ten_to_one_to_zero_coalesces_without_inferring_cause(self):
        # Synthetic TEST geometry; loss sequence mirrors the observed assault,
        # without retaining map coordinates or a mission-specific route.
        force = [actor(i, x=800 + i * 5) for i in range(12)]
        defenders = [actor(100 + i, owner=0, x=950 + i * 5) for i in range(8)]
        defenders += [actor(110 + i, owner=0, kind=32, hp=50, x=930, y=850 + i * 10) for i in range(3)]
        observed = [state(1000, force, defenders), state(1067, force[2:], defenders),
                    state(1138, force[-1:], defenders), state(1244, [])]
        result = recent_battle_outcomes(observed[-1], history(*observed[:-1]), COMBAT)
        self.assertEqual(len(result), 1)
        encounter = result[0]
        self.assertEqual(encounter["own_units_no_longer_observed"], 12)
        self.assertEqual(encounter["own_types_no_longer_observed"], {"Marine": 12})
        self.assertEqual(encounter["previously_visible_armed_composition"], {"Firebat": 3, "Marine": 8})
        self.assertEqual(encounter["observed_hostile_hp"], 470)
        self.assertEqual((encounter["first_observed_frame"], encounter["last_observed_frame"]), (1000, 1244))
        self.assertIn("does not establish death or its cause", encounter["observation_note"])
        self.assertIn("do not establish current enemy positions", encounter["current_enemy_uncertainty"])
        self.assertNotIn("hostiles", encounter)

    def test_reused_pool_id_is_a_different_generation(self):
        old = state(10, [actor(1, generation=31)], [actor(20, owner=0)])
        current = state(20, [actor(1, generation=0)])
        result = recent_battle_outcomes(current, history(old), COMBAT)
        self.assertEqual(result[0]["own_units_no_longer_observed"], 1)

    def test_reobserved_identity_is_not_retained_as_a_current_loss(self):
        old = state(10, [actor(1)], [actor(20, owner=0)])
        missing = state(20, [])
        reappeared = state(30, [actor(1)])
        self.assertEqual(recent_battle_outcomes(reappeared, history(old, missing), COMBAT), [])

    def test_no_loss_inferred_from_stale_hidden_future_or_distant_enemy_locations(self):
        for enemies in ([], [actor(20, owner=0, visible=False)], [actor(20, owner=0, x=3000)]):
            with self.subTest(enemies=enemies):
                initial = state(10, [actor(1)], enemies)
                now = state(20, [], [actor(21, owner=0)])
                self.assertEqual(recent_battle_outcomes(now, history(initial), COMBAT), [])
        initial = state(10, [actor(1)], [actor(20, owner=0)])
        quiet = state(20, [actor(1)])
        self.assertEqual(recent_battle_outcomes(state(30, []), history(initial, quiet), COMBAT), [])

    def test_enemy_strength_comes_from_one_snapshot_not_a_union(self):
        first = state(10, [actor(1), actor(2)], [actor(20, owner=0), actor(21, owner=0)])
        second = state(20, [actor(2)], [actor(30 + i, owner=0, kind=32, hp=50) for i in range(3)])
        result = recent_battle_outcomes(state(30, []), history(first, second), COMBAT)[0]
        self.assertEqual(result["own_units_no_longer_observed"], 2)
        self.assertEqual(result["previously_visible_armed_composition"], {"Firebat": 3})
        self.assertEqual(result["observed_hostile_hp"], 150)
        self.assertEqual(result["armed_force_observed_frame"], 20)

    def test_spatially_separate_losses_are_not_one_encounter(self):
        before = state(10, [actor(1), actor(2, x=4000)],
                       [actor(20, owner=0, x=900), actor(21, owner=0, x=4100)])
        result = recent_battle_outcomes(state(20, []), history(before), COMBAT)
        self.assertEqual(len(result), 2)
        self.assertEqual([event["own_units_no_longer_observed"] for event in result], [1, 1])
        self.assertEqual({event["observed_location"]["x"] for event in result}, {800, 4000})

    def test_output_is_limited_to_four_recent_encounters(self):
        observations = []
        for index in range(6):
            observations.extend([state(index * 1000, [actor(index)], [actor(20, owner=0)]),
                                 state(index * 1000 + 20, [])])
        result = recent_battle_outcomes(observations[-1], history(*observations[:-1]), COMBAT)
        self.assertEqual([event["first_observed_frame"] for event in result], [2000, 3000, 4000, 5000])
        self.assertTrue(all(event["own_units_no_longer_observed"] == 1 for event in result))

    def test_malformed_and_future_snapshots_do_not_manufacture_losses(self):
        initial = state(10, [actor(1)], [actor(20, owner=0)])
        events = history(initial)
        malformed = copy.deepcopy(events[0])
        malformed["combat_snapshot"]["frame"] = 15
        malformed["combat_snapshot"]["own_combat"][0]["generation"] = "1"
        events.extend([malformed, {"combat_snapshot": {"frame": 17}}, None,
                       {"combat_snapshot": history_snapshot(state(100, []), COMBAT)}])
        self.assertEqual(recent_battle_outcomes(state(20, [actor(1)]), events, COMBAT), [])

    def test_missing_generation_is_not_guessed(self):
        unit = actor(1)
        del unit["generation"]
        initial = state(10, [unit], [actor(20, owner=0)])
        self.assertEqual(history_snapshot(initial, COMBAT)["own_combat"], [])
        self.assertEqual(recent_battle_outcomes(state(20, []), history(initial), COMBAT), [])

    def test_history_and_state_are_not_mutated(self):
        initial = state(10, [actor(1)], [actor(20, owner=0)])
        events, current = history(initial), state(20, [])
        frozen = copy.deepcopy((events, current))
        result = recent_battle_outcomes(current, events, COMBAT)
        self.assertEqual((events, current), frozen)
        result[0]["observed_location"]["x"] = 123
        self.assertEqual((events, current), frozen)


if __name__ == "__main__":
    unittest.main()
