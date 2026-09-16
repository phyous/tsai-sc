"""Behavior checks for legal command menus and the original input path."""
import copy
import itertools
import struct
import unittest
from unittest.mock import patch

from tsai_sc.controller import InputAdapter, building_sites, candidates, depot_sites, request_for, supply, verify_command
from tsai_sc.game import CAMERA, UNIT_BASE, UNIT_SIZE, UNIT_NAMES


def unit(index, kind, x, y, *, owner=6, completed=True, visible=True, order=3, queue=()):
    return {
        'id': index, 'type_id': kind, 'owner': owner, 'x': x, 'y': y,
        'address': UNIT_BASE + index * UNIT_SIZE,
        'completed': completed, 'visible': visible, 'order_id': order,
        'build_queue': list(queue), 'type': UNIT_NAMES.get(kind, f'Unit {kind}'),
        'hp': 60, 'remaining_build_time': 0,
    }


def mission():
    return {
        'player_id': 6, 'minerals': 150, 'gas': 0,
        'frame': 329,
        'objective_progress': {'supply_depots': {'current': 1, 'target': 3},
                               'refineries': {'current': 0, 'target': 1},
                               'gas': {'current': 0, 'target': 100}},
        'camera': {'x': 0, 'y': 384},
        'map': {'width_tiles': 64, 'height_tiles': 64},
        'units': [
            unit(1, 7, 733, 500), unit(2, 106, 576, 528),
            unit(3, 109, 912, 480), unit(4, 176, 464, 352, owner=11),
            unit(5, 188, 320, 608, owner=11),
        ] + [unit(20 + i, 0, 900 + i * 10, 1200) for i in range(16)],
    }


class Bridge:
    def __init__(self, camera=(0, 384)):
        self.camera_position = camera
        self.calls = []
        self.running = False
        self.fail_on = None
        self.reject_on = None
        self.input_running = []
        self.pan_to = None

    def rpc(self, command, *args):
        self.calls.append((command, *args))
        self.input_running.append((command, self.running))
        if command == self.fail_on:
            raise RuntimeError('input failed')
        if command == self.reject_on:
            return {'ok': False}
        if command == 'clickHold' and args[1] >= 348 and self.pan_to is not None:
            self.camera_position = self.pan_to

    def read_memory(self, address, length):
        if (address, length) != (CAMERA, 8):
            raise AssertionError('Adapter read outside camera coordinates')
        return struct.pack('<II', *self.camera_position)

    def resume(self):
        self.running = True
        self.calls.append(('resume',))

    def pause(self):
        self.running = False
        self.calls.append(('pause',))


