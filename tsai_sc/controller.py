"""Bounded Jev decisions translated to ordinary StarCraft keyboard/mouse input.

The adapter supplies targets and placement candidates, never a policy fallback.
Only the selected model choice is executed. Guest memory is strictly read-only.
"""
from __future__ import annotations

import math
import struct
import time
from .game import CAMERA, game_to_screen, read_state, read_selection

BUILDING_SIZE = {106: (128, 96), 109: (96, 64), 110: (128, 64),
                 111: (128, 96), 122: (128, 96)}
IDLE = {1, 2, 3}
BUILDING_ORDERS = {30, 33, 34, 35}
GAS_ORDERS = {81, 82, 83, 84}
MINERAL_ORDERS = {79, 80, 85, 86, 87, 88, 89, 90}
SQUAD_ORDERS = {'attack_move', 'attack_target', 'retreat', 'regroup', 'explore'}


class CommandUnavailable(RuntimeError):
    """A moving target or scripted camera change prevented this command."""


def own_units(state, type_id):
    return [u for u in state['units'] if u['owner'] == state['player_id'] and u['type_id'] == type_id]


def supply(state):
    if 'supply' in state:
        return state['supply']['used'], state['supply']['available']
    # Boot Camp starts with sixteen Marines, one SCV, a CC and a depot.
    # Only the Terran unit/building types offered by this harness are relevant.
    if 'supply_used' in state and 'supply_total' in state:
        return state['supply_used'], state['supply_total']
    own = [u for u in state['units'] if u['owner'] == state['player_id']]
    used = sum(u['type_id'] in {0, 7} for u in own)
    used += sum(len(u['build_queue']) for u in own if u['type_id'] == 106)
    total = sum(({106: 10, 109: 8}.get(u['type_id'], 0)) for u in own if u['completed'])
    return used, total


def verify_command(before, after, action):
    """Separate dispatch from observed engine acceptance without retrying policy."""
    kind = action['kind']
    if kind in {'wait', 'continue'}:
        return {'accepted': True, 'evidence': 'No new command requested'}
    units = {u['id']: u for u in after['units']}
    if kind in SQUAD_ORDERS:
        actors = [units[i] for i in action['units'] if i in units]
        # A previous attack does not prove a new retreat was accepted. Match the
        # order family and destination, allowing attack-move to acquire enemies.
        point = action['point']
        def matches(u):
            destination = u.get('order_target', {})
            near = isinstance(destination, dict) and 'x' in destination and math.dist((destination['x'], destination['y']), (point['x'], point['y'])) <= 48
            if kind in {'retreat', 'regroup'}:
                return u['order_id'] == 6 and near
            if kind == 'attack_target':
                target = next((t for t in before['units'] if t['id'] == action['target']), None)
                return u['order_id'] == 10 and target is not None and u.get('order_target_address') == target.get('address')
            return (u['order_id'] == 14 and near) or u['order_id'] == 10
        active = [u for u in actors if matches(u)]
        return {'accepted': bool(active), 'evidence': 'Observed surviving squad orders',
                'actor_orders': {str(u['id']): u['order_id'] for u in actors}}
    unit = units.get(action['unit'])
    if unit is None:
        return {'accepted': False, 'evidence': 'Actor no longer observable'}
    if kind == 'train':
        trained = action.get('train_type', 7)
        accepted = trained in unit['build_queue'] or len(own_units(after, trained)) > len(own_units(before, trained))
    elif kind == 'build':
        started = any(math.dist((u['x'], u['y']), (action['point']['x'], action['point']['y'])) < 24 for u in own_units(after, action['building']))
        accepted = started or unit['order_id'] in {30, 33}
    elif kind == 'gather':
        target = next(u for u in before['units'] if u['id'] == action['target'])
        accepted = unit['order_id'] in (GAS_ORDERS if target['type_id'] == 110 else MINERAL_ORDERS)
    else:
        accepted = False
    return {'accepted': accepted, 'evidence': 'Observed actor order and production state', 'actor_order_id': unit['order_id']}


