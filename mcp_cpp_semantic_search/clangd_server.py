"""Own one clangd process; protocol shutdown is handled by ClangdClient."""
import asyncio
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

class ClangdServer:
    def __init__(self, clangd_path: str, compile_db_path: Path, *, background_index=True):
        self.clangd_path = clangd_path
        self.compile_db_path = Path(compile_db_path)
        self.background_index = background_index
        self.process = None
        self._stderr_task = None
        self.stderr_tail = []

    @property
    def is_alive(self):
        return self.process is not None and self.process.returncode is None

    async def start(self):
        if self.process is not None:
            raise RuntimeError("clangd process has already been created")
        if not self.compile_db_path.is_file():
            raise FileNotFoundError(self.compile_db_path)
        args = [
            self.clangd_path,
            f"--compile-commands-dir={self.compile_db_path.parent}",
            "--background-index" if self.background_index else "--background-index=false",
            "--header-insertion=never", "--clang-tidy=false", "--log=error",
        ]
        self.process = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
        self._stderr_task = asyncio.create_task(self._read_stderr())

    async def _read_stderr(self):
        while line := await self.process.stderr.readline():
            message = line.decode("utf-8", errors="replace").rstrip()
            self.stderr_tail = (self.stderr_tail + [message])[-20:]
            logger.warning("clangd: %s", message)

    async def stop(self):
        process = self.process
        if process is None:
            return
        if process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), 2)
            except asyncio.TimeoutError:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 3)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
        if self._stderr_task:
            await self._stderr_task
        self.process = None
