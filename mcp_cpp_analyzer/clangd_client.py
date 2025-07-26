import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Dict, Any, Optional
from pathlib import Path
import time
from .clangd_server import ClangdServer

logger = logging.getLogger(__name__)
@dataclass
class PendingRequest:
    id:str
    method:str
    future:asyncio.Future
    start_time:float

    @property
    def elapsed_ms(self)->float:
        return (time.time()-self.start_time)

class ClangdClient:
    def __init__(self, clangd_server: ClangdServer):
        self.clangd_server = clangd_server
        self.request_counter = 0
        self.pending_requests: Dict[str, PendingRequest] = {}
        self.initialized = False
        self._reader_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        logger.info("Starting LSP client...")

        await self.clangd_server.start()
        
        await asyncio.sleep(0.1)
        
        await self._verify_connection()
        
        self._reader_task = asyncio.create_task(self._read_messages())
        
        await self._initialize()
        
    async def _initialize(self) -> None:
        
        logger.debug("Initializing LSP connection...")
        
        from .config import get_global_config
        config = get_global_config()
        root_uri = path_to_uri(config.compile_db_dir)
        logger.debug(f" Root URI: {root_uri}")
        
        init_params = {
            "processId": None,
            "clientInfo": {
                "name": "mini-clangaroo",
                "version": "0.1.0"
            },
            "rootUri": root_uri,
            "capabilities": {
                "textDocument": {
                    "definition": {"dynamicRegistration": False},
                    "references": {"dynamicRegistration": False},
                    "hover": {"dynamicRegistration": False},
                    "synchronization": {
                        "dynamicRegistration": False,
                        "willSave": False,
                        "willSaveWaitUntil": False,
                        "didSave": False
                    }
                },
                "workspace": {
                    "symbol": {"dynamicRegistration": False},
                    "workspaceEdit": {"documentChanges": False}
                }
            }
        }
        
        await self.request("initialize", init_params, timeout=config.lsp_init_timeout)
        await self.notify("initialized", {})
        self.initialized = True
        
    async def _verify_connection(self) -> None:
        if not self.clangd_server.is_alive:
            raise RuntimeError("Clangd process is not alive")
        
        stdin = self.clangd_server.process.stdin
        
        if hasattr(stdin, '_transport'):
            transport = stdin._transport
            
            if transport is None:
                raise RuntimeError("Transport is None")
            
            if hasattr(transport, '_closing') and transport._closing:
                raise RuntimeError("Transport is closing")
            
            if hasattr(transport, '_loop'):
                loop = transport._loop
                if loop is None:
                    raise RuntimeError("Transport loop is None")
                if loop.is_closed():
                    raise RuntimeError("Transport loop is closed")
    
    async def _send_message(self, message: Dict[str, Any]) -> None:
        try:
            await self._verify_connection()
        except Exception as e:
            logger.error(f"Connection verification failed: {e}")
            raise RuntimeError(f"Connection verification failed: {e}")
        
        content = json.dumps(message, separators=(',', ':'))
        header = f"Content-Length: {len(content)}\r\n\r\n"
        data = (header + content).encode('utf-8')
        
        try:
            stdin = self.clangd_server.process.stdin
            stdin.write(data)
            await stdin.drain()
            logger.debug(f"Successfully sent message to clangd ({len(data)} bytes)")
        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            logger.error(f"Pipe/connection error: {e}")
            raise RuntimeError(f"Failed to send message to clangd (pipe broken): {e}")
        except AttributeError as e:
            logger.error(f"AttributeError (likely proactor issue): {e}")
            raise RuntimeError(f"Failed to send message to clangd (transport error): {e}")
        except Exception as e:
            logger.error(f"Unexpected error in _send_message: {type(e).__name__}: {e}")
            raise RuntimeError(f"Failed to send message to clangd (unexpected error): {e}")
    async def notify(self, method: str, params: Dict[str, Any]) -> None:
        message = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params
        }
        
        await self._send_message(message)

    async def request(self,method:str,params:Dict[str,Any],timeout:float=10.0)->Any:
        request_id = str(self.request_counter)
        self.request_counter += 1
        message = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params
        }
        future = asyncio.Future()
        pending = PendingRequest(
            id=request_id,
            method=method,
            future=future,
            start_time=time.time()
        )
        self.pending_requests[request_id] = pending
        
        try:
            await self._send_message(message)
            result = await asyncio.wait_for(future, timeout)
            return result
        except asyncio.TimeoutError:
            if request_id in self.pending_requests:
                del self.pending_requests[request_id]
            logger.warning(f"LSP request timeout: {method} after {timeout}s")
            raise
        except Exception as e:
            if request_id in self.pending_requests:
                del self.pending_requests[request_id]
            raise

    async def _read_messages(self) -> None:
        logger.debug(" About to read from clangd stdout")
        buffer = b""
        try:
            while True:
                if not self.clangd_server.is_alive:
                    logger.error(" Clangd process died during message reading")
                    break
                    
                try:
                    data = await asyncio.wait_for(
                        self.clangd_server.process.stdout.read(4096),
                        timeout=1.0
                    )
                    if not data:
                        logger.warning("No data received from clangd stdout")
                        break
                except asyncio.TimeoutError:
                    continue
                    
                buffer += data
                
                # Parse messages
                while b"\r\n\r\n" in buffer:
                    try:
                        # Find header end
                        header_end = buffer.find(b"\r\n\r\n")
                        header = buffer[:header_end].decode('utf-8')
                        
                        # Parse Content-Length
                        content_length = 0
                        for line in header.split('\r\n'):
                            if line.startswith('Content-Length:'):
                                content_length = int(line.split(':', 1)[1].strip())
                                break
                                
                        if content_length == 0:
                            logger.error(f"Invalid header: {header}")
                            buffer = buffer[header_end + 4:]
                            continue
                            
                        # Check if we have the full message
                        message_start = header_end + 4
                        message_end = message_start + content_length
                        
                        if len(buffer) >= message_end:
                            content = buffer[message_start:message_end].decode('utf-8')
                            buffer = buffer[message_end:]
                            
                            # Parse and handle message
                            try:
                                message = json.loads(content)
                                await self._handle_message(message)
                            except json.JSONDecodeError as e:
                                logger.error(f"Invalid JSON from clangd: {e}")
                        else:
                            break
                            
                    except Exception as e:
                        logger.error(f"Error parsing message: {e}")
                        buffer = buffer[header_end + 4:]
                        
        except asyncio.CancelledError:
            logger.debug("Message reading task cancelled")
        except Exception as e:
            logger.error(f"Error in message reading: {e}")
    
    async def _handle_message(self, message: Dict[str, Any]) -> None:
        """Handle incoming LSP message"""
        
        try:
            if "id" in message and "method" not in message:
                # Response to our request
                await self._handle_response(message)
            elif "method" in message:
                # Notification from clangd server,ignore most of them
                method = message.get("method")
                if method == "window/logMessage":
                    # Log messages from clangd
                    params = message.get("params", {})
                    level = params.get("type", 1)
                    message_text = params.get("message", "")
                    if level == 1:
                        logger.error(f"clangd: {message_text}")
                    elif level == 2:
                        logger.warning(f"clangd: {message_text}")
                    else:
                        logger.debug(f"clangd: {message_text}")
                        
        except Exception as e:
            logger.error(f"Error handling message: {e}")
    async def _handle_response(self, message: Dict[str, Any]) -> None:
        request_id = str(message["id"])
        
        if request_id not in self.pending_requests:
            logger.warning(f"Received response for unknown request: {request_id}")
            return
            
        pending = self.pending_requests[request_id]
        del self.pending_requests[request_id]
        
        elapsed_ms = pending.elapsed_ms
        logger.debug(f"LSP {pending.method} completed in {elapsed_ms:.2f}ms")
        
        if "error" in message:
            error = message["error"]
            error_msg = f"LSP error in {pending.method}: {error.get('message', 'Unknown error')}"
            pending.future.set_exception(RuntimeError(error_msg))
        else:
            pending.future.set_result(message.get("result"))
    async def stop(self) -> None:
        logger.info("Stopping LSP client...")
        
        if self.initialized:
            try:
                await self.request("shutdown", {})
                await self.notify("exit", {})
            except Exception as e:
                logger.warning(f"Error during LSP shutdown: {e}")
                
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
                
        await self.clangd_server.stop()
        
        logger.info("LSP client stopped")

def path_to_uri(path: Path) -> str:
    """Convert file path to LSP URI"""
    resolved_path = path.resolve()
    
    path_str = str(resolved_path)
    
    # 将反斜杠转换为正斜杠（URI标准）
    path_str = path_str.replace('\\', '/')
    
    # 对于Windows驱动器路径，确保格式正确
    # C:/path/to/file -> file:///C:/path/to/file
    if len(path_str) >= 2 and path_str[1] == ':':
        # Windows驱动器路径
        return f"file:///{path_str}"
    else:
        # Unix-like路径
        return f"file://{path_str}"


def uri_to_path(uri: str) -> Path:
    """Convert LSP URI to file path"""
    if uri.startswith("file:///"):
        # Windows风格: file:///C:/path/to/file
        path_str = uri[8:]  # 移除 "file:///"
        return Path(path_str)
    elif uri.startswith("file://"):
        # Unix风格: file://path/to/file
        path_str = uri[7:]  # 移除 "file://"
        return Path(path_str)
    return Path(uri) 