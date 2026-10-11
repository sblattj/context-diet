"""audit/mcp-usage.py: per-server MCP counting, dedupe, normalization, deny suggestion."""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
KIT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(KIT, "lib"))
sys.path.insert(0, HERE)

import fixtures_helper as fx  # noqa: E402

spec = importlib.util.spec_from_file_location("mcp_usage", os.path.join(KIT, "audit", "mcp-usage.py"))
mcp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mcp)

NOW = "2026-10-11T00:00:00Z"


def mcp_call(tool, ts, tid, mid="m1", entrypoint=None):
    ev = {"type": "assistant", "timestamp": ts, "message": {"id": mid, "role": "assistant", "content": [
        {"type": "tool_use", "id": tid, "name": tool, "input": {}}]}}
    if entrypoint:
        ev["entrypoint"] = entrypoint
    return ev


class McpUsage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.sess = os.path.join(self.tmp, "projects", "-p")
        self.cfg = os.path.join(self.tmp, "claude.json")

    def run_tool(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = mcp.main([self.sess, "--config", self.cfg, "--now", NOW, *extra])
        self.assertEqual(rc, 0, err.getvalue())
        return out.getvalue()

    def write(self, events, cfg):
        fx.write_jsonl(os.path.join(self.sess, "s.jsonl"), events)
        with open(self.cfg, "w") as f:
            json.dump(cfg, f)

    def rows(self, *extra):
        return {r["server"]: r for r in json.loads(self.run_tool("--json", *extra))["servers"]}

    def test_counts_per_server_and_ignores_non_mcp(self):
        self.write([mcp_call("mcp__alpha__read", "2026-10-05T00:00:00Z", "a"),
                    mcp_call("mcp__alpha__write", "2026-10-06T00:00:00Z", "b"),
                    mcp_call("mcp__beta__x", "2026-10-01T00:00:00Z", "c"),
                    fx.other_tool("2026-10-06T00:00:00Z")],
                   {"mcpServers": {"alpha": {}, "beta": {}}})
        r = self.rows()
        self.assertEqual(r["alpha"]["calls"], 2)
        self.assertEqual(r["alpha"]["last_used"], "2026-10-06")
        self.assertEqual(r["beta"]["calls"], 1)
        self.assertNotIn("Bash", r)

    def test_split_message_counted_once(self):
        ev = mcp_call("mcp__alpha__read", "2026-10-05T00:00:00Z", "same", mid="msg1")
        self.write([ev, dict(ev), dict(ev)], {"mcpServers": {"alpha": {}}})
        self.assertEqual(self.rows()["alpha"]["calls"], 1)

    def test_normalized_connector_name(self):
        self.write([mcp_call("mcp__claude_ai_Some_Connector__go", "2026-10-05T00:00:00Z", "a")],
                   {"mcpServers": {"claude.ai Some Connector": {}}})
        r = self.rows()
        self.assertEqual(r["claude.ai Some Connector"]["calls"], 1)
        self.assertEqual(r["claude.ai Some Connector"]["configured"], "yes")
        self.assertNotIn("claude_ai_Some_Connector", r)

    def test_zero_calls_deny_and_block(self):
        self.write([mcp_call("mcp__alpha__read", "2026-10-05T00:00:00Z", "a")],
                   {"mcpServers": {"alpha": {}, "idle": {}, "claude.ai Idle Thing": {}}})
        r = self.rows()
        self.assertEqual(r["alpha"]["suggestion"], "keep")
        self.assertEqual(r["idle"]["suggestion"], "deny")
        text = self.run_tool()
        self.assertIn('"deniedMcpServers": [{"serverName": "claude.ai Idle Thing"}, '
                      '{"serverName": "idle"}]', text)
        self.assertNotIn('"serverName": "alpha"', text)

    def test_window_filter(self):
        self.write([mcp_call("mcp__old__x", "2026-07-01T00:00:00Z", "a")],
                   {"mcpServers": {"old": {}}})
        r = self.rows()["old"]
        self.assertEqual((r["calls"], r["suggestion"], r["last_used"]), (0, "deny", "2026-07-01"))
        self.assertEqual(self.rows("--window-days", "200")["old"]["suggestion"], "keep")

    def test_unconfigured_used_server_kept_and_headless_skipped(self):
        self.write([mcp_call("mcp__plug__x", "2026-10-05T00:00:00Z", "a"),
                    mcp_call("mcp__plug__x", "2026-10-05T00:00:00Z", "b", entrypoint="sdk-cli")],
                   {"mcpServers": {}})
        r = self.rows()["plug"]
        self.assertEqual((r["calls"], r["configured"], r["suggestion"]), (1, "no", "keep"))
        self.assertEqual(self.rows("--include-headless")["plug"]["calls"], 2)

    def test_project_scoped_config_and_mcp_json(self):
        proj = os.path.join(self.tmp, "proj")
        os.makedirs(proj)
        with open(os.path.join(proj, ".mcp.json"), "w") as f:
            json.dump({"mcpServers": {"fromfile": {}}}, f)
        names = mcp.configured_servers(os.path.join(self.tmp, "none.json"), proj, False)
        self.assertEqual(names, ["fromfile"])
        with open(self.cfg, "w") as f:
            json.dump({"mcpServers": {"top": {}},
                       "projects": {proj: {"mcpServers": {"here": {}}},
                                    "/elsewhere": {"mcpServers": {"there": {}}}}}, f)
        self.assertEqual(mcp.configured_servers(self.cfg, proj, False), ["fromfile", "here", "top"])
        self.assertIn("there", mcp.configured_servers(self.cfg, proj, True))

    def test_csv_output(self):
        self.write([mcp_call("mcp__alpha__read", "2026-10-05T00:00:00Z", "a")],
                   {"mcpServers": {"alpha": {}}})
        lines = self.run_tool("--csv").splitlines()
        self.assertEqual(lines[0], "server,calls,last_used,configured,suggestion")
        self.assertEqual(lines[1], "alpha,1,2026-10-05,yes,keep")


if __name__ == "__main__":
    unittest.main()
