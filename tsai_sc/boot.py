"""Navigate a fresh isolated demo profile to the original Boot Camp mission."""
from __future__ import annotations
import argparse
from .engine import BottleShipBridge
from .game import read_state


def boot(bridge: BottleShipBridge, name: str = "Jev"):
    """The startup sequence is UI navigation, with no mission-state memory writes."""
    if not name.isascii() or not name.isalnum() or not 1 <= len(name) <= 12:
        raise ValueError("Player name must have 1–12 ASCII letters/numbers")
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
    bridge.rpc('clickAt', 542, 388)  # Start Boot Camp, preserving the tutorial.
    bridge.rpc('sleep', 10000)
    bridge.rpc('clickAt', 155, 230)  # Disable first-run tips.
    bridge.rpc('clickAt', 200, 262)  # Dismiss the tips dialog.
    bridge.rpc('sleep', 2000)
    bridge.pause()
    return _verify_initial(bridge)


def _verify_initial(bridge: BottleShipBridge):
    state = read_state(bridge.read_memory)
    own = [unit for unit in state['units']
           if unit['owner'] == state['player_id'] and unit['completed']]
    counts = {type_id: sum(unit['type_id'] == type_id for unit in own)
              for type_id in (7, 106, 109, 110)}
    if (state['status'] != 'running' or state['player_id'] != 6
            or state['minerals'] != 150 or state['gas'] != 0
            or state['supply']['used'] != 17 or state['supply']['available'] != 18
            or counts != {7: 1, 106: 1, 109: 1, 110: 0}):
        raise RuntimeError('Boot Camp initial-state verification failed; capture the game and inspect it before continuing')
    return {'mission': 'Boot Camp', 'player': state['player_id'],
            'minerals': state['minerals'], 'gas': state['gas'],
            'supply_used': state['supply']['used'], 'supply_total': state['supply']['available'],
            'scvs': counts[7], 'supply_depots': counts[109], 'paused': True}


def restart(bridge: BottleShipBridge):
    """Restart the current Boot Camp through its original in-game menus.

    Call after stopping the controller. Only ordinary input is sent, then the
    untouched mission's initial resources, supply and unit counts are checked.
    """
    bridge.resume()
    try:
        bridge.rpc('keyHold', 'f10', 60)
        bridge.rpc('sleep', 400)
        bridge.rpc('clickHold', 310, 250, 100, 0)  # End Mission.
        bridge.rpc('sleep', 400)
        bridge.rpc('clickHold', 310, 114, 100, 0)  # Restart Mission.
        bridge.rpc('sleep', 400)
        bridge.rpc('clickHold', 310, 150, 100, 0)  # Confirm restart.
        bridge.rpc('sleep', 6000)
    finally:
        bridge.pause()
    return _verify_initial(bridge)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--name', default='Jev')
    parser.add_argument('--restart', action='store_true', help='Restart the current mission using its in-game menus')
    parser.add_argument('--screenshot', default='.runtime/boot-camp.png')
    args = parser.parse_args()
    bridge = BottleShipBridge()
    print(restart(bridge) if args.restart else boot(bridge, args.name))
    bridge.capture(args.screenshot)
    print(f'Original game is paused. Screenshot: {args.screenshot}')


if __name__ == '__main__':
    main()
