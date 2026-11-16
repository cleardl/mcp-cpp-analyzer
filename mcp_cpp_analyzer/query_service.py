"""Batch semantic queries with bounded snapshots, handles and result cursors."""
import asyncio
from collections import OrderedDict, defaultdict
import json
from pathlib import Path
import secrets
import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .clangd_client import ClangdClient, LspError
from .clangd_server import ClangdServer
from .lsp_methods import LSPMethods
from . import __version__, build_info

Operation = Literal["definition", "declaration", "implementation", "type_definition", "hover",
                    "references", "callers", "callees", "supertypes", "subtypes", "context", "symbol_info"]
FileOperation = Literal["symbols", "diagnostics"]


class SymbolTarget(BaseModel):
    """Use a returned symbol_id, or file + 1-based Unicode codepoint line/column."""
    model_config = ConfigDict(extra="forbid")
    symbol_id: str | None = None
    file: str | None = None
    line: int | None = Field(default=None, ge=1)
    column: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_target(self):
        positioned = (self.file, self.line, self.column)
        if self.symbol_id:
            if any(value is not None for value in positioned):
                raise ValueError("Use symbol_id OR file/line/column")
        elif not all(value is not None for value in positioned):
            raise ValueError("Supply symbol_id, or all of file, line and column")
        return self


class BoundedStore:
    def __init__(self, *, max_entries=256, ttl=600, max_bytes=16 * 1024 * 1024):
        self.entries = OrderedDict()
        self.max_entries, self.ttl, self.max_bytes = max_entries, ttl, max_bytes
        self.byte_count = 0
        self.prefix = secrets.token_hex(4)
        self.counter = 0

    def put(self, value):
        size = len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        if size > self.max_bytes:
            raise LspError("RESULT_TOO_LARGE", "Result exceeds cache budget; narrow the query scope")
        self._expire()
        while self.entries and (len(self.entries) >= self.max_entries or self.byte_count + size > self.max_bytes):
            _, (_, _, removed) = self.entries.popitem(last=False)
            self.byte_count -= removed
        self.counter += 1
        key = f"{self.prefix}:{self.counter:x}"
        self.entries[key] = (time.monotonic(), value, size)
        self.byte_count += size
        return key

    def _expire(self):
        now = time.monotonic()
        for key, (created, _, size) in list(self.entries.items()):
            if now - created > self.ttl:
                self.entries.pop(key)
                self.byte_count -= size

    def get(self, key):
        self._expire()
        if key not in self.entries:
            raise LspError("EXPIRED_HANDLE", "Handle expired or belongs to another server; repeat discovery")
        self.entries.move_to_end(key)
        return self.entries[key][1]


