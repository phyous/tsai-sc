import struct
import unittest

from tsai_sc.game import (
    BUILD_SIGNATURE, BUILD_SIGNATURE_ADDRESS, CAMERA, GAME, GAME_MODE,
    LOCAL_PLAYER, MAP_IDENTITY, PENDING_VICTORY, TERRAN_SUPPLY, UNIT_BASE,
    UNIT_SIZE, VICTORY, GameStateError, game_to_screen, read_state,
)


class Memory:
    def __init__(self):
        self.data = bytearray(0x6A0000)
        self.data[BUILD_SIGNATURE_ADDRESS:BUILD_SIGNATURE_ADDRESS + len(BUILD_SIGNATURE)] = BUILD_SIGNATURE
        self.put(GAME_MODE, "H", 3)
        self.put(LOCAL_PLAYER, "I", 6)
        self.put(GAME + 0xE4, "HH", 64, 64)
        self.put(GAME + 0x14C, "I", 300)
        self.put(CAMERA, "II", 352, 352)
        path = b"campaign\\terranED\\tutorial"
        self.data[MAP_IDENTITY:MAP_IDENTITY + len(path)] = path
        self.put(GAME + 6 * 4, "I", 150)
        self.put(TERRAN_SUPPLY + 24, "I", 36)
        self.put(TERRAN_SUPPLY + 0x30 + 24, "I", 34)
        self.put(TERRAN_SUPPLY + 0x60 + 24, "I", 400)
        self.unit(0, 7, 6, 700, 500)

    def put(self, address, fmt, *values):
        struct.pack_into("<" + fmt, self.data, address, *values)

    def unit(self, index, kind, owner, x, y, *, visible=True, completed=True):
        address = UNIT_BASE + index * UNIT_SIZE
        sprite = 0x580000 + index * 0x40
        self.put(address + 8, "II", 60 * 256, sprite)
        self.put(address + 0x28, "HH", x, y)
        self.put(address + 0x4C, "BB", owner, 3)
        self.put(address + 0x64, "H", kind)
        self.put(address + 0xDC, "I", int(completed))
        self.put(address + 0x98, "HHHHH", 228, 228, 228, 228, 228)
        self.data[sprite + 0x0C] = 64 if visible else 0

    def read(self, address, length):
        return bytes(self.data[address:address + length])


class DecoderTests(unittest.TestCase):
    def test_fog_of_war_and_objective_counts(self):
        memory = Memory()
        memory.unit(1, 37, 0, 80, 80, visible=False)
        memory.unit(2, 176, 11, 720, 620)
        memory.unit(3, 109, 6, 900, 480)
        memory.unit(4, 109, 6, 900, 550, completed=False)
        state = read_state(memory.read)
        self.assertEqual([unit["id"] for unit in state["units"]], [0, 2, 3, 4])
        self.assertEqual(state["objective_progress"]["supply_depots"]["current"], 1)
        self.assertEqual(state["units"][0]["screen"], {"x": 348, "y": 148})
        self.assertEqual(state["minerals"], 150)
        self.assertEqual(state["supply"], {"used": 17, "available": 18})

    def test_objectives_and_pending_result_do_not_award_victory(self):
        memory = Memory()
        for index in range(1, 4):
            memory.unit(index, 109, 6, 900, 400 + index * 70)
        memory.unit(4, 110, 6, 560, 750)
        memory.put(GAME + 0x30 + 24, "I", 104)
        memory.data[PENDING_VICTORY + 6] = 3
        self.assertEqual(read_state(memory.read)["status"], "running")
        memory.data[VICTORY + 6] = 3
        self.assertEqual(read_state(memory.read)["status"], "victory")
        memory.data[VICTORY + 6] = 2
        self.assertEqual(read_state(memory.read)["status"], "defeat")

    def test_menu_and_short_reads_are_rejected(self):
        with self.assertRaises(GameStateError):
            read_state(lambda address, length: bytes(length))
        with self.assertRaises(GameStateError):
            read_state(lambda address, length: b"")

    def test_invalid_sprite_pointer_is_rejected(self):
        memory = Memory()
        memory.put(UNIT_BASE + 12, "I", 0xDEADBEEF)
        with self.assertRaises(GameStateError):
            read_state(memory.read)

    def test_stale_game_memory_in_a_menu_is_rejected(self):
        memory = Memory()
        memory.put(GAME_MODE, "H", 1)
        with self.assertRaises(GameStateError):
            read_state(memory.read)

    def test_camera_conversion_never_clicks_command_panel(self):
        self.assertIsNone(game_to_screen(700, 800, {"x": 352, "y": 352}))
        self.assertIsNone(game_to_screen(900, 680, {"x": 352, "y": 352}))
        self.assertIsNone(game_to_screen(100, 500, {"x": 352, "y": 352}))


if __name__ == "__main__":
    unittest.main()
