"""Input-only startup contracts and rejection of noninitial mission snapshots."""
from copy import deepcopy
import unittest
from unittest.mock import Mock, call, patch

from tsai_sc.boot import boot, restart, _verify_initial
from tsai_sc.game import GameStateError


def fixture(mission='strongarm'):
    if mission == 'strongarm':
        minerals, gas, supply, counts = 250, 200, (12, 42), {0:8,7:4,106:1,109:4,110:1,111:2,122:1}
    else:
        minerals, gas, supply, counts = 150, 0, (17, 18), {0:16,7:1,106:1,109:1}
    return {'status':'running', 'mission_id':mission, 'player_id':6,
            'minerals':minerals, 'gas':gas, 'supply':{'used':supply[0],'available':supply[1]},
            'units':[{'owner':6,'completed':True,'type_id':kind}
                     for kind, count in counts.items() for _ in range(count)]}


class BootTests(unittest.TestCase):
    def test_restart_infers_mission_and_uses_only_normal_menu_input(self):
        for mission in ('strongarm','boot_camp'):
            bridge=Mock()
            with self.subTest(mission=mission), patch('tsai_sc.boot.read_state',return_value=fixture(mission)):
                result=restart(bridge)
            self.assertEqual(result['mission_id'],mission)
            self.assertTrue(result['paused'])
            self.assertEqual(bridge.rpc.call_args_list,[
                call('keyHold','f10',60), call('sleep',400),
                call('clickHold',310,250,100,0), call('sleep',400),
                call('clickHold',310,114,100,0), call('sleep',400),
                call('clickHold',310,150,100,0), call('sleep',6000),
            ])
            self.assertEqual(bridge.pause.call_count,2)
            bridge.resume.assert_called_once()
            bridge._request.assert_not_called()

    def test_restart_refuses_to_switch_missions(self):
        bridge=Mock()
        with patch('tsai_sc.boot.read_state',return_value=fixture('boot_camp')):
            with self.assertRaisesRegex(ValueError,'cannot change'):
                restart(bridge,'strongarm')
        bridge.resume.assert_not_called()
        bridge.rpc.assert_not_called()

    def test_noninitial_state_rejected(self):
        for field,value in [('minerals',249),('gas',208),('status','victory'),('mission_id','boot_camp')]:
            state=fixture();state[field]=value
            with self.subTest(field=field), patch('tsai_sc.boot.read_state',return_value=state):
                with self.assertRaisesRegex(RuntimeError,'initial-state'):
                    _verify_initial(Mock(),'strongarm')
        for mutate in ('supply','extra_worker','missing_marine'):
            state=fixture()
            if mutate=='supply':state['supply']['used']=13
            elif mutate=='extra_worker':state['units'].append({'owner':6,'completed':True,'type_id':7})
            else:state['units'].pop(0)
            with self.subTest(mutate=mutate), patch('tsai_sc.boot.read_state',return_value=state):
                with self.assertRaises(RuntimeError):_verify_initial(Mock(),'strongarm')

    def test_fresh_strongarm_uses_skip_tutorial_and_validates(self):
        bridge=Mock();bridge._request.return_value={'loaded':True}
        with patch('tsai_sc.boot.read_state',side_effect=[GameStateError('briefing'), fixture()]):
            result=boot(bridge,mission='strongarm')
        self.assertEqual(result['marines'],8)
        self.assertIn(call('clickHold',150,442,100,0),bridge.rpc.call_args_list)
        self.assertNotIn(call('clickAt',542,388),bridge.rpc.call_args_list)
        self.assertIn(call('clickHold',542,388,100,0),bridge.rpc.call_args_list)
        self.assertEqual(bridge.pause.call_count,2)

    def test_fresh_boot_does_not_click_start_in_already_loaded_game(self):
        bridge=Mock();bridge._request.return_value={'loaded':True}
        with patch('tsai_sc.boot.read_state',return_value=fixture()):
            boot(bridge,mission='strongarm')
        self.assertNotIn(call('clickHold',542,388,100,0),bridge.rpc.call_args_list)

    def test_bad_name_or_mission_sends_no_input(self):
        bridge=Mock()
        for name,mission in [('a b','strongarm'),('Jev','unknown')]:
            with self.assertRaises(ValueError):boot(bridge,name,mission)
        self.assertEqual(bridge.mock_calls,[])

    def test_input_error_parks_and_keeps_original_error(self):
        bridge=Mock();bridge.rpc.side_effect=RuntimeError('input transport failed')
        bridge.pause.side_effect=[None,RuntimeError('cleanup transport failed')]
        with patch('tsai_sc.boot.read_state',return_value=fixture()):
            with self.assertRaisesRegex(RuntimeError,'input transport failed'):
                restart(bridge)
        self.assertEqual(bridge.pause.call_count,2)


if __name__=='__main__':
    unittest.main()
