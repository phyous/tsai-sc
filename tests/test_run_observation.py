import unittest

from tsai_sc.run import visible_structure_sightings


class StructureMemoryBoundaryTests(unittest.TestCase):
    def test_memory_excludes_hidden_friendly_neutral_and_mobile_entities(self):
        units = [dict(id=i, type='TEST unit', type_id=kind, x=32*i, y=96,
                      visible=visible, relationship=relation, order_target={'x': 999, 'y': 999})
                 for i, kind, visible, relation in [
                     (1, 111, True, 'enemy'), (2, 106, False, 'enemy'),
                     (3, 109, True, 'owned'), (4, 109, True, 'ally'),
                     (5, 176, True, 'neutral'), (6, 0, True, 'enemy')]]
        seen = visible_structure_sightings({'frame': 120, 'units': units})
        self.assertEqual(seen, [{'id': 1, 'type': 'TEST unit', 'type_id': 111,
                                'x': 32, 'y': 96, 'last_seen_frame': 120}])
        self.assertNotIn('order_target', seen[0])


if __name__ == '__main__':
    unittest.main()
