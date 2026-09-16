"""Enter or restart an original StarCraft demo mission through normal game UI."""
from __future__ import annotations
import argparse
from contextlib import contextmanager
from .engine import BottleShipBridge
from .game import GameStateError, read_state


INITIAL = {
    'boot_camp': {'name': 'Boot Camp', 'minerals': 150, 'gas': 0, 'supply': (17, 18),
                  'counts': {0: 16, 7: 1, 106: 1, 109: 1, 110: 0, 111: 0, 122: 0}},
    'strongarm': {'name': 'Strongarm', 'minerals': 250, 'gas': 200, 'supply': (12, 42),
                 'counts': {0: 8, 7: 4, 106: 1, 109: 4, 110: 1, 111: 2, 122: 1}},
}


def _mission(mission):
    if mission not in INITIAL:
        raise ValueError('Choose boot_camp or strongarm')
    return INITIAL[mission]


@contextmanager
def _park_after(bridge):
    try:
        yield
    except BaseException:
        try:
            bridge.pause()
        except Exception:
            pass  # Preserve the original input/transport error.
        raise
    else:
        bridge.pause()


def boot(bridge: BottleShipBridge, name: str = 'Jev', mission: str = 'strongarm'):
    """Navigate a fresh profile; the original Skip Tutorial button enters Strongarm."""
    _mission(mission)
    if not name.isascii() or not name.isalnum() or not 1 <= len(name) <= 12:
        raise ValueError('Player name must have 1–12 ASCII letters/numbers')
    with _park_after(bridge):
        result = bridge._request('/boot', {})
        if not result.get('loaded'):
            raise RuntimeError('Original demo executable did not finish loading')
        bridge.rpc('sleep', 5000)
        bridge.rpc('clickAt', 214, 120)  # Single Player.
        bridge.rpc('sleep', 1200)
        bridge.rpc('type', name)
        bridge.rpc('key', 'enter')
        bridge.rpc('sleep', 1200)
        bridge.rpc('key', 'enter')  # Accept the player / campaign.
        bridge.rpc('sleep', 1200)
        bridge.rpc('key', 'enter')  # Leave the campaign introduction.
        bridge.rpc('sleep', 7000)
        if mission == 'strongarm':
            bridge.rpc('clickHold', 150, 442, 100, 0)  # Original Skip Tutorial button.
            bridge.rpc('sleep', 4000)
            bridge.rpc('keyHold', 'enter', 60)  # Leave the Strongarm chapter title.
            bridge.rpc('sleep', 2500)
            # Title cards use wall time: a slow host may have already reached
            # the briefing when Enter was pressed. Check before clicking Start.
            bridge.pause()
            try:
                loaded = read_state(bridge.read_memory)['mission_id'] == mission
            except GameStateError:
                loaded = False
            bridge.resume()
            if not loaded:
                bridge.rpc('clickHold', 542, 388, 100, 0)  # Start Strongarm briefing.

        else:
            bridge.rpc('clickAt', 542, 388)  # Start Boot Camp.
        bridge.rpc('sleep', 10000)
        bridge.rpc('clickAt', 155, 230)  # Disable first-run tips.
        bridge.rpc('clickAt', 200, 262)  # Dismiss the tips dialog.
        bridge.rpc('sleep', 2000)
    return _verify_initial(bridge, mission)


def _verify_initial(bridge: BottleShipBridge, mission: str):
    expected = _mission(mission)
    state = read_state(bridge.read_memory)
    own = [unit for unit in state['units']
           if unit['owner'] == state['player_id'] and unit['completed']]
    counts = {kind: sum(unit['type_id'] == kind for unit in own)
              for kind in expected['counts']}
    if (state['status'] != 'running' or state['mission_id'] != mission or state['player_id'] != 6
            or state['minerals'] != expected['minerals'] or state['gas'] != expected['gas']
            or (state['supply']['used'], state['supply']['available']) != expected['supply']
            or counts != expected['counts']):
        raise RuntimeError(f'{expected["name"]} initial-state verification failed; capture the game and inspect it before continuing')
    return {'mission': expected['name'], 'mission_id': mission, 'player': state['player_id'],
            'minerals': state['minerals'], 'gas': state['gas'],
            'supply_used': state['supply']['used'], 'supply_total': state['supply']['available'],
            'scvs': counts[7], 'marines': counts[0], 'supply_depots': counts[109],
            'barracks': counts[111], 'paused': True}


def restart(bridge: BottleShipBridge, mission: str | None = None):
    """Restart the active mission using F10, End Mission, Restart and confirmation.

    Stop the controller before calling this function. A supplied mission must
    match the current mission; restarting never selects another scenario.
    """
    bridge.pause()
    current = read_state(bridge.read_memory)['mission_id']
    mission = current if mission is None else mission
    _mission(mission)
    if mission != current:
        raise ValueError('Restart cannot change the current mission; use a fresh boot to select another mission')
    with _park_after(bridge):
        bridge.resume()
        bridge.rpc('keyHold', 'f10', 60)
        bridge.rpc('sleep', 400)
        bridge.rpc('clickHold', 310, 250, 100, 0)  # End Mission.
        bridge.rpc('sleep', 400)
        bridge.rpc('clickHold', 310, 114, 100, 0)  # Restart Mission.
        bridge.rpc('sleep', 400)
        bridge.rpc('clickHold', 310, 150, 100, 0)  # Confirm restart.
        bridge.rpc('sleep', 6000)
    return _verify_initial(bridge, mission)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--name', default='Jev')
    parser.add_argument('--mission', choices=tuple(INITIAL), help='Fresh boot defaults to strongarm; restart defaults to the current mission')
    parser.add_argument('--restart', action='store_true', help='Restart the current mission using its in-game menus')
    parser.add_argument('--screenshot', default='.runtime/mission-start.png')
    args = parser.parse_args()
    bridge = BottleShipBridge()
    result = restart(bridge, args.mission) if args.restart else boot(bridge, args.name, args.mission or 'strongarm')
    print(result)
    bridge.capture(args.screenshot)
    print(f'Original game is paused. Screenshot: {args.screenshot}')


if __name__ == '__main__':
    main()
