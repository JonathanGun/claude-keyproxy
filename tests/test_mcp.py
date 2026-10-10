import io
import json
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from claude_keyproxy import mcp
from claude_keyproxy import server as keyproxy


class McpTest(unittest.TestCase):
    """A real proxy in a thread, and the MCP server talking to it over JSON-RPC lines."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.proxy = keyproxy.make_server(0, os.path.join(self.tmp.name, "index.json"), "http://127.0.0.1:1")
        threading.Thread(target=self.proxy.serve_forever, daemon=True).start()
        self.addCleanup(self.proxy.server_close)
        self.addCleanup(self.proxy.shutdown)
        self.api = mcp.Api(self.proxy.server_port, ensure=lambda: False)
        self.next_id = 0

    def rpc(self, method, params=None):
        self.next_id += 1
        return mcp.handle(self.api, {"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params or {}})

    def tool(self, tool_name, /, **arguments):
        result = self.rpc("tools/call", {"name": tool_name, "arguments": arguments})["result"]
        text = result["content"][0]["text"]
        return (json.loads(text) if not result["isError"] else text), result["isError"]

    def test_handshake_and_tool_list(self):
        init = self.rpc("initialize", {"protocolVersion": "2025-03-26"})["result"]
        self.assertEqual(init["protocolVersion"], "2025-03-26")
        self.assertIn("tools", init["capabilities"])
        self.assertIsNone(mcp.handle(self.api, {"jsonrpc": "2.0", "method": "notifications/initialized"}))
        names = [t["name"] for t in self.rpc("tools/list")["result"]["tools"]]
        self.assertEqual(names, ["list_keys", "add_key", "replace_token", "delete_key", "reorder_keys",
                                 "reset_cooldowns", "start_sign_in", "finish_sign_in", "cancel_sign_in"])
        self.assertEqual(self.rpc("nope")["error"]["code"], -32601)

    def test_manages_keys_without_ever_returning_a_token(self):
        self.tool("add_key", name="work", token="sk-ant-oat01-testtesttest1111")
        keys, error = self.tool("add_key", name="personal", token="sk-ant-oat01-testtesttest2222")
        self.assertFalse(error)
        self.assertEqual([(k["name"], k["last4"]) for k in keys], [("work", "1111"), ("personal", "2222")])

        self.tool("reorder_keys", order=["personal", "work"])
        self.tool("replace_token", name="work", token="sk-ant-oat01-testtesttest3333")
        self.tool("delete_key", name="personal")
        listed, _ = self.tool("list_keys")
        self.assertEqual([(k["name"], k["last4"]) for k in listed["keys"]], [("work", "3333")])
        self.assertEqual(listed["cooldowns"], {})
        self.assertNotIn("testtesttest", json.dumps(listed))

    def test_reset_cooldowns(self):
        self.proxy.pool.cooldowns.until["work"] = 9e12
        self.assertEqual(self.tool("reset_cooldowns"), ({}, False))

    def test_errors_come_back_as_tool_errors(self):
        self.assertEqual(self.tool("delete_key", name="nope"), ("no key named nope", True))
        self.assertEqual(self.tool("add_key", name="work"), ("missing argument: token", True))
        self.assertEqual(self.tool("drop_tables")[1], True)

    def test_a_proxy_that_is_down_and_will_not_start(self):
        down = mcp.Api(1, ensure=lambda: False)
        result = mcp.handle(down, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                   "params": {"name": "list_keys"}})["result"]
        self.assertTrue(result["isError"])
        self.assertIn("did not start", result["content"][0]["text"])

    def test_serves_json_rpc_lines(self):
        stdin = io.StringIO('{"jsonrpc":"2.0","id":1,"method":"ping"}\nnot json\n\n'
                            '{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        stdout = io.StringIO()
        mcp.serve(self.api, stdin, stdout)
        replies = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual(replies, [{"jsonrpc": "2.0", "id": 1, "result": {}},
                                   {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}])


if __name__ == "__main__":
    unittest.main()