class ControllerTests(unittest.TestCase):
    def test_supply_accounts_for_queue_and_incomplete_depots(self):
        state = mission()
        self.assertEqual(supply(state), (17, 18))
        state['units'][1]['build_queue'] = [7]
        state['units'].append(unit(6, 109, 700, 700, completed=False))
        self.assertEqual(supply(state), (18, 18))
        state['units'][-1]['completed'] = True
        self.assertEqual(supply(state), (18, 26))

    def test_authoritative_supply_overrides_unit_count_estimate(self):
        state = mission()
        state['supply'] = {'used': 18, 'available': 18}
        self.assertEqual(supply(state), (18, 18))
        self.assertNotIn('train_scv', candidates(state))

    def test_costs_and_existing_orders_gate_commands(self):
        state = mission()
        initial = candidates(state)
        self.assertIn('mine_1', initial)
        self.assertIn('train_scv', initial)
        self.assertIn('build_refinery', initial)
        self.assertTrue(any(key.startswith('depot_') for key in initial))
        state['minerals'] = 0
        state['units'][0]['order_id'] = 87
        self.assertEqual(list(candidates(state)), ['wait'])

    def test_busy_builder_is_not_retasked(self):
        for order in (30, 33, 34, 35):
            with self.subTest(order=order):
                state = mission()
                state['units'][0]['order_id'] = order
                offered = candidates(state)
                self.assertFalse(any(action.get('unit') == 1 for action in offered.values()))

    def test_incomplete_and_hidden_workers_are_not_click_targets(self):
        # Original demo gas harvesting hides the SCV sprite while order83 runs.
        for attrs in ({'completed': False}, {'visible': False, 'order_id': 83}):
            with self.subTest(attrs=attrs):
                state = mission()
                state['units'][0].update(attrs)
                offered = candidates(state)
                self.assertFalse(any(action.get('unit') == 1 for action in offered.values()))

    def test_gas_requires_finished_refinery(self):
        state = mission()
        refinery = unit(6, 110, 320, 608, completed=False)
        state['units'].append(refinery)
        self.assertNotIn('gas_1', candidates(state))
        self.assertNotIn('build_refinery', candidates(state))
        refinery['completed'] = True
        self.assertEqual(candidates(state)['gas_1']['target'], 6)
        state['units'][0]['order_id'] = 83
        self.assertNotIn('gas_1', candidates(state))

    def test_worker_cap_includes_hidden_gas_workers(self):
        state = mission()
        state['supply'] = {'used': 24, 'available': 34}
        state['units'].extend(unit(40 + i, 7, 320, 608, visible=False, order=83) for i in range(7))
        self.assertNotIn('train_scv', candidates(state))

    def test_completed_and_incomplete_depots_count_toward_build_limit(self):
        state = mission()
        state['units'].extend([
            unit(6, 109, 752, 640),
            unit(7, 109, 880, 640, completed=False),
        ])
        self.assertFalse(any(key.startswith('depot_') for key in candidates(state)))

    def test_depot_centers_align_footprint_to_tiles_and_avoid_blockers(self):
        state = mission()
        points = depot_sites(state)
        self.assertTrue(points)
        for point in points:
            self.assertEqual((point['x'] - 48) % 32, 0)
            self.assertEqual((point['y'] - 32) % 32, 0)
        blocked = copy.deepcopy(state)
        blocked['units'].append(unit(6, 109, **points[0]))
        self.assertNotIn(points[0], depot_sites(blocked))

    def test_barracks_centers_align_four_by_three_tile_footprint(self):
        state = mission()
        points = building_sites(state, 111)
        self.assertTrue(points)
        for point in points:
            self.assertEqual((point['x'] - 64) % 32, 0)
            self.assertEqual((point['y'] - 48) % 32, 0)
            self.assertGreaterEqual(point['x'] - 64, 8)
            self.assertGreaterEqual(point['y'] - 48, 8)
            self.assertLessEqual(point['x'] + 64, state['map']['width_tiles'] * 32 - 8)
            self.assertLessEqual(point['y'] + 48, state['map']['height_tiles'] * 32 - 8)

    def test_building_sites_use_full_observed_building_footprints(self):
        state = mission()
        point = building_sites(state, 111)[0]
        for type_id in (111, 122):
            blocked = copy.deepcopy(state)
            # The centers differ, but their128px-wide footprints overlap.
            blocked['units'].append(unit(6, type_id, point['x'] + 120, point['y']))
            self.assertNotIn(point, building_sites(blocked, 111))

    def test_building_sites_use_own_base_and_ignore_hidden_enemy_locations(self):
        state = mission()
        original = building_sites(state, 111)
        state['units'].append(unit(6, 111, **original[0], owner=0, visible=False))
        self.assertEqual(building_sites(state, 111), original)
        state['units'] = [u for u in state['units'] if u['type_id'] != 106]
        self.assertEqual(building_sites(state, 111), [])
        with self.assertRaises(ValueError):
            building_sites(state, 110)

    def test_building_sites_search_north_and_are_bounded_near_own_base(self):
        state = mission()
        cc = state['units'][1]
        for type_id in (109, 111):
            points = building_sites(state, type_id)
            self.assertLessEqual(len(points), 12)
            self.assertEqual(len(points), len({(p['x'], p['y']) for p in points}))
            self.assertTrue(any(p['y'] < cc['y'] - 100 for p in points))
            self.assertTrue(any(p['y'] > cc['y'] + 100 for p in points))
            for p in points:
                self.assertLessEqual(abs(p['x'] - cc['x']), 367)
                self.assertLessEqual(abs(p['y'] - cc['y']), 367)

    def test_building_sites_fall_back_to_outer_ring_when_inner_ring_is_occupied(self):
        state = mission()
        state['units'] = [unit(2, 106, 800, 800)]
        inner = building_sites(state, 111)
        self.assertEqual(len(inner), 12)
        state['units'].extend(unit(100 + i, 111, **p) for i, p in enumerate(inner))
        outer = building_sites(state, 111)
        self.assertTrue(outer)
        self.assertFalse(any(p in inner for p in outer))
        self.assertTrue(all(max(abs(p['x'] - 800), abs(p['y'] - 800)) >= 192 for p in outer))

    def adapter(self, bridge):
        adapter = InputAdapter(bridge)
        adapter._settle = lambda *args: None
        return adapter

    def execute(self, adapter, state, action, fresh=None):
        with patch('tsai_sc.controller.read_state', return_value=fresh or state) as read, \
                patch('tsai_sc.controller.read_selection', return_value=[action['unit']]):
            result = adapter.execute(state, action)
        self.assertEqual(read.call_count, 1)
        return result

    def test_refinery_click_uses_upper_left_corner_not_geyser_center(self):
        bridge = Bridge()
        adapter = self.adapter(bridge)
        state = mission()
        state['units'][0].update(x=400, y=500)
        action = candidates(state)['build_refinery']
        result = self.execute(adapter, state, action)
        self.assertTrue(result['issued'])
        # Live original-engine regression: center(320,224) erroneously placed
        # the ghost at(384,256); correct cursor coordinate is(256,192).
        self.assertIn(('clickHold', 256, 192, 100, 0), bridge.calls)
        self.assertNotIn(('clickHold', 320, 224, 100, 0), bridge.calls)
        self.assertIn(('keyHold', 'r', 60), bridge.calls)
        self.assertEqual(bridge.calls[-1], ('pause',))
        self.assertFalse(bridge.running)

    def test_barracks_uses_b_b_and_four_by_three_tile_upper_left(self):
        bridge = Bridge()
        state = mission()
        state['units'][0].update(x=400, y=500)
        action = {'kind': 'build', 'unit': 1, 'building': 111,
                  'point': {'x': 320, 'y': 592}}
        result = self.execute(self.adapter(bridge), state, action)
        self.assertTrue(result['issued'])
        self.assertEqual([call[1] for call in bridge.calls if call[0] == 'keyHold'], ['b', 'b'])
        # center(320,592) - camera(0,384) - half footprint(64,48)
        self.assertIn(('clickHold', 256, 160, 100, 0), bridge.calls)
        self.assertFalse(bridge.running)

    def test_gather_right_clicks_target_without_build_offset(self):
        bridge = Bridge()
        adapter = self.adapter(bridge)
        state = mission()
        state['units'][0].update(x=400, y=500)
        state['units'][3].update(x=480, y=576)
        self.execute(adapter, state, candidates(state)['mine_1'])
        self.assertIn(('clickHold', 480, 192, 100, 1), bridge.calls)

    def test_refinery_near_screen_edge_pans_before_opening_build_cursor(self):
        # Attempt05: geyser center at(64,288) produced upper-left x=0,
        # touching the scroll edge. Pan first so the whole footprint is safe.
        bridge = Bridge(camera=(256, 320))
        bridge.pan_to = (0, 384)
        state = mission()
        state['camera'] = {'x': 256, 'y': 320}
        state['units'][0].update(x=400, y=500)
        self.execute(self.adapter(bridge), state, candidates(state)['build_refinery'])
        pan = ('clickHold', 26, 386, 100, 0)
        self.assertIn(pan, bridge.calls)
        self.assertLess(bridge.calls.index(pan), bridge.calls.index(('keyHold', 'b', 60)))
        self.assertIn(('clickHold', 256, 192, 100, 0), bridge.calls)
        self.assertNotIn(('clickHold', 0, 256, 100, 0), bridge.calls)

    def test_build_footprint_has_larger_camera_margin_than_unit_target(self):
        bridge = Bridge(camera=(256, 384))
        bridge.pan_to = (0, 384)
        adapter = self.adapter(bridge)
        self.assertEqual(adapter.focus(320, 608), {'x': 64, 'y': 224})
        self.assertEqual(bridge.calls, [])
        self.assertEqual(adapter.focus(320, 608, margin_x=88, margin_y=60), {'x': 320, 'y': 224})
        self.assertEqual(bridge.calls, [('clickHold', 26, 386, 100, 0)])

    def test_actor_is_refreshed_and_selected_while_paused(self):
        bridge = Bridge()
        adapter = self.adapter(bridge)
        state = mission()
        state['units'][0].update(x=400, y=500)
        state['units'][3].update(x=480, y=576)
        fresh = copy.deepcopy(state)
        fresh['units'][0].update(x=420, y=510)
        self.execute(adapter, state, candidates(state)['mine_1'], fresh)
        self.assertIn(('clickHold', 420, 126, 1, 0), bridge.calls)
        self.assertNotIn(('clickHold', 400, 116, 1, 0), bridge.calls)
        self.assertIn(('clickHold', False), bridge.input_running)
        # With the actor already visible there must be no game input before its
        # fresh selection. Escape used to cancel the previously selected unit.
        self.assertEqual(bridge.calls[:3], [
            ('resume',), ('pause',), ('clickHold', 420, 126, 1, 0),
        ])

    def test_new_commands_never_send_escape_or_blanket_reset_keys(self):
        # Real attempt03 regression: unconditional Escape canceled the selected
        # CC queue (refunding50) and interrupted an SCV construction order33.
        # Each action may send only its own command hotkeys after actor selection.
        cases = [
            ({'kind': 'train', 'unit': 2}, ['s']),
            ({'kind': 'gather', 'unit': 1, 'target': 4}, []),
            ({'kind': 'build', 'unit': 1, 'building': 110,
              'point': {'x': 320, 'y': 608}}, ['b', 'r']),
            ({'kind': 'build', 'unit': 1, 'building': 109,
              'point': {'x': 336, 'y': 608}}, ['b', 's']),
        ]
        for action, expected_keys in cases:
            with self.subTest(kind=action['kind'], building=action.get('building')):
                bridge = Bridge()
                state = mission()
                state['units'][0].update(x=400, y=500)
                state['units'][3].update(x=480, y=576)
                self.execute(self.adapter(bridge), state, action)
                keys = [call[1] for call in bridge.calls if call[0] == 'keyHold']
                self.assertEqual(keys, expected_keys)
                self.assertNotIn('escape', keys)
                first_input = next(call for call in bridge.calls if call[0] not in {'resume', 'pause'})
                self.assertEqual(first_input[0], 'clickHold')

    def test_actor_becoming_hidden_or_incomplete_prevents_command(self):
        for attrs in ({'visible': False}, {'completed': False}):
            with self.subTest(attrs=attrs):
                bridge = Bridge()
                adapter = self.adapter(bridge)
                state = mission()
                state['units'][0].update(x=400, y=500)
                fresh = copy.deepcopy(state)
                fresh['units'][0].update(attrs)
                result = self.execute(adapter, state, candidates(state)['build_refinery'], fresh)
                self.assertFalse(result['issued'])
                self.assertEqual(result['reason'], 'Actor became unavailable')
                self.assertFalse(any(call[0] == 'clickHold' for call in bridge.calls))
                self.assertNotIn(('keyHold', 'b', 60), bridge.calls)
                self.assertFalse(bridge.running)

    def test_actor_moving_outside_viewport_prevents_command(self):
        bridge = Bridge()
        adapter = self.adapter(bridge)
        state = mission()
        state['units'][0].update(x=400, y=500)
        fresh = copy.deepcopy(state)
        fresh['units'][0]['x'] = 700
        result = self.execute(adapter, state, candidates(state)['build_refinery'], fresh)
        self.assertFalse(result['issued'])
        self.assertEqual(result['reason'], 'Actor left the viewport')
        self.assertNotIn(('keyHold', 'b', 60), bridge.calls)
        self.assertFalse(bridge.running)

    def test_scripted_camera_override_rejects_command_without_ending_run(self):
        bridge = Bridge(camera=(0, 0))
        state = mission()
        state['units'][0].update(x=900, y=800)
        action = candidates(state)['mine_1']
        result = self.adapter(bridge).execute(state, action)
        self.assertFalse(result['issued'])
        self.assertIn('Camera', result['reason'])
        self.assertEqual(sum(call[0] == 'clickHold' for call in bridge.calls), 2)
        self.assertFalse(any(call[0] == 'keyHold' for call in bridge.calls))
        self.assertFalse(bridge.running)

    def test_wait_does_not_issue_unselected_commands(self):
        bridge = Bridge()
        result = self.adapter(bridge).execute(mission(), {'kind': 'wait'})
        self.assertEqual(result, {'issued': True, 'inputs': []})
        self.assertEqual(bridge.calls, [])

    def test_input_failure_still_pauses_runtime(self):
        bridge = Bridge()
        bridge.fail_on = 'keyHold'
        with self.assertRaisesRegex(RuntimeError, 'input failed'):
            self.execute(self.adapter(bridge), mission(), {'kind': 'train', 'unit': 2})
        self.assertFalse(bridge.running)
        self.assertEqual(bridge.calls[-1], ('pause',))

    def test_rejected_selection_prevents_hotkeys_reaching_previous_unit(self):
        bridge = Bridge()
        bridge.reject_on = 'clickHold'
        with self.assertRaisesRegex(RuntimeError, 'unit selection was rejected'):
            self.execute(self.adapter(bridge), mission(), {'kind': 'train', 'unit': 2})
        self.assertFalse(any(call[0] == 'keyHold' for call in bridge.calls))
        self.assertFalse(bridge.running)

    def test_wrong_economic_actor_blocks_commands_and_records_actual_selection(self):
        for selected in ([], [2], [1, 2]):
            bridge = Bridge()
            state = mission()
            state['units'][0].update(x=400, y=500)
            action = {'kind': 'build', 'unit': 1, 'building': 111,
                      'point': {'x': 320, 'y': 592}}
            def actual_selection(_):
                self.assertFalse(bridge.running)
                return selected
            with patch('tsai_sc.controller.read_state', return_value=state), \
                    patch('tsai_sc.controller.read_selection', side_effect=actual_selection):
                result = self.adapter(bridge).execute(state, action)
            self.assertFalse(result['issued'])
            self.assertEqual(result['selected_units'], selected)
            self.assertFalse(any(call[0] == 'keyHold' for call in bridge.calls))
            self.assertEqual([call for call in bridge.calls if call[0] == 'clickHold'],
                             [('clickHold', 400, 116, 1, 0)])
            self.assertFalse(bridge.running)


