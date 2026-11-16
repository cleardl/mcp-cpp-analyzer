"""Private shared-backend executable, started on demand by the stdio proxy."""
import argparse
import ctypes
from ctypes import wintypes
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, required=True)
    arguments = parser.parse_args()
    directory = arguments.profile.resolve().parent
    descriptor = os.open(directory / "backend.log", os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    os.dup2(descriptor, 1)
    os.dup2(descriptor, 2)
    if descriptor not in (1, 2):
        os.close(descriptor)
    sys.stdout = open(1, "w", encoding="utf-8", buffering=1, closefd=False)
    sys.stderr = open(2, "w", encoding="utf-8", buffering=1, closefd=False)
    import msvcrt
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.SetStdHandle.argtypes = [wintypes.DWORD, wintypes.HANDLE]
    kernel.SetStdHandle.restype = wintypes.BOOL
    for slot, fd in ((-11, 1), (-12, 2)):
        if not kernel.SetStdHandle(slot, msvcrt.get_osfhandle(fd)):
            raise ctypes.WinError(ctypes.get_last_error())
    logging.basicConfig(level=logging.WARNING,
        handlers=[RotatingFileHandler(directory / "analysis.log", maxBytes=1024 * 1024,
                                      backupCount=2, encoding="utf-8")])
    from mcp_cpp_analyzer.shared_runtime import atomic_json, load_profile
    from mcp_cpp_analyzer.windows_runtime import process_created
    profile = None
    job = None
    clean = False
    exit_code = 1
    try:
        profile = load_profile(arguments.profile)
        atomic_json(directory / "starting.json", {"pid": os.getpid(), "created": process_created(),
                                                  "launch_id": profile["launch_id"]})
        from mcp_cpp_analyzer.windows_job import ProcessTree
        job = ProcessTree()
        if getattr(sys, "frozen", False):
            sandbox = Path(__file__).resolve().with_name("monty.exe")
            if not sandbox.is_file():
                raise FileNotFoundError(f"Bundled Code Mode runtime is missing: {sandbox}")
            os.environ["MONTY_BIN"] = str(sandbox)
        import asyncio
        from mcp_cpp_analyzer.shared_backend import SharedBackend
        asyncio.run(SharedBackend(profile).run())
        clean = True
        exit_code = 0
    except BaseException as error:
        logging.exception("Shared backend stopped unexpectedly")
        if profile is not None:
            atomic_json(directory / "failure.json", {"launch_id": profile["launch_id"], "error": str(error)})
    finally:
        if job is not None:
            try:
                job.finish(clean=clean)
            except Exception as error:
                logging.exception("Backend process-tree cleanup failed")
                if profile is not None:
                    atomic_json(directory / "failure.json", {"launch_id": profile["launch_id"], "error": str(error)})
                exit_code = 1
                job.finish(clean=False)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
