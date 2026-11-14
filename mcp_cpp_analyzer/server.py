"""FastMCP 4 server: five batch-first tools, with optional discovery/code transforms."""
import asyncio
import json
from typing import Annotated, Literal

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.dependencies import CurrentContext
from fastmcp.server.lifespan import lifespan as server_lifespan
from fastmcp.tools import ToolResult
from pydantic import Field

from .config import Config
from .query_service import FileOperation, Operation, QueryService, SymbolTarget, compact_payload

Detail = Literal["compact", "standard", "full"]
QueryText = Annotated[str, Field(min_length=1, max_length=4096)]
BatchText = Annotated[list[QueryText], Field(min_length=1, max_length=16)]
BatchNames = Annotated[list[Annotated[str, Field(min_length=1, max_length=200)]], Field(min_length=1, max_length=16)]
Limit = Annotated[int, Field(ge=1, le=100)]
READ_ONLY = {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False}
FIELDS = {"file", "line", "column", "name", "kind", "symbol_id", "container", "detail",
          "signature", "source", "source_start_line", "source_truncated", "text_truncated",
          "range", "severity", "message", "call_sites", "usr", "id", "containerName"}

def render_results(service, results, *, detail="compact", fields=None, max_items=80, max_chars=24000):
    if fields is not None and (not fields or set(fields) - FIELDS):
        raise ToolError(f"fields must be selected from: {', '.join(sorted(FIELDS))}")
    results = [dict(result, items=list(result.get("items", []))) for result in results]
    remaining = max_items
    for result in results:
        rows = result["items"]
        if len(rows) > remaining:
            result["items"] = rows[:remaining]
            result["returned"] = remaining
            result["truncated"] = True
            result["cursor"] = f"{result['_key']}/{result['_offset'] + remaining}"
        remaining -= len(result["items"])

    def serialize():
        return json.dumps(compact_payload(results, service.config.workspace_root,
                                           detail=detail, fields=fields),
                          ensure_ascii=False, separators=(",", ":"))

    text = serialize()
    while len(text) > max_chars:
        candidate = next((result for result in reversed(results) if result["items"]), None)
        if candidate is None:
            raise ToolError("Response metadata exceeds max_chars; submit fewer queries")
        candidate["items"].pop()
        candidate["returned"] = len(candidate["items"])
        candidate["truncated"] = True
        candidate["cursor"] = f"{candidate['_key']}/{candidate['_offset'] + candidate['returned']}"
        text = serialize()
    if results and all("error" in result for result in results):
        raise ToolError(text)
    for result in results:
        if result.get("_key"):
            service.pages.get(result["_key"])
    # A single compact text representation avoids duplicating text and structuredContent.
    if service.config.tool_mode == "code":
        return ToolResult(content=[], structured_content=json.loads(text))
    return ToolResult(content=text)