class CommandFeedbackTests(unittest.TestCase):
    def test_live_refinery_regression_distinguishes_failed_click_from_build_order(self):
        before = mission()
        action = candidates(before)['build_refinery']
        after = copy.deepcopy(before)
        # Attempt01: placement cursor rejected the center click, actor stayed3.
        self.assertFalse(verify_command(before, after, action)['accepted'])

        # Attempt02: corrected upper-left click yielded place-building order30
        # with refinery110 queued and target at the authentic geyser center.
        after['units'][0].update(order_id=30, build_queue=[110], order_target={'x': 320, 'y': 608})
        self.assertTrue(verify_command(before, after, action)['accepted'])
        after['units'][0]['order_id'] = 33
        self.assertTrue(verify_command(before, after, action)['accepted'])

    def test_build_can_finish_before_verification(self):
        before = mission()
        action = candidates(before)['build_refinery']
        after = copy.deepcopy(before)
        after['units'].append(unit(6, 110, 320, 608))
        self.assertTrue(verify_command(before, after, action)['accepted'])
        after['units'][-1]['x'] = 800
        self.assertFalse(verify_command(before, after, action)['accepted'])

    def test_hidden_gas_worker_did_not_accept_mineral_command(self):
        before = mission()
        before['units'][0].update(order_id=83, visible=False)
        action = {'kind': 'gather', 'unit': 1, 'target': 4}
        after = copy.deepcopy(before)
        # Attempt02 clicked the last position of an SCV inside the refinery;
        # gas order83 persisted and therefore did not confirm mineral gathering.
        self.assertFalse(verify_command(before, after, action)['accepted'])
        after['units'][0].update(order_id=87, visible=True)
        self.assertTrue(verify_command(before, after, action)['accepted'])
        after['units'][0]['order_id'] = 90  # Return minerals remains productive.
        self.assertTrue(verify_command(before, after, action)['accepted'])

    def test_gas_harvest_acceptance_survives_sprite_hiding_and_return_trip(self):
        before = mission()
        before['units'].append(unit(6, 110, 320, 608))
        action = candidates(before)['gas_1']
        after = copy.deepcopy(before)
        self.assertFalse(verify_command(before, after, action)['accepted'])
        after['units'][0].update(order_id=83, visible=False)
        self.assertTrue(verify_command(before, after, action)['accepted'])
        after['units'][0].update(order_id=84, visible=True)
        self.assertTrue(verify_command(before, after, action)['accepted'])

    def test_training_requires_queue_or_new_worker(self):
        before = mission()
        action = candidates(before)['train_scv']
        after = copy.deepcopy(before)
        self.assertFalse(verify_command(before, after, action)['accepted'])
        after['units'][1]['build_queue'] = [7]
        self.assertTrue(verify_command(before, after, action)['accepted'])
        after['units'][1]['build_queue'] = []
        after['units'].append(unit(6, 7, 600, 600))
        self.assertTrue(verify_command(before, after, action)['accepted'])

    def test_missing_actor_does_not_confirm_command(self):
        before = mission()
        action = candidates(before)['mine_1']
        after = copy.deepcopy(before)
        after['units'] = [u for u in after['units'] if u['id'] != action['unit']]
        self.assertFalse(verify_command(before, after, action)['accepted'])


