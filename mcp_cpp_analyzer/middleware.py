import logging
from pathlib import Path
from fastmcp.server.middleware import Middleware, MiddlewareContext
from mcp import McpError
from mcp.types import ErrorData
from .utils import BackendValidator

logger = logging.getLogger(__name__)


class BackendValidationMiddleware(Middleware):
    def __init__(self, lsp_methods_getter, lsp_client_getter):
        self.lsp_methods_getter = lsp_methods_getter
        self.lsp_client_getter = lsp_client_getter
    
    async def on_call_tool(self, context: MiddlewareContext, call_next):
        logger.debug(f"Backend validation for tool: {context.method}")
        
        lsp_methods = self.lsp_methods_getter()
        lsp_client = self.lsp_client_getter()
        
        is_ready, error_msg = BackendValidator.validate_backend_ready(lsp_methods, lsp_client)
        
        if not is_ready:
            logger.error(f"Clangd Backend validation failed: {error_msg}")
            raise McpError(
                ErrorData(
                    code=-32603,  # Internal error
                    message=f"Clangd Backend not ready: {error_msg}"
                )
            )
        
        logger.debug("Backend validation passed")
        
        return await call_next(context)


class PathValidationMiddleware(Middleware):
    
    def __init__(self):
        self.tools_requiring_file_validation = {
            'get_definition', 
            'get_references',
            'get_hover'
        }
    
    async def on_call_tool(self, context: MiddlewareContext, call_next):
        
        tool_name = context.message.name
        args = context.message.arguments or {}
        
        if tool_name in self.tools_requiring_file_validation and 'file' in args:
            file_path = args['file']
            logger.debug(f"Validating file path for {tool_name}: {file_path}")
            
            if not self._validate_file_path(file_path):
                raise McpError(
                    ErrorData(
                        code=-32602,  # Invalid params
                        message=f"Invalid file path format: {file_path}"
                    )
                )
        
        return await call_next(context)
    
    def _validate_file_path(self, file_path: str) -> bool:
        from pathlib import Path
        
        try:
            if not Path(file_path).is_absolute():
                logger.error(f"Only absolute file paths are allowed. Received: {file_path}")
                return False
            
            if file_path.startswith('/') and ('%3A' in file_path or '%2F' in file_path):
                logger.error(f"Invalid Windows path format: {file_path}")
                return False
            
            path = Path(file_path).resolve()
            if not path.exists():
                logger.error(f"File not found: {path}")
                return False
            
            return True
            
        except Exception as e:
            logger.error(f"Path validation error: {e}")
            return False


class LoggingMiddleware(Middleware):
    
    async def on_call_tool(self, context: MiddlewareContext, call_next):
        import time
        
        start_time = time.perf_counter()
        
        tool_name = context.message.name
        args = context.message.arguments or {}
        
        if 'file' in args:
            file_info = f" (file: {Path(args['file']).name})"
        else:
            file_info = ""
        
        logger.info(f"Tool call started: {tool_name}{file_info}")
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(f"Tool parameters: {args}")
        
        try:
            result = await call_next(context)
            
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.info(f"Tool call completed: {tool_name} ({duration_ms:.2f}ms)")
            
            return result
            
        except Exception as e:
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.error(f"Tool call failed: {tool_name} ({duration_ms:.2f}ms) - {type(e).__name__}: {e}")
            raise 