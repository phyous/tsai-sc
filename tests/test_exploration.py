"""Persistent visitation facts never infer fog, enemy absence, or routes."""
import copy
import json
import unittest

from tsai_sc.exploration import endpoint_fact, visitation_summary


def actor(uid=1, *, x=100, y=100, owner=6, generation=1, kind=0, **extra):
    return {"id": uid, "generation": generation, "owner": owner, "type_id": kind,
            "hp": 40, "x": x, "y": y, "completed": True, "visible": True, **extra}


def state(frame=100, units=()):
    return {"frame": frame, "player_id": 6, "map": {"width_tiles": 96, "height_tiles": 64},
            "units": list(units)}


def event(frame, *units, **extra):
    own = [{key: value for key, value in unit.items() if key not in {"owner", "completed", "visible"}} for unit in units]
    return {"combat_snapshot": {"frame": frame, "own_combat": own}, **extra}


class ExplorationTests(unittest.TestCase):
    def test_visits_persist_beyond64_events_and_squad_name_or_unit_generation_changes(self):
        history = [event(1, actor(x=270, y=520), squad="Old Alpha")]
        history += [event(frame, actor(generation=2, x=900, y=900), squad="New Alpha") for frame in range(2, 100)]
        summary = visitation_summary(state(), history)
        fact = endpoint_fact(summary, {"x": 300, "y": 600})
        self.assertEqual(fact, {"cell": [1, 2], "previously_occupied": True,
                              "last_prior_observed_frame": 1, "currently_occupied": False})
        self.assertNotIn("Alpha", json.dumps(summary))

    def test_latest_prior_frame_is_order_independent_and_current_arrival_is_separate(self):
        summary = visitation_summary(state(100, [actor(x=800, y=800)]),
                                     [event(50, actor()), event(90, actor()), event(20, actor()),
                                      event(100, actor(x=800, y=800))])
        self.assertEqual(endpoint_fact(summary, {"x": 100, "y": 100})["last_prior_observed_frame"], 90)
        first_arrival = endpoint_fact(summary, {"x": 800, "y": 800})
        self.assertFalse(first_arrival["previously_occupied"])
        self.assertIsNone(first_arrival["last_prior_observed_frame"])
        self.assertTrue(first_arrival["currently_occupied"])

    def test_historical_and_current_visits_can_both_be_true_and_frame_zero_is_valid(self):
        summary = visitation_summary(state(1, [actor()]), [event(0, actor())])
        self.assertEqual(endpoint_fact(summary, {"x": 100, "y": 100}),
                         {"cell": [0, 0], "previously_occupied": True,
                          "last_prior_observed_frame": 0, "currently_occupied": True})

    def test_hostile_allied_worker_unfinished_and_command_coordinates_never_mark_cells(self):
        history = [event(10, actor(), point={"x": 3000, "y": 2000},
                         squad_centers=[{"x": 2500, "y": 1700}])]
        history[0]["combat_snapshot"]["visible_hostile_combat"] = [{"x": 3000, "y": 2000}]
        history[0]["combat_snapshot"]["own_combat"].append(actor(2, owner=0, x=2800, y=1900))
        current = state(100, [actor(3, owner=0, x=3000, y=2000), actor(4, owner=2, x=2800, y=1800),
                              actor(5, kind=7, x=2400, y=1600), actor(6, x=2200, y=1400, completed=False)])
        summary = visitation_summary(current, history)
        self.assertEqual(sum(value is not None for row in summary["prior_last_seen"] for value in row), 1)
        self.assertFalse(any("X" in row for row in summary["current_presence"]))
        self.assertNotIn("3000", json.dumps(summary))

    def test_cell_boundaries_and_map_edges_are_exact(self):
        summary = visitation_summary(state(10, [actor(x=3071, y=2047)]),
                                     [event(1, actor(x=255, y=255)), event(2, actor(x=256, y=256))])
        self.assertEqual(endpoint_fact(summary, {"x": 255, "y": 255})["cell"], [0, 0])
        self.assertEqual(endpoint_fact(summary, {"x": 256, "y": 256})["cell"], [1, 1])
        self.assertTrue(endpoint_fact(summary, {"x": 3071, "y": 2047})["currently_occupied"])
        for point in ({"x": 3072, "y": 0}, {"x": -1, "y": 0}, {"x": 0, "y": 2048},
                      {"x": True, "y": 0}, {"x": 1.0, "y": 0}):
            with self.assertRaises(ValueError):
                endpoint_fact(summary, point)

    def test_future_malformed_out_of_map_and_duplicate_rows_do_not_invent_visits(self):
        history = [None, {}, {"combat_snapshot": []}, event(101, actor()), event(True, actor())]
        for change in ({"x": -1}, {"x": 3072}, {"y": 2048}, {"x": True}, {"y": 1.0},
                       {"generation": 32}, {"hp": 0}, {"id": 1700}, {"type_id": False}):
            invalid = actor(); invalid.update(change); history.append(event(1, invalid))
        history.append(event(2, actor(x=800), actor(x=900)))
        summary = visitation_summary(state(), history)
        self.assertTrue(all(value is None for row in summary["prior_last_seen"] for value in row))

    def test_no_path_interpolation_between_samples(self):
        summary = visitation_summary(state(), [event(1, actor(x=10)), event(2, actor(x=1000))])
        self.assertFalse(endpoint_fact(summary, {"x": 500, "y": 100})["previously_occupied"])
        for phrase in ("not fog coverage", "cleared territory", "passability", "may have been visible", "No path"):
            self.assertIn(phrase, summary["interpretation"])

    def test_input_budgets_and_integer_metadata_are_enforced(self):
        with self.assertRaises(ValueError):
            visitation_summary(state(), [{}] * 2001)
        self.assertIsInstance(visitation_summary(state(), [{}] * 2000), dict)
        for key, value in (("frame", True), ("player_id", 6.0), ("frame", -1)):
            invalid = state(); invalid[key] = value
            with self.assertRaises(ValueError):
                visitation_summary(invalid)
        oversized = state(); oversized["map"]["width_tiles"] = 97
        with self.assertRaises(ValueError):
            visitation_summary(oversized)
        summary = visitation_summary(state(units=[actor()] * 1701))
        self.assertFalse(any("X" in row for row in summary["current_presence"]))

    def test_output_is_small_and_inputs_are_not_mutated(self):
        units = [actor(y * 12 + x, x=x * 256, y=y * 256) for y in range(8) for x in range(12)]
        current, history = state(999999, units), [event(999998, *units)]
        original = copy.deepcopy((current, history))
        summary = visitation_summary(current, history)
        self.assertEqual((current, history), original)
        self.assertLess(len(json.dumps(summary, separators=(",", ":"))), 1600)
        summary["prior_last_seen"][0][0] = 123
        self.assertEqual((current, history), original)
        bad = visitation_summary(current, history); bad["current_presence"][0] = "?" * 12
        with self.assertRaises(ValueError):
            endpoint_fact(bad, {"x": 0, "y": 0})


if __name__ == "__main__":
    unittest.main()
