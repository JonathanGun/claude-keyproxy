import os
import socket
import sys
import tempfile
import unittest

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
sys.path.insert(0, SRC)
from claude_keyproxy import cli


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class UpTest(unittest.TestCase):
    """Starts a real proxy in the background on a free port, with a throwaway index and log."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.port = free_port()
        env = {"KEYPROXY_PORT": str(self.port), "CLAUDE_KEY_INDEX": os.path.join(self.tmp.name, "index.json"),
               "XDG_CACHE_HOME": self.tmp.name, "PYTHONPATH": SRC}
        old = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        self.addCleanup(lambda: [os.environ.pop(k) if v is None else os.environ.__setitem__(k, v)
                                 for k, v in old.items()])
        self.addCleanup(cli.stop, self.port)

    def log(self):
        with open(cli.log_path()) as f:
            return f.read()

    def test_starts_once_and_restarts_with_new_options(self):
        self.assertFalse(cli.running(self.port))
        self.assertEqual(cli.up([]), 0, self.log())
        self.assertTrue(cli.running(self.port))
        self.assertEqual(cli.up(["--headless"]), 0)  # already running: the option waits for a restart
        self.assertNotIn("headless", self.log())
        self.assertEqual(cli.up(["--restart", "--headless"]), 0)
        self.assertIn("key manager off (headless)", self.log())
        cli.stop(self.port)
        self.assertFalse(cli.running(self.port))

    def test_reports_a_proxy_that_does_not_start(self):
        self.assertEqual(cli.up(["--strategy", "nope"]), 1)
        self.assertIn("invalid choice", self.log())


if __name__ == "__main__":
    unittest.main()
