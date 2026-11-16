"""Real Windows client jobs, shared indexes, recovery, isolation, and idle exit."""
import asyncio
from contextlib import AsyncExitStack
import json
import logging
import os
from pathlib import Path
import signal
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from fastmcp import Client
from fastmcp.client.transports import StdioTransport

from mcp_cpp_analyzer.shared_protocol import connect
from mcp_cpp_analyzer.shared_runtime import atomic_json, new_profile, read_json
from mcp_cpp_analyzer.windows_runtime import process_alive

ROOT = Path(__file__).resolve().parents[1]
STATUS_CODE = '''s = await call_tool("workspace_status", {})
return {"ready": s["ready"], "workspace": s["workspace"], "compile_db": s["compile_db"],
        "clangd_pid": s["clangd_pid"], "shared": s["shared_backend"], "version": s["service_version"]}'''


@unittest.skipUnless(os.name == "nt", "Shared process launch currently targets Windows")
class SharedRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        work = Path(os.environ.get("CPP_ANALYZER_TEST_WORK_DIR", ROOT / "resource/test-work")).resolve()
        work.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="shared-", dir=work)
        self.folder = Path(self.temporary.name).resolve()
        self.assertEqual(self.folder.parent, work)
        self.addCleanup(self.temporary.cleanup)
        self.host = self.folder / "runtime"
        self.proxy = os.environ.get("CPP_ANALYZER_TEST_PROXY")
        self.clangd = os.environ.get("CPP_ANALYZER_TEST_CLANGD")
        if not self.clangd:
            self.skipTest("Set CPP_ANALYZER_TEST_CLANGD to a clangd executable")
        self.profiles = {}
        self.workspaces = []
        for name, leaf in (("中文 # project", "leaf"), ("other", "other_leaf")):
            workspace = self.folder / name
            workspace.mkdir()
            source = workspace / "sample.cpp"
            source.write_text(f"#include <stddef.h>\nint {leaf}(int value) {{ return value + 1; }}\n"
                              f"int caller(int value) {{ return {leaf}(value); }}\n", encoding="utf-8")
            (workspace / "compile_commands.json").write_text(json.dumps([{
                "file": str(source), "directory": str(workspace),
                "arguments": ["clang++", "-std=c++20", "-c", str(source)]}]), encoding="utf-8")
            self.workspaces.append(workspace)
            if not self.proxy:
                with patch.dict(os.environ, {"LOCALAPPDATA": str(self.host)}):
                    profile = new_profile(workspace=workspace, compile_db=workspace, clangd=self.clangd,
                        backend_command=Path(sys.executable).with_name("pythonw.exe"),
                        backend_arguments=[ROOT / "server.py"])
                atomic_json(Path(profile["profile_path"]), profile)
                self.profiles[workspace] = profile

    def client(self, workspace, name, *, mode="auto"):
        command, args = ((self.proxy, []) if self.proxy else (sys.executable,
            ["-X", "utf8", "-B", str(ROOT / "proxy.py"), "--profile", self.profiles[workspace]["profile_path"]]))
        transport = StdioTransport(command=command, args=args, cwd=str(workspace), keep_alive=False,
            env={"TMP": os.environ["TMP"], "LOCALAPPDATA": str(self.host),
                 "CPP_ANALYZER_COMPILE_DB_PATH": str(workspace)}, log_file=self.folder / (name + ".log"))
        return Client(transport, init_timeout=90, timeout=60, mode=mode)

    async def status(self, client):
        response = await client.call_tool_mcp("execute", {"code": STATUS_CODE})
        self.assertFalse(response.is_error, response)
        self.assertTrue(response.structured_content["ready"], response)
        return response.structured_content

    async def wait_dead(self, *pids):
        async with asyncio.timeout(45):
            while any(process_alive(pid) for pid in pids):
                await asyncio.sleep(0.1)

    async def test_shared_backend_survives_client_jobs_and_crashes_then_exits_when_unused(self):
        workspace, other = self.workspaces
        with self.assertNoLogs("mcp.client.stdio", level=logging.ERROR):
          async with AsyncExitStack() as stack:
            first_scope = await stack.enter_async_context(AsyncExitStack())
            first, second, third = await asyncio.gather(*(
                (first_scope if name == "first" else stack).enter_async_context(self.client(workspace, name, mode=mode))
                for name, mode in (("first", "auto"), ("second", "legacy"), ("third", "2026-07-28"))))
            states = await asyncio.gather(*(self.status(client) for client in (first, second, third)))
            original = states[0]
            self.assertEqual({state["shared"]["pid"] for state in states}, {original["shared"]["pid"]})
            self.assertEqual({state["clangd_pid"] for state in states}, {original["clangd_pid"]})
            self.assertTrue(all(Path(state["workspace"]) == workspace for state in states))
            self.assertEqual(original["shared"]["clients"], 3)
            self.assertEqual(original["shared"]["idle_timeout_sec"], 30)

            discover = await first.call_tool_mcp("execute", {"code": '''r = await call_tool("query_files", {
    "files": ["sample.cpp"], "operations": ["symbols"], "detail": "standard"})
return {"symbol_id": [item["symbol_id"] for item in r["results"][0]["items"] if item["name"] == "caller"][0]}'''})
            handle = discover.structured_content["symbol_id"]
            query_code = ('return await call_tool("query_symbols", {"targets": [{"symbol_id": ' + json.dumps(handle) +
                          '}], "operations": ["callees"], "detail": "standard"})')
            calls = await second.call_tool_mcp("execute", {"code": query_code})
            self.assertEqual(calls.structured_content["results"][0]["items"][0]["name"], "leaf")

            independent = await stack.enter_async_context(self.client(other, "independent"))
            isolated = await self.status(independent)
            self.assertNotEqual(isolated["shared"]["pid"], original["shared"]["pid"])
            self.assertNotEqual(isolated["clangd_pid"], original["clangd_pid"])
            self.assertEqual(Path(isolated["workspace"]), other)

            endpoint = read_json(Path(original["shared"]["runtime_directory"]) / "instance.json")
            with self.assertRaises((ConnectionError, ValueError)):
                await asyncio.to_thread(connect, {**endpoint, "token": "00" * 32})
            self.assertEqual((await self.status(first))["shared"]["clients"], 3)

            interrupted = asyncio.create_task(first.call_tool_mcp("execute", {"code": "while True:\n    pass"}))
            await asyncio.sleep(0.3)
            self.assertFalse(interrupted.done())
            os.kill(original["shared"]["pid"], signal.SIGTERM)
            with self.assertRaisesRegex(Exception, "BACKEND_RESTARTED"):
                await asyncio.wait_for(interrupted, 10)
            await self.wait_dead(original["clangd_pid"])
            recovered = await asyncio.gather(*(self.status(client) for client in (first, second, third)))
            replacement = recovered[0]
            self.assertNotEqual(replacement["shared"]["pid"], original["shared"]["pid"])
            self.assertEqual({state["shared"]["pid"] for state in recovered}, {replacement["shared"]["pid"]})
            stale = await second.call_tool_mcp("execute", {"code": query_code})
            self.assertTrue(stale.is_error)
            self.assertIn("EXPIRED_HANDLE", str(stale.content))

            abandoned = asyncio.create_task(first.call_tool_mcp("execute", {"code": "while True:\n    pass"}))
            await asyncio.sleep(0.2)
            await first_scope.aclose()
            with self.assertRaises(Exception):
                await asyncio.wait_for(abandoned, 5)
            self.assertEqual((await self.status(second))["shared"]["pid"], replacement["shared"]["pid"])
            async with self.client(workspace, "warm") as warm:
                self.assertEqual((await self.status(warm))["shared"]["pid"], replacement["shared"]["pid"])
          await self.wait_dead(replacement["shared"]["pid"], replacement["clangd_pid"],
                               isolated["shared"]["pid"], isolated["clangd_pid"])
          for state in (replacement, isolated):
            directory = Path(state["shared"]["runtime_directory"])
            self.assertFalse((directory / "instance.json").exists())
            failure = read_json(directory / "failure.json")
            self.assertIsNone(failure, failure)

    async def asyncTearDown(self):
        # Keep failing tests scoped to their own private runtime. Let normal idle
        # cleanup finish before TemporaryDirectory removes Windows-open log files.
        home = self.host / "mcp-cpp-analyzer"
        failed = any(test is self for test, _ in self._outcome.result.errors + self._outcome.result.failures)
        if failed:
            for file in self.folder.glob("*.log"):
                print(file.name + ":\n" + file.read_text(encoding="utf-8")[-4000:], file=sys.stderr)
        if not home.exists():
            return
        for directory in home.iterdir():
            for name in ("instance.json", "starting.json"):
                value = read_json(directory / name)
                if value is not None and process_alive(value["pid"]):
                    await self.wait_dead(value["pid"])


if __name__ == "__main__":
    unittest.main()