def building_sites(state, building_id):
    """Offer open tile-aligned candidates; the original engine validates terrain.

    This is a compact placement heuristic, not a map hack: no terrain or game
    state is altered. Placement failures are recorded and fed back to Jev.
    Points are footprint centers. A Barracks occupies four by three tiles, so
    its center is aligned to x modulo32=0 and y modulo32=16.
    """
    if building_id not in {109, 111}:
        raise ValueError('Placement candidates support Supply Depots and Barracks')
    width, height = BUILDING_SIZE[building_id]
    centers = own_units(state, 106)
    if not centers:
        return []
    cc = centers[0]
    result = []
    for dx, dy in [(176, 96), (304, 96), (176, 192), (304, 192), (-176, 160), (48, 224)]:
        x = ((cc['x'] + dx) // 32) * 32 + (width // 2) % 32
        y = ((cc['y'] + dy) // 32) * 32 + (height // 2) % 32
        margin_x, margin_y = max(64, width // 2 + 8), max(64, height // 2 + 8)
        if not (margin_x <= x < state['map']['width_tiles'] * 32 - margin_x and
                margin_y <= y < state['map']['height_tiles'] * 32 - margin_y):
            continue
        blocked = False
        for unit in state['units']:
            if unit['owner'] != state['player_id'] and unit.get('visible') is not True:
                continue
            w, h = BUILDING_SIZE.get(unit['type_id'], (64, 64) if unit['type_id'] in {176, 177, 178, 188} else
                                     (128, 96) if 106 <= unit['type_id'] <= 173 else (24, 24))
            if abs(unit['x'] - x) < (w + width) / 2 + 8 and abs(unit['y'] - y) < (h + height) / 2 + 8:
                blocked = True
                break
        if not blocked:
            result.append({'x': x, 'y': y})
    return result


def depot_sites(state):
    """Compatibility helper for the original tutorial/depot action menu."""
    return building_sites(state, 109)


def candidates(state):
    """Construct a finite action menu with costs, targets and prerequisites."""
    actions = {'wait': {'kind': 'wait', 'label': 'Wait for current orders to make progress'}}
    workers = [u for u in own_units(state, 7) if u['completed'] and u['visible']]
    cc = own_units(state, 106)
    depots, refineries = own_units(state, 109), own_units(state, 110)
    used, total = supply(state)
    minerals = [u for u in state['units'] if u['type_id'] in {176, 177, 178}]
    geysers = [u for u in state['units'] if u['type_id'] == 188]
    ready_refineries = [u for u in refineries if u['completed']]
    for worker in workers:
        wid = worker['id']
        if worker['order_id'] in BUILDING_ORDERS:
            continue
        if minerals and worker['order_id'] not in MINERAL_ORDERS:
            target = min(minerals, key=lambda m: math.dist((m['x'], m['y']), (cc[0]['x'], cc[0]['y']) if cc else (worker['x'], worker['y'])))
            actions[f'mine_{wid}'] = {'kind': 'gather', 'unit': wid, 'target': target['id'], 'label': f'SCV {wid}: gather minerals'}
        if ready_refineries and worker['order_id'] not in GAS_ORDERS:
            actions[f'gas_{wid}'] = {'kind': 'gather', 'unit': wid, 'target': ready_refineries[0]['id'], 'label': f'SCV {wid}: gather gas at refinery'}
    for center in cc:
        if state['minerals'] >= 50 and used < total and not center['build_queue'] and len(own_units(state, 7)) < 8:
            actions['train_scv'] = {'kind': 'train', 'unit': center['id'], 'label': 'Train one SCV (50 minerals)'}
    available = [w for w in workers if w['order_id'] not in BUILDING_ORDERS]
    if available and state['minerals'] >= 100:
        # Select a nearby worker to implement the model's build decision. This
        # deterministic target selection is disclosed as part of the harness.
        if len(depots) < 3:
            for index, point in enumerate(depot_sites(state)[:2]):
                worker = min(available, key=lambda w: math.dist((w['x'], w['y']), (point['x'], point['y'])))
                actions[f'depot_{index}'] = {'kind': 'build', 'unit': worker['id'], 'building': 109, 'point': point,
                                           'label': f'Build supply depot at ({point["x"]},{point["y"]}) (100 minerals)'}
        if not refineries and geysers:
            target = geysers[0]
            worker = min(available, key=lambda w: math.dist((w['x'], w['y']), (target['x'], target['y'])))
            actions['build_refinery'] = {'kind': 'build', 'unit': worker['id'], 'building': 110,
                                         'point': {'x': target['x'], 'y': target['y']}, 'label': 'Build refinery on visible geyser (100 minerals)'}
    return actions


def request_for(state, actions, history):
    compact_units = [{k: u[k] for k in ('id', 'type', 'owner', 'x', 'y', 'hp', 'completed', 'order_id', 'build_queue', 'remaining_build_time')} for u in state['units'] if u['type_id'] != 0]
    workers = own_units(state, 7)
    missing_depots = max(0, 3 - len(own_units(state, 109)))
    missing_refineries = max(0, 1 - len(own_units(state, 110)))
    mineral_cost = 100 * (missing_depots + missing_refineries)
    for unit in compact_units:
        if unit['type'] == 'SCV':
            unit['current_job'] = ('being trained' if not unit['completed'] else
                                   'constructing' if unit['order_id'] in BUILDING_ORDERS else
                                   'gathering gas' if unit['order_id'] in GAS_ORDERS else
                                   'gathering minerals' if unit['order_id'] in MINERAL_ORDERS else 'idle')
    model_state = {
        'game': 'Original StarCraft shareware; Boot Camp tutorial mission',
        'objective': 'Own three completed supply depots and one completed refinery, and accumulate 100 gas. One depot exists at the start.',
        'resources': {'minerals': state['minerals'], 'gas': state['gas'], 'supply_used': supply(state)[0], 'supply_total': supply(state)[1]},
        'frame': state['frame'], 'player_id': state['player_id'], 'units': compact_units,
        'progress': state['objective_progress'],
        'remaining_work': {
            'supply_depots_still_to_start': missing_depots,
            'refineries_still_to_start': missing_refineries,
            'minerals_needed_for_remaining_buildings': mineral_cost,
            'mineral_shortfall': max(0, mineral_cost - state['minerals']),
            'additional_gas_needed': max(0, 100 - state['gas']),
            'workers_collecting_minerals': sum(w['order_id'] in MINERAL_ORDERS for w in workers),
            'workers_collecting_gas': sum(w['order_id'] in GAS_ORDERS for w in workers),
            'workers_constructing': sum(w['order_id'] in BUILDING_ORDERS for w in workers),
        },
        'rules': 'SCVs gather minerals and gas, and build structures. SCVs cost50 minerals; depot/refinery each cost100. More SCVs accelerate economy. A constructing SCV must keep building. A refinery needs assigned SCVs to gather gas. Mining orders85-90; gas orders81-84; idle1-3; constructing33. Commands persist until complete or replaced. No combat is needed in this tutorial.',
        'idle_workers': [u['id'] for u in state['units'] if u['type_id'] == 7 and u['owner'] == state['player_id'] and u['completed'] and u['order_id'] in IDLE],
        'recent_actions': [event for event in history if event['command'] != 'Wait for current orders to make progress'][-6:],
    }
    questions = {'action': {
        'type': 'choice',
        'instructions': 'Choose the next command that advances the UNFINISHED StarCraft mission objectives. Check remaining_work and the current_job of each worker. Continuing to collect a resource whose remaining need is zero does not advance the mission. Waiting is appropriate only when existing orders advance an unfinished objective. Reassign workers when resources needed by unfinished objectives are not being collected. Idle SCVs produce nothing. History reports previous commands, but current_job is authoritative: a later command replaces the previous one. A small workforce helps gather resources and construct buildings concurrently.',
        'criteria': {key: action['label'] for key, action in actions.items()},
    }}
    return model_state, questions


class InputAdapter:
    """Execute a selected command through the game's normal input path."""
    def __init__(self, bridge, on_frame=None):
        self.bridge = bridge
        self.on_frame = on_frame
        self.inputs = []
        self.map_size = (64, 64)

    def _settle(self, seconds=.2):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            time.sleep(min(.08, max(0, until - time.monotonic())))
            if self.on_frame:
                self.on_frame()

    def _input(self, command, *args):
        result = self._queue_input(command, *args)
        self._settle()
        return result

    def _queue_input(self, command, *args):
        """Queue input without advancing a paused coordinate snapshot."""
        self.inputs.append({'command': command, 'args': list(args)})
        result = self.bridge.rpc(command, *args)
        if isinstance(result, dict) and result.get('ok') is False:
            raise RuntimeError('Original game input was rejected by the runtime')
        return result

    def camera(self):
        x, y = struct.unpack('<II', self.bridge.read_memory(CAMERA, 8))
        return {'x': x, 'y': y}

    def focus(self, x, y, *, margin_x=24, margin_y=28):
        point = game_to_screen(x, y, self.camera(), height=312)
        if point and margin_x <= point['x'] <= 639 - margin_x and margin_y <= point['y'] <= 311 - margin_y:
            return point
        # Original minimap zoom is a power of two:64x64 uses2px/tile,
        # Strongarm's96x64 uses1px/tile, centered in the128px panel.
        w, h = self.map_size
        pixels_per_tile = 2 ** math.floor(math.log2(128 / max(w, h)))
        scale = 32 / pixels_per_tile
        for _ in range(2):
            self._input('clickHold', round(6 + (128 - w * 32 / scale) / 2 + x / scale),
                        round(348 + (128 - h * 32 / scale) / 2 + y / scale), 100, 0)
            point = game_to_screen(x, y, self.camera(), height=312)
            if point is not None:
                return point
        raise CommandUnavailable('Camera did not expose the selected target; re-observe before issuing another command')

    def _select_squad(self, state, action):
        """Select every available requested unit, checking each ordinary click.

        Shift-down and the click are queued while paused. Refresh coordinates
        after queuing Shift, then resume only after queuing the click. Selection
        uses a1ms tap: the original UI handles queued mouse events, and a100ms
        hold lets a moving unit leave the hit point before mouse-up selection.
        """
        self.bridge.pause()
        actual = read_selection(self.bridge.read_memory)
        requested = set(action['units'])

        def available_units(snapshot):
            return {u['id']: u for u in snapshot['units'] if u['id'] in requested
                    and u['owner'] == state['player_id'] and u['completed'] and u['visible']}

        fresh = read_state(self.bridge.read_memory)
        available = available_units(fresh)
        if actual and set(actual) == set(available):
            return {'selected_units': actual, 'available_requested_units': sorted(available),
                    'selection_method': 'already_selected'}
        checks = []
        # A missed/toggled click may be retried once, but never accepted as a
        # smaller squad while other requested units remain alive and visible.
        for _ in range(2):
            for unit_id in action['units'][:12]:
                self.bridge.pause()
                fresh = read_state(self.bridge.read_memory)
                available = available_units(fresh)
                actual = read_selection(self.bridge.read_memory)
                actor = available.get(unit_id)
                if actor is None or (unit_id in actual and set(actual).issubset(requested)):
                    continue
                self.bridge.resume()
                self.focus(actor['x'], actor['y'])
                self.bridge.pause()
                actual = read_selection(self.bridge.read_memory)
                if unit_id in actual and set(actual).issubset(requested):
                    continue
                extend = bool(actual) and set(actual).issubset(requested)
                if extend:
                    self._queue_input('key', 'shift', {'down': True})
                try:
                    # Shift may enqueue work, but the guest has not advanced.
                    # Read again here, immediately before queuing the click.
                    fresh = read_state(self.bridge.read_memory)
                    actor = available_units(fresh).get(unit_id)
                    point = game_to_screen(actor['x'], actor['y'], fresh['camera'], height=312) if actor else None
                    if point is None:
                        continue
                    self._queue_input('clickHold', point['x'], point['y'], 1, 0)
                    self.bridge.resume()
                    self._settle()
                finally:
                    self.bridge.pause()
                    if extend:
                        self._queue_input('key', 'shift', {'up': True})
                actual = read_selection(self.bridge.read_memory)
                checks.append({'clicked_unit': unit_id, 'selected_units': list(actual)})
                if not set(actual).issubset(requested):
                    return {'issued': False, 'inputs': list(self.inputs),
                            'reason': 'Observed selection contains an unrequested unit',
                            'selected_units': actual, 'available_requested_units': sorted(available),
                            'selection_checks': checks}
            fresh = read_state(self.bridge.read_memory)
            available = available_units(fresh)
            actual = read_selection(self.bridge.read_memory)
            if actual and set(actual) == set(available):
                return {'selected_units': actual, 'available_requested_units': sorted(available),
                        'selection_method': 'shift_click', 'selection_checks': checks}
        return {'issued': False, 'inputs': list(self.inputs),
                'reason': 'Could not select every surviving visible requested unit',
                'selected_units': actual, 'available_requested_units': sorted(available),
                'missing_units': sorted(set(available) - set(actual)),
                'selection_checks': checks}

    def _squad(self, state, action):
        """Apply the model's command to a verified selection."""
        selection = self._select_squad(state, action)
        if selection.get('issued') is False:
            return selection
        actual = selection['selected_units']
        self.bridge.resume()
        target = action['point']
        if action['kind'] == 'attack_target':
            self.bridge.pause()
            fresh = read_state(self.bridge.read_memory)
            enemy = next((u for u in fresh['units'] if u['id'] == action['target'] and u['visible']
                          and u['owner'] in fresh.get('enemy_players', [])), None)
            self.bridge.resume()
            if enemy:
                target = {'x': enemy['x'], 'y': enemy['y']}
            else:
                return {'issued': False, 'inputs': list(self.inputs), 'reason': 'Focus target is no longer visible', 'selected_units': actual}
        point = self.focus(**target)
        self._input('keyHold', 'a' if action['kind'] in {'attack_move', 'attack_target', 'explore'} else 'm', 60)
        self._input('clickHold', point['x'], point['y'], 100, 0)
        self._input('move', 320, 280)
        return {'issued': True, 'inputs': list(self.inputs), **selection}

    def execute(self, state, action):
        try:
            return self._execute(state, action)
        except CommandUnavailable as error:
            self.bridge.pause()
            return {'issued': False, 'inputs': list(self.inputs), 'reason': str(error)}

    def _execute(self, state, action):
        self.inputs = []
        self.map_size = (state['map']['width_tiles'], state['map']['height_tiles'])
        if action['kind'] in {'wait', 'continue'}:
            return {'issued': True, 'inputs': []}
        if action['kind'] in SQUAD_ORDERS:
            try:
                result = self._squad(state, action)
            finally:
                try:
                    event = {'command': 'key', 'args': ['shift', {'up': True}]}
                    self.inputs.append(event)
                    released = self.bridge.rpc(event['command'], *event['args'])
                    if isinstance(released, dict) and released.get('ok') is False:
                        raise RuntimeError('Original game modifier cleanup was rejected by the runtime')
                finally:
                    self.bridge.pause()
            # Include finally's ordinary key release in every returned trace.
            result['inputs'] = list(self.inputs)
            return result
        units = {u['id']: u for u in state['units']}
        unit = units[action['unit']]
        self.bridge.resume()
        try:
            # Escape is not a harmless reset: on a selected CC it cancels the
            # training queue, and on an SCV it can stop construction.
            point = self.focus(unit['x'], unit['y'])
            # Motion continues while a camera is moving. Freeze and refresh the
            # actor, then queue its click before resuming the emulator.
            self.bridge.pause()
            fresh = read_state(self.bridge.read_memory)
            actor = next((u for u in fresh['units'] if u['id'] == unit['id']), None)
            if not actor or not actor['visible'] or not actor['completed']:
                return {'issued': False, 'inputs': list(self.inputs), 'reason': 'Actor became unavailable'}
            point = game_to_screen(actor['x'], actor['y'], fresh['camera'], height=312)
            if point is None:
                return {'issued': False, 'inputs': list(self.inputs), 'reason': 'Actor left the viewport'}
            self.inputs.append({'command': 'clickHold', 'args': [point['x'], point['y'], 100, 0]})
            selection = self.bridge.rpc('clickHold', point['x'], point['y'], 100, 0)
            if isinstance(selection, dict) and selection.get('ok') is False:
                raise RuntimeError('Original game unit selection was rejected by the runtime')
            self.bridge.resume()
            self._settle()
            if action['kind'] == 'train':
                self._input('keyHold', {7: 's', 0: 'm', 32: 'f'}[action.get('train_type', 7)], 60)
            elif action['kind'] == 'gather':
                target = units[action['target']]
                point = self.focus(target['x'], target['y'])
                self._input('clickHold', point['x'], point['y'], 100, 1)
            elif action['kind'] == 'build':
                w, h = BUILDING_SIZE[action['building']]
                point = self.focus(**action['point'], margin_x=w // 2 + 24, margin_y=h // 2 + 28)
                self._input('keyHold', 'b', 60)
                self._input('keyHold', {109: 's', 110: 'r', 111: 'b'}[action['building']], 60)
                # The original shareware placement cursor denotes the upper-left
                # tile, whereas CUnit coordinates denote the footprint center.
                px, py = point['x'] - w // 2, point['y'] - h // 2
                if px < 0 or py < 20:
                    raise CommandUnavailable('Building footprint is outside the safe placement viewport')
                self._input('move', px, py)
                self._input('clickHold', px, py, 100, 0)
            else:
                raise ValueError('Unsupported action')
            self._input('move', 320, 280)  # Do not leave the pointer on a scroll edge.
            return {'issued': True, 'inputs': list(self.inputs)}
        finally:
            self.bridge.pause()
