"""Regression tests for schema, index coverage, native stdout, and symbol evidence."""
import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import unittest
from unittest.mock import AsyncMock

from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from mcp_cpp_analyzer.clangd_client import ClangdClient
from mcp_cpp_analyzer.config import Config
from mcp_cpp_analyzer.query_service import BoundedStore, QueryService, SymbolTarget, compact_payload
from mcp_cpp_analyzer.server import FIELDS, create_server, render_results

ROOT = Path(__file__).resolve().parents[1]


class FeedbackTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = Path(os.environ.get("CPP_ANALYZER_TEST_WORK_DIR", ROOT / "resource/test-work")).resolve()
        directory.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="feedback-", dir=directory)
        self.root = Path(self.temporary.name).resolve()
        if self.root.parent != directory:
            raise RuntimeError("Test directory escaped the resource workspace")
        self.addCleanup(self.temporary.cleanup)
        backend = os.environ.get("CPP_ANALYZER_TEST_CLANGD") or shutil.which("clangd")
        if backend is None:
            self.skipTest("Set CPP_ANALYZER_TEST_CLANGD to a clangd executable")
        (self.root / "compile_commands.json").write_text("[]", encoding="utf-8")
        self.config = Config(compile_db_dir=self.root, workspace_root=self.root,
                             clangd_path=backend, background_index=False)

    async def test_fields_schema_exposes_values_and_rejects_invalid_input_before_queries(self):
        class NoQueries:
            def __init__(self, config):
                self.config = config
                self.pages = BoundedStore()
                self.calls = 0
            async def start(self):
                pass
            async def stop(self):
                pass
            async def files(self, *args, **kwargs):
                self.calls += 1
                raise AssertionError("Invalid fields must be rejected before querying")
        instance = NoQueries(replace(self.config, tool_mode="direct"))
        server = create_server(instance.config, service_factory=lambda _: instance)
        async with Client(server) as client:
            for tool in await client.list_tools():
                schema = tool.model_dump(mode="json", by_alias=True)["inputSchema"]
                if "fields" in schema["properties"]:
                    branches = schema["properties"]["fields"]["anyOf"]
                    array = next(branch for branch in branches if branch.get("type") == "array")
                    self.assertEqual(set(array["items"]["enum"]), FIELDS)
                    self.assertEqual(array["minItems"], 1)
            response = await client.call_tool_mcp("query_files", {
                "files": ["sample.cpp"], "operations": ["symbols"], "fields": ["not_a_field"]})
            self.assertTrue(response.is_error)
        self.assertEqual(instance.calls, 0)

    @unittest.skipUnless(os.name == "nt", "Windowed worker packaging targets Windows")
    async def test_windowed_worker_preserves_pipe_results_errors_and_original_binary(self):
        import hashlib
        import pefile
        from build import prepare_worker
        from pydantic_monty import AsyncMonty, MontyRuntimeError

        original = Path(sysconfig.get_path("scripts")) / "monty.exe"
        digest = hashlib.sha256(original.read_bytes()).hexdigest()
        worker = prepare_worker(original, self.root)
        with pefile.PE(str(worker), fast_load=True) as image:
            self.assertEqual(image.OPTIONAL_HEADER.Subsystem, 2)
            self.assertEqual(image.OPTIONAL_HEADER.CheckSum, image.generate_checksum())
        async with AsyncMonty(binary_path=worker, min_processes=1, max_processes=1) as pool:
            async with pool.checkout() as session:
                self.assertEqual(await session.feed_run("40 + 2"), 42)
                with self.assertRaisesRegex(MontyRuntimeError, "worker-error"):
                    await session.feed_run('raise ValueError("worker-error")')
        self.assertEqual(hashlib.sha256(original.read_bytes()).hexdigest(), digest)

    async def test_index_progress_is_observable_but_does_not_claim_complete_coverage(self):
        service = QueryService(replace(self.config, background_index=True))
        self.assertEqual(service.status()["index_activity"]["state"], "not_observed")
        async def progress(value):
            await service.client._handle_message({"method": "$/progress", "params": {
                "token": "backgroundIndexProgress", "value": value}})
        await progress({"kind": "begin", "title": "indexing", "message": "0/2"})
        await progress({"kind": "report", "percentage": 50, "message": "1/2"})
        state = service.status()
        self.assertEqual(state["index_activity"]["state"], "indexing")
        self.assertEqual(state["active_progress"][0]["title"], "indexing")
        await progress({"kind": "end"})
        state = service.status()
        self.assertEqual(state["index_activity"]["state"], "idle")
        self.assertEqual(state["index_activity"]["completed_runs"], 1)
        self.assertIsNone(state["index_complete"])

    def mixed_encoding_search(self):
        good = self.root / "utf8.cpp"
        prefix = "/* 中😀 */ int "
        good.write_text(prefix + "MixedGood;\nint MixedOther;\n", encoding="utf-8")
        bad = self.root / "legacy.cpp"
        bad.write_bytes("/* 中文 */ int MixedBad;\n".encode("gbk"))
        def symbol(path, name, line=0, column=0):
            return {"name": name, "kind": 13, "location": {"uri": path.as_uri(), "range": {
                "start": {"line": line, "character": column},
                "end": {"line": line, "character": column + len(name)}}}}
        values = [symbol(bad, "MixedBad"), symbol(bad, "MixedBadAgain"),
                  symbol(good, "MixedGood", column=len(prefix.encode("utf-16-le")) // 2),
                  symbol(good, "MixedOther", line=1, column=4)]
        service = QueryService(self.config)
        service.methods.search_symbols = AsyncMock(return_value=values)
        return service, values, good, bad, len(prefix) + 1

    async def test_non_utf8_search_keeps_valid_symbols_warnings_and_raw_cursor_offsets(self):
        service, _, good, bad, column = self.mixed_encoding_search()
        results = await service.search(["Mixed"], limit=3)
        page = results[0]
        self.assertNotIn("error", page)
        self.assertEqual([row["name"] for row in page["items"]], ["MixedGood"])
        self.assertEqual((page["items"][0]["file"], page["items"][0]["column"]), (str(good), column))
        self.assertEqual(page["backend_returned"], 4)
        self.assertTrue(page["truncated"])
        self.assertEqual(len(page["skipped_files"]), 1)
        failure = page["skipped_files"][0]
        self.assertEqual(failure["file"], str(bad))
        self.assertEqual(failure["code"], "SOURCE_ENCODING_UNSUPPORTED")
        self.assertIn("byte", failure["message"])
        payload = compact_payload(results, self.root, fields=["name"])
        self.assertEqual(payload["status"], "partial")
        self.assertIn("SOURCE_FILES_SKIPPED", payload["results"][0]["warnings"])
        mapped = payload["results"][0]["skipped_files"][0]["file"]
        self.assertEqual(self.root / payload["files"][mapped], bad)
        following = (await service.read_page(page["cursor"]))[0]
        self.assertEqual([row["name"] for row in following["items"]], ["MixedOther"])
        self.assertFalse(following["truncated"])

    async def test_search_output_budget_does_not_repeat_rows_after_skipping_a_file(self):
        service, _, _, _, _ = self.mixed_encoding_search()
        results = await service.search(["Mixed"], limit=4)
        response = render_results(service, results, fields=["name"], max_items=1).structured_content
        page = response["results"][0]
        self.assertEqual(response["status"], "partial")
        self.assertEqual(page["rows"], [["MixedGood"]])
        self.assertTrue(page["truncated"])
        self.assertTrue(page["cursor"].endswith("/3"))
        following = (await service.read_page(page["cursor"]))[0]
        self.assertEqual([row["name"] for row in following["items"]], ["MixedOther"])

    async def test_all_non_utf8_search_results_are_partial_and_other_file_errors_are_not_hidden(self):
        service, values, _, bad, _ = self.mixed_encoding_search()
        service.methods.search_symbols.return_value = values[:2]
        results = await service.search(["MixedBad"])
        payload = compact_payload(results, self.root)
        self.assertEqual(payload["status"], "partial")
        self.assertEqual(payload["results"][0]["items"], [])
        self.assertFalse(payload["results"][0]["truncated"])
        explicit = await service.files([str(bad)], ["symbols"])
        self.assertEqual(explicit[0]["error"]["code"], "SOURCE_ENCODING_UNSUPPORTED")
        self.assertIn(str(bad), explicit[0]["error"]["message"])
        bad.unlink()
        missing = await service.search(["MixedBad"])
        self.assertEqual(missing[0]["error"]["code"], "FileNotFoundError")

    async def test_code_mode_schema_and_results_have_one_model_facing_representation(self):
        class StatusOnly:
            def __init__(self, config):
                self.config = config
            async def start(self):
                pass
            async def stop(self):
                pass
            def status(self):
                return {"ready": True, "evidence": ["中文", {"nested": 42}]}
        async with Client(create_server(self.config, service_factory=StatusOnly)) as client:
            schema = await client.call_tool_mcp("get_schema", {"tools": ["query_files"]})
            self.assertFalse(schema.is_error)
            self.assertEqual(schema.content, [])
            text = schema.structured_content["result"]
            self.assertIn("Allowed fields:", text)
            self.assertIn("index_location", text)
            result = await client.call_tool_mcp("execute", {
                "code": 'return await call_tool("workspace_status", {})'})
            self.assertEqual(result.content, [])
            self.assertEqual(result.structured_content, {
                "ready": True, "evidence": ["中文", {"nested": 42}]})
            failure = await client.call_tool_mcp("execute", {"code": 'raise ValueError("intentional failure")'})
            self.assertTrue(failure.is_error)
            self.assertIn("intentional failure", str(failure.content))

    async def test_cold_empty_search_and_declaration_only_definition_are_explicit(self):
        source = self.root / "sample.cpp"
        source.write_text("int only_declared();\nint caller() { return only_declared(); }\n", encoding="utf-8")
        self.database([source])
        service = QueryService(self.config)
        await service.start()
        try:
            search = await service.search(["symbol_that_does_not_exist"])
            self.assertEqual(search[0]["items"], [])
            self.assertIn("INDEX_COVERAGE_UNVERIFIED", search[0]["warnings"])
            definitions = await service.query([SymbolTarget(file=str(source), line=2, column=23)], ["definition"])
            self.assertEqual(definitions[0]["location_kind"], "declaration", definitions)
            self.assertIn("IMPLEMENTATION_UNCONFIRMED", definitions[0]["warnings"])
            outline = await service.files([str(source)], ["symbols"])
            self.assertEqual(outline[0]["coverage"], "document")
        finally:
            await service.stop()

    def database(self, sources):
        entries = [{"file": str(source), "directory": str(source.parent),
                    "arguments": ["clang++", "-c", str(source)],
                    "output": str(source.with_suffix(".obj"))} for source in sources]
        (self.root / "compile_commands.json").write_text(json.dumps(entries), encoding="utf-8")

    async def test_definition_at_definition_does_not_misreport_clangd_declaration_toggle(self):
        header = self.root / "api.h"
        header.write_text("int function();\n", encoding="utf-8")
        source = self.root / "api.cpp"
        source.write_text('#include "api.h"\nint function() { return 42; }\n', encoding="utf-8")
        self.database([source])
        service = QueryService(self.config)
        await service.start()
        try:
            target = SymbolTarget(file=str(source), line=2, column=5)
            definition, declaration = await service.query([target], ["definition", "declaration"])
            self.assertEqual(definition["location_kind"], "ast_definition", definition)
            self.assertTrue(definition["declaration_navigation"])
            self.assertNotIn("IMPLEMENTATION_UNCONFIRMED", definition.get("warnings", []))
            self.assertEqual(len(definition["items"]), 1)
            self.assertEqual(Path(definition["items"][0]["file"]), source)
            self.assertEqual(definition["items"][0]["line"], 2)
            self.assertEqual(Path(declaration["items"][0]["file"]), header)
        finally:
            await service.stop()

    async def test_same_usr_in_separate_targets_uses_callsite_ast_and_keeps_index_conflicts(self):
        sources = []
        for name in ("a", "b"):
            folder = self.root / ("plugin-" + name)
            folder.mkdir()
            source = folder / "entry.cpp"
            source.write_text("int shared_function() { return 1; }\n"
                              f"int entry_{name}() {{ return shared_function(); }}\n", encoding="utf-8")
            sources.append(source)
        self.database(sources)
        service = QueryService(self.config)
        await service.start()
        try:
            await service.files([str(source) for source in sources], ["symbols"])
            for source in sources:
                result = await service.query([SymbolTarget(file=str(source), line=2, column=5)], ["callees"])
                self.assertNotIn("error", result[0], result)
                row = result[0]["items"][0]
                self.assertEqual(Path(row["file"]), source)
                self.assertEqual(row["location_source"], "ast_definition")
                self.assertEqual(Path(row["call_site_file"]), source)
                self.assertFalse(result[0]["linkage_verified"])
                if row.get("ambiguity"):
                    self.assertIn("INDEX_LOCATION_CONFLICT", result[0]["warnings"])
                    self.assertNotEqual(Path(row["index_location"]["file"]), source)
                    payload = compact_payload(result, self.root, fields=["name"])
                    columns = payload["results"][0]["columns"]
                    projected = dict(zip(columns, payload["results"][0]["rows"][0]))
                    self.assertIn("index_location", projected)
                    self.assertIn("call_sites", projected)
                    self.assertEqual(self.root / payload["files"][projected["file"]], source)
                    alternate = projected["index_location"]["file"]
                    self.assertNotEqual(self.root / payload["files"][alternate], source)
        finally:
            await service.stop()

    async def test_native_fd_and_windows_handle_logs_do_not_enter_protocol_stdout(self):
        script = '''import ctypes, json, os
from mcp_cpp_analyzer.stdio import reserve_protocol_stdout
protocol = reserve_protocol_stdout()
print("python-print-log", flush=True)
os.write(1, b"native-fd-log\\n")
if os.name == "nt":
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetStdHandle.argtypes = [wintypes.DWORD]
    kernel.GetStdHandle.restype = wintypes.HANDLE
    kernel.WriteFile.argtypes = [wintypes.HANDLE, wintypes.LPCVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    data = b"native-handle-log\\n"
    written = wintypes.DWORD()
    if not kernel.WriteFile(kernel.GetStdHandle(-11), data, len(data), ctypes.byref(written), None):
        raise ctypes.WinError(ctypes.get_last_error())
protocol.write(json.dumps({"protocol": True}) + "\\n")
protocol.flush()
'''
        result = await asyncio.to_thread(subprocess.run, [sys.executable, "-X", "utf8", "-B", "-c", script],
                                          cwd=ROOT, capture_output=True, encoding="utf-8", check=True)
        self.assertEqual(json.loads(result.stdout), {"protocol": True})
        self.assertIn("native-fd-log", result.stderr)
        self.assertIn("python-print-log", result.stderr)
        if os.name == "nt":
            self.assertIn("native-handle-log", result.stderr)

    async def test_explicit_stdio_stream_preserves_lifespan_and_isolates_python_prints(self):
        script = '''from mcp_cpp_analyzer.stdio import reserve_protocol_stdout
protocol = reserve_protocol_stdout()
from mcp_cpp_analyzer.transport import serve_stdio
import anyio
from fastmcp import FastMCP
from fastmcp.server.lifespan import lifespan
@lifespan
async def life(server):
    print("lifespan-print", flush=True)
    try:
        yield {"value": 42}
    finally:
        print("lifespan-closed", flush=True)
server = FastMCP("stdio-isolation-test", lifespan=life)
@server.tool
def answer() -> dict:
    print("handler-print", flush=True)
    return {"answer": 42}
anyio.run(serve_stdio, server, protocol)
'''
        log = self.root / "stderr.log"
        transport = StdioTransport(command=sys.executable, args=["-X", "utf8", "-B", "-c", script],
                                   cwd=str(ROOT), keep_alive=False, log_file=log)
        with self.assertNoLogs("mcp.client.stdio", level="ERROR"):
            async with Client(transport, timeout=20) as client:
                response = await client.call_tool_mcp("answer", {})
                self.assertFalse(response.is_error)
                self.assertEqual(response.structured_content, {"answer": 42})
        diagnostics = log.read_text(encoding="utf-8")
        for message in ("lifespan-print", "handler-print", "lifespan-closed"):
            self.assertIn(message, diagnostics)


if __name__ == "__main__":
    unittest.main()
