import logging
from pathlib import Path
from typing import Tuple, Optional, Dict, Any
from fastmcp import Context

logger = logging.getLogger(__name__)


class ErrorHandler:
    @staticmethod
    async def handle_tool_error(ctx: Context, error_msg: str, result_key: str = "error") -> Dict[str, Any]:
        
        logger.error(error_msg)
        await ctx.error(error_msg)
        return {"error": error_msg, result_key: []}
    
    @staticmethod
    async def handle_tool_success(ctx: Context, result: Any, count_key: str = "count", 
                                result_key: str = "results", success_msg: str = None) -> Dict[str, Any]:
        if result is None:
            result = []
        
        count = len(result) if isinstance(result, list) else 1
        
        if success_msg:
            await ctx.info(success_msg)
        else:
            await ctx.info(f"Found {count} {result_key}")
        
        return {
            result_key: result,
            count_key: count
        }


class BackendValidator:
    @staticmethod
    def validate_backend_ready(lsp_methods, lsp_client) -> Tuple[bool, Optional[str]]:
        if lsp_methods is None or lsp_client is None:
            return False, "Backend initialization failed"
        
        if not lsp_client.clangd_server.is_alive:
            return False, "Clangd process is not running"
        
        return True, None
    
    @staticmethod
    def get_process_info(lsp_client) -> Dict[str, Any]:
        if not lsp_client or not lsp_client.clangd_server.process:
            return {"alive": False, "pid": None, "returncode": None}
        
        process = lsp_client.clangd_server.process
        return {
            "alive": lsp_client.clangd_server.is_alive,
            "pid": process.pid,
            "returncode": process.returncode
        }


def get_file_language_id(file_path: Path) -> str:
    suffix = file_path.suffix.lower()
    
    if suffix in ['.cpp', '.cc', '.cxx', '.c++']:
        return 'cpp'
    elif suffix in ['.c']:
        return 'c'
    elif suffix in ['.h', '.hpp', '.hxx', '.h++']:
        return 'cpp'
    else:
        return 'cpp'


def format_location_info(file_path: Path, line: int, column: int) -> str:
    return f"{file_path.name}:{line}:{column}" 