class QueryService:
    def __init__(self, config):
        self.config = config
        self.backend = ClangdServer(config.clangd_path, config.find_compile_commands(),
                                    background_index=config.background_index)
        self.client = ClangdClient(self.backend, config.workspace_root,
                                   request_timeout=config.request_timeout, init_timeout=config.init_timeout)
        self.methods = LSPMethods(self.client, max_open_documents=config.max_open_documents)
        self.documents = self.methods.document_manager
        self.handles = BoundedStore(max_entries=4096, max_bytes=4 * 1024 * 1024)
        self.pages = BoundedStore(max_entries=128, ttl=300)
        self._semaphore = asyncio.Semaphore(config.concurrency)
        self.runtime_status = None

    async def start(self):
        await self.client.start()

    async def stop(self):
        try:
            if self.client.initialized:
                await self.documents.close_all_documents()
        finally:
            await self.client.stop()

    def status(self):
        progress = self.client.index_progress
        index_state = ("disabled" if not self.config.background_index else
                       "not_observed" if progress is None else
                       "idle" if progress["kind"] == "end" else "indexing")
        return {
            "ready": self.client.initialized and self.backend.is_alive,
            "service_version": __version__,
            "build": build_info(),
            "server": self.client.server_info, "position_encoding": self.client.position_encoding,
            "workspace": str(self.config.workspace_root), "compile_db": str(self.config.find_compile_commands()),
            "failure": str(self.client.failure) if self.client.failure else None,
            "pending_requests": len(self.client.pending_requests),
            "clangd_pid": self.backend.process.pid if self.backend.is_alive else None,
            **({"shared_backend": self.runtime_status()} if self.runtime_status is not None else {}),
            "open_documents": len(self.documents.open_documents),
            "index_complete": None, "active_progress": list(self.client.progress.values()),
            "index_activity": {"state": index_state, "last_progress": progress,
                               "completed_runs": self.client.index_runs_completed},
            "capabilities": self.client.capabilities, "stderr_tail": self.backend.stderr_tail,
        }

    @staticmethod
    def _location_identity(item):
        if "targetUri" in item:
            uri, span = item["targetUri"], item.get("targetSelectionRange", item["targetRange"])
        else:
            item = item.get("location", item)
            uri, span = item["uri"], item.get("selectionRange", item["range"])
        return (Path.from_uri(uri).resolve(), span["start"]["line"], span["start"]["character"])

    async def _callee_location(self, value, snapshot, detail, snapshots):
        related = value["to"]
        row = await self._location(related, detail=detail, snapshots=snapshots)
        row["location_source"] = "index_unverified"
        sites = value.get("fromRanges", [])
        if not sites:
            return row
        position = sites[0]["start"]
        info = await self.methods.query("symbol_info", snapshot, position["line"] + 1,
            snapshot.column(position["line"], position["character"], self.client.position_encoding))
        matches = [item for item in info or [] if item.get("id") == related.get("data")]
        if len(matches) == 1 and matches[0].get("definitionRange"):
            definition = matches[0]["definitionRange"]
            original = row
            row = await self._location({**definition,
                **{name: related[name] for name in ("name", "kind", "containerName") if name in related}},
                detail=detail, snapshots=snapshots)
            row["location_source"] = "ast_definition"
            if self._location_identity(definition) != self._location_identity(related):
                row["ambiguity"] = "index_location_conflict"
                row["index_location"] = {key: original[key] for key in ("file", "line", "column", "name") if key in original}
        return row

    def file_path(self, value):
        path = Path(value)
        return path.resolve() if path.is_absolute() else (self.config.workspace_root / path).resolve()

    async def _handle(self, row, snapshot):
        return self.handles.put({"file": str(snapshot.path), "line": row["line"],
                                 "column": row["column"], "digest": snapshot.digest})

    async def _location(self, item, *, detail="compact", snapshots=None):
        if "targetUri" in item:
            uri, span = item["targetUri"], item.get("targetSelectionRange", item["targetRange"])
        else:
            location = item.get("location", item)
            uri = location["uri"]
            span = location.get("selectionRange", location["range"])
        path = Path.from_uri(uri).resolve()
        if snapshots is None:
            snapshots = {}
        if path not in snapshots:
            snapshots[path] = await self.documents.snapshot(path)
        snapshot = snapshots[path]
        start = span["start"]
        row = {"file": str(snapshot.path), "line": start["line"] + 1,
               "column": snapshot.column(start["line"], start["character"], self.client.position_encoding)}
        for name in ("name", "kind", "containerName", "detail", "severity", "message"):
            if name in item:
                row["container" if name == "containerName" else name] = item[name]
        row["symbol_id"] = await self._handle(row, snapshot)
        if detail == "full":
            row["range"] = self._range(snapshot, span)
        return row

    def _range(self, snapshot, span):
        return [span["start"]["line"] + 1,
                snapshot.column(span["start"]["line"], span["start"]["character"], self.client.position_encoding),
                span["end"]["line"] + 1,
                snapshot.column(span["end"]["line"], span["end"]["character"], self.client.position_encoding)]

    def _flatten_symbols(self, values, uri, container=""):
        result = []
        for item in values:
            normalized = dict(item)
            if "location" not in normalized:
                normalized["uri"] = uri
                normalized["containerName"] = container
            result.append(normalized)
            result.extend(self._flatten_symbols(item.get("children", []), uri, item.get("name", container)))
        return result

    async def _rows(self, operation, raw, *, snapshot=None, line=None, column=None, detail="compact"):
        snapshots = {snapshot.path: snapshot} if snapshot is not None else {}
        if operation in ("hover", "context"):
            if not raw:
                return []
            contents = raw.get("contents", "")
            if isinstance(contents, list):
                text = "\n".join(item if isinstance(item, str) else item.get("value", "") for item in contents)
            else:
                text = contents if isinstance(contents, str) else contents.get("value", "")
            cap = 8000 if detail == "full" else 600
            row = {"file": str(snapshot.path), "line": line, "column": column,
                   "signature": text[:cap], "text_truncated": len(text) > cap}
            row["symbol_id"] = await self._handle(row, snapshot)
            if operation == "context":
                symbols = self._flatten_symbols(await self.methods.document_symbols(snapshot), snapshot.uri)
                position = snapshot.position(line, column, self.client.position_encoding)
                candidates = [item for item in symbols if "range" in item
                              and (item["range"]["start"]["line"], item["range"]["start"]["character"])
                              <= (position["line"], position["character"])
                              <= (item["range"]["end"]["line"], item["range"]["end"]["character"])]
                if candidates:
                    chosen = min(candidates, key=lambda item: item["range"]["end"]["line"] - item["range"]["start"]["line"])
                    begin, end = chosen["range"]["start"]["line"], chosen["range"]["end"]["line"] + 1
                    max_lines = 80 if detail == "full" else 12
                    source = "\n".join(snapshot.lines[begin:min(end, begin + max_lines)])
                    row.update(name=chosen["name"], source=source[:8000 if detail == "full" else 2000],
                               source_start_line=begin + 1,
                               source_truncated=end > begin + max_lines or len(source) > (8000 if detail == "full" else 2000))
            return [row]
        if operation == "symbol_info":
            return raw if isinstance(raw, list) else ([raw] if raw else [])
        values = raw if isinstance(raw, list) else ([raw] if raw else [])
        rows = []
        for value in values:
            if operation in ("callers", "callees"):
                related = dict(value["from" if operation == "callers" else "to"])
                if operation == "callees":
                    row = await self._callee_location(value, snapshot, detail, snapshots)
                else:
                    row = await self._location(related, detail=detail, snapshots=snapshots)
                    row["location_source"] = "index_unverified"
                sites_source = snapshots[Path(row["file"])] if operation == "callers" else snapshot
                sites = value.get("fromRanges", [])
                cap = 64 if detail == "full" else 4
                row["call_site_file"] = str(sites_source.path)
                row["call_sites"] = [self._range(sites_source, span) for span in sites[:cap]]
                if len(sites) > cap:
                    row["call_sites_truncated"] = True
            elif operation == "diagnostics":
                row = await self._location({**value, "uri": snapshot.uri}, detail=detail, snapshots=snapshots)
            else:
                row = await self._location(value, detail=detail, snapshots=snapshots)
            rows.append(row)
        return rows

    async def _page(self, entry, *, offset=0, limit=20, detail="compact", key=None):
        snapshot = None
        if entry.get("source"):
            snapshot = await self.documents.snapshot(entry["source"])
            if snapshot.digest != entry["digest"]:
                raise LspError("STALE_RESULT", "Source changed; repeat the semantic query")
        values = entry["values"]
        selected = values[offset:offset + limit]
        if entry["op"] in ("hover", "context"):
            rows = await self._rows(entry["op"], selected[0], snapshot=snapshot,
                                    line=entry["line"], column=entry["column"], detail=detail) if selected else []
        elif entry["op"] == "symbol_info":
            rows = selected
        else:
            rows = await self._rows(entry["op"], selected, snapshot=snapshot, detail=detail)
        next_offset = offset + len(selected)
        key = (key or self.pages.put(entry)) if values else None
        document_local = entry.get("source") and entry["op"] in ("symbols", "diagnostics", "hover", "context", "symbol_info")
        result = {"op": entry["op"], "items": rows, "returned": len(rows),
                  "backend_returned": len(values), "truncated": next_offset < len(values),
                  "coverage": "document" if document_local else "index-dependent", "_key": key, "_offset": offset,
                  **entry.get("metadata", {})}
        warnings = list(result.get("warnings", []))
        if not document_local and not values:
            warnings.append("INDEX_COVERAGE_UNVERIFIED")
        if any(row.get("ambiguity") for row in rows):
            warnings.append("INDEX_LOCATION_CONFLICT")
        if entry["op"] in ("callers", "callees"):
            result["linkage_verified"] = False
            if any(row.get("location_source") == "index_unverified" for row in rows):
                warnings.append("CALL_LOCATION_UNVERIFIED")
        if warnings:
            result["warnings"] = list(dict.fromkeys(warnings))
        if key:
            result["result_id"] = key
        if next_offset < len(values):
            result["cursor"] = f"{key}/{next_offset}"
        return result

    async def _bounded(self, function):
        async with self._semaphore:
            try:
                return await function()
            except (LspError, OSError, ValueError, UnicodeError, asyncio.TimeoutError) as error:
                return {"error": {"code": str(getattr(error, "code", type(error).__name__)),
                                  "message": str(error) or "clangd query timed out"}}

    async def search(self, queries, *, path_prefix=None, kinds=None, limit=20, detail="compact"):
        async def one(index, query):
            if not query.strip():
                raise ValueError("Symbol search must contain a non-whitespace name")
            raw = await self.methods.search_symbols(query)
            if path_prefix:
                prefix = self.file_path(path_prefix)
                if not prefix.exists():
                    raise FileNotFoundError(f"Search scope does not exist: {prefix}")
                raw = [item for item in raw if Path.from_uri(item["location"]["uri"]).resolve().is_relative_to(prefix)]
            if kinds:
                raw = [item for item in raw if item["kind"] in kinds]
            return {"query": index, **await self._page({"op": "symbols", "values": raw}, limit=limit, detail=detail)}
        values = await asyncio.gather(*(self._bounded(lambda i=i, q=q: one(i, q)) for i, q in enumerate(queries)))
        return [{"query": index, **value} for index, value in enumerate(values)]

    async def query(self, targets, operations, *, limit=20, detail="compact"):
        groups = defaultdict(list)
        results = {}
        for index, target in enumerate(targets):
            try:
                value = self.handles.get(target.symbol_id) if target.symbol_id else target.model_dump()
                groups[self.file_path(value["file"])].append((index, value))
            except LspError as error:
                for operation in operations:
                    results[(index, operation)] = {"target": index, "op": operation,
                        "error": {"code": error.code, "message": str(error)}}

        async def group(path, members):
            async with self.documents.document(path) as snapshot:
                async def one(index, target, operation):
                    if target.get("digest") and target["digest"] != snapshot.digest:
                        raise LspError("STALE_SYMBOL", "Source changed; search for this symbol again")
                    raw = await self.methods.query("hover" if operation == "context" else operation,
                                                   snapshot, target["line"], target["column"])
                    if operation in ("hover", "context"):
                        values = [raw] if raw else []
                    else:
                        values = raw if isinstance(raw, list) else ([raw] if raw else [])
                    metadata = {}
                    if operation == "definition" and values:
                        info = await self.methods.query("symbol_info", snapshot, target["line"], target["column"])
                        ast_definitions = {self._location_identity(item["definitionRange"])
                                           for item in info or [] if item.get("definitionRange")}
                        declarations = {self._location_identity(item["declarationRange"])
                                        for item in info or [] if item.get("declarationRange")}
                        locations = {self._location_identity(item) for item in values}
                        if locations <= ast_definitions:
                            metadata["location_kind"] = "ast_definition"
                        elif len(info or []) == 1 and ast_definitions and locations <= declarations:
                            # clangd toggles from a definition back to its declaration.
                            # This API asks for the definition, which the same AST knows.
                            values = [info[0]["definitionRange"]]
                            metadata.update(location_kind="ast_definition", declaration_navigation=True)
                        elif locations <= declarations:
                            metadata.update(location_kind="declaration", warnings=["IMPLEMENTATION_UNCONFIRMED"])
                        else:
                            metadata["location_kind"] = "indexed_location"
                    page = await self._page({"op": operation, "values": values, "source": str(path),
                                             "digest": snapshot.digest, "line": target["line"],
                                             "column": target["column"], "metadata": metadata}, limit=limit, detail=detail)
                    return {"target": index, **page}
                jobs = [(i, op, self._bounded(lambda i=i, t=t, op=op: one(i, t, op)))
                        for i, t in members for op in operations]
                values = await asyncio.gather(*(job[2] for job in jobs))
                for (index, operation, _), value in zip(jobs, values):
                    results[(index, operation)] = {"target": index, "op": operation, **value}
        async def guarded_group(path, members):
            try:
                await group(path, members)
            except (LspError, OSError, ValueError, UnicodeError, asyncio.TimeoutError) as error:
                for index, _ in members:
                    for operation in operations:
                        results[(index, operation)] = {"target": index, "op": operation,
                            "error": {"code": str(getattr(error, "code", type(error).__name__)),
                                      "message": str(error) or "clangd query timed out"}}
        await asyncio.gather(*(guarded_group(path, members) for path, members in groups.items()))
        return [results[(index, operation)] for index in range(len(targets)) for operation in operations]

    async def files(self, files, operations, *, limit=20, detail="compact"):
        async def one(index, file):
            path = self.file_path(file)
            async with self.documents.document(path) as snapshot:
                groups = []
                for operation in operations:
                    if operation == "symbols":
                        values = self._flatten_symbols(await self.methods.document_symbols(snapshot), snapshot.uri)
                    else:
                        values = await self.methods.diagnostics(snapshot)
                    groups.append({"file_query": index, **await self._page(
                        {"op": operation, "values": values, "source": str(path), "digest": snapshot.digest},
                        limit=limit, detail=detail)})
                return {"results": groups}
        values = await asyncio.gather(*(self._bounded(lambda i=i, f=f: one(i, f)) for i, f in enumerate(files)))
        return [row for index, value in enumerate(values) for row in value.get("results", [{"file_query": index, **value}])]

    async def read_page(self, cursor, *, limit=20, detail="compact"):
        key, separator, value = cursor.partition("/")
        offset = int(value) if separator else 0
        if offset < 0:
            raise ValueError("Cursor offset must be nonnegative")
        entry = self.pages.get(key)
        if offset > len(entry["values"]):
            raise ValueError("Cursor offset is outside the cached result")
        if entry.get("source"):
            async with self.documents.document(entry["source"]):
                return [await self._page(entry, offset=offset, limit=limit, detail=detail, key=key)]
        return [await self._page(entry, offset=offset, limit=limit, detail=detail, key=key)]