def create_server(config, *, service_factory=QueryService):
    @server_lifespan
    async def lifespan(app):
        service = service_factory(config)
        try:
            await service.start()
            yield {"service": service}
        finally:
            await service.stop()

    mcp = FastMCP(
        "cpp-analyzer", version="0.2.0", lifespan=lifespan,
        instructions=(
            ("Use search/get_schema to discover tools, then execute sandboxed code with call_tool. "
             "Filter and combine queries in the sandbox; return only the needed data. "
             if config.tool_mode == "code" else "") +
            "C++ semantic queries. Start with search_symbols or query_files(symbols); reuse returned symbol_id. "
            "Batch independent targets and operations. compact results use files + columns/rows; file indexes refer "
            "to files relative to root, positions are 1-based Unicode codepoints. Empty results are valid; index "
            "coverage is not guaranteed. Use cursors for more rows, fields to project, full only when needed. "
            "Changed files invalidate symbol handles and result cursors."
        ),
    )

    def service(ctx: Context):
        return ctx.lifespan_context["service"]

    @mcp.tool(annotations=READ_ONLY)
    async def search_symbols(
        queries: BatchNames, ctx: Context = CurrentContext(), path_prefix: str | None = None,
        kinds: list[int] | None = None, limit: Limit = 20, detail: Detail = "compact",
        fields: list[str] | None = None,
    ) -> ToolResult:
        """Batch name discovery. Optional path/kind filters; returns reusable symbol_id and result cursors."""
        backend = service(ctx)
        results = await backend.search(queries, path_prefix=path_prefix, kinds=kinds, limit=limit, detail=detail)
        return render_results(backend, results, detail=detail, fields=fields)

    @mcp.tool(annotations=READ_ONLY)
    async def query_symbols(
        targets: Annotated[list[SymbolTarget], Field(min_length=1, max_length=16)],
        operations: Annotated[list[Operation], Field(min_length=1, max_length=12)],
        ctx: Context = CurrentContext(), limit: Limit = 20, detail: Detail = "compact",
        fields: list[str] | None = None,
        max_items: Annotated[int, Field(ge=1, le=500)] = 80,
        max_chars: Annotated[int, Field(ge=1000, le=100000)] = 24000,
    ) -> ToolResult:
        """Batch targets x operations: definitions, references, context, callers/callees and type hierarchy."""
        if len(targets) * len(operations) > 64:
            raise ToolError("A batch supports at most 64 target/operation pairs")
        if len(set(operations)) != len(operations):
            raise ToolError("operations must be unique")
        backend = service(ctx)
        results = await backend.query(targets, operations, limit=limit, detail=detail)
        return render_results(backend, results, detail=detail, fields=fields,
                              max_items=max_items, max_chars=max_chars)

    @mcp.tool(annotations=READ_ONLY)
    async def query_files(
        files: BatchText, operations: Annotated[list[FileOperation], Field(min_length=1, max_length=2)],
        ctx: Context = CurrentContext(), limit: Limit = 20, detail: Detail = "compact",
        fields: list[str] | None = None,
    ) -> ToolResult:
        """Batch file outlines or current-version compiler diagnostics. Paths can be workspace-relative."""
        if len(set(operations)) != len(operations):
            raise ToolError("operations must be unique")
        backend = service(ctx)
        return render_results(backend, await backend.files(files, operations, limit=limit, detail=detail),
                              detail=detail, fields=fields)

    @mcp.tool(annotations=READ_ONLY)
    async def read_results(
        cursors: BatchText, ctx: Context = CurrentContext(), limit: Limit = 20, detail: Detail = "compact",
        fields: list[str] | None = None,
    ) -> ToolResult:
        """Batch page/expand cached results. Expired or changed-source cursors explicitly fail."""
        backend = service(ctx)
        async def one(cursor):
            return (await backend.read_page(cursor, limit=limit, detail=detail))[0]
        results = await asyncio.gather(
            *(backend._bounded(lambda cursor=cursor: one(cursor)) for cursor in cursors))
        return render_results(backend, results, detail=detail, fields=fields)

    @mcp.tool(annotations=READ_ONLY)
    async def workspace_status(ctx: Context = CurrentContext()) -> ToolResult:
        """Report clangd version, capabilities, index activity and failures; works after backend death."""
        value = service(ctx).status()
        if config.tool_mode == "code":
            return ToolResult(content=[], structured_content=value)
        return ToolResult(content=json.dumps(value, ensure_ascii=False, separators=(",", ":")))

    if config.tool_mode == "search":
        from fastmcp.server.transforms.search import RegexSearchTransform
        mcp.add_transform(RegexSearchTransform())
    elif config.tool_mode == "code":
        from fastmcp.experimental.transforms.code_mode import CodeMode
        mcp.add_transform(CodeMode(max_tool_calls=50, wall_clock_timeout_secs=120))
    return mcp

def create_mcp():
    """Native FastMCP factory using the client's working directory and environment."""
    return create_server(Config.from_environment())
