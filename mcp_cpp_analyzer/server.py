import asyncio
import logging
import time
import sys
import traceback
from pathlib import Path
from fastmcp import FastMCP, Context
from contextlib import asynccontextmanager
from typing import Dict, Any, Optional
import uvicorn
from .clangd_server import ClangdServer
from .lsp_methods import LSPMethods
from .config import Config, get_global_config, set_config_from_cli
from .clangd_client import ClangdClient
from .utils import ErrorHandler, BackendValidator, format_location_info
from .middleware import BackendValidationMiddleware, PathValidationMiddleware, LoggingMiddleware


logger = logging.getLogger(__name__)

lsp_methods: Optional[LSPMethods] = None
lsp_client: Optional[ClangdClient] = None

@asynccontextmanager
async def app_lifespan(app):
    global lsp_methods, lsp_client
    
    logger.info("Initializing MCP-CPP-Analyzer Server")
    
    try:
        config = get_global_config()
        logger.info(f"Compile DB Dir: {config.compile_db_dir}")
        logger.info(f"Clangd: {config.clangd_path}")
        
        clangd_server = ClangdServer(
            config.clangd_path, 
            config.find_compile_commands()
        )
        lsp_client = ClangdClient(clangd_server)
        
        await lsp_client.start()
        
        lsp_methods = LSPMethods(lsp_client)
        
        logger.info("Backend initialized successfully")
        
        yield
        
    except Exception as e:
        logger.error(f"Failed to initialize backend: {e}")
        logger.error(f"Initialization traceback: {traceback.format_exc()}")
        #to let app catch
        raise
    
    finally:
        logger.info("Cleaning up MCP-CPP-Analyzer Server")
        if lsp_client:
            try:
                await lsp_client.stop()
            except Exception as e:
                logger.warning(f"Error stopping LSP client: {e}")
        
        lsp_methods = None
        lsp_client = None
        logger.info("Backend cleanup completed")

mcp = FastMCP("MCP-CPP-Analyzer-Server",
             lifespan=app_lifespan,
             instructions="A C++ code intelligence server powered by clangd." \
                          " Use this tool instead of search when you need to find symbol definitions, declarations, or references in C++ codebases. " \
                          "Requires absolute file paths and 1-based line/column numbers.")

def get_lsp_methods():
    return lsp_methods

def get_lsp_client():
    return lsp_client

mcp.add_middleware(LoggingMiddleware())
mcp.add_middleware(BackendValidationMiddleware(get_lsp_methods, get_lsp_client))
mcp.add_middleware(PathValidationMiddleware())

