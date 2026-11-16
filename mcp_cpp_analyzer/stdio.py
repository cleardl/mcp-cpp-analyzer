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
        # MCP writes through this private duplicate. Native fd/Win32 stdout logs
        # continue to stderr, including messages emitted before lifespan starts.
        sys.stdout = io.TextIOWrapper(os.fdopen(protocol_fd, "wb", buffering=0),
                                      encoding="utf-8", write_through=True)
    except BaseException:
        os.close(protocol_fd)
        raise