class ModelObservationTests(unittest.TestCase):
    def test_remaining_building_cost_counts_unfinished_structures_as_started(self):
        state = mission()
        model, _ = request_for(state, candidates(state), [])
        self.assertEqual(model['remaining_work']['supply_depots_still_to_start'], 2)
        self.assertEqual(model['remaining_work']['minerals_needed_for_remaining_buildings'], 300)
        self.assertEqual(model['remaining_work']['mineral_shortfall'], 150)
        state['minerals'] = 40
        state['gas'] = 24
        state['units'].extend([
            unit(6, 109, 752, 640, completed=False),
            unit(7, 110, 320, 608, completed=False),
        ])
        model, _ = request_for(state, candidates(state), [])
        remaining = model['remaining_work']
        self.assertEqual(remaining['supply_depots_still_to_start'], 1)
        self.assertEqual(remaining['refineries_still_to_start'], 0)
        self.assertEqual(remaining['minerals_needed_for_remaining_buildings'], 100)
        self.assertEqual(remaining['mineral_shortfall'], 60)
        self.assertEqual(remaining['additional_gas_needed'], 76)
        # Starting a building does not rewrite completed objective progress.
        self.assertEqual(model['progress']['supply_depots']['current'], 1)
        self.assertEqual(model['progress']['refineries']['current'], 0)

    def test_remaining_needs_are_zero_when_resources_and_buildings_suffice(self):
        state = mission()
        state['gas'] = 108
        state['units'].extend([
            unit(6, 109, 752, 640), unit(7, 109, 880, 640, completed=False),
            unit(8, 110, 320, 608),
        ])
        model, _ = request_for(state, candidates(state), [])
        remaining = model['remaining_work']
        for key in ('supply_depots_still_to_start', 'refineries_still_to_start',
                    'minerals_needed_for_remaining_buildings', 'mineral_shortfall',
                    'additional_gas_needed'):
            self.assertEqual(remaining[key], 0)

    def test_current_jobs_and_workforce_counts_describe_actual_orders(self):
        state = mission()
        state['units'].extend([
            unit(6, 7, 500, 600, order=33),
            unit(7, 7, 520, 600, order=87),
            unit(8, 7, 320, 608, order=83, visible=False),
            unit(9, 7, 576, 528, completed=False),
            unit(10, 7, 100, 100, owner=0, order=87),
        ])
        model, _ = request_for(state, candidates(state), [])
        jobs = {u['id']: u.get('current_job') for u in model['units']}
        self.assertEqual({key: jobs[key] for key in (1, 6, 7, 8, 9)}, {
            1: 'idle', 6: 'constructing', 7: 'gathering minerals',
            8: 'gathering gas', 9: 'being trained',
        })
        remaining = model['remaining_work']
        self.assertEqual(remaining['workers_collecting_minerals'], 1)
        self.assertEqual(remaining['workers_collecting_gas'], 1)
        self.assertEqual(remaining['workers_constructing'], 1)