def compact_payload(results, root, *, detail="compact", fields=None):
    """One shared file table, explicit columns, and no repeated URI/range metadata."""
    paths = []
    converted = []
    errors = sum("error" in result for result in results)
    def map_files(value):
        if isinstance(value, list):
            return [map_files(item) for item in value]
        if not isinstance(value, dict):
            return value
        converted = {}
        for key, item in value.items():
            if key in ("file", "call_site_file") and isinstance(item, str):
                path = Path(item)
                name = str(path.relative_to(root)).replace("\\", "/") if path.is_relative_to(root) else str(path)
                if name not in paths:
                    paths.append(name)
                converted[key] = paths.index(name)
            else:
                converted[key] = map_files(item)
        return converted
    for result in results:
        item = {key: value for key, value in result.items() if not key.startswith("_")}
        rows = item.pop("items", [])
        if rows:
            normalized = []
            for row in rows:
                row = dict(row)
                if fields:
                    protected = {key: row[key] for key in ("location_source", "ambiguity", "index_location") if key in row}
                    if row.get("ambiguity"):
                        protected.update({key: row[key] for key in ("file", "line", "column", "call_site_file", "call_sites")
                                          if key in row})
                    row = {**{key: row[key] for key in fields if key in row}, **protected,
                           **{key: True for key in ("text_truncated", "source_truncated", "call_sites_truncated")
                              if row.get(key) is True}}
                normalized.append(map_files(row))
            if detail == "compact":
                columns = list(dict.fromkeys(key for row in normalized for key in row))
                item["columns"] = columns
                item["rows"] = [[row.get(key) for key in columns] for row in normalized]
            else:
                item["items"] = normalized
        elif "error" not in item:
            item["items"] = []
        converted.append(item)
    return {"status": "error" if errors == len(results) else ("partial" if errors else "ok"),
            "root": str(root), "files": paths, "position_encoding": "unicode-codepoints",
            "results": converted}
