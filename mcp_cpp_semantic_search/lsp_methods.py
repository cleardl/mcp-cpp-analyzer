"""Document snapshots and typed semantic requests; no global session state."""
import asyncio
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import cached_property
import hashlib
from pathlib import Path

from .clangd_client import LspError, path_to_uri

@dataclass
class Snapshot:
    path: Path
    text: str
    digest: str
    version: int = 1

    @property
    def uri(self):
        return path_to_uri(self.path)

    @cached_property
    def lines(self):
        return self.text.splitlines()

    def position(self, line, column, encoding):
        lines = self.lines
        if line < 1 or line > len(lines) or column < 1 or column > len(lines[line - 1]) + 1:
            raise ValueError(f"Position is outside the document: {self.path}:{line}:{column}")
        prefix = lines[line - 1][:column - 1]
        units = len(prefix.encode("utf-8")) if encoding == "utf-8" else (
            len(prefix.encode("utf-16-le")) // 2 if encoding == "utf-16" else len(prefix))
        return {"line": line - 1, "character": units}

    def column(self, line, units, encoding):
        lines = self.lines
        if line >= len(lines):
            if line == len(lines) and units == 0:
                return 1
            raise ValueError(f"LSP returned a position outside {self.path}: {line}:{units}")
        text = lines[line]
        if encoding == "utf-32":
            return units + 1
        codec, width = ("utf-8", 1) if encoding == "utf-8" else ("utf-16-le", 2)
        return len(text.encode(codec)[:units * width].decode(codec)) + 1

class DocumentManager:
    def __init__(self, lsp_client, *, max_open_documents=32):
        self.lsp_client = lsp_client
        self.max_open_documents = max_open_documents
        self.open_documents = OrderedDict()
        self._locks = {}
        self._active = set()
        self._sync_lock = asyncio.Lock()

    async def snapshot(self, path):
        path = Path(path).resolve()
        def read():
            before = path.stat()
            if not path.is_file() or before.st_size > 16 * 1024 * 1024:
                raise ValueError(f"Expected a source file no larger than 16 MiB: {path}")
            raw = path.read_bytes()
            after = path.stat()
            if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
                raise LspError("DOCUMENT_CHANGED", f"File changed while reading: {path}")
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError as error:
                raise LspError("SOURCE_ENCODING_UNSUPPORTED",
                               f"Source file must be UTF-8: {path}; byte {error.start}: {error.reason}") from error
            return Snapshot(path, text, hashlib.sha256(raw).hexdigest())
        return await asyncio.to_thread(read)

    @asynccontextmanager
    async def document(self, path):
        path = Path(path).resolve()
        lock = self._locks.setdefault(path, asyncio.Lock())
        async with lock:
            self._active.add(path)
            try:
                async with self._sync_lock:
                    current = await self.snapshot(path)
                    previous = self.open_documents.get(path)
                    if previous is None:
                        await self.lsp_client.notify("textDocument/didOpen", {
                            "textDocument": {"uri": current.uri,
                                             "languageId": "c" if path.suffix == ".c" else "cpp",
                                             "version": 1, "text": current.text}})
                    elif previous.digest != current.digest:
                        current.version = previous.version + 1
                        self.lsp_client.diagnostic_events.setdefault(current.uri, asyncio.Event()).clear()
                        await self.lsp_client.notify("textDocument/didChange", {
                            "textDocument": {"uri": current.uri, "version": current.version},
                            "contentChanges": [{"text": current.text}],
                            "wantDiagnostics": True})
                    else:
                        current.version = previous.version
                    self.open_documents[path] = current
                    self.open_documents.move_to_end(path)
                    await self._evict()
                yield current
                latest = await self.snapshot(path)
                if latest.digest != current.digest:
                    raise LspError("DOCUMENT_CHANGED", f"File changed during semantic query: {path}")
            finally:
                self._active.discard(path)
                async with self._sync_lock:
                    await self._evict()

    async def _evict(self):
        for path in list(self.open_documents):
            if len(self.open_documents) <= self.max_open_documents:
                break
            if path not in self._active:
                document = self.open_documents.pop(path)
                await self.lsp_client.notify("textDocument/didClose", {"textDocument": {"uri": document.uri}})
                self.lsp_client.diagnostics.pop(document.uri, None)
                self.lsp_client.diagnostic_events.pop(document.uri, None)
                lock = self._locks.get(path)
                if lock is not None and not lock.locked():
                    self._locks.pop(path, None)

    async def close_all_documents(self):
        for document in list(self.open_documents.values()):
            await self.lsp_client.notify("textDocument/didClose", {"textDocument": {"uri": document.uri}})
        self.open_documents.clear()
        self._locks.clear()

class LSPMethods:
    POSITION_METHODS = {
        "definition": ("textDocument/definition", "definitionProvider"),
        "declaration": ("textDocument/declaration", "declarationProvider"),
        "implementation": ("textDocument/implementation", "implementationProvider"),
        "type_definition": ("textDocument/typeDefinition", "typeDefinitionProvider"),
        "hover": ("textDocument/hover", "hoverProvider"),
        "references": ("textDocument/references", "referencesProvider"),
    }

    def __init__(self, lsp_client, *, max_open_documents=32):
        self.lsp_client = lsp_client
        self.document_manager = DocumentManager(
            lsp_client, max_open_documents=max_open_documents)

    def require(self, capability):
        if not self.lsp_client.initialized or not self.lsp_client.clangd_server.is_alive:
            raise LspError("BACKEND_UNAVAILABLE", str(self.lsp_client.failure or "clangd is not ready"))
        if self.lsp_client.capabilities.get(capability) is None or self.lsp_client.capabilities.get(capability) is False:
            raise LspError("UNSUPPORTED", f"clangd does not advertise {capability}")

    async def query(self, operation, snapshot, line, column):
        position = snapshot.position(line, column, self.lsp_client.position_encoding)
        params = {"textDocument": {"uri": snapshot.uri}, "position": position}
        if operation in self.POSITION_METHODS:
            method, capability = self.POSITION_METHODS[operation]
            self.require(capability)
            if operation == "references":
                params["context"] = {"includeDeclaration": True}
            return await self.lsp_client.request(method, params)
        if operation in ("callers", "callees", "supertypes", "subtypes"):
            is_call = operation in ("callers", "callees")
            self.require("callHierarchyProvider" if is_call else "typeHierarchyProvider")
            prepared = await self.lsp_client.request(
                "textDocument/prepareCallHierarchy" if is_call else "textDocument/prepareTypeHierarchy", params)
            results = []
            for item in prepared or []:
                method = {"callers": "callHierarchy/incomingCalls", "callees": "callHierarchy/outgoingCalls",
                          "supertypes": "typeHierarchy/supertypes", "subtypes": "typeHierarchy/subtypes"}[operation]
                results.extend(await self.lsp_client.request(method, {"item": item}) or [])
            return results
        if operation == "symbol_info":
            return await self.lsp_client.request("textDocument/symbolInfo", params)
        raise ValueError(f"Unknown semantic operation: {operation}")

    async def search_symbols(self, query):
        self.require("workspaceSymbolProvider")
        return await self.lsp_client.request("workspace/symbol", {"query": query}) or []

    async def document_symbols(self, snapshot):
        self.require("documentSymbolProvider")
        return await self.lsp_client.request("textDocument/documentSymbol",
                                             {"textDocument": {"uri": snapshot.uri}}) or []

    async def diagnostics(self, snapshot):
        event = self.lsp_client.diagnostic_events.setdefault(snapshot.uri, asyncio.Event())
        async with asyncio.timeout(self.lsp_client.request_timeout):
            while True:
                latest = self.lsp_client.diagnostics.get(snapshot.uri)
                if latest is not None and latest.get("version") == snapshot.version:
                    return latest["diagnostics"]
                event.clear()
                await event.wait()
