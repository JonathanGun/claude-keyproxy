import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from claude_keyproxy import keypool


class FakeResponse:
    def __init__(self, headers):
        self.headers = headers

    def getheader(self, name):
        return self.headers.get(name)


class Clock:
    """A clock the test moves by hand."""

    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


class CooldownsTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.cooldowns = keypool.Cooldowns(self.clock)

    def test_uses_the_reset_header(self):
        reset = int(self.clock.now) + 900
        wait = self.cooldowns.cool("work", FakeResponse({"anthropic-ratelimit-unified-reset": str(reset)}), 300)
        self.assertEqual(wait, 900)

    def test_uses_retry_after_when_there_is_no_reset(self):
        self.assertEqual(self.cooldowns.cool("work", FakeResponse({"retry-after": "120"}), 300), 120)

    def test_falls_back_to_the_default_and_comes_back_after_it(self):
        self.assertEqual(self.cooldowns.cool("work", FakeResponse({}), 300), 300)
        self.assertEqual(self.cooldowns.status(), {"work": 300})
        self.clock.now += 299
        self.assertEqual(self.cooldowns.left("work"), 1)
        self.clock.now += 1
        self.assertEqual((self.cooldowns.left("work"), self.cooldowns.status()), (0, {}))

    def test_reset_clears_every_key(self):
        self.cooldowns.cool("work", FakeResponse({}), 300)
        self.cooldowns.reset()
        self.assertEqual(self.cooldowns.status(), {})


class KeyPoolTest(unittest.TestCase):
    def test_a_key_never_shows_its_token(self):
        key = keypool.Key("work", "sk-ant-oat01-secretsecret1234", 1)
        self.assertEqual(repr(key), "Key(name='work', token='...1234', priority=1)")
        self.assertNotIn("secret", f"{key} {[key]}")

    def test_runs_before_any_key_is_added(self):
        pool = keypool.KeyPool("/nonexistent/index.json")
        self.assertEqual(pool.load(), [])
        self.assertEqual(pool.candidates(), [])


    def test_skips_a_key_whose_file_is_gone(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "work.key"), "w") as f:
                f.write("token-work\n")
            index = os.path.join(tmp, "index.json")
            with open(index, "w") as f:
                json.dump({"keys": [{"priority": 1, "name": "work", "path": os.path.join(tmp, "work.key")},
                                    {"priority": 2, "name": "gone", "path": os.path.join(tmp, "gone.key")}]}, f)
            self.assertEqual(keypool.KeyPool(index).load(), [keypool.Key("work", "token-work", 1)])

    def test_parse_usage_without_headers_or_with_a_missing_window(self):
        self.assertIsNone(keypool.parse_usage({}.get))
        usage = keypool.parse_usage({"anthropic-ratelimit-unified-status": "rejected"}.get)
        self.assertEqual(usage, {"status": "rejected", "5h": None, "7d": None})


if __name__ == "__main__":
    unittest.main()
