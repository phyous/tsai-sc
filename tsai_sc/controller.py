"""Bounded Jev decisions translated to ordinary StarCraft keyboard/mouse input.

The adapter supplies targets and placement candidates, never a policy fallback.
Only the selected model choice is executed. Guest memory is strictly read-only.
"""
from __future__ import annotations

import math
import struct
import time
from .game import CAMERA, game_to_screen, read_state

BUILDING_SIZE = {106: (128, 96), 109: (96, 64), 110: (128, 64)}
IDLE = {1, 2, 3}
BUILDING_ORDERS = {30, 33, 34, 35}
GAS_ORDERS = {81, 82, 83, 84}
MINERAL_ORDERS = {79, 80, 85, 86, 87, 88, 89, 90}


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
    if kind == 'wait':
        return {'accepted': True, 'evidence': 'No new command requested'}
    units = {u['id']: u for u in after['units']}
    unit = units.get(action['unit'])
    if unit is None:
        return {'accepted': False, 'evidence': 'Actor no longer observable'}
    if kind == 'train':
        accepted = 7 in unit['build_queue'] or len(own_units(after, 7)) > len(own_units(before, 7))
    elif kind == 'build':
        started = any(math.dist((u['x'], u['y']), (action['point']['x'], action['point']['y'])) < 24 for u in own_units(after, action['building']))
        accepted = started or unit['order_id'] in {30, 33}
    elif kind == 'gather':
        target = next(u for u in before['units'] if u['id'] == action['target'])
        accepted = unit['order_id'] in (GAS_ORDERS if target['type_id'] == 110 else MINERAL_ORDERS)
    else:
        accepted = False
    return {'accepted': accepted, 'evidence': 'Observed actor order and production state', 'actor_order_id': unit['order_id']}


def depot_sites(state):
    """Offer open tile-aligned candidates; the original engine validates terrain.

    This is a compact placement heuristic, not a map hack: no terrain or game
    state is altered. Placement failures are recorded and fed back to Jev.
    """
    centers = own_units(state, 106)
    if not centers:
        return []
    cc = centers[0]
    result = []
    for dx, dy in [(176, 96), (304, 96), (176, 192), (304, 192), (-176, 160), (48, 224)]:
        x = ((cc['x'] + dx) // 32) * 32 + 16
        y = ((cc['y'] + dy) // 32) * 32
        if not (64 <= x < state['map']['width_tiles'] * 32 - 64 and 64 <= y < state['map']['height_tiles'] * 32 - 64):
            continue
        blocked = False
        for unit in state['units']:
            w, h = BUILDING_SIZE.get(unit['type_id'], (64, 64) if unit['type_id'] in {176, 177, 178, 188} else (24, 24))
            if abs(unit['x'] - x) < (w + 96) / 2 + 8 and abs(unit['y'] - y) < (h + 64) / 2 + 8:
                blocked = True
                break
        if not blocked:
            result.append({'x': x, 'y': y})
    return result


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

    def _settle(self, seconds=.2):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            time.sleep(min(.08, max(0, until - time.monotonic())))
            if self.on_frame:
                self.on_frame()

    def _input(self, command, *args):
        self.inputs.append({'command': command, 'args': list(args)})
        result = self.bridge.rpc(command, *args)
        if isinstance(result, dict) and result.get('ok') is False:
            raise RuntimeError('Original game input was rejected by the runtime')
        self._settle()
        return result

    def camera(self):
        x, y = struct.unpack('<II', self.bridge.read_memory(CAMERA, 8))
        return {'x': x, 'y': y}

    def focus(self, x, y, *, margin_x=24, margin_y=28):
        point = game_to_screen(x, y, self.camera(), height=312)
        if point and margin_x <= point['x'] <= 639 - margin_x and margin_y <= point['y'] <= 311 - margin_y:
            return point
        # Boot Camp is64x64 tiles. The stock640x480 minimap starts at(6,348).
        self._input('clickHold', round(6 + x / 16), round(348 + y / 16), 100, 0)
        point = game_to_screen(x, y, self.camera(), height=312)
        if point is None:
            raise RuntimeError('Could not place selected target inside the game viewport')
        return point

    def execute(self, state, action):
        self.inputs = []
        if action['kind'] == 'wait':
            return {'issued': True, 'inputs': []}
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
                self._input('keyHold', 's', 60)
            elif action['kind'] == 'gather':
                target = units[action['target']]
                point = self.focus(target['x'], target['y'])
                self._input('clickHold', point['x'], point['y'], 100, 1)
            elif action['kind'] == 'build':
                w, h = BUILDING_SIZE[action['building']]
                point = self.focus(**action['point'], margin_x=w // 2 + 24, margin_y=h // 2 + 28)
                self._input('keyHold', 'b', 60)
                self._input('keyHold', {109: 's', 110: 'r'}[action['building']], 60)
                # The original shareware placement cursor denotes the upper-left
                # tile, whereas CUnit coordinates denote the footprint center.
                px, py = point['x'] - w // 2, point['y'] - h // 2
                if px < 0 or py < 20:
                    raise RuntimeError('Building footprint is outside the safe placement viewport')
                self._input('move', px, py)
                self._input('clickHold', px, py, 100, 0)
            else:
                raise ValueError('Unsupported action')
            self._input('move', 320, 280)  # Do not leave the pointer on a scroll edge.
            return {'issued': True, 'inputs': list(self.inputs)}
        finally:
            self.bridge.pause()