class TacticalControllerTests(unittest.TestCase):
    def state(self):
        state = mission()
        state.update(mission='Strongarm', mission_id='strongarm', enemy_players=[0, 3])
        state['map'] = {'width_tiles': 96, 'height_tiles': 64}
        state['units'] = [
            unit(1, 0, 300, 500), unit(2, 0, 360, 520),
            unit(3, 7, 320, 540), unit(4, 111, 500, 500),
            unit(9, 37, 480, 560, owner=3),
        ]
        return state

    def action(self, kind='attack_move'):
        return {'kind': kind, 'units': [1, 2], 'point': {'x': 500, 'y': 560}}

    def execute(self, state, action, *, actual=(1, 2), bridge=None, snapshots=None):
        bridge = bridge or Bridge()
        adapter = InputAdapter(bridge)
        adapter._settle = lambda *args: None
        reader = {'side_effect': itertools.chain(snapshots, itertools.repeat(snapshots[-1]))} if snapshots is not None else {'return_value': state}
        def observed_selection(_):
            if 'units' not in action:
                return [action['unit']]
            clicks = [call for call in bridge.calls if call[0] == 'clickHold' and call[2] < 348]
            if not clicks:
                return []
            if len(clicks) == 1 and 1 in actual:
                return [1]
            return list(actual)
        with patch('tsai_sc.controller.read_state', **reader), patch('tsai_sc.controller.read_selection', side_effect=observed_selection):
            result = adapter.execute(state, action)
        return result, bridge

    def test_squad_uses_shift_selection_then_attack_and_releases_modifier(self):
        result, bridge = self.execute(self.state(), self.action())
        self.assertTrue(result['issued'])
        self.assertEqual(result['selected_units'], [1, 2])
        self.assertIn(('key', 'shift', {'down': True}), bridge.calls)
        up = ('key', 'shift', {'up': True})
        self.assertIn(up, bridge.calls)
        self.assertLess(bridge.calls.index(up), bridge.calls.index(('keyHold', 'a', 60)))
        self.assertIn(('clickHold', 38, 398, 100, 0), bridge.calls)
        self.assertEqual(result['destination_input'], 'minimap')
        self.assertEqual(bridge.calls[-2:], [up, ('pause',)])
        self.assertFalse(bridge.running)
        self.assertFalse(any(call[0] == 'keyHold' and call[1] == 'escape' for call in bridge.calls))
        self.assertEqual(result['inputs'][-1], {'command': 'key', 'args': ['shift', {'up': True}]})
        self.assertEqual(result['selection_method'], 'shift_click')

        selection_inputs = [(command, running) for command, running in bridge.input_running
                            if command in {'clickHold', 'key'}]
        self.assertEqual(selection_inputs[:3], [('clickHold', False), ('key', False), ('clickHold', False)])

    def test_exact_existing_selection_skips_clicks_and_modifier_presses(self):
        state = self.state()
        bridge = Bridge()
        adapter = InputAdapter(bridge)
        adapter._settle = lambda *args: None
        with patch('tsai_sc.controller.read_state', return_value=state) as fresh, \
                patch('tsai_sc.controller.read_selection', return_value=[2, 1]):
            result = adapter.execute(state, self.action())
        self.assertTrue(result['issued'])
        self.assertEqual(result['selected_units'], [2, 1])
        self.assertEqual(result['selection_method'], 'already_selected')
        self.assertEqual(fresh.call_count, 1)
        # Only the requested order's destination is clicked, not each Marine.
        self.assertEqual([call for call in bridge.calls if call[0] == 'clickHold'], [('clickHold', 38, 398, 100, 0)])
        self.assertNotIn(('key', 'shift', {'down': True}), bridge.calls)
        self.assertEqual(result['inputs'][-1], {'command': 'key', 'args': ['shift', {'up': True}]})

    def test_partial_existing_selection_does_not_skip_missing_squad_members(self):
        state = self.state()
        bridge = Bridge()
        adapter = InputAdapter(bridge)
        adapter._settle = lambda *args: None
        def selection(_):
            return [1, 2] if any(call[0] == 'clickHold' for call in bridge.calls) else [1]
        with patch('tsai_sc.controller.read_state', return_value=state), \
                patch('tsai_sc.controller.read_selection', side_effect=selection):
            result = adapter.execute(state, self.action())
        self.assertEqual(result['selection_method'], 'shift_click')
        self.assertIn(('clickHold', 360, 136, 1, 0), bridge.calls)
        self.assertNotIn(('clickHold', 300, 116, 1, 0), bridge.calls)

    def test_exact_selection_still_requires_current_owned_visible_complete_units(self):
        state = self.state()
        fresh = copy.deepcopy(state)
        fresh['units'][0]['visible'] = False
        bridge = Bridge()
        adapter = InputAdapter(bridge)
        adapter._settle = lambda *args: None
        action = self.action()
        action['units'] = [1]
        with patch('tsai_sc.controller.read_state', return_value=fresh), \
                patch('tsai_sc.controller.read_selection', return_value=[1]):
            result = adapter.execute(state, action)
        self.assertFalse(result['issued'])
        self.assertFalse(any(call[0] == 'keyHold' for call in bridge.calls))
        self.assertFalse(bridge.running)

    def test_selection_cannot_command_units_outside_model_requested_squad(self):
        for actual in ([], [3], [1, 3], [1, 99]):
            with self.subTest(actual=actual):
                result, bridge = self.execute(self.state(), self.action(), actual=actual)
                self.assertFalse(result['issued'])
                self.assertFalse(any(call[0] == 'keyHold' for call in bridge.calls))
                self.assertEqual(bridge.calls[-2:], [('key', 'shift', {'up': True}), ('pause',)])

    def test_live_visible_requested_units_cannot_be_silently_omitted(self):
        result, bridge = self.execute(self.state(), self.action(), actual=[1])
        self.assertFalse(result['issued'])
        self.assertEqual(result['selected_units'], [1])
        self.assertEqual(result['missing_units'], [2])
        self.assertFalse(any(call[0] == 'keyHold' for call in bridge.calls))
        self.assertEqual(sum(call == ('clickHold', 360, 136, 1, 0) for call in bridge.calls), 1)
        self.assertEqual(sum(call == ('drag', 356, 132, 364, 140, 0) for call in bridge.calls), 1)

    def test_tiny_box_recovers_occluded_requested_unit_after_click_misses(self):
        for dragged_selection in ([1, 2], [1, 2, 3]):
            with self.subTest(dragged_selection=dragged_selection):
                state = self.state()
                bridge = Bridge()
                adapter = InputAdapter(bridge)
                adapter._settle = lambda *args: None
                def selection(_):
                    if any(call[0] == 'drag' for call in bridge.calls):
                        return dragged_selection
                    return [1] if any(call[0] == 'clickHold' for call in bridge.calls) else []
                with patch('tsai_sc.controller.read_state', return_value=state), \
                        patch('tsai_sc.controller.read_selection', side_effect=selection):
                    result = adapter.execute(state, self.action())
                self.assertEqual(result['issued'], dragged_selection == [1, 2])
                self.assertEqual(result['selected_units'], dragged_selection)
                self.assertEqual(sum(call[0] == 'drag' for call in bridge.calls), 1)
                self.assertIn(('drag', 356, 132, 364, 140, 0), bridge.calls)
                self.assertIn(('drag', False), bridge.input_running)
                self.assertEqual(result['selection_checks'][-1]['method'], 'box_drag')
                if dragged_selection != [1, 2]:
                    self.assertFalse(any(call[0] == 'keyHold' for call in bridge.calls))
                self.assertFalse(bridge.running)

    def test_shift_is_queued_before_final_moving_actor_snapshot(self):
        state = self.state()
        bridge = Bridge()
        adapter = InputAdapter(bridge)
        adapter._settle = lambda *args: None
        def snapshot(_):
            self.assertFalse(bridge.running)
            fresh = copy.deepcopy(state)
            if ('key', 'shift', {'down': True}) in bridge.calls:
                fresh['units'][1].update(x=420, y=530)
            return fresh
        def selection(_):
            count = sum(call[0] == 'clickHold' for call in bridge.calls)
            return [] if count == 0 else [1] if count == 1 else [1, 2]
        with patch('tsai_sc.controller.read_state', side_effect=snapshot), \
                patch('tsai_sc.controller.read_selection', side_effect=selection):
            result = adapter.execute(state, self.action())
        self.assertTrue(result['issued'])
        self.assertIn(('clickHold', 420, 146, 1, 0), bridge.calls)
        self.assertNotIn(('clickHold', 360, 136, 1, 0), bridge.calls)

    def test_unavailable_unit_after_camera_refresh_is_not_clicked(self):
        for change in ({'visible': False}, {'completed': False}, {'owner': 0}):
            with self.subTest(change=change):
                state = self.state()
                fresh = copy.deepcopy(state)
                fresh['units'][0].update(change)
                action = self.action()
                action['units'] = [1]
                result, bridge = self.execute(state, action, snapshots=[state, fresh], actual=[])
                self.assertFalse(result['issued'])
                self.assertFalse(any(call[0] in {'clickHold', 'keyHold'} for call in bridge.calls))

    def test_shift_is_released_when_second_selection_click_raises(self):
        class FailedSecondClick(Bridge):
            def rpc(self, command, *args):
                result = super().rpc(command, *args)
                if command == 'clickHold' and sum(call[0] == 'clickHold' for call in self.calls) == 2:
                    raise RuntimeError('selection click failed')
                return result
        bridge = FailedSecondClick()
        with self.assertRaisesRegex(RuntimeError, 'selection click failed'):
            self.execute(self.state(), self.action(), bridge=bridge)
        down_index = bridge.calls.index(('key', 'shift', {'down': True}))
        self.assertIn(('key', 'shift', {'up': True}), bridge.calls[down_index + 1:])
        self.assertEqual(bridge.calls[-1], ('pause',))
        self.assertFalse(bridge.running)

    def test_disappeared_or_newly_allied_focus_target_does_not_become_ground_attack(self):
        for change in ({'visible': False}, {'owner': 2}):
            with self.subTest(change=change):
                state = self.state()
                fresh = copy.deepcopy(state)
                fresh['units'][-1].update(change)
                action = self.action('attack_target')
                action['target'] = 9
                result, bridge = self.execute(state, action, snapshots=[state] * 4 + [fresh])
                self.assertFalse(result['issued'])
                self.assertFalse(any(call[0] == 'keyHold' for call in bridge.calls))

    def test_focus_uses_current_visible_enemy_position(self):
        state = self.state()
        fresh = copy.deepcopy(state)
        fresh['units'][-1].update(x=540, y=590)
        action = self.action('attack_target')
        action['target'] = 9
        result, bridge = self.execute(state, action, snapshots=[state] * 4 + [fresh])
        self.assertTrue(result['issued'])
        self.assertIn(('clickHold', 540, 206, 100, 0), bridge.calls)

    def test_strongarm_minimap_uses_centered_one_pixel_per_tile_geometry(self):
        bridge = Bridge(camera=(0, 0))
        bridge.pan_to = (1280, 864)
        adapter = InputAdapter(bridge)
        adapter.map_size = (96, 64)
        adapter._settle = lambda *args: None
        self.assertEqual(adapter.focus(1600, 1024), {'x': 320, 'y': 160})
        # Actual 96x64 minimap rectangle x22..118,y380..444, world/32.
        self.assertEqual(bridge.calls, [('clickHold', 72, 412, 100, 0)])

    def test_minimap_ground_attack_does_not_focus_or_click_destination_building_sprite(self):
        state = self.state()
        # The destination is occupied by a friendly building and remains
        # off-camera. Ground attack must not pan then A-click that sprite.
        state['units'][3].update(x=2000, y=1500)
        for kind in ('attack_move', 'explore'):
            action = self.action(kind)
            action['point'] = {'x': 2000, 'y': 1500}
            result, bridge = self.execute(state, action)
            self.assertTrue(result['issued'])
            self.assertIn(('clickHold', 84, 427, 100, 0), bridge.calls)
            self.assertEqual(sum(call[0] == 'clickHold' and call[2] >= 348 for call in bridge.calls), 1)
            self.assertLess(bridge.calls.index(('keyHold', 'a', 60)),
                            bridge.calls.index(('clickHold', 84, 427, 100, 0)))

    def test_minimap_coordinates_clamp_rounding_to_inside_map_edges(self):
        adapter = InputAdapter(Bridge())
        adapter.map_size = (96, 64)
        self.assertEqual(adapter.minimap_point(0, 0), {'x': 22, 'y': 380})
        self.assertEqual(adapter.minimap_point(3071, 2047), {'x': 117, 'y': 443})
        adapter.map_size = (64, 64)
        self.assertEqual(adapter.minimap_point(2047, 2047), {'x': 133, 'y': 475})
        for point in [(-1, 20), (2048, 20), (20, float('nan'))]:
            with self.assertRaisesRegex(RuntimeError, 'outside'):
                adapter.minimap_point(*point)

    def test_marine_training_uses_barracks_hotkey_and_verifies_marine_queue(self):
        state = self.state()
        action = {'kind': 'train', 'unit': 4, 'train_type': 0}
        result, bridge = self.execute(state, action)
        self.assertTrue(result['issued'])
        self.assertEqual([call[1] for call in bridge.calls if call[0] == 'keyHold'], ['m'])
        after = copy.deepcopy(state)
        after['units'][3]['build_queue'] = [7]
        self.assertFalse(verify_command(state, after, action)['accepted'])
        after['units'][3]['build_queue'] = [0]
        self.assertTrue(verify_command(state, after, action)['accepted'])

    def test_infantry_weapons_uses_verified_engineering_bay_and_w(self):
        state = self.state()
        state['gas'] = 200
        state['units'][3].update(type_id=122, order_id=23)
        action = {'kind': 'upgrade', 'unit': 4, 'upgrade': 'infantry_weapons'}
        result, bridge = self.execute(state, action)
        self.assertTrue(result['issued'])
        self.assertEqual(result['selected_units'], [4])
        self.assertEqual([call for call in bridge.calls if call[0] == 'keyHold'], [('keyHold', 'w', 60)])
        after = copy.deepcopy(state)
        after['units'][3]['order_id'] = 76
        after['gas'] = 100
        after['minerals'] += 8  # Concurrent mining need not produce an exact mineral delta.
        self.assertTrue(verify_command(state, after, action)['accepted'])
        after['gas'] = 200
        self.assertFalse(verify_command(state, after, action)['accepted'])
        after['gas'] = 100
        state['units'][3]['order_id'] = 76
        self.assertFalse(verify_command(state, after, action)['accepted'])

    def test_retreat_requires_move_order_to_requested_destination(self):
        state = self.state()
        after = copy.deepcopy(state)
        actor = after['units'][0]
        action = self.action('retreat')
        actor.update(order_id=10, order_target=action['point'])
        self.assertFalse(verify_command(state, after, action)['accepted'])
        actor.update(order_id=6, order_target={'x': 100, 'y': 100})
        self.assertFalse(verify_command(state, after, action)['accepted'])
        actor['order_target'] = action['point']
        self.assertTrue(verify_command(state, after, action)['accepted'])

    def test_regroup_or_retreat_can_follow_a_friendly_at_the_destination(self):
        for kind in ('regroup', 'retreat'):
            with self.subTest(kind=kind):
                before = self.state()
                before['allied_players'] = [2, 6]
                before['units'][2].update(owner=2, x=500, y=560, generation=3)
                before['units'][0]['generation'] = 4
                after = copy.deepcopy(before)
                actor, target = after['units'][0], after['units'][2]
                actor.update(order_id=49, order_target_address=target['address'])
                # A Follow target's live pointer/position is authoritative even
                # when its cached order_target coordinate has not caught up.
                actor['order_target'] = {'x': 0, 'y': 0}
                self.assertTrue(verify_command(before, after, self.action(kind))['accepted'])

    def test_follow_accepts_clicks_inside_friendly_building_footprint(self):
        before = self.state()
        after = copy.deepcopy(before)
        actor, target = after['units'][0], after['units'][3]
        actor.update(order_id=49, order_target_address=target['address'])
        action = self.action('regroup')
        action['point'] = {'x': target['x'] + 60, 'y': target['y'] + 44}
        self.assertTrue(verify_command(before, after, action)['accepted'])
        action['point']['x'] = target['x'] + 100
        self.assertFalse(verify_command(before, after, action)['accepted'])

    def test_follow_rejects_unrelated_hostile_missing_or_dead_targets(self):
        for change in ({'x': 900}, {'owner': 3}, {'hp': 0}):
            with self.subTest(change=change):
                before = self.state()
                before['units'][2].update(x=500, y=560)
                after = copy.deepcopy(before)
                actor, target = after['units'][0], after['units'][2]
                actor.update(order_id=49, order_target_address=target['address'],
                             order_target={'x': 500, 'y': 560})
                target.update(change)
                self.assertFalse(verify_command(before, after, self.action('regroup'))['accepted'])
        before = self.state()
        after = copy.deepcopy(before)
        actor = after['units'][0]
        for address in (None, 0, 0xDEADBEEF):
            actor.update(order_id=49, order_target_address=address)
            self.assertFalse(verify_command(before, after, self.action('regroup'))['accepted'])

    def test_follow_does_not_accept_recycled_actor_or_target_identity(self):
        before = self.state()
        before['units'][0]['generation'] = 1
        before['units'][2].update(x=500, y=560, generation=2)
        for index in (0, 2):
            with self.subTest(index=index):
                after = copy.deepcopy(before)
                after['units'][0].update(order_id=49, order_target_address=after['units'][2]['address'])
                after['units'][index]['generation'] += 1
                self.assertFalse(verify_command(before, after, self.action('regroup'))['accepted'])

    def test_follow_honors_explicit_target_and_is_not_attack_acceptance(self):
        before = self.state()
        before['units'][2].update(x=500, y=560)
        after = copy.deepcopy(before)
        after['units'][0].update(order_id=49, order_target_address=after['units'][2]['address'])
        action = self.action('regroup')
        action['target'] = 4
        self.assertFalse(verify_command(before, after, action)['accepted'])
        action['target'] = 3
        self.assertTrue(verify_command(before, after, action)['accepted'])
        for kind in ('attack_move', 'explore'):
            self.assertFalse(verify_command(before, after, self.action(kind))['accepted'])

    def test_follow_requires_observed_ally_and_matches_recorded_actor_generation(self):
        before = self.state()
        before['allied_players'] = [2, 6]
        before['units'][0]['generation'] = 5
        before['units'][2].update(owner=2, x=500, y=560)
        after = copy.deepcopy(before)
        after['units'][0].update(order_id=49, order_target_address=after['units'][2]['address'])
        action = self.action('regroup')
        action['unit_generations'] = {'1': 4}
        self.assertFalse(verify_command(before, after, action)['accepted'])
        action['unit_generations']['1'] = 5
        self.assertTrue(verify_command(before, after, action)['accepted'])
        after['units'][2]['visible'] = False
        self.assertFalse(verify_command(before, after, action)['accepted'])

    def test_focus_verification_matches_target_pointer(self):
        state = self.state()
        after = copy.deepcopy(state)
        action = self.action('attack_target')
        action['target'] = 9
        actor = after['units'][0]
        actor.update(order_id=10, order_target_address=state['units'][2]['address'])
        self.assertFalse(verify_command(state, after, action)['accepted'])
        actor['order_target_address'] = state['units'][-1]['address']
        self.assertTrue(verify_command(state, after, action)['accepted'])

    def test_attack_move_verification_accepts_destination_or_enemy_acquisition(self):
        state = self.state()
        after = copy.deepcopy(state)
        action = self.action()
        actor = after['units'][0]
        actor.update(order_id=14, order_target={'x': 100, 'y': 100})
        self.assertFalse(verify_command(state, after, action)['accepted'])
        actor['order_target'] = action['point']
        self.assertTrue(verify_command(state, after, action)['accepted'])
        actor.update(order_id=10, order_target={'x': 450, 'y': 560})
        self.assertTrue(verify_command(state, after, action)['accepted'])


if __name__ == '__main__':
    unittest.main()
