"""Reserve the protocol pipe before imports or native initialization can log."""
import io
import os
import sys


def reserve_protocol_stdout():
    if sys.stdout.fileno() != 1:
        raise RuntimeError("The stdio entrypoint requires standard stdout descriptor 1")
    sys.stdout.flush()
    sys.stderr.reconfigure(encoding="utf-8")
    protocol_fd = os.dup(1)
    try:
        os.dup2(2, 1)
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            import msvcrt

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.SetStdHandle.argtypes = (wintypes.DWORD, wintypes.HANDLE)
            kernel32.SetStdHandle.restype = wintypes.BOOL
            if not kernel32.SetStdHandle(-11, msvcrt.get_osfhandle(2)):
                raise ctypes.WinError(ctypes.get_last_error())
        protocol = io.TextIOWrapper(os.fdopen(protocol_fd, "wb", buffering=0),
                                    encoding="utf-8", write_through=True)
        # Both Python print() and native stdout belong to diagnostics. Only the
        # MCP transport receives the private protocol stream.
        sys.stdout = sys.stderr
        return protocol
    except BaseException:
        os.close(protocol_fd)
        raise


async def serve_stdio(server, protocol):
    import anyio
    from fastmcp.server.context import reset_transport, set_transport
    from mcp.server.stdio import stdio_server

    token = set_transport("stdio")
    try:
        async with stdio_server(stdout=anyio.wrap_file(protocol)) as (reader, writer):
            # FastMCP 4.2's runner cannot accept explicit streams. Its low-level
            # server drives the same FastMCP lifespan through the installed proxy.
            low_level = server._mcp_server
            await low_level.run(reader, writer, low_level.create_initialization_options())
    finally:
        reset_transport(token)