@mcp.tool
async def get_definition(file: str, line: int, column: int, ctx: Context) -> Dict[str, Any]:
    """Get C++ symbol definition location
    
    Args:
        file: Source file path (ABSOLUTE PATH ONLY - relative paths are not allowed)
        line: Line number (1-based, use the actual line number shown in your editor)
        column: Column number (1-based, use the actual column number shown in your editor)
        
    Returns:
        Definition locations with file, line, column information
        
    Note:
        Use the exact line and column numbers as displayed in your editor. 
        The tool will automatically convert them to 0-based indexing for LSP protocol.
        No need to subtract 1 from the editor position.
        
        CRITICAL: Please double-check that the line and column numbers you provide 
        EXACTLY match what you see in your editor. Incorrect line/column numbers 
        are the most common cause of "definition not found" errors.
        
        WINDOWS USERS: Use standard Windows absolute path format.
        Do NOT use Unix-style paths (e.g., /c%3A/path) or URL-encoded paths.
        
        IMPORTANT: Only absolute file paths are accepted. Relative paths will be rejected.
    """    
    await ctx.info(f"Finding definition for {format_location_info(Path(file), line, column)}")
    
    try:
        result = await lsp_methods.get_definition(file, line, column)
        
        if not result:
            return await ErrorHandler.handle_tool_error(ctx, "Definition not found. Please verify that the line and column numbers exactly match your editor display.", "definitions")

        response = await ErrorHandler.handle_tool_success(
            ctx, result, 
            count_key="count",
            result_key="definitions", 
            success_msg=f"Found {len(result)} definitions"
        )
        
        response.update({
            "source_location": format_location_info(Path(file), line, column),
            "resolved_file_path": str(Path(file).resolve())
        })
        
        return response
        
    except Exception as e:
        logger.error(f"Exception in get_definition: {type(e).__name__}: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        return await ErrorHandler.handle_tool_error(ctx, f"Error finding definition: {e}", "definitions")

@mcp.tool
async def get_references(file: str, line: int, column: int, ctx: Context, include_declaration: bool = True) -> Dict[str, Any]:
    """Get C++ symbol references
    
    Args:
        file: Source file path (ABSOLUTE PATH ONLY - relative paths are not allowed)
        line: Line number (1-based, use the actual line number shown in your editor)
        column: Column number (1-based, use the actual column number shown in your editor)
        include_declaration: Whether to include declaration in results
        
    Returns:
        Reference locations with file, line, column information
        
    Note:
        Use the exact line and column numbers as displayed in your editor.
        The tool will automatically convert them to 0-based indexing for LSP protocol.
        No need to subtract 1 from the editor position.
        
        CRITICAL: Please double-check that the line and column numbers you provide 
        EXACTLY match what you see in your editor. Incorrect line/column numbers 
        are the most common cause of "references not found" errors.
        
        WINDOWS USERS: Use standard Windows absolute path format.
        Do NOT use Unix-style paths (e.g., /c%3A/path) or URL-encoded paths.
        
        IMPORTANT: Only absolute file paths are accepted. Relative paths will be rejected.
    """
    
    await ctx.info(f"Finding references for {format_location_info(Path(file), line, column)}")
    
    try:
        result = await lsp_methods.get_references(file, line, column, include_declaration)
        
        if not result:
            return await ErrorHandler.handle_tool_error(ctx, "No references found. Please verify that the line and column numbers exactly match your editor display.", "references")
        
        # 返回结果
        response = await ErrorHandler.handle_tool_success(
            ctx, result,
            count_key="count",
            result_key="references",
            success_msg=f"Found {len(result)} references"
        )
        
        response.update({
            "source_location": format_location_info(Path(file), line, column),
            "resolved_file_path": str(Path(file).resolve()),
            "include_declaration": include_declaration
        })
        
        return response
        
    except Exception as e:
        logger.error(f"Exception in get_references: {type(e).__name__}: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        return await ErrorHandler.handle_tool_error(ctx, f"Error finding references: {e}", "references")

@mcp.tool
async def get_hover(file: str, line: int, column: int, ctx: Context) -> Dict[str, Any]:
    """Get C++ symbol hover information
    
    Args:
        file: Source file path (ABSOLUTE PATH ONLY - relative paths are not allowed)
        line: Line number (1-based, use the actual line number shown in your editor)
        column: Column number (1-based, use the actual column number shown in your editor)
        
    Returns:
        Hover information including documentation, type information, and location details
        
    Note:
        Use the exact line and column numbers as displayed in your editor.
        The tool will automatically convert them to 0-based indexing for LSP protocol.
        No need to subtract 1 from the editor position.
        
        CRITICAL: Please double-check that the line and column numbers you provide 
        EXACTLY match what you see in your editor. Incorrect line/column numbers 
        are the most common cause of "hover info not found" errors.
        
        WINDOWS USERS: Use standard Windows absolute path format.
        Do NOT use Unix-style paths (e.g., /c%3A/path) or URL-encoded paths.
        
        IMPORTANT: Only absolute file paths are accepted. Relative paths will be rejected.
    """
    
    await ctx.info(f"Getting hover information for {format_location_info(Path(file), line, column)}")
    
    try:
        result = await lsp_methods.get_hover(file, line, column)
        
        if not result:
            return await ErrorHandler.handle_tool_error(ctx, "Hover information not found. Please verify that the line and column numbers exactly match your editor display.", "hover")
        
        response = {
            "success": True,
            "hover_info": result,
            "source_location": format_location_info(Path(file), line, column),
            "resolved_file_path": str(Path(file).resolve())
        }
        
        await ctx.info("Successfully retrieved hover information")
        return response
        
    except Exception as e:
        logger.error(f"Exception in get_hover: {type(e).__name__}: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        return await ErrorHandler.handle_tool_error(ctx, f"Error getting hover information: {e}", "hover")

@mcp.tool
async def diagnose_clangd(ctx: Context) -> Dict[str, Any]:
    """Diagnose clangd server status and responsiveness
    
    Returns:
        Diagnostic information about clangd process and project status
    """

    process_info = BackendValidator.get_process_info(lsp_client)
    
    result = {
        "timestamp": time.time(),
        "compile_commands_path": str(lsp_client.clangd_server.compile_db_path),
        "compile_commands_exists": lsp_client.clangd_server.compile_db_path.exists(),
        "process": process_info
    }
    
    try:
        await ctx.info("Testing clangd responsiveness...")
        start_time = time.time()
        
        result_test = await lsp_client.request("workspace/symbol", {"query": ""}, timeout=2.0)
        
        elapsed = time.time() - start_time
        result["responsiveness_test"] = {
            "status": "OK",
            "elapsed_seconds": round(elapsed, 2),
            "symbols_found": len(result_test) if result_test else 0
        }
        await ctx.info(f"Clangd responded in {elapsed:.2f}s")
        
    except asyncio.TimeoutError:
        result["responsiveness_test"] = {
            "status": "TIMEOUT",
            "message": "clangd not responding within 5 seconds"
        }
        await ctx.error("Clangd responsiveness test timed out")
    except Exception as e:
        result["responsiveness_test"] = {
            "status": "ERROR", 
            "message": str(e)
        }
        await ctx.error(f"Responsiveness test error: {e}")
    
    return result

def main():
    try:
        set_config_from_cli()
        config = get_global_config()
        
        log_handlers = [logging.StreamHandler(sys.stderr)]
        logging.basicConfig(
            level=getattr(logging, config.log_level.upper()),
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            handlers=log_handlers
        )
        logger.info(f"Starting MCP-CPP-Analyzer Server (transport={config.transport})")

        if config.transport == "stdio":
            mcp.run(transport="stdio")
        else:
            app = mcp.http_app(transport="sse")
            app.router.lifespan_context = app_lifespan
            uvicorn.run(app, host=config.host, port=config.port)
        
    except KeyboardInterrupt:
        logger.info("Server stopped by user")
    except Exception as e:
        logger.error(f"Server error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()