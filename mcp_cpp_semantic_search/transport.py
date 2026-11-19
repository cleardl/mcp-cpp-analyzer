"""MCP transport adapters; imported only by the analysis backend and tests."""
import asyncio
from contextlib import asynccontextmanager

import anyio
import mcp_types
from mcp.shared.message import SessionMessage

from .shared_protocol import MAX_FRAME


async def serve_stdio(server, protocol):
    from fastmcp.server.context import reset_transport, set_transport
    from mcp.server.stdio import stdio_server

    token = set_transport("stdio")
    try:
        async with stdio_server(stdout=anyio.wrap_file(protocol)) as (reader, writer):
            low_level = server._mcp_server
            await low_level.run(reader, writer, low_level.create_initialization_options())
    finally:
        reset_transport(token)


@asynccontextmanager
async def socket_transport(reader, writer):
    incoming, reads = anyio.create_memory_object_stream(0)
    writes, outgoing = anyio.create_memory_object_stream(0)

    async def read():
        async with incoming:
            while True:
                try:
                    frame = await reader.readline()
                except ConnectionResetError:
                    return
                if not frame:
                    return
                if len(frame) > MAX_FRAME:
                    raise ValueError("MCP frame exceeds 16 MiB")
                try:
                    try:
                        message = mcp_types.jsonrpc_message_adapter.validate_json(frame, by_name=False)
                    except ValueError as error:
                        await incoming.send(error)
                    else:
                        await incoming.send(SessionMessage(message))
                except (anyio.ClosedResourceError, anyio.BrokenResourceError):
                    return

    async def write():
        async with outgoing:
            async for message in outgoing:
                frame = message.message.model_dump_json(by_alias=True, exclude_unset=True).encode("utf-8") + b"\n"
                try:
                    writer.write(frame)
                    await writer.drain()
                except (BrokenPipeError, ConnectionResetError):
                    await incoming.aclose()
                    return

    async with anyio.create_task_group() as group:
        group.start_soon(read)
        group.start_soon(write)
        try:
            yield reads, writes
        finally:
            group.cancel_scope.cancel()
