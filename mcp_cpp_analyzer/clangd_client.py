"""Async LSP connection with cancellation, failure propagation and notifications."""
import asyncio
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

class LspError(RuntimeError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)

def path_to_uri(path: Path) -> str:
    return Path(path).resolve().as_uri()

class ClangdClient:
    def __init__(self, clangd_server, workspace_root, *, request_timeout=30, init_timeout=60):
        self.clangd_server = clangd_server
        self.workspace_root = Path(workspace_root)
        self.request_timeout = request_timeout
        self.init_timeout = init_timeout
        self.pending_requests = {}
        self.request_counter = 0
        self.initialized = False
        self.capabilities = {}
        self.server_info = {}
        self.position_encoding = "utf-16"
        self.failure = None
        self.diagnostics = {}
        self.diagnostic_events = {}
        self.progress = {}
        self._writer_lock = asyncio.Lock()
        self._reader_task = None
        self._process_task = None
        self._closing = False

    async def start(self):
        await self.clangd_server.start()
        self._reader_task = asyncio.create_task(self._read_messages())
        self._process_task = asyncio.create_task(self._watch_process())
        try:
            result = await self.request("initialize", {
                "processId": None, "rootUri": path_to_uri(self.workspace_root),
                "clientInfo": {"name": "mcp-cpp-analyzer", "version": "0.2.0"},
                "capabilities": {
                    "general": {"positionEncodings": ["utf-8", "utf-16"]},
                    "workspace": {"configuration": True, "workspaceFolders": True},
                    "window": {"workDoneProgress": True},
                    "textDocument": {
                        "definition": {"linkSupport": True},
                        "declaration": {"linkSupport": True},
                        "implementation": {"linkSupport": True},
                        "typeDefinition": {"linkSupport": True},
                        "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
                        "publishDiagnostics": {"versionSupport": True, "relatedInformation": True},
                    },
                },
                "workspaceFolders": [{"uri": path_to_uri(self.workspace_root),
                                      "name": self.workspace_root.name}],
            }, timeout=self.init_timeout)
            self.capabilities = result["capabilities"]
            self.server_info = result.get("serverInfo", {})
            self.position_encoding = self.capabilities.get("positionEncoding", "utf-16")
            if self.position_encoding not in ("utf-8", "utf-16", "utf-32"):
                raise LspError("POSITION_ENCODING", f"Unsupported position encoding: {self.position_encoding}")
            await self.notify("initialized", {})
            self.initialized = True
        except BaseException:
            await self.stop()
            raise

    def _fail(self, error):
        if self.failure is None:
            self.failure = error
        self.initialized = False
        for future in tuple(self.pending_requests.values()):
            if not future.done():
                future.set_exception(error)

    async def _watch_process(self):
        code = await self.clangd_server.process.wait()
        if not self._closing:
            self._fail(LspError("BACKEND_EXITED", f"clangd exited with code {code}"))

    async def _send_message(self, message):
        if not self.clangd_server.is_alive:
            raise LspError("BACKEND_UNAVAILABLE", "clangd process is not running")
        if self.failure is not None:
            raise self.failure
        body = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        async with self._writer_lock:
            stream = self.clangd_server.process.stdin
            stream.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body)
            await stream.drain()

    async def notify(self, method, params):
        await self._send_message({"jsonrpc": "2.0", "method": method, "params": params})

    async def request(self, method, params, timeout=None):
        self.request_counter += 1
        request_id = self.request_counter
        future = asyncio.get_running_loop().create_future()
        self.pending_requests[request_id] = future
        try:
            await self._send_message({"jsonrpc": "2.0", "id": request_id,
                                      "method": method, "params": params})
            return await asyncio.wait_for(future, timeout or self.request_timeout)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            if self.clangd_server.is_alive and self.failure is None:
                try:
                    await asyncio.wait_for(self.notify("$/cancelRequest", {"id": request_id}), 1)
                except (OSError, LspError, asyncio.TimeoutError) as error:
                    logger.warning("LSP cancellation delivery failed: %s", error)
            raise
        finally:
            self.pending_requests.pop(request_id, None)
            if not future.done():
                future.cancel()

    async def _read_messages(self):
        stream = self.clangd_server.process.stdout
        try:
            while True:
                headers = {}
                while True:
                    line = await stream.readline()
                    if not line:
                        raise LspError("BACKEND_EOF", "clangd closed its output stream")
                    if line == b"\r\n":
                        break
                    key, value = line.decode("ascii").split(":", 1)
                    headers[key.lower()] = value.strip()
                length = int(headers.get("content-length", "0"))
                if not 0 < length <= 32 * 1024 * 1024:
                    raise LspError("INVALID_FRAME", f"Invalid LSP Content-Length: {length}")
                message = json.loads(await stream.readexactly(length))
                await self._handle_message(message)
        except asyncio.CancelledError:
            raise
        except (OSError, ValueError, UnicodeError, asyncio.IncompleteReadError, LspError) as error:
            if not self._closing:
                self._fail(error)

    async def _handle_message(self, message):
        if "method" not in message:
            future = self.pending_requests.get(message.get("id"))
            if future is not None and not future.done():
                if "error" in message:
                    error = message["error"]
                    future.set_exception(LspError(error["code"], error["message"]))
                else:
                    future.set_result(message.get("result"))
            return
        method = message["method"]
        params = message.get("params", {})
        if "id" in message:
            response = {"jsonrpc": "2.0", "id": message["id"]}
            if method == "workspace/configuration":
                response["result"] = [None for _ in params.get("items", [])]
            elif method == "workspace/workspaceFolders":
                response["result"] = [{"uri": path_to_uri(self.workspace_root), "name": self.workspace_root.name}]
            elif method in ("window/workDoneProgress/create", "client/registerCapability",
                            "client/unregisterCapability"):
                response["result"] = None
            else:
                response["error"] = {"code": -32601, "message": f"Unsupported client method: {method}"}
            await self._send_message(response)
        elif method == "textDocument/publishDiagnostics":
            self.diagnostics[params["uri"]] = params
            self.diagnostic_events.setdefault(params["uri"], asyncio.Event()).set()
        elif method == "$/progress":
            token = str(params["token"])
            if params["value"].get("kind") == "end":
                self.progress.pop(token, None)
            else:
                self.progress[token] = params["value"]
        elif method == "window/logMessage":
            logger.log(logging.WARNING if params.get("type", 1) <= 2 else logging.DEBUG,
                       "clangd: %s", params.get("message", ""))

    async def stop(self):
        if self._closing:
            return
        self._closing = True
        try:
            if self.initialized and self.clangd_server.is_alive and self.failure is None:
                try:
                    await self.request("shutdown", None, timeout=3)
                    await self.notify("exit", None)
                except (LspError, OSError, asyncio.TimeoutError) as error:
                    logger.warning("LSP shutdown failed: %s", error)
            await self.clangd_server.stop()
        finally:
            self._fail(LspError("BACKEND_STOPPED", "clangd connection has been stopped"))
            tasks = [task for task in (self._reader_task, self._process_task) if task is not None]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.pending_requests.clear()
