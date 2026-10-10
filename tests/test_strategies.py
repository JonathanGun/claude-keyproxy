import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from claude_keyproxy import keypool, strategies


class StrategiesTest(unittest.TestCase):
    def test_loads_a_strategy_by_file_name(self):
        self.assertIn("priority", strategies.names())
        choose = strategies.load("priority")
        keys = [keypool.Key("b", "t", 2), keypool.Key("a", "t", 1)]
        self.assertEqual([k.name for k in choose(keys, keypool.Cooldowns())], ["a", "b"])

    def test_refuses_an_unknown_strategy(self):
        with self.assertRaises(ValueError) as ctx:
            strategies.load("nope")
        self.assertIn("priority", str(ctx.exception))

    def test_tries_the_soonest_key_when_every_key_is_cooling_down(self):
        cooldowns = keypool.Cooldowns(clock=lambda: 0.0)
        cooldowns.until = {"a": 600.0, "b": 60.0}
        keys = [keypool.Key("a", "t", 1), keypool.Key("b", "t", 2)]
        self.assertEqual([k.name for k in strategies.load("priority")(keys, cooldowns)], ["b"])


if __name__ == "__main__":
    unittest.main()
