import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from claude_keyproxy import setup_token as st

TOKEN = "sk-ant-oat01-" + "x" * 40


class ParseTest(unittest.TestCase):
    def test_finds_the_osc8_link(self):
        buf = b"\x1b]8;id=1;https://claude.ai/oauth/authorize?code=true\x1b\\sign in\x1b]8;;\x1b\\"
        self.assertEqual(st.find_link(buf), "https://claude.ai/oauth/authorize?code=true")

    def test_finds_a_plain_link(self):
        buf = b"\x1b[1mOpen https://claude.ai/oauth/authorize?x=1 \x1b[0m"
        self.assertEqual(st.find_link(buf), "https://claude.ai/oauth/authorize?x=1")

    def test_finds_the_token_only_once_it_is_complete(self):
        self.assertIsNone(st.find_token(b"Paste code here if prompted >"))
        self.assertIsNone(st.find_token(TOKEN.encode()))  # may still be arriving
        self.assertEqual(st.find_token(b"\x1b[32m" + TOKEN.encode() + b"\x1b[0m\n"), TOKEN)


class FakePty:
    """Stands in for the terminal: records what is typed, and answers with `reply`."""

    def __init__(self, reply):
        self.reply = reply
        self.typed = []
        self.closed = False
        self.buf = b""

    def write(self, data):
        self.typed.append(data)

    def read_until(self, find, timeout):
        self.buf = self.reply
        return find(self.buf)

    def close(self):
        self.closed = True


class SessionTest(unittest.TestCase):
    def session(self, reply):
        self.pty = FakePty(reply)
        session = st.Session("claude", spawn=lambda argv, env, cwd: self.pty)
        self.addCleanup(session.close)
        return session

    def test_types_the_code_then_enter_and_returns_the_token(self):
        token = self.session(TOKEN.encode() + b"\n").token("the-code-123")
        self.assertEqual((token, self.pty.typed, self.pty.closed), (TOKEN, [b"the-code-123", b"\r"], True))

    def test_a_rejected_code_says_why_and_still_closes(self):
        with self.assertRaises(st.SetupError) as ctx:
            self.session(b"OAuth error: invalid code\n").token("the-code-123")
        self.assertIn("invalid code", str(ctx.exception))
        self.assertTrue(self.pty.closed)


if __name__ == "__main__":
    unittest.main()
