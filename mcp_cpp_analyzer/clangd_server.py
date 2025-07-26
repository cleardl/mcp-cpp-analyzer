import asyncio
import logging
from typing import Optional, Dict, Any
from pathlib import Path

logger = logging.getLogger(__name__)

class ClangdServer:
    def __init__(self, clangd_path: str, compile_db_path: Path):
        logger.info("Clangd init")
        self.clangd_path = clangd_path
        self.compile_db_path = compile_db_path
        self.process: Optional[asyncio.subprocess.Process] = None
        self._stderr_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        if self.process:
            logger.warning("Clangd process already started")
            return
        
        self._verify_compile_database()
        
        args = [
            self.clangd_path,
            "--header-insertion=never",
            "--clang-tidy=false",
            "--completion-style=detailed",
            "--log=error",
            "--background-index",
            f"--compile-commands-dir={self.compile_db_path.parent}"
        ]

        try:
            self.process = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            logger.info(f"Clangd process started with PID: {self.process.pid}")
            
            self._stderr_task = asyncio.create_task(self._monitor_stderr())
            
        except Exception as e:
            logger.error(f"Failed to start clangd: {e}")
            raise

    async def stop(self) -> None:
        if not self.process:
            return
            
        logger.info("Stopping clangd process...")
        
        if self._stderr_task:
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except asyncio.CancelledError:
                pass
            self._stderr_task = None
            
        try:
            self.process.terminate()
            
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("Clangd didn't stop gracefully, force killing...")
                self.process.kill()
                await self.process.wait()
                
            logger.info("Clangd process stopped")
            
        except Exception as e:
            logger.error(f"Error stopping clangd: {e}")
        finally:
            self.process = None
            
    async def _monitor_stderr(self) -> None:
        if not self.process or not self.process.stderr:
            return
            
        try:
            while self.process.returncode is None:
                line = await self.process.stderr.readline()
                if not line:
                    break
                    
                stderr_text = line.decode('utf-8', errors='replace').strip()
                if stderr_text:
                    logger.warning(f"[CLANGD STDERR] {stderr_text}")
                    
        except Exception as e:
            logger.error(f"Error monitoring clangd stderr: {e}")

    def _verify_compile_database(self) -> None:
        compile_db_path = self.compile_db_path
        
        if not compile_db_path.exists():
            raise FileNotFoundError(f"Compile database not found: {compile_db_path}")
        
        logger.info(f"Found compile database: {compile_db_path}")

    @property
    def is_alive(self) -> bool:
        if self.process is None:
            return False
            
        if self.process.returncode is not None:
            logger.debug(f" Clangd process died with return code: {self.process.returncode}")
            return False
            
        if (self.process.stdin is None or self.process.stdin.is_closing()):
            logger.debug(f" Clangd process stdin is closed")
            return False
            
        if (self.process.stdout is None or self.process.stderr is None):
            logger.debug(f" Clangd process stdout/stderr is None")
            return False
            
        return